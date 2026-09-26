from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from .agents import (
    conflict_policy_agent_node,
    coordinator_node,
    entity_agent_node,
    payment_agent_node,
    shipment_agent_node,
    verifier_agent_node,
)
from .state import InvestigationState


def build_investigation_graph():
    """Build and compile the LangGraph 6-Agent investigation workflow."""
    workflow = StateGraph(InvestigationState)

    # Add 6 agent nodes
    workflow.add_node("coordinator", coordinator_node)
    workflow.add_node("entity_agent", entity_agent_node)
    workflow.add_node("shipment_agent", shipment_agent_node)
    workflow.add_node("payment_agent", payment_agent_node)
    workflow.add_node("conflict_policy_agent", conflict_policy_agent_node)
    workflow.add_node("verifier_agent", verifier_agent_node)

    # Define linear collaborative edges
    workflow.add_edge(START, "coordinator")
    workflow.add_edge("coordinator", "entity_agent")
    workflow.add_edge("entity_agent", "shipment_agent")
    workflow.add_edge("shipment_agent", "payment_agent")
    workflow.add_edge("payment_agent", "conflict_policy_agent")
    workflow.add_edge("conflict_policy_agent", "verifier_agent")
    workflow.add_edge("verifier_agent", END)

    return workflow.compile()
