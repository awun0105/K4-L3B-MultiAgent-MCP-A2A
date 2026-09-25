from __future__ import annotations

from typing import Any

from .mcp_gateway import EvidenceGateway
from .specialists import Coordinator
from .tooling import CaseToolRuntime, load_catalog
from .trace import TraceWriter


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Run the L3B coordinator and specialist-agent workflow.

    Remote MCP tools stay on the original EvidenceGateway. Local analysis tools
    only rank entities, detect conflicts, calibrate confidence and verify output.
    """
    catalog = await load_catalog(gateway)
    tools = CaseToolRuntime(gateway, catalog, trace, str(case["case_id"]))
    return await Coordinator(case, tools, trace).run()
