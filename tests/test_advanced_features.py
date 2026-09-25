from __future__ import annotations

import asyncio
from pathlib import Path

from student_agent.a2a import A2ABus
from student_agent.contracts import Contracts
from student_agent.llm_reasoner import LLMReasoner
from student_agent.local_tools import (
    LOCAL_TOOL_NAMES,
    arbitrate_specialist_consensus,
    detect_anomaly_signals,
    evaluate_evidence_provenance,
    explain_decision_rationale,
    reconstruct_event_timeline,
)
from student_agent.mcp_server import LocalEvidenceGateway
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case


def test_all_local_tools_registered() -> None:
    expected = {
        "rank_entity_candidates",
        "detect_data_conflicts",
        "calibrate_confidence",
        "recommend_resolution",
        "verify_output_invariants",
        "reconstruct_event_timeline",
        "arbitrate_specialist_consensus",
        "detect_anomaly_signals",
        "evaluate_evidence_provenance",
        "explain_decision_rationale",
    }
    assert expected.issubset(set(LOCAL_TOOL_NAMES))


def test_a2a_p2p_negotiation_and_mermaid() -> None:
    bus = A2ABus(max_hops=10)
    _ = bus.request(
        sender="coordinator",
        recipient="shipment-agent",
        intent="analyze_shipment",
        correlation_id="CASE_TEST",
    )
    prop = bus.propose(
        sender="shipment-agent",
        recipient="payment-agent",
        intent="negotiate_delay_compensation",
        correlation_id="CASE_TEST",
        payload={"delay_type": "seller_delay"},
    )
    conf = bus.confirm(
        sender="payment-agent",
        recipient="shipment-agent",
        intent="compensation_terms_confirmed",
        correlation_id="CASE_TEST",
        in_reply_to=prop.message_id,
    )
    assert len(bus.log) == 3
    assert conf.in_reply_to == prop.message_id
    mermaid = bus.to_mermaid()
    assert "sequenceDiagram" in mermaid
    assert "shipment_agent->>payment_agent" in mermaid


def test_reconstruct_event_timeline_sla_breach() -> None:
    order_data = {
        "order_purchase_timestamp": "2018-01-01T10:00:00",
        "shipping_limit_date": "2018-01-03T10:00:00",
        "order_delivered_carrier_date": "2018-01-05T10:00:00",
        "order_delivered_customer_date": "2018-01-20T10:00:00",
        "order_estimated_delivery_date": "2018-01-15T10:00:00",
    }
    shipment_data = {
        "delivered_carrier_date": "2018-01-05T10:00:00",
        "delivered_customer_date": "2018-01-20T10:00:00",
    }
    timeline = reconstruct_event_timeline(order_data, shipment_data)
    assert timeline["timeline_complete"] is True
    assert timeline["seller_sla_breach"] is True
    assert timeline["logistics_sla_breach"] is True
    assert timeline["seller_delay_hours"] == 48.0


def test_arbitrate_specialist_consensus() -> None:
    findings = {
        "entity_status": "resolved",
        "shipment_verdict": "seller_delay",
        "payment_verdict": "reconciled",
        "order_status": "delivered",
    }
    res = arbitrate_specialist_consensus(findings)
    assert res["consensus_issue"] == "late_delivery_seller"
    assert res["confidence_multiplier"] >= 0.90


def test_detect_anomaly_signals() -> None:
    order_data = {"order_total_brl": 100.0}
    payment_data = {
        "payments": [
            {"payment_sequential": 1, "payment_value": 50.0},
            {"payment_sequential": 1, "payment_value": 50.0},  # duplicate sequential
        ]
    }
    signals = detect_anomaly_signals(order_data, payment_data, {}, {})
    assert any(s["risk_type"] == "DUPLICATE_SEQUENTIAL" for s in signals)


def test_evaluate_evidence_provenance() -> None:
    valid_ref = "ev_order_1234567890abcdef12345"
    fake_ref = "invalid_reference_code"
    eval_res = evaluate_evidence_provenance([valid_ref, fake_ref])
    assert eval_res["valid_count"] == 1
    assert eval_res["invalid_count"] == 1
    assert eval_res["is_provenance_clean"] is False
    assert eval_res["clean_refs"] == [valid_ref]


def test_explain_decision_rationale() -> None:
    text = explain_decision_rationale(
        {"primary_issue": "late_delivery_seller", "case_status": "action_required"},
        {"recommended_refund_brl": 15.0},
        {"responsible_parties": [{"party_type": "seller"}]},
    )
    assert "action_required" in text
    assert "late_delivery_seller" in text
    assert "seller" in text
    assert "15.00 BRL" in text


def test_llm_reasoner_offline_fallback() -> None:
    reasoner = LLMReasoner(api_base="http://localhost:99999/v1", timeout=0.1)
    is_avail = asyncio.run(reasoner.is_available())
    assert is_avail is False

    res = asyncio.run(
        reasoner.analyze_root_cause(
            case_id="CASE_TEST",
            order_summary={},
            shipment_verdict="on_time",
            payment_verdict="reconciled",
        )
    )
    assert res is None


def test_mock_mcp_server_and_gateway(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    gateway = LocalEvidenceGateway(contracts)

    tools = asyncio.run(gateway.list_tools())
    assert "get_order" in tools
    assert "get_shipment" in tools

    order = asyncio.run(gateway.call("get_order", case_id="CASE_TEST", order_id="ord_12345678"))
    assert order["schema_version"] == "day09-mcp-evidence-v1"
    assert order["domain"] == "order"
    assert order["evidence_ref"].startswith("ev_order_")


def test_full_pipeline_with_local_gateway_and_dag_dispatch(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    trace_path = tmp_path / "dag_trace.jsonl"
    trace = TraceWriter(trace_path, contracts)
    gateway = LocalEvidenceGateway(contracts)

    case = {
        "case_id": "L3B_CASE_099",
        "customer_unique_id": "cust_12345678",
        "candidate_order_ids": ["ord_12345678"],
        "claims": [{"claim_id": "claim_01"}],
    }

    output = asyncio.run(solve_case(case, gateway, trace))  # type: ignore[arg-type]
    contracts.validate_output(output, "output_validation")

    assert output["case_id"] == "L3B_CASE_099"
    assert output["schema_version"] == "day09-l3b-output-v2"
    assert output["entity_resolution"]["status"] == "resolved"
    assert output["evidence_refs"]
