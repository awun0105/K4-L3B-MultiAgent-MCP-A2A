from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .extract import as_float, as_str, parse_dt, unique_ids

LOCAL_TOOL_NAMES = (
    "rank_entity_candidates",
    "detect_data_conflicts",
    "calibrate_confidence",
    "recommend_resolution",
    "verify_output_invariants",
    "reconstruct_event_timeline",
    "arbitrate_specialist_consensus",
    "detect_anomaly_signals",
    "evaluate_evidence_provenance",
    "explain_decision_rationale",
)


def rank_entity_candidates(
    *,
    hinted_customer_id: str | None,
    hinted_order_ids: Sequence[str],
    order_records: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    ranked: list[dict[str, Any]] = []
    for order_id, record in order_records.items():
        score = 0.35
        customer = as_str(record.get("customer_unique_id") or record.get("customer_id"))
        if hinted_customer_id and customer == hinted_customer_id:
            score += 0.45
        if order_id in hinted_order_ids:
            score += 0.2
        status = as_str(record.get("order_status"))
        if status:
            score += 0.05
        ranked.append(
            {
                "order_id": order_id,
                "customer_unique_id": customer,
                "score": round(min(score, 1.0), 4),
                "status": status,
            }
        )
    ranked.sort(key=lambda item: (-float(item["score"]), str(item["order_id"])))
    accepted: list[str] = []
    rejected: list[str] = []
    status = "not_found"
    confidence = 0.15
    if ranked:
        top = ranked[0]
        runner = ranked[1] if len(ranked) > 1 else None
        top_score = float(top["score"])
        gap = top_score - float(runner["score"]) if runner else 1.0
        if top_score >= 0.55 and gap >= 0.08:
            accepted = [str(top["order_id"])]
            rejected = [str(item["order_id"]) for item in ranked[1:]]
            status = "resolved"
            confidence = min(0.95, 0.5 + top_score / 2)
        elif top_score >= 0.4:
            accepted = unique_ids(item["order_id"] for item in ranked[:2])
            rejected = [str(item["order_id"]) for item in ranked[2:]]
            status = "ambiguous"
            confidence = 0.4
        else:
            rejected = [str(item["order_id"]) for item in ranked]
            status = "not_found"
            confidence = 0.2
    hinted_rejected = [item for item in hinted_order_ids if item not in accepted]
    rejected = unique_ids([*rejected, *hinted_rejected])
    return {
        "status": status,
        "resolved_order_ids": accepted,
        "rejected_candidates": rejected,
        "confidence": round(confidence, 4),
        "ranked": ranked,
    }


def detect_data_conflicts(
    order_data: Mapping[str, Any] | None,
    shipment_data: Mapping[str, Any] | None,
    payment_data: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    order_status = as_str((order_data or {}).get("order_status"))
    ship_status = as_str(
        (shipment_data or {}).get("shipment_status")
        or (shipment_data or {}).get("status")
        or (shipment_data or {}).get("delivery_status")
    )
    if order_status and ship_status and order_status.lower() != ship_status.lower():
        delivered = {"delivered", "arrived"}
        if order_status.lower() in delivered and ship_status.lower() not in delivered:
            conflicts.append(
                {
                    "field": "delivery_status",
                    "sources": ["order", "shipment"],
                    "selected_source": "shipment",
                    "resolution_code": "PREFER_SHIPMENT_STATUS",
                }
            )
        elif ship_status.lower() in delivered and order_status.lower() not in delivered:
            conflicts.append(
                {
                    "field": "delivery_status",
                    "sources": ["order", "shipment"],
                    "selected_source": "order",
                    "resolution_code": "PREFER_ORDER_STATUS",
                }
            )

    order_delivered = parse_dt(
        (order_data or {}).get("order_delivered_customer_date")
        or (order_data or {}).get("delivered_customer_date")
    )
    ship_delivered = parse_dt(
        (shipment_data or {}).get("delivered_customer_date")
        or (shipment_data or {}).get("delivery_date")
    )
    if order_delivered and ship_delivered and order_delivered.date() != ship_delivered.date():
        conflicts.append(
            {
                "field": "delivered_customer_date",
                "sources": ["order", "shipment"],
                "selected_source": "shipment",
                "resolution_code": "PREFER_SHIPMENT_TIMELINE",
            }
        )

    captured = as_float((payment_data or {}).get("captured_total_brl"))
    order_total = as_float((order_data or {}).get("order_total_brl"))
    if captured is not None and order_total is not None and abs(captured - order_total) > 0.5:
        conflicts.append(
            {
                "field": "captured_total_brl",
                "sources": ["order", "payment"],
                "selected_source": "payment",
                "resolution_code": "PREFER_PAYMENT_LEDGER",
            }
        )
    return conflicts[:5]


def calibrate_confidence(
    *,
    entity_confidence: float,
    timeline_complete: bool,
    conflict_count: int,
    evidence_count: int,
    issue_is_insufficient: bool,
) -> float:
    value = entity_confidence
    if timeline_complete:
        value += 0.08
    if evidence_count >= 3:
        value += 0.07
    elif evidence_count == 0:
        value -= 0.25
    value -= min(0.2, 0.06 * conflict_count)
    if issue_is_insufficient:
        value = min(value, 0.45)
    return round(min(0.97, max(0.05, value)), 4)


def recommend_resolution(
    *,
    primary_issue: str,
    refundable_total_brl: float | None,
    responsible_party: str,
) -> dict[str, Any]:
    actions: list[str] = []
    refund = 0.0
    reason = "NO_REFUND"
    status = "no_action"
    if primary_issue in {"canceled_order_paid", "unavailable_order_paid", "duplicate_charge"}:
        refund = float(refundable_total_brl or 0.0)
        reason = "REVERSE_CAPTURE"
        status = "action_required"
        actions = ["issue_full_refund", "notify_customer"]
    elif primary_issue in {"refund_pending"}:
        status = "action_required"
        actions = ["expedite_refund", "notify_customer"]
        refund = float(refundable_total_brl or 0.0)
        reason = "COMPLETE_REFUND"
    elif primary_issue in {"refund_failed"}:
        status = "action_required"
        actions = ["retry_refund", "escalate_payments"]
        refund = float(refundable_total_brl or 0.0)
        reason = "RETRY_REFUND"
    elif primary_issue in {"payment_mismatch"}:
        status = "action_required"
        actions = ["reconcile_payment_ledger"]
        reason = "RECONCILE_LEDGER"
    elif primary_issue in {"late_delivery_seller", "late_delivery_logistics"}:
        status = "action_required"
        actions = ["apply_delay_compensation", "notify_customer"]
        refund = round(float(refundable_total_brl or 0.0) * 0.1, 2)
        reason = "DELAY_COMPENSATION"
    elif primary_issue == "insufficient_evidence":
        status = "needs_investigation"
        actions = ["request_additional_evidence"]
        reason = "INSUFFICIENT_EVIDENCE"
    elif primary_issue == "unsupported_claim":
        status = "no_action"
        actions = ["close_unsupported_claim"]
        reason = "CLAIM_UNSUPPORTED"
    elif primary_issue == "valid_split_payment":
        status = "no_action"
        actions = ["confirm_split_payment_valid"]
        reason = "NO_REFUND"

    if responsible_party == "seller" and "contact_seller" not in actions:
        actions.append("contact_seller")
    return {
        "case_status": status,
        "resolution_actions": unique_ids(actions, limit=8),
        "recommended_refund_brl": round(max(refund, 0.0), 2),
        "reason_code": reason,
    }


def verify_output_invariants(output: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    assessment = output.get("assessment") or {}
    status = assessment.get("case_status")
    refund = (output.get("financial_resolution") or {}).get("recommended_refund_brl")
    if status == "no_action" and isinstance(refund, (int, float)) and refund > 0:
        problems.append("no_action_with_refund")
    entity = output.get("entity_resolution") or {}
    if entity.get("status") == "not_found" and assessment.get("primary_issue") not in {
        "insufficient_evidence",
        "unsupported_claim",
    }:
        problems.append("missing_entity_not_insufficient")
    shipment = output.get("shipment_analysis") or {}
    if shipment.get("verdict") == "seller_delay" and not shipment.get("late_seller_ids"):
        problems.append("seller_delay_missing_seller")
    confidence = assessment.get("confidence")
    if isinstance(confidence, (int, float)) and not 0 <= confidence <= 1:
        problems.append("confidence_out_of_range")
    return problems


# ==========================================
# New Advanced Local Multi-Agent Tools
# ==========================================


def reconstruct_event_timeline(
    order_data: Mapping[str, Any] | None,
    shipment_data: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Reconstruct chronological event milestones and calculate SLA fulfillment deltas."""
    merged = {**dict(order_data or {}), **dict(shipment_data or {})}
    purchase = parse_dt(merged.get("order_purchase_timestamp"))
    approved = parse_dt(merged.get("order_approved_at"))
    shipping_limit = parse_dt(merged.get("shipping_limit_date"))
    carrier_delivered = parse_dt(
        merged.get("order_delivered_carrier_date") or merged.get("delivered_carrier_date")
    )
    customer_delivered = parse_dt(
        merged.get("order_delivered_customer_date") or merged.get("delivered_customer_date")
    )
    estimated_delivery = parse_dt(
        merged.get("order_estimated_delivery_date") or merged.get("estimated_delivery_date")
    )

    milestones: list[dict[str, Any]] = []
    for label, dt in [
        ("purchase", purchase),
        ("payment_approved", approved),
        ("shipping_deadline", shipping_limit),
        ("handed_to_carrier", carrier_delivered),
        ("delivered_to_customer", customer_delivered),
        ("estimated_deadline", estimated_delivery),
    ]:
        if dt is not None:
            milestones.append({"milestone": label, "timestamp": dt.isoformat()})

    milestones.sort(key=lambda m: m["timestamp"])

    seller_sla_breach = False
    logistics_sla_breach = False
    seller_delay_hours: float | None = None
    delivery_delay_days: float | None = None

    if shipping_limit and carrier_delivered:
        diff_sec = (carrier_delivered - shipping_limit).total_seconds()
        seller_delay_hours = round(diff_sec / 3600.0, 2)
        if diff_sec > 0:
            seller_sla_breach = True

    if estimated_delivery and customer_delivered:
        diff_days = (customer_delivered - estimated_delivery).total_seconds() / 86400.0
        delivery_delay_days = round(diff_days, 2)
        if diff_days > 0:
            logistics_sla_breach = True

    return {
        "timeline_complete": bool(purchase and carrier_delivered and customer_delivered),
        "milestones": milestones,
        "seller_sla_breach": seller_sla_breach,
        "logistics_sla_breach": logistics_sla_breach,
        "seller_delay_hours": seller_delay_hours,
        "delivery_delay_days": delivery_delay_days,
    }


def arbitrate_specialist_consensus(findings: Mapping[str, Any]) -> dict[str, Any]:
    """Multi-agent consensus algorithm weighting shipment, payment, and order assessments."""
    entity_status = str(findings.get("entity_status") or "not_found")
    if entity_status in ("not_found", "ambiguous"):
        return {
            "consensus_issue": "insufficient_evidence",
            "confidence_multiplier": 0.4,
            "rationale": "Entity unresolved or ambiguous; insufficient evidence gate active.",
        }

    ship_verdict = str(findings.get("shipment_verdict") or "insufficient_evidence")
    pay_verdict = str(findings.get("payment_verdict") or "insufficient_evidence")
    order_status = str(findings.get("order_status") or "").lower()

    if order_status in ("canceled", "cancelled"):
        return {
            "consensus_issue": "canceled_order_paid",
            "confidence_multiplier": 0.95,
            "rationale": "Order is canceled in system ledger with captured payment.",
        }
    if order_status == "unavailable":
        return {
            "consensus_issue": "unavailable_order_paid",
            "confidence_multiplier": 0.95,
            "rationale": "Order inventory unavailable; seller cannot fulfill.",
        }
    if pay_verdict == "duplicate_capture":
        return {
            "consensus_issue": "duplicate_charge",
            "confidence_multiplier": 0.92,
            "rationale": "Duplicate sequential payment charges identified by payment agent.",
        }
    if ship_verdict == "seller_delay":
        return {
            "consensus_issue": "late_delivery_seller",
            "confidence_multiplier": 0.90,
            "rationale": "Hand-off to carrier occurred after shipping limit deadline.",
        }
    if ship_verdict in ("logistics_delay", "lost"):
        return {
            "consensus_issue": "late_delivery_logistics",
            "confidence_multiplier": 0.88,
            "rationale": "Carrier dispatched in time but delivery missed estimated date.",
        }

    return {
        "consensus_issue": "unsupported_claim"
        if ship_verdict == "on_time"
        else "insufficient_evidence",
        "confidence_multiplier": 0.85,
        "rationale": "Consensus based on corroborated shipment and payment ledgers.",
    }


def detect_anomaly_signals(
    order_data: Mapping[str, Any] | None,
    payment_data: Mapping[str, Any] | None,
    shipment_data: Mapping[str, Any] | None,
    customer_data: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """Scan order, payment, and customer history for fraud, risk, or dispute anomalies."""
    signals: list[dict[str, Any]] = []

    # 1. Negative amounts
    for name, data in [("payment", payment_data), ("order", order_data)]:
        for key, val in (data or {}).items():
            amt = as_float(val)
            if amt is not None and amt < 0:
                signals.append(
                    {
                        "risk_type": "NEGATIVE_AMOUNT",
                        "severity": "HIGH",
                        "description": f"Negative value detected in {name}.{key}: {amt}",
                    }
                )

    # 2. Duplicate payment sequentials
    if payment_data:
        payments = payment_data.get("payments") or [payment_data]
        if isinstance(payments, list):
            seqs = [
                p.get("payment_sequential")
                for p in payments
                if isinstance(p, Mapping) and p.get("payment_sequential") is not None
            ]
            if len(seqs) != len(set(seqs)):
                signals.append(
                    {
                        "risk_type": "DUPLICATE_SEQUENTIAL",
                        "severity": "HIGH",
                        "description": "Repeated payment sequential entries detected.",
                    }
                )

    # 3. High dispute customer history
    if customer_data:
        total_orders = len(customer_data.get("order_ids") or [])
        if total_orders > 10:
            signals.append(
                {
                    "risk_type": "HIGH_VOLUME_BUYER",
                    "severity": "LOW",
                    "description": f"Customer has {total_orders} historical orders.",
                }
            )

    return signals[:5]


def evaluate_evidence_provenance(
    evidence_refs: Sequence[str],
    trace_events: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Audit evidence references for strict provenance, schema conformity, and uniqueness."""
    valid_refs: list[str] = []
    invalid_refs: list[str] = []

    for ref in evidence_refs:
        if isinstance(ref, str) and ref.startswith("ev_") and 23 <= len(ref) <= 99:
            valid_refs.append(ref)
        else:
            invalid_refs.append(str(ref))

    unique_valid = list(dict.fromkeys(valid_refs))
    return {
        "valid_count": len(unique_valid),
        "invalid_count": len(invalid_refs),
        "is_provenance_clean": len(invalid_refs) == 0 and len(unique_valid) > 0,
        "clean_refs": unique_valid[:30],
    }


def explain_decision_rationale(
    assessment: Mapping[str, Any],
    financial_resolution: Mapping[str, Any],
    root_cause: Mapping[str, Any],
) -> str:
    """Generate a clean, structured rationale text for the investigation outcome."""
    issue = assessment.get("primary_issue", "insufficient_evidence")
    status = assessment.get("case_status", "needs_investigation")
    refund = financial_resolution.get("recommended_refund_brl", 0.0)
    parties = root_cause.get("responsible_parties") or []
    responsible = parties[0].get("party_type", "unknown") if parties else "unknown"

    return (
        f"Case concluded with status '{status}' under primary issue '{issue}'. "
        f"Responsible party identified as '{responsible}'. "
        f"Recommended financial remedy: {refund:.2f} BRL."
    )
