from __future__ import annotations

import logging
from typing import Any

from ..state import InvestigationState

logger = logging.getLogger(__name__)


async def entity_agent_node(state: InvestigationState) -> dict[str, Any]:
    """Entity Agent resolves customer identity, orders, and rejects invalid candidates."""
    case = state["case"]
    case_id = state["case_id"]
    gateway = state["gateway"]
    trace = state["trace"]
    llm = state["llm"]

    candidate_ids = state.get("candidate_order_ids", [])
    cust_hint = state.get("customer_unique_id_hint")
    claimed_order_id = case.get("customer_request", {}).get("claimed_order_id")

    # 1. Ask LLM to evaluate candidates and identify priority order candidates
    ranking_prompt = f"""You are an expert E-Commerce Entity Resolution Agent.
Case ID: {case_id}
Customer Request Message: {case.get("customer_request", {}).get("message")}
Claimed Order ID: {claimed_order_id}
Customer Unique ID Hint: {cust_hint}
Candidate Order IDs: {candidate_ids}

Analyze the candidates. Determine:
1. Which candidate IDs look like valid Olist order UUIDs (typically 32-char hex strings)
   vs obvious dummy candidates (e.g. 'candidate-001', invalid formats).
2. Rank the candidates from most likely to least likely to be the authentic order.
3. Suggest which candidate to query first.

Return JSON in this exact structure:
{{
  "priority_candidates": ["<most likely candidate>", ...],
  "obvious_rejected": ["<dummy candidate>", ...]
}}
"""
    try:
        ranking_result = await llm.generate_json(
            prompt=ranking_prompt,
            system_instruction=(
                "You are an expert Olist E-commerce Entity Resolution Agent. Return JSON only."
            ),
        )
        priority_candidates = ranking_result.get("priority_candidates", [])
        obvious_rejected = set(ranking_result.get("obvious_rejected", []))
    except Exception as exc:
        logger.warning("LLM candidate ranking fallback: %s", exc)
        priority_candidates = []
        obvious_rejected = set()
        for cand in candidate_ids:
            # 32-hex UUID is standard Olist order format
            if len(cand) == 32 and all(c in "0123456789abcdefABCDEF" for c in cand):
                priority_candidates.append(cand)
            else:
                obvious_rejected.add(cand)

    # Ensure all candidates are considered
    for cand in candidate_ids:
        if cand not in priority_candidates and cand not in obvious_rejected:
            priority_candidates.append(cand)

    # 2. Query MCP customer history if available
    customer_history_data: dict[str, Any] = {}
    related_order_ids: list[str] = []
    customer_unique_id = cust_hint

    if cust_hint:
        try:
            ev = await gateway.call(
                "get_customer_history", case_id=case_id, customer_unique_id=cust_hint
            )
            customer_history_data = ev.get("data", {})
            trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor="entity_agent",
                tool_name="get_customer_history",
                evidence_refs=[ev["evidence_ref"]],
            )
            orders = customer_history_data.get("orders", [])
            for o in orders:
                oid = o.get("order_id")
                if oid and oid not in related_order_ids:
                    related_order_ids.append(oid)
        except Exception as exc:
            logger.warning("Failed get_customer_history for %s: %s", cust_hint, exc)

    # 3. Test priority candidates with get_order
    resolved_order_ids: list[str] = []
    rejected_candidates: list[str] = []
    order_data: dict[str, Any] = {}

    for cand in priority_candidates:
        if cand in obvious_rejected:
            rejected_candidates.append(cand)
            continue
        try:
            ev = await gateway.call("get_order", case_id=case_id, order_id=cand)
            data = ev.get("data", {})
            trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor="entity_agent",
                tool_name="get_order",
                evidence_refs=[ev["evidence_ref"]],
            )
            if data and data.get("order_id") == cand:
                resolved_order_ids.append(cand)
                order_data = data
                # If we have resolved our primary order, break to avoid excessive MCP calls
                break
            else:
                rejected_candidates.append(cand)
        except Exception:
            rejected_candidates.append(cand)

    # Include any remaining candidates as rejected
    for cand in candidate_ids:
        if cand not in resolved_order_ids and cand not in rejected_candidates:
            rejected_candidates.append(cand)

    status = "resolved" if resolved_order_ids else "not_found"
    confidence = 0.95 if resolved_order_ids else 0.5

    # If claimed_order_id is in resolved orders or customer history, record it
    if resolved_order_ids and resolved_order_ids[0] not in related_order_ids:
        related_order_ids.append(resolved_order_ids[0])

    # Emit handoff to shipment_agent
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="entity_agent",
        target="shipment_agent",
        attributes={"resolved_orders_count": len(resolved_order_ids)},
    )

    return {
        "status": status,
        "resolved_order_ids": resolved_order_ids,
        "rejected_candidates": rejected_candidates,
        "entity_confidence": confidence,
        "customer_unique_id": customer_unique_id,
        "related_order_ids": related_order_ids,
        "order_data": order_data,
    }
