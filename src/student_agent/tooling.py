from __future__ import annotations

from collections.abc import Mapping
from contextlib import suppress
from typing import Any
from weakref import WeakKeyDictionary

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

CAPABILITY_ALIASES: dict[str, tuple[str, ...]] = {
    "order": (
        "get_order",
        "get_order_details",
        "lookup_order",
        "fetch_order",
        "get_order_by_id",
    ),
    "order_items": (
        "get_order_items",
        "list_order_items",
        "get_items",
        "get_order_item",
    ),
    "search": (
        "search_orders",
        "find_orders",
        "resolve_order",
        "search_entities",
        "lookup_orders",
    ),
    "customer": (
        "get_customer_history",
        "get_customer",
        "get_customer_profile",
        "lookup_customer",
    ),
    "customer_history": (
        "get_customer_history",
        "get_customer_orders",
        "list_customer_orders",
    ),
    "shipment": (
        "get_shipment_summary",
        "get_shipment",
        "get_shipments",
        "get_delivery",
        "get_logistics",
        "get_shipping",
    ),
    "payment": (
        "get_order_payments",
        "get_payment",
        "get_payments",
        "get_payment_status",
        "list_payments",
        "get_payment_timeline",
    ),
    "refund": (
        "get_refund_timeline",
        "get_refund",
        "get_refunds",
        "get_refund_status",
        "list_refunds",
    ),
    "product": ("get_product_context", "get_product", "get_products", "lookup_product"),
    "seller": ("get_sellers", "get_seller", "get_seller_profile", "lookup_seller"),
    "policy": ("get_policy", "get_policies", "lookup_policy", "get_case_policy"),
}

AGENT_CAPABILITIES: dict[str, frozenset[str]] = {
    "entity-agent": frozenset({"search", "order", "customer", "customer_history"}),
    "customer-agent": frozenset({"customer", "customer_history"}),
    "order-agent": frozenset({"order", "order_items", "product", "seller"}),
    "shipment-agent": frozenset({"shipment", "order"}),
    "payment-agent": frozenset({"payment", "refund"}),
    "policy-agent": frozenset({"policy"}),
    "conflict-agent": frozenset(),
    "verifier": frozenset(),
    "coordinator": frozenset(),
}


class ToolCatalog:
    """Maps logical capabilities onto discovered remote MCP tool names."""

    def __init__(self, discovered: list[str]) -> None:
        self.discovered = list(discovered)
        self._index = {name.lower(): name for name in discovered}

    def resolve(self, capability: str) -> str | None:
        for alias in CAPABILITY_ALIASES.get(capability, ()):
            match = self._index.get(alias.lower())
            if match:
                return match
        return self._index.get(capability.lower())


_CATALOGS: WeakKeyDictionary[Any, ToolCatalog] = WeakKeyDictionary()


def attach_catalog(gateway: EvidenceGateway, discovered: list[str]) -> ToolCatalog:
    catalog = ToolCatalog(discovered)
    with suppress(TypeError):
        _CATALOGS[gateway] = catalog
    return catalog


async def load_catalog(gateway: EvidenceGateway) -> ToolCatalog:
    catalog = _CATALOGS.get(gateway)
    if catalog is not None:
        return catalog
    return attach_catalog(gateway, await gateway.list_tools())


def stringify_args(arguments: Mapping[str, Any]) -> dict[str, str]:
    payload: dict[str, str] = {}
    for key, value in arguments.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            payload[key] = ",".join(str(item) for item in value if item is not None)
        else:
            payload[key] = str(value)
    return payload


class CaseToolRuntime:
    """Least-privilege, cached, retry-limited wrapper around the original MCP gateway."""

    def __init__(
        self,
        gateway: EvidenceGateway,
        catalog: ToolCatalog,
        trace: TraceWriter,
        case_id: str,
        *,
        retry_budget: int = 1,
    ) -> None:
        self.gateway = gateway
        self.catalog = catalog
        self.trace = trace
        self.case_id = case_id
        self.retry_budget = retry_budget
        self._cache: dict[tuple[str, tuple[tuple[str, str], ...]], dict[str, Any]] = {}
        self.remote_calls = 0

    async def call(
        self,
        capability: str,
        *,
        actor: str,
        **arguments: Any,
    ) -> dict[str, Any] | None:
        allowed = AGENT_CAPABILITIES.get(actor, frozenset())
        if capability not in allowed:
            return None
        tool_name = self.catalog.resolve(capability)
        if tool_name is None:
            return None
        args = stringify_args(arguments)
        cache_key = (tool_name, tuple(sorted(args.items())))
        cached = self._cache.get(cache_key)
        if cached is not None:
            self._emit_consumed(actor, tool_name, cached)
            return cached

        evidence: dict[str, Any] | None = None
        attempts = self.retry_budget + 1
        for _ in range(attempts):
            try:
                evidence = await self.gateway.call(tool_name, case_id=self.case_id, **args)
                self.remote_calls += 1
                break
            except Exception:
                # MCP transports can surface connection failures as ExceptionGroup
                # subclasses; an optional evidence source must not restart a case.
                continue
        if evidence is None:
            return None
        self._cache[cache_key] = evidence
        self._emit_consumed(actor, tool_name, evidence)
        return evidence

    def _emit_consumed(self, actor: str, tool_name: str, evidence: Mapping[str, Any]) -> None:
        ref = evidence.get("evidence_ref")
        refs = [ref] if isinstance(ref, str) else None
        self.trace.emit(
            case_id=self.case_id,
            event_type="tool_result_consumed",
            actor=actor,
            tool_name=tool_name,
            evidence_refs=refs,
            attributes={"domain": evidence.get("domain")},
        )
