from __future__ import annotations

import logging
from typing import Any

from ..state import InvestigationState

logger = logging.getLogger(__name__)

CAUSE_CODES_MAP = {
    "canceled_order_paid": "ORDER_CANCELED_BEFORE_FULFILLMENT",
    "unavailable_order_paid": "ORDER_UNAVAILABLE_STOCK",
    "late_delivery_seller": "SELLER_DISPATCH_DELAY",
    "late_delivery_logistics": "CARRIER_DELIVERY_DELAY",
    "valid_split_payment": "VALID_SPLIT_PAYMENT_SCHEDULE",
    "payment_mismatch": "PAYMENT_AMOUNT_MISMATCH",
    "duplicate_charge": "DUPLICATE_PAYMENT_CHARGE",
    "refund_pending": "REFUND_PENDING_GATEWAY_SETTLEMENT",
    "refund_failed": "REFUND_GATEWAY_REVERSAL_FAILURE",
    "unsupported_claim": "UNSUPPORTED_CUSTOMER_CLAIM",
    "insufficient_evidence": "INSUFFICIENT_INVESTIGATION_EVIDENCE",
}


async def conflict_policy_agent_node(state: InvestigationState) -> dict[str, Any]:
    """Conflict & Policy Agent retrieves policy, resolves conflicts, and determines root cause."""
    case_id = state["case_id"]
    gateway = state["gateway"]
    trace = state["trace"]
    llm = state["llm"]

    policy_version = state.get("policy_version", "EC_POLICY_V2")
    resolved_order_ids = state.get("resolved_order_ids", [])
    primary_order_id = resolved_order_ids[0] if resolved_order_ids else None
    claims = state.get("claims", [])
    scope = state.get("investigation_scope", {})

    policy_data: dict[str, Any] = {}
    order_items_data: list[dict[str, Any]] = []
    product_context: list[dict[str, Any]] = []
    affected_item_ids: list[str] = []

    # 1. Query get_policy
    try:
        ev = await gateway.call("get_policy", case_id=case_id, policy_version=policy_version)
        policy_data = ev.get("data", {})
        trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor="conflict_policy_agent",
            tool_name="get_policy",
            evidence_refs=[ev["evidence_ref"]],
        )
    except Exception as exc:
        logger.warning("Failed get_policy for %s: %s", policy_version, exc)

    # 2. Query get_order_items if resolved
    if primary_order_id:
        try:
            ev = await gateway.call("get_order_items", case_id=case_id, order_id=primary_order_id)
            order_items_data = ev.get("data", [])
            trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor="conflict_policy_agent",
                tool_name="get_order_items",
                evidence_refs=[ev["evidence_ref"]],
            )
            for item in order_items_data:
                iid = item.get("order_item_id") or item.get("product_id")
                if iid and str(iid) not in affected_item_ids:
                    affected_item_ids.append(str(iid))
        except Exception as exc:
            logger.warning("Failed get_order_items for %s: %s", primary_order_id, exc)

        # 3. Query get_product_context if scope specifies
        if scope.get("include_product_context"):
            try:
                ev = await gateway.call(
                    "get_product_context", case_id=case_id, order_id=primary_order_id
                )
                product_context = ev.get("data", [])
                trace.emit(
                    case_id=case_id,
                    event_type="tool_result_consumed",
                    actor="conflict_policy_agent",
                    tool_name="get_product_context",
                    evidence_refs=[ev["evidence_ref"]],
                )
            except Exception as exc:
                logger.warning("Failed get_product_context for %s: %s", primary_order_id, exc)

    # 4. Synthesize Findings with LLM & Policy Rules
    policy_rules = policy_data.get("rules", {})
    shipment_analysis = state.get("shipment_analysis", {})
    payment_analysis = state.get("payment_analysis", {})

    prompt = f"""You are the Policy and Conflict Resolution Agent for E-Commerce Claims.
Case ID: {case_id}
Customer Claims: {claims}
Entity Resolution: {state.get("status")}, Resolved Orders: {resolved_order_ids}
Shipment Analysis: {shipment_analysis}
Payment Analysis: {payment_analysis}
Available Policy Rules for Primary Issues: {list(policy_rules.keys())}

Determine:
1. 'primary_issue': Select strictly ONE issue from policy rules.
2. 'secondary_issues': List of secondary issue names (if any, max 5).
3. 'data_conflicts': List of any source discrepancies detected. Each must have:
   {{"field": str, "sources": [str, str], "selected_source": str, "resolution_code": str}}
4. 'claim_evaluations': For each claim in Customer Claims:
   {{"claim_id": str, "verdict": "supported"|"unsupported"|"partially_supported"}}

Return JSON only in this format:
{{
  "primary_issue": "<selected issue>",
  "secondary_issues": ["<issue>", ...],
  "data_conflicts": [...],
  "claim_evaluations": [...]
}}
"""
    try:
        decision = await llm.generate_json(
            prompt=prompt,
            system_instruction=(
                "You are an expert E-Commerce Policy Decision Agent. Return strictly JSON."
            ),
        )
        primary_issue = decision.get("primary_issue", "insufficient_evidence")
        secondary_issues = decision.get("secondary_issues", [])
        data_conflicts = decision.get("data_conflicts", [])
        claim_evaluations = decision.get("claim_evaluations", [])
    except Exception as exc:
        logger.warning("LLM policy decision fallback: %s", exc)
        primary_issue = "insufficient_evidence"
        secondary_issues = []
        data_conflicts = []
        claim_evaluations = []

    # Fallback sanity check on primary_issue against valid enum
    valid_issues = {
        "canceled_order_paid",
        "unavailable_order_paid",
        "late_delivery_seller",
        "late_delivery_logistics",
        "valid_split_payment",
        "payment_mismatch",
        "duplicate_charge",
        "refund_pending",
        "refund_failed",
        "unsupported_claim",
        "insufficient_evidence",
    }
    if primary_issue not in valid_issues or primary_issue == "insufficient_evidence":
        # Extract explicit topic from customer claims
        claim_topics = [c.get("topic") for c in claims if c.get("topic") in valid_issues]
        matched_topic = claim_topics[0] if claim_topics else None

        ship_verdict = shipment_analysis.get("verdict")
        pay_verdict = payment_analysis.get("verdict")

        if matched_topic:
            primary_issue = matched_topic
        elif ship_verdict == "logistics_delay":
            primary_issue = "late_delivery_logistics"
        elif ship_verdict == "seller_delay":
            primary_issue = "late_delivery_seller"
        elif pay_verdict == "duplicate_capture":
            primary_issue = "duplicate_charge"
        elif pay_verdict == "refund_failed":
            primary_issue = "refund_failed"
        elif pay_verdict == "refund_pending":
            primary_issue = "refund_pending"
        elif pay_verdict == "capture_mismatch":
            primary_issue = "payment_mismatch"
        else:
            primary_issue = "unsupported_claim"

    # 5. Extract Policy details for the selected primary_issue
    rule = policy_rules.get(primary_issue, {})
    case_status = rule.get(
        "case_status", "no_action" if primary_issue == "unsupported_claim" else "action_required"
    )
    recommended_refund_brl = float(rule.get("refund_brl", 0.0))
    recommended_action = rule.get("recommended_action", "document_no_action")
    responsible_parties = rule.get("responsible_parties", [])
    if not responsible_parties:
        responsible_parties = [
            {
                "party_type": "customer" if primary_issue == "unsupported_claim" else "platform",
                "party_id": None,
            }
        ]

    cause_code = CAUSE_CODES_MAP.get(primary_issue, "INVESTIGATION_POLICY_APPLIED")
    root_cause_analysis = {
        "ranked_causes": [{"cause_code": cause_code, "rank": 1}],
        "responsible_parties": responsible_parties,
    }

    # Financial Resolution
    refund_lines = []
    if recommended_refund_brl > 0:
        refund_lines.append(
            {
                "reason_code": primary_issue,
                "amount_brl": recommended_refund_brl,
                "entity_id": primary_order_id or "ORDER_DISPUTE",
            }
        )

    financial_resolution = {
        "currency": "BRL",
        "recommended_refund_brl": recommended_refund_brl,
        "refund_lines": refund_lines,
    }

    resolution_actions = [recommended_action]

    # Map Claim Assessments with gathered evidence refs
    all_refs = gateway.get_collected_evidence_refs()
    claim_assessments = []
    eval_map = {e.get("claim_id"): e.get("verdict", "supported") for e in claim_evaluations}

    for c in claims:
        cid = c.get("claim_id")
        verdict = eval_map.get(
            cid, "supported" if case_status == "action_required" else "unsupported"
        )
        claim_assessments.append(
            {
                "claim_id": cid,
                "verdict": verdict,
                "confidence": 0.95,
                "evidence_refs": all_refs[:10],
            }
        )

    # Validate data_conflicts format
    sanitized_conflicts = []
    for dc in data_conflicts:
        if (
            isinstance(dc, dict)
            and "field" in dc
            and "sources" in dc
            and len(dc.get("sources", [])) >= 2
        ):
            sanitized_conflicts.append(
                {
                    "field": str(dc["field"])[:100],
                    "sources": [str(s)[:80] for s in dc["sources"]][:5],
                    "selected_source": str(dc.get("selected_source"))[:80]
                    if dc.get("selected_source")
                    else None,
                    "resolution_code": str(dc.get("resolution_code", "POLICY_PRECEDENCE"))[:80],
                }
            )

    # Emit policy_decided trace event
    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="conflict_policy_agent",
        decision_code=primary_issue,
        attributes={
            "case_status": case_status,
            "refund_brl": recommended_refund_brl,
        },
    )

    # Emit handoff to verifier_agent
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="conflict_policy_agent",
        target="verifier_agent",
        attributes={"primary_issue": primary_issue},
    )

    return {
        "policy_data": policy_data,
        "order_items_data": order_items_data,
        "product_context": product_context,
        "affected_item_ids": affected_item_ids,
        "primary_issue": primary_issue,
        "secondary_issues": secondary_issues[:10],
        "case_status": case_status,
        "assessment_confidence": 0.95,
        "claim_assessments": claim_assessments,
        "root_cause_analysis": root_cause_analysis,
        "data_conflicts": sanitized_conflicts[:5],
        "financial_resolution": financial_resolution,
        "resolution_actions": resolution_actions,
    }
