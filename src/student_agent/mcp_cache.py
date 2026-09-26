from __future__ import annotations

from typing import Any

from .mcp_gateway import EvidenceGateway


class CachedEvidenceGateway:
    """In-memory per-case caching wrapper for EvidenceGateway to prevent duplicate MCP calls."""

    def __init__(self, inner: EvidenceGateway) -> None:
        self._inner = inner
        self._cache: dict[tuple[str, str, tuple[tuple[str, str], ...]], dict[str, Any]] = {}
        self._collected_evidence_refs: list[str] = []

    def reset_case(self) -> None:
        """Clear cache and collected refs for a new case to prevent cross-case contamination."""
        self._cache.clear()
        self._collected_evidence_refs.clear()

    async def list_tools(self) -> list[str]:
        return await self._inner.list_tools()

    async def call(self, tool_name: str, *, case_id: str, **arguments: Any) -> dict[str, Any]:
        sorted_args = tuple(sorted((str(k), str(v)) for k, v in arguments.items() if v is not None))
        cache_key = (tool_name, case_id, sorted_args)

        if cache_key in self._cache:
            return self._cache[cache_key]

        # Call underlying MCP gateway
        evidence = await self._inner.call(tool_name, case_id=case_id, **arguments)
        self._cache[cache_key] = evidence

        ref = evidence.get("evidence_ref")
        if ref and ref not in self._collected_evidence_refs:
            self._collected_evidence_refs.append(ref)

        return evidence

    def get_collected_evidence_refs(self) -> list[str]:
        return list(self._collected_evidence_refs)
