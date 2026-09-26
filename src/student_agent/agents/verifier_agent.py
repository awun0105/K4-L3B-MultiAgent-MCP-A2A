from __future__ import annotations

import logging
from typing import Any

from ..contracts import Contracts
from ..state import InvestigationState

logger = logging.getLogger(__name__)


async def verifier_agent_node(state: InvestigationState) -> dict[str, Any]:
    """Verifier Agent aggregates all sections, validates schema, and finalizes output."""
    case_id = state["case_id"]
    gateway = state["gateway"]
    trace = state["trace"]
    contracts: Contracts = state["contracts"]

    # Gather collected evidence refs
    all_refs = gateway.get_collected_evidence_refs()
    if not all_refs:
        # Fallback empty list or gathered refs
        all_refs = []

    # Build affected entities
    affected_entities = {
        "order_ids": list(state.get("resolved_order_ids", []))[:20],
        "item_ids": list(state.get("affected_item_ids", []))[:20],
        "seller_ids": list(state.get("affected_seller_ids", []))[:20],
        "payment_references": list(state.get("affected_payment_references", []))[:20],
        "shipment_ids": list(state.get("affected_shipment_ids", []))[:20],
    }

    # Build entity resolution
    entity_resolution = {
        "status": state.get("status", "not_found"),
        "resolved_order_ids": list(state.get("resolved_order_ids", []))[:20],
        "rejected_candidates": list(state.get("rejected_candidates", []))[:20],
        "confidence": float(state.get("entity_confidence", 0.95)),
    }

    # Build customer context
    customer_context = {
        "customer_unique_id": state.get("customer_unique_id"),
        "related_order_ids": list(state.get("related_order_ids", []))[:20],
    }

    # Build shipment analysis
    shipment_analysis = state.get(
        "shipment_analysis",
        {
            "verdict": "insufficient_evidence",
            "late_seller_ids": [],
            "timeline_complete": False,
        },
    )

    # Build payment analysis
    payment_analysis = state.get(
        "payment_analysis",
        {
            "verdict": "insufficient_evidence",
            "captured_total_brl": None,
            "refunded_total_brl": None,
            "refundable_total_brl": None,
        },
    )

    # Build assessment
    assessment = {
        "primary_issue": state.get("primary_issue", "insufficient_evidence"),
        "secondary_issues": list(state.get("secondary_issues", []))[:10],
        "case_status": state.get("case_status", "no_action"),
        "confidence": float(state.get("assessment_confidence", 0.95)),
    }

    # Build root cause analysis
    root_cause_analysis = state.get(
        "root_cause_analysis",
        {
            "ranked_causes": [{"cause_code": "INSUFFICIENT_INVESTIGATION_EVIDENCE", "rank": 1}],
            "responsible_parties": [{"party_type": "unknown", "party_id": None}],
        },
    )

    # Build financial resolution
    financial_resolution = state.get(
        "financial_resolution",
        {
            "currency": "BRL",
            "recommended_refund_brl": 0.0,
            "refund_lines": [],
        },
    )

    # Build resolution actions
    resolution_actions = list(state.get("resolution_actions", ["document_no_action"]))[:8]

    # Build claim assessments
    claim_assessments = state.get("claim_assessments", [])

    output = {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case_id,
        "assessment": assessment,
        "affected_entities": affected_entities,
        "claim_assessments": claim_assessments[:5],
        "entity_resolution": entity_resolution,
        "customer_context": customer_context,
        "shipment_analysis": shipment_analysis,
        "payment_analysis": payment_analysis,
        "root_cause_analysis": root_cause_analysis,
        "evidence_refs": all_refs[:30],
        "data_conflicts": list(state.get("data_conflicts", []))[:5],
        "financial_resolution": financial_resolution,
        "resolution_actions": resolution_actions,
    }

    # Validate output schema
    contracts.validate_output(output, f"outputs/{case_id}.json")

    # Emit verification_completed trace event
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier_agent",
        attributes={"validation_status": "passed", "evidence_count": len(all_refs)},
    )

    return {"final_output": output}
