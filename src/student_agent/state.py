from __future__ import annotations

from typing import Any, TypedDict

from .contracts import Contracts
from .llm_client import OllamaLLMClient
from .mcp_cache import CachedEvidenceGateway
from .trace import TraceWriter


class InvestigationState(TypedDict, total=False):
    # Context & Injected Services
    case: dict[str, Any]
    case_id: str
    gateway: CachedEvidenceGateway
    trace: TraceWriter
    llm: OllamaLLMClient
    contracts: Contracts

    # Case Request Info
    claims: list[dict[str, Any]]
    candidate_order_ids: list[str]
    policy_version: str
    customer_unique_id_hint: str | None
    investigation_scope: dict[str, Any]

    # Entity Resolution
    status: str
    resolved_order_ids: list[str]
    rejected_candidates: list[str]
    entity_confidence: float
    customer_unique_id: str | None
    related_order_ids: list[str]

    # Specialist Data & Findings
    order_data: dict[str, Any]
    shipment_summary: dict[str, Any]
    shipment_analysis: dict[str, Any]
    sellers_data: list[dict[str, Any]]
    order_items_data: list[dict[str, Any]]
    payments_data: list[dict[str, Any]]
    payment_timeline: dict[str, Any]
    payment_analysis: dict[str, Any]
    policy_data: dict[str, Any]
    product_context: list[dict[str, Any]]

    # Affected Entities
    affected_item_ids: list[str]
    affected_seller_ids: list[str]
    affected_payment_references: list[str]
    affected_shipment_ids: list[str]

    # Conflict, Policy, Assessment
    primary_issue: str
    secondary_issues: list[str]
    case_status: str
    assessment_confidence: float
    claim_assessments: list[dict[str, Any]]
    root_cause_analysis: dict[str, Any]
    data_conflicts: list[dict[str, Any]]
    financial_resolution: dict[str, Any]
    resolution_actions: list[str]

    # Final Output
    final_output: dict[str, Any]
