from __future__ import annotations

import logging
from typing import Any

from ..state import InvestigationState

logger = logging.getLogger(__name__)


async def shipment_agent_node(state: InvestigationState) -> dict[str, Any]:
    """Shipment Agent investigates carrier and seller delivery timelines."""
    case_id = state["case_id"]
    gateway = state["gateway"]
    trace = state["trace"]
    resolved_order_ids = state.get("resolved_order_ids", [])

    shipment_summary: dict[str, Any] = {}
    sellers_data: list[dict[str, Any]] = []
    affected_seller_ids: list[str] = []
    late_seller_ids: list[str] = []
    affected_shipment_ids: list[str] = []

    primary_order_id = resolved_order_ids[0] if resolved_order_ids else None

    if primary_order_id:
        affected_shipment_ids.append(primary_order_id)
        # 1. Query get_shipment_summary
        try:
            ev = await gateway.call(
                "get_shipment_summary", case_id=case_id, order_id=primary_order_id
            )
            shipment_summary = ev.get("data", {})
            trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor="shipment_agent",
                tool_name="get_shipment_summary",
                evidence_refs=[ev["evidence_ref"]],
            )
        except Exception as exc:
            logger.warning("Failed get_shipment_summary for %s: %s", primary_order_id, exc)

        # 2. Query get_sellers
        try:
            ev = await gateway.call("get_sellers", case_id=case_id, order_id=primary_order_id)
            sellers_data = ev.get("data", [])
            trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor="shipment_agent",
                tool_name="get_sellers",
                evidence_refs=[ev["evidence_ref"]],
            )
            for s in sellers_data:
                sid = s.get("seller_id")
                if sid and sid not in affected_seller_ids:
                    affected_seller_ids.append(sid)
        except Exception as exc:
            logger.warning("Failed get_sellers for %s: %s", primary_order_id, exc)

    # 3. Analyze shipment verdict
    order_status = shipment_summary.get("order_status")
    delivered_carrier_at = shipment_summary.get("delivered_carrier_at")
    delivered_customer_at = shipment_summary.get("delivered_customer_at")
    estimated_delivery_at = shipment_summary.get("estimated_delivery_at")
    shipping_limits = shipment_summary.get("shipping_limits", [])
    events = shipment_summary.get("events", [])

    timeline_complete = bool(
        delivered_carrier_at and delivered_customer_at and estimated_delivery_at
    )

    verdict = "insufficient_evidence"
    if not shipment_summary:
        verdict = "insufficient_evidence"
    elif order_status == "canceled":
        verdict = "lost"
    else:
        # Check explicit events in shipment summary
        is_seller_delay = False
        is_logistics_delay = False

        for evt in events:
            evt_type = evt.get("event_type")
            actor = evt.get("actor")
            if evt_type == "delivered_late":
                if actor == "seller":
                    is_seller_delay = True
                elif actor == "logistics_provider":
                    is_logistics_delay = True

        # Check shipping limit vs delivered to carrier
        for limit in shipping_limits:
            limit_at = limit.get("shipping_limit_at")
            sid = limit.get("seller_id")
            if delivered_carrier_at and limit_at and delivered_carrier_at > limit_at:
                is_seller_delay = True
                if sid and sid not in late_seller_ids:
                    late_seller_ids.append(sid)

        # Check delivery date vs estimated delivery date
        if (
            delivered_customer_at
            and estimated_delivery_at
            and delivered_customer_at > estimated_delivery_at
            and not is_seller_delay
        ):
            is_logistics_delay = True

        if is_seller_delay and is_logistics_delay:
            verdict = "conflicting"
        elif is_seller_delay:
            verdict = "seller_delay"
        elif is_logistics_delay:
            verdict = "logistics_delay"
        elif (
            delivered_customer_at
            and estimated_delivery_at
            and delivered_customer_at <= estimated_delivery_at
            or timeline_complete
        ):
            verdict = "on_time"
        else:
            verdict = "insufficient_evidence"

    shipment_analysis = {
        "verdict": verdict,
        "late_seller_ids": late_seller_ids,
        "timeline_complete": timeline_complete,
    }

    # Emit handoff to payment_agent
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="shipment_agent",
        target="payment_agent",
        attributes={"shipment_verdict": verdict},
    )

    return {
        "shipment_summary": shipment_summary,
        "sellers_data": sellers_data,
        "affected_seller_ids": affected_seller_ids,
        "affected_shipment_ids": affected_shipment_ids,
        "shipment_analysis": shipment_analysis,
    }
