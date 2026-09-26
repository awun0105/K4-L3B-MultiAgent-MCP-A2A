from __future__ import annotations

import contextlib
import logging
from typing import Any

from ..state import InvestigationState

logger = logging.getLogger(__name__)


async def payment_agent_node(state: InvestigationState) -> dict[str, Any]:
    """Payment Agent investigates payments, installments, captures, and refund status."""
    case_id = state["case_id"]
    gateway = state["gateway"]
    trace = state["trace"]
    resolved_order_ids = state.get("resolved_order_ids", [])

    payments_data: list[dict[str, Any]] = []
    payment_timeline: dict[str, Any] = {}
    refund_timeline: dict[str, Any] = {}
    affected_payment_references: list[str] = []

    primary_order_id = resolved_order_ids[0] if resolved_order_ids else None

    if primary_order_id:
        # 1. Query get_order_payments
        try:
            ev = await gateway.call(
                "get_order_payments", case_id=case_id, order_id=primary_order_id
            )
            payments_data = ev.get("data", [])
            trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor="payment_agent",
                tool_name="get_order_payments",
                evidence_refs=[ev["evidence_ref"]],
            )
            for idx, p in enumerate(payments_data):
                pref = (
                    p.get("payment_reference")
                    or f"pay-{primary_order_id}-{p.get('payment_sequential', idx + 1)}"
                )
                if pref not in affected_payment_references:
                    affected_payment_references.append(str(pref))
        except Exception as exc:
            logger.warning("Failed get_order_payments for %s: %s", primary_order_id, exc)

        # 2. Query get_payment_timeline
        try:
            ev = await gateway.call(
                "get_payment_timeline", case_id=case_id, order_id=primary_order_id
            )
            payment_timeline = ev.get("data", {})
            trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor="payment_agent",
                tool_name="get_payment_timeline",
                evidence_refs=[ev["evidence_ref"]],
            )
        except Exception as exc:
            logger.warning("Failed get_payment_timeline for %s: %s", primary_order_id, exc)

        # 3. Query get_refund_timeline (may fail if no refunds exist, which is normal)
        try:
            ev = await gateway.call(
                "get_refund_timeline", case_id=case_id, order_id=primary_order_id
            )
            refund_timeline = ev.get("data", {})
            trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor="payment_agent",
                tool_name="get_refund_timeline",
                evidence_refs=[ev["evidence_ref"]],
            )
        except Exception:
            # Tolerated if no refund record exists
            pass

    # 4. Calculate Financials
    captured_total = 0.0
    for p in payments_data:
        with contextlib.suppress(ValueError, TypeError):
            captured_total += float(p.get("payment_value", 0.0))

    # Round to 2 decimal places
    captured_total = round(captured_total, 2)
    refunded_total = 0.0
    refundable_total = captured_total

    # Inspect refund events if any
    refund_events = refund_timeline.get("events", []) if isinstance(refund_timeline, dict) else []
    refund_verdict: str | None = None
    for revt in refund_events:
        status = revt.get("status")
        ramount = revt.get("amount") or revt.get("refund_amount", 0.0)
        try:
            val = float(ramount)
            if status in ("completed", "refunded"):
                refunded_total += val
                refundable_total = max(0.0, refundable_total - val)
        except (ValueError, TypeError):
            pass

        if status == "failed":
            refund_verdict = "refund_failed"
        elif status == "pending":
            refund_verdict = "refund_pending"
        elif status in ("completed", "refunded"):
            refund_verdict = "refunded"

    # Inspect payment timeline events for duplicates or mismatches
    payment_events = (
        payment_timeline.get("events", []) if isinstance(payment_timeline, dict) else []
    )
    has_duplicate = False
    has_mismatch = False
    for pevt in payment_events:
        etype = pevt.get("event_type")
        if etype in ("duplicate_charge", "duplicate_capture"):
            has_duplicate = True
        elif etype in ("payment_mismatch", "capture_mismatch"):
            has_mismatch = True

    if not payments_data:
        verdict = "insufficient_evidence"
    elif refund_verdict:
        verdict = refund_verdict
    elif has_duplicate:
        verdict = "duplicate_capture"
    elif has_mismatch:
        verdict = "capture_mismatch"
    else:
        verdict = "reconciled"

    payment_analysis = {
        "verdict": verdict,
        "captured_total_brl": captured_total if payments_data else None,
        "refunded_total_brl": round(refunded_total, 2) if payments_data else None,
        "refundable_total_brl": round(refundable_total, 2) if payments_data else None,
    }

    # Emit handoff to conflict_policy_agent
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="payment_agent",
        target="conflict_policy_agent",
        attributes={"payment_verdict": verdict, "captured_total": captured_total},
    )

    return {
        "payments_data": payments_data,
        "payment_timeline": payment_timeline,
        "affected_payment_references": affected_payment_references,
        "payment_analysis": payment_analysis,
    }
