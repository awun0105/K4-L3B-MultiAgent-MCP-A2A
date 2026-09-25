from __future__ import annotations

import hashlib
import json
import secrets
from typing import Any

from mcp.server.mcpserver import MCPServer


def _create_evidence(domain: str, data: dict[str, Any], case_id: str = "") -> dict[str, Any]:
    serialized = json.dumps(data, sort_keys=True)
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    random_suffix = secrets.token_hex(16)
    return {
        "schema_version": "day09-mcp-evidence-v1",
        "evidence_ref": f"ev_{domain}_{random_suffix}",
        "result_hash": f"sha256:{digest}",
        "domain": domain,
        "data": data,
    }


def create_mock_mcp_server(name: str = "day09-mock-mcp") -> MCPServer:
    """Create a fully compliant standalone Mock MCP Server for offline testing and demo."""
    server = MCPServer(name)

    @server.tool()
    def get_order(order_id: str, case_id: str = "") -> str:
        """Fetch order details from e-commerce database."""
        data = {
            "order_id": order_id,
            "customer_unique_id": f"cust_{order_id[:8]}",
            "seller_id": "seller_brazil_01",
            "order_status": "delivered",
            "order_purchase_timestamp": "2018-01-01T10:00:00",
            "order_delivered_carrier_date": "2018-01-04T10:00:00",
            "order_delivered_customer_date": "2018-01-12T10:00:00",
            "order_estimated_delivery_date": "2018-01-15T10:00:00",
            "shipping_limit_date": "2018-01-03T10:00:00",
            "price": 120.0,
            "freight_value": 15.0,
            "order_item_id": f"item_{order_id[:6]}",
        }
        return json.dumps(_create_evidence("order", data, case_id))

    @server.tool()
    def get_order_items(order_id: str, case_id: str = "") -> str:
        """Fetch item records belonging to an order."""
        data = {
            "order_id": order_id,
            "items": [
                {
                    "order_item_id": f"item_{order_id[:6]}_1",
                    "product_id": "prod_tech_001",
                    "seller_id": "seller_brazil_01",
                    "price": 120.0,
                    "freight_value": 15.0,
                }
            ],
        }
        return json.dumps(_create_evidence("item", data, case_id))

    @server.tool()
    def get_customer_history(customer_unique_id: str, case_id: str = "") -> str:
        """Fetch customer profile and historical order IDs."""
        data = {
            "customer_unique_id": customer_unique_id,
            "order_ids": [f"ord_{customer_unique_id[:8]}_1", f"ord_{customer_unique_id[:8]}_2"],
            "total_lifetime_spend_brl": 450.0,
            "account_status": "active",
        }
        return json.dumps(_create_evidence("customer", data, case_id))

    @server.tool()
    def get_shipment(order_id: str, case_id: str = "") -> str:
        """Fetch logistics and delivery telemetry."""
        data = {
            "shipment_id": f"shp_{order_id[:8]}",
            "order_id": order_id,
            "shipment_status": "delivered",
            "carrier_id": "correios_br",
            "delivered_carrier_date": "2018-01-04T10:00:00",
            "delivered_customer_date": "2018-01-12T10:00:00",
            "estimated_delivery_date": "2018-01-15T10:00:00",
            "shipping_limit_date": "2018-01-03T10:00:00",
            "seller_id": "seller_brazil_01",
        }
        return json.dumps(_create_evidence("shipment", data, case_id))

    @server.tool()
    def get_payment(order_id: str, case_id: str = "") -> str:
        """Fetch payment ledger records."""
        data = {
            "order_id": order_id,
            "payment_sequential": 1,
            "payment_type": "credit_card",
            "payment_installments": 1,
            "payment_value": 135.0,
            "payment_reference": f"pay_{order_id[:8]}",
            "captured_total_brl": 135.0,
        }
        return json.dumps(_create_evidence("payment", data, case_id))

    @server.tool()
    def get_refund(order_id: str, case_id: str = "") -> str:
        """Fetch refund ledger records."""
        data = {
            "order_id": order_id,
            "refund_status": "none",
            "refunded_total_brl": 0.0,
            "refund_reference": None,
        }
        return json.dumps(_create_evidence("refund", data, case_id))

    @server.tool()
    def get_policy(policy_id: str = "EC_POLICY_V2", case_id: str = "") -> str:
        """Fetch platform dispute and refund policies."""
        data = {
            "policy_id": policy_id,
            "late_delivery_threshold_hours": 48,
            "seller_sla_window_days": 3,
            "full_refund_allowed_statuses": ["canceled", "unavailable"],
            "delay_compensation_rate": 0.10,
        }
        return json.dumps(_create_evidence("policy", data, case_id))

    @server.tool()
    def search_orders(order_id: str = "", customer_unique_id: str = "", case_id: str = "") -> str:
        """Search order index by ID or customer identifier."""
        matched = [order_id] if order_id else [f"ord_{customer_unique_id[:8]}"]
        data = {
            "order_ids": matched,
            "query": {"order_id": order_id, "customer_unique_id": customer_unique_id},
        }
        return json.dumps(_create_evidence("order", data, case_id))

    return server


class LocalEvidenceGateway:
    """In-memory gateway providing offline MCP evidence responses without network overhead."""

    def __init__(self, contracts: Any = None) -> None:
        self.contracts = contracts
        self._handlers = {
            "get_order": self._get_order,
            "get_order_items": self._get_order_items,
            "get_customer_history": self._get_customer_history,
            "get_shipment": self._get_shipment,
            "get_payment": self._get_payment,
            "get_refund": self._get_refund,
            "get_policy": self._get_policy,
            "search_orders": self._search_orders,
        }

    async def list_tools(self) -> list[str]:
        return sorted(self._handlers.keys())

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        handler = self._handlers.get(tool_name)
        if handler is None:
            raise RuntimeError(f"Unknown mock tool: {tool_name}")
        evidence = handler(case_id=case_id, **arguments)
        if self.contracts:
            self.contracts.validate_evidence(evidence, f"Mock tool {tool_name}")
        return evidence

    def _get_order(self, *, case_id: str, order_id: str = "ord_sample", **_: Any) -> dict[str, Any]:
        data = {
            "order_id": order_id,
            "customer_unique_id": f"cust_{order_id[:8]}",
            "seller_id": "seller_brazil_01",
            "order_status": "delivered",
            "order_purchase_timestamp": "2018-01-01T10:00:00",
            "order_delivered_carrier_date": "2018-01-04T10:00:00",
            "order_delivered_customer_date": "2018-01-12T10:00:00",
            "order_estimated_delivery_date": "2018-01-15T10:00:00",
            "shipping_limit_date": "2018-01-03T10:00:00",
            "price": 120.0,
            "freight_value": 15.0,
            "order_item_id": f"item_{order_id[:6]}",
        }
        return _create_evidence("order", data, case_id)

    def _get_order_items(
        self, *, case_id: str, order_id: str = "ord_sample", **_: Any
    ) -> dict[str, Any]:
        data = {
            "order_id": order_id,
            "items": [
                {
                    "order_item_id": f"item_{order_id[:6]}_1",
                    "product_id": "prod_tech_001",
                    "seller_id": "seller_brazil_01",
                    "price": 120.0,
                    "freight_value": 15.0,
                }
            ],
        }
        return _create_evidence("item", data, case_id)

    def _get_customer_history(
        self, *, case_id: str, customer_unique_id: str = "cust_sample", **_: Any
    ) -> dict[str, Any]:
        data = {
            "customer_unique_id": customer_unique_id,
            "order_ids": [f"ord_{customer_unique_id[:8]}_1", f"ord_{customer_unique_id[:8]}_2"],
            "total_lifetime_spend_brl": 450.0,
            "account_status": "active",
        }
        return _create_evidence("customer", data, case_id)

    def _get_shipment(
        self, *, case_id: str, order_id: str = "ord_sample", **_: Any
    ) -> dict[str, Any]:
        data = {
            "shipment_id": f"shp_{order_id[:8]}",
            "order_id": order_id,
            "shipment_status": "delivered",
            "carrier_id": "correios_br",
            "delivered_carrier_date": "2018-01-04T10:00:00",
            "delivered_customer_date": "2018-01-12T10:00:00",
            "estimated_delivery_date": "2018-01-15T10:00:00",
            "shipping_limit_date": "2018-01-03T10:00:00",
            "seller_id": "seller_brazil_01",
        }
        return _create_evidence("shipment", data, case_id)

    def _get_payment(
        self, *, case_id: str, order_id: str = "ord_sample", **_: Any
    ) -> dict[str, Any]:
        data = {
            "order_id": order_id,
            "payment_sequential": 1,
            "payment_type": "credit_card",
            "payment_installments": 1,
            "payment_value": 135.0,
            "payment_reference": f"pay_{order_id[:8]}",
            "captured_total_brl": 135.0,
        }
        return _create_evidence("payment", data, case_id)

    def _get_refund(
        self, *, case_id: str, order_id: str = "ord_sample", **_: Any
    ) -> dict[str, Any]:
        data = {
            "order_id": order_id,
            "refund_status": "none",
            "refunded_total_brl": 0.0,
            "refund_reference": None,
        }
        return _create_evidence("refund", data, case_id)

    def _get_policy(
        self, *, case_id: str, policy_id: str = "EC_POLICY_V2", **_: Any
    ) -> dict[str, Any]:
        data = {
            "policy_id": policy_id,
            "late_delivery_threshold_hours": 48,
            "seller_sla_window_days": 3,
            "full_refund_allowed_statuses": ["canceled", "unavailable"],
            "delay_compensation_rate": 0.10,
        }
        return _create_evidence("policy", data, case_id)

    def _search_orders(
        self, *, case_id: str, order_id: str = "", customer_unique_id: str = "", **_: Any
    ) -> dict[str, Any]:
        matched = [order_id] if order_id else [f"ord_{customer_unique_id[:8]}"]
        data = {
            "order_ids": matched,
            "query": {"order_id": order_id, "customer_unique_id": customer_unique_id},
        }
        return _create_evidence("order", data, case_id)
