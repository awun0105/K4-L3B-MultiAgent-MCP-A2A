from __future__ import annotations

import logging
from typing import Any

from .graph import build_investigation_graph
from .llm_client import OllamaLLMClient
from .mcp_cache import CachedEvidenceGateway
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

logger = logging.getLogger(__name__)

# Global singletons for efficiency across cases
_LLM_CLIENT: OllamaLLMClient | None = None
_GRAPH = None


def _get_llm_client() -> OllamaLLMClient:
    global _LLM_CLIENT
    if _LLM_CLIENT is None:
        _LLM_CLIENT = OllamaLLMClient()
    return _LLM_CLIENT


def _get_graph():
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_investigation_graph()
    return _GRAPH


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Execute the L3B Multi-Agent investigation workflow for a single case."""
    case_id = case["case_id"]
    cached_gateway = CachedEvidenceGateway(gateway)
    cached_gateway.reset_case()

    llm = _get_llm_client()
    graph = _get_graph()
    contracts = trace.contracts

    initial_state = {
        "case": case,
        "case_id": case_id,
        "gateway": cached_gateway,
        "trace": trace,
        "llm": llm,
        "contracts": contracts,
    }

    result = await graph.ainvoke(initial_state)
    final_output = result.get("final_output")

    if not final_output:
        raise ValueError(f"Workflow failed to produce a final output for {case_id}")

    return final_output
