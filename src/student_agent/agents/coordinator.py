from __future__ import annotations

import logging
from typing import Any

from ..state import InvestigationState

logger = logging.getLogger(__name__)


async def coordinator_node(state: InvestigationState) -> dict[str, Any]:
    """Coordinator receives the case, extracts scope and claims, and assigns tasks."""
    case = state["case"]
    case_id = state["case_id"]
    trace = state["trace"]

    customer_req = case.get("customer_request", {})
    claims = customer_req.get("claims", [])
    candidate_order_ids = case.get("candidate_order_ids", [])
    policy_version = case.get("policy_version", "EC_POLICY_V2")
    customer_unique_id_hint = case.get("customer_unique_id_hint")
    investigation_scope = case.get("investigation_scope", {})

    # Emit task assignment to entity_agent
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="entity_agent",
        attributes={"task": "resolve_entities"},
    )

    # Emit handoff to entity_agent
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="coordinator",
        target="entity_agent",
    )

    return {
        "claims": claims,
        "candidate_order_ids": candidate_order_ids,
        "policy_version": policy_version,
        "customer_unique_id_hint": customer_unique_id_hint,
        "investigation_scope": investigation_scope,
    }
