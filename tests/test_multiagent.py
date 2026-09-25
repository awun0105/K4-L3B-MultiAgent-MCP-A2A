from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from student_agent.a2a import A2ABus
from student_agent.contracts import Contracts
from student_agent.local_tools import LOCAL_TOOL_NAMES, rank_entity_candidates
from student_agent.tooling import ToolCatalog
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case


def _evidence(domain: str, data: dict[str, Any], suffix: str) -> dict[str, Any]:
    return {
        "schema_version": "day09-mcp-evidence-v1",
        "evidence_ref": f"ev_{suffix}aaaaaaaaaaaaaaaaaaaa",
        "result_hash": f"sha256:{'ab' * 32}",
        "domain": domain,
        "data": data,
    }


class FakeGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def list_tools(self) -> list[str]:
        return [
            "get_order",
            "get_order_items",
            "get_customer_history",
            "get_shipment",
            "get_payment",
            "get_refund",
            "get_policy",
        ]

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append((tool_name, {"case_id": case_id, **arguments}))
        order_id = arguments.get("order_id", "ord_late_1")
        if tool_name == "get_order":
            return _evidence(
                "order",
                {
                    "order_id": order_id,
                    "customer_unique_id": "cust_1",
                    "seller_id": "seller_9",
                    "order_status": "delivered",
                    "order_purchase_timestamp": "2018-01-01T10:00:00",
                    "order_delivered_carrier_date": "2018-01-06T10:00:00",
                    "order_delivered_customer_date": "2018-01-20T10:00:00",
                    "order_estimated_delivery_date": "2018-01-10T10:00:00",
                    "shipping_limit_date": "2018-01-03T10:00:00",
                    "price": 100.0,
                    "freight_value": 10.0,
                    "order_item_id": "item_1",
                },
                "order1",
            )
        if tool_name == "get_customer_history":
            return _evidence(
                "customer",
                {"customer_unique_id": "cust_1", "order_ids": ["ord_late_1", "ord_old"]},
                "cust01",
            )
        if tool_name == "get_shipment":
            return _evidence(
                "shipment",
                {
                    "shipment_id": "shp_1",
                    "shipment_status": "delivered",
                    "delivered_customer_date": "2018-01-20T10:00:00",
                    "delivered_carrier_date": "2018-01-06T10:00:00",
                    "estimated_delivery_date": "2018-01-10T10:00:00",
                    "shipping_limit_date": "2018-01-03T10:00:00",
                    "seller_id": "seller_9",
                },
                "ship01",
            )
        if tool_name == "get_payment":
            return _evidence(
                "payment",
                {
                    "payment_sequential": 1,
                    "payment_type": "credit_card",
                    "payment_value": 110.0,
                    "payment_reference": "pay_1",
                },
                "pay001",
            )
        if tool_name == "get_refund":
            return _evidence("refund", {"refund_status": "none", "refund_value": 0}, "ref001")
        if tool_name == "get_policy":
            return _evidence("policy", {"policy_id": "late_delivery"}, "pol001")
        raise RuntimeError(f"unexpected tool {tool_name}")


def test_solve_case_emits_multi_agent_trace_and_valid_output(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    gateway = FakeGateway()
    case = {
        "case_id": "L3B_CASE_001",
        "customer_unique_id": "cust_1",
        "candidate_order_ids": ["ord_late_1"],
        "claims": [{"claim_id": "late_delivery"}],
    }
    output = asyncio.run(solve_case(case, gateway, trace))  # type: ignore[arg-type]
    contracts.validate_output(output, "test-output")
    assert output["assessment"]["primary_issue"] == "late_delivery_seller"
    assert output["shipment_analysis"]["verdict"] == "seller_delay"
    assert "seller_9" in output["shipment_analysis"]["late_seller_ids"]
    assert output["entity_resolution"]["status"] == "resolved"
    assert output["evidence_refs"]
    events = [
        json.loads(line)
        for line in (tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()
        if line
    ]
    types = {event["event_type"] for event in events}
    actors = {event["actor"] for event in events}
    assert "task_assigned" in types
    assert "handoff" in types
    assert "tool_result_consumed" in types
    assert "verification_completed" in types
    assert {"coordinator", "entity-agent", "shipment-agent", "payment-agent", "verifier"} <= actors
    assert all(call[1]["case_id"] == "L3B_CASE_001" for call in gateway.calls)
    unique_calls = {(name, tuple(sorted(args.items()))) for name, args in gateway.calls}
    assert len(gateway.calls) == len(unique_calls)


def test_tool_catalog_prefers_canonical_aliases() -> None:
    catalog = ToolCatalog(["get_order_items", "get_order", "get_customer_history"])
    assert catalog.resolve("order") == "get_order"
    assert catalog.resolve("order_items") == "get_order_items"
    assert catalog.resolve("customer_history") == "get_customer_history"


def test_rank_entity_rejects_unrelated_candidate() -> None:
    result = rank_entity_candidates(
        hinted_customer_id="cust_1",
        hinted_order_ids=["good", "noise"],
        order_records={
            "good": {"customer_unique_id": "cust_1", "order_status": "delivered"},
            "noise": {"customer_unique_id": "other", "order_status": "created"},
        },
    )
    assert result["status"] == "resolved"
    assert result["resolved_order_ids"] == ["good"]
    assert "noise" in result["rejected_candidates"]


def test_a2a_bus_enforces_hop_budget() -> None:
    bus = A2ABus(max_hops=1)
    bus.request(
        sender="coordinator",
        recipient="entity-agent",
        intent="resolve",
        correlation_id="L3B_CASE_001",
    )
    with pytest.raises(RuntimeError, match="hop budget"):
        bus.request(
            sender="coordinator",
            recipient="order-agent",
            intent="inspect",
            correlation_id="L3B_CASE_001",
        )


def test_local_tool_inventory_is_explicit() -> None:
    assert "rank_entity_candidates" in LOCAL_TOOL_NAMES
    assert "verify_output_invariants" in LOCAL_TOOL_NAMES
