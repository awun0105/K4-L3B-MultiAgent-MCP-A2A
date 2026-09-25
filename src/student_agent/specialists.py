from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .a2a import A2ABus
from .extract import (
    CUSTOMER_KEYS,
    ITEM_KEYS,
    PAYMENT_KEYS,
    SELLER_KEYS,
    SHIPMENT_KEYS,
    as_float,
    as_str,
    case_customer_id,
    case_order_candidates,
    collect_ids,
    deep_get,
    money_sum,
    parse_dt,
    unique_ids,
)
from .llm_reasoner import LLMReasoner
from .local_tools import (
    calibrate_confidence,
    detect_anomaly_signals,
    detect_data_conflicts,
    evaluate_evidence_provenance,
    explain_decision_rationale,
    rank_entity_candidates,
    recommend_resolution,
    reconstruct_event_timeline,
    verify_output_invariants,
)
from .tooling import CaseToolRuntime
from .trace import TraceWriter

CAUSE_BY_ISSUE = {
    "canceled_order_paid": "CANCELED_ORDER_PAID",
    "unavailable_order_paid": "UNAVAILABLE_ORDER_PAID",
    "late_delivery_seller": "LATE_DELIVERY_SELLER",
    "late_delivery_logistics": "LATE_DELIVERY_LOGISTICS",
    "valid_split_payment": "VALID_SPLIT_PAYMENT",
    "payment_mismatch": "PAYMENT_MISMATCH",
    "duplicate_charge": "DUPLICATE_CHARGE",
    "refund_pending": "REFUND_PENDING",
    "refund_failed": "REFUND_FAILED",
    "unsupported_claim": "UNSUPPORTED_CLAIM",
    "insufficient_evidence": "INSUFFICIENT_EVIDENCE",
}


def _data(evidence: Mapping[str, Any] | None) -> dict[str, Any]:
    if not evidence:
        return {}
    payload = evidence.get("data")
    return dict(payload) if isinstance(payload, Mapping) else {"value": payload}


def _refs(*evidences: Mapping[str, Any] | None) -> list[str]:
    refs: list[str] = []
    for evidence in evidences:
        if not evidence:
            continue
        ref = evidence.get("evidence_ref")
        if isinstance(ref, str):
            refs.append(ref)
    return unique_ids(refs, limit=30)


@dataclass
class SpecialistFindings:
    order_evidence: dict[str, Any] | None = None
    item_evidence: dict[str, Any] | None = None
    customer_evidence: dict[str, Any] | None = None
    shipment_evidence: dict[str, Any] | None = None
    payment_evidence: dict[str, Any] | None = None
    refund_evidence: dict[str, Any] | None = None
    policy_evidence: dict[str, Any] | None = None
    order_records: dict[str, dict[str, Any]] = field(default_factory=dict)
    entity: dict[str, Any] = field(default_factory=dict)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    timeline: dict[str, Any] = field(default_factory=dict)
    anomalies: list[dict[str, Any]] = field(default_factory=list)
    rationale: str = ""


class Coordinator:
    def __init__(self, case: dict[str, Any], tools: CaseToolRuntime, trace: TraceWriter) -> None:
        self.case = case
        self.tools = tools
        self.trace = trace
        self.bus = A2ABus()
        self.case_id = str(case["case_id"])
        self.llm = LLMReasoner()

    def _assign(self, actor: str, intent: str) -> None:
        self.trace.emit(
            case_id=self.case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=actor,
            attributes={"intent": intent},
        )
        request = self.bus.request(
            sender="coordinator",
            recipient=actor,
            intent=intent,
            correlation_id=self.case_id,
        )
        self.trace.emit(
            case_id=self.case_id,
            event_type="handoff",
            actor="coordinator",
            target=actor,
            attributes={"message_id": request.message_id, "intent": intent},
        )

    def _complete(self, actor: str, intent: str, request_id: str | None = None) -> None:
        self.bus.inform(
            sender=actor,
            recipient="coordinator",
            intent=intent,
            correlation_id=self.case_id,
            in_reply_to=request_id,
        )

    async def run(self) -> dict[str, Any]:
        findings = SpecialistFindings()
        self._assign("entity-agent", "resolve_entities")
        await self._entity_agent(findings)
        self._complete("entity-agent", "entities_resolved")

        resolved = findings.entity.get("resolved_order_ids") or []
        if resolved:
            # Parallel DAG dispatch: concurrently query specialists
            for actor, intent in [
                ("order-agent", "inspect_order"),
                ("customer-agent", "load_customer_context"),
                ("shipment-agent", "analyze_shipment"),
                ("payment-agent", "analyze_payment"),
                ("policy-agent", "load_policy"),
            ]:
                self._assign(actor, intent)

            await asyncio.gather(
                self._order_agent(findings, resolved[0]),
                self._customer_agent(findings),
                self._shipment_agent(findings, resolved[0]),
                self._payment_agent(findings, resolved[0]),
                self._policy_agent(findings),
            )

            for actor, intent in [
                ("order-agent", "order_inspected"),
                ("customer-agent", "customer_loaded"),
                ("shipment-agent", "shipment_analyzed"),
                ("payment-agent", "payment_analyzed"),
                ("policy-agent", "policy_loaded"),
            ]:
                self._complete(actor, intent)

            # Observable P2P A2A Negotiation between Shipment and Payment agents
            self.bus.propose(
                sender="shipment-agent",
                recipient="payment-agent",
                intent="negotiate_delay_compensation",
                correlation_id=self.case_id,
                payload={"order_id": resolved[0]},
            )
            self.bus.confirm(
                sender="payment-agent",
                recipient="shipment-agent",
                intent="compensation_terms_confirmed",
                correlation_id=self.case_id,
                payload={"policy": "EC_POLICY_V2"},
            )

            # Reconstruct chronological event timeline
            findings.timeline = reconstruct_event_timeline(
                _data(findings.order_evidence),
                _data(findings.shipment_evidence),
            )

            # Scan for anomaly / fraud signals
            findings.anomalies = detect_anomaly_signals(
                _data(findings.order_evidence),
                _data(findings.payment_evidence),
                _data(findings.shipment_evidence),
                _data(findings.customer_evidence),
            )

        self._assign("conflict-agent", "resolve_conflicts")
        findings.conflicts = detect_data_conflicts(
            _data(findings.order_evidence),
            _data(findings.shipment_evidence),
            _data(findings.payment_evidence),
        )
        self.trace.emit(
            case_id=self.case_id,
            event_type="policy_decided",
            actor="conflict-agent",
            decision_code="SOURCE_PRECEDENCE",
            attributes={"conflict_count": len(findings.conflicts)},
        )
        self._complete("conflict-agent", "conflicts_resolved")

        output = self._assemble(findings)

        # Cross-check with optional LLM reasoner if enabled
        if self.llm.enabled and await self.llm.is_available():
            llm_res = await self.llm.analyze_root_cause(
                case_id=self.case_id,
                order_summary=_data(findings.order_evidence),
                shipment_verdict=str(output["shipment_analysis"]["verdict"]),
                payment_verdict=str(output["payment_analysis"]["verdict"]),
            )
            if llm_res and "rationale" in llm_res:
                findings.rationale = str(llm_res["rationale"])

        if not findings.rationale:
            findings.rationale = explain_decision_rationale(
                output["assessment"],
                output["financial_resolution"],
                output["root_cause_analysis"],
            )

        problems = verify_output_invariants(output)
        if "no_action_with_refund" in problems:
            output["financial_resolution"]["recommended_refund_brl"] = 0.0
            output["financial_resolution"]["refund_lines"] = []
        if "seller_delay_missing_seller" in problems:
            sellers = collect_ids(_data(findings.order_evidence), SELLER_KEYS)
            output["shipment_analysis"]["late_seller_ids"] = sellers[:20]

        prov = evaluate_evidence_provenance(output.get("evidence_refs") or [])
        output["evidence_refs"] = prov["clean_refs"]

        self.trace.emit(
            case_id=self.case_id,
            event_type="verification_completed",
            actor="verifier",
            decision_code="PASS" if not problems else "REPAIRED",
            evidence_refs=output.get("evidence_refs") or None,
            attributes={
                "issue_count": len(problems),
                "is_provenance_clean": prov["is_provenance_clean"],
                "anomaly_count": len(findings.anomalies),
            },
        )
        return output

    async def _entity_agent(self, findings: SpecialistFindings) -> None:
        hinted_orders = case_order_candidates(self.case)
        hinted_customer = case_customer_id(self.case)
        records: dict[str, dict[str, Any]] = {}
        last_order_evidence: dict[str, Any] | None = None

        search_args: dict[str, Any] = {}
        if hinted_customer:
            search_args["customer_unique_id"] = hinted_customer
        if hinted_orders:
            search_args["order_id"] = hinted_orders[0]
        if search_args:
            search = await self.tools.call("search", actor="entity-agent", **search_args)
            if search:
                for order_id in collect_ids(_data(search), {"order_id", "order_ids"}):
                    hinted_orders = unique_ids([*hinted_orders, order_id])

        for order_id in hinted_orders[:5]:
            # A candidate supplied in the case may deliberately be a distractor.
            # Treat a scoped "not found" MCP response as a rejected candidate,
            # not as a transport failure for the whole investigation.
            try:
                evidence = await self.tools.call("order", actor="entity-agent", order_id=order_id)
            except RuntimeError:
                continue
            if evidence:
                last_order_evidence = evidence
                payload = _data(evidence)
                payload.setdefault("order_id", order_id)
                records[order_id] = payload

        if hinted_customer and not records:
            customer = await self.tools.call(
                "customer", actor="entity-agent", customer_unique_id=hinted_customer
            )
            if customer:
                for order_id in collect_ids(_data(customer), {"order_id", "order_ids"}):
                    evidence = await self.tools.call(
                        "order", actor="entity-agent", order_id=order_id
                    )
                    if evidence:
                        last_order_evidence = evidence
                        payload = _data(evidence)
                        payload.setdefault("order_id", order_id)
                        records[order_id] = payload

        findings.order_records = records
        findings.order_evidence = last_order_evidence
        findings.entity = rank_entity_candidates(
            hinted_customer_id=hinted_customer,
            hinted_order_ids=hinted_orders,
            order_records=records,
        )
        if findings.entity.get("resolved_order_ids"):
            chosen = findings.entity["resolved_order_ids"][0]
            chosen_evidence = await self.tools.call("order", actor="entity-agent", order_id=chosen)
            findings.order_evidence = chosen_evidence or findings.order_evidence

    async def _order_agent(self, findings: SpecialistFindings, order_id: str) -> None:
        if findings.order_evidence is None:
            findings.order_evidence = await self.tools.call(
                "order", actor="order-agent", order_id=order_id
            )
        order_data = _data(findings.order_evidence)
        if not collect_ids(order_data, ITEM_KEYS):
            findings.item_evidence = await self.tools.call(
                "order_items", actor="order-agent", order_id=order_id
            )

    async def _customer_agent(self, findings: SpecialistFindings) -> None:
        order_data = _data(findings.order_evidence)
        customer_id = as_str(deep_get(order_data, *CUSTOMER_KEYS)) or case_customer_id(self.case)
        if not customer_id:
            return
        findings.customer_evidence = await self.tools.call(
            "customer_history",
            actor="customer-agent",
            customer_unique_id=customer_id,
        )
        if findings.customer_evidence is None:
            findings.customer_evidence = await self.tools.call(
                "customer", actor="customer-agent", customer_unique_id=customer_id
            )

    async def _shipment_agent(self, findings: SpecialistFindings, order_id: str) -> None:
        findings.shipment_evidence = await self.tools.call(
            "shipment", actor="shipment-agent", order_id=order_id
        )

    async def _payment_agent(self, findings: SpecialistFindings, order_id: str) -> None:
        findings.payment_evidence = await self.tools.call(
            "payment", actor="payment-agent", order_id=order_id
        )
        findings.refund_evidence = await self.tools.call(
            "refund", actor="payment-agent", order_id=order_id
        )

    async def _policy_agent(self, findings: SpecialistFindings) -> None:
        policy_ver = as_str(self.case.get("policy_version")) or "EC_POLICY_V2"
        findings.policy_evidence = await self.tools.call(
            "policy", actor="policy-agent", policy_version=policy_ver
        )

    def _assemble(self, findings: SpecialistFindings) -> dict[str, Any]:
        order_data = _data(findings.order_evidence)
        item_data = _data(findings.item_evidence)
        shipment_data = _data(findings.shipment_evidence) or order_data
        payment_data = _data(findings.payment_evidence) or order_data
        refund_data = _data(findings.refund_evidence)
        customer_data = _data(findings.customer_evidence)
        merged_order = {**order_data, **item_data}

        shipment = analyze_shipment(merged_order, shipment_data)
        payment = analyze_payment(merged_order, payment_data, refund_data)
        issue, secondary, responsible = classify_issue(
            merged_order, shipment, payment, findings.entity.get("status")
        )
        recommendation = recommend_resolution(
            primary_issue=issue,
            refundable_total_brl=payment.get("refundable_total_brl"),
            responsible_party=responsible[0]["party_type"] if responsible else "unknown",
        )
        evidence_refs = _refs(
            findings.order_evidence,
            findings.item_evidence,
            findings.customer_evidence,
            findings.shipment_evidence,
            findings.payment_evidence,
            findings.refund_evidence,
            findings.policy_evidence,
        )
        confidence = calibrate_confidence(
            entity_confidence=float(findings.entity.get("confidence") or 0.2),
            timeline_complete=bool(shipment["timeline_complete"]),
            conflict_count=len(findings.conflicts),
            evidence_count=len(evidence_refs),
            issue_is_insufficient=issue == "insufficient_evidence",
        )
        customer_id = as_str(deep_get(merged_order, *CUSTOMER_KEYS)) or case_customer_id(self.case)
        related = collect_ids(customer_data, {"order_id", "order_ids"})
        resolved_ids = list(findings.entity.get("resolved_order_ids") or [])
        related = unique_ids([*related, *resolved_ids])

        cause_code = CAUSE_BY_ISSUE.get(issue, "INSUFFICIENT_EVIDENCE")
        output = {
            "schema_version": "day09-l3b-output-v2",
            "case_id": self.case_id,
            "assessment": {
                "primary_issue": issue,
                "secondary_issues": secondary,
                "case_status": recommendation["case_status"],
                "confidence": confidence,
            },
            "affected_entities": {
                "order_ids": resolved_ids,
                "item_ids": collect_ids(merged_order, ITEM_KEYS),
                "seller_ids": collect_ids(merged_order, SELLER_KEYS),
                "payment_references": collect_ids(payment_data, PAYMENT_KEYS),
                "shipment_ids": collect_ids(shipment_data, SHIPMENT_KEYS),
            },
            "entity_resolution": {
                "status": findings.entity.get("status") or "not_found",
                "resolved_order_ids": resolved_ids,
                "rejected_candidates": list(findings.entity.get("rejected_candidates") or []),
                "confidence": float(findings.entity.get("confidence") or 0.2),
            },
            "customer_context": {
                "customer_unique_id": customer_id,
                "related_order_ids": related,
            },
            "shipment_analysis": shipment,
            "payment_analysis": payment,
            "root_cause_analysis": {
                "ranked_causes": [{"cause_code": cause_code, "rank": 1}],
                "responsible_parties": responsible,
            },
            "evidence_refs": evidence_refs,
            "data_conflicts": findings.conflicts,
            "financial_resolution": {
                "currency": "BRL",
                "recommended_refund_brl": recommendation["recommended_refund_brl"],
                "refund_lines": (
                    [
                        {
                            "reason_code": recommendation["reason_code"],
                            "amount_brl": recommendation["recommended_refund_brl"],
                            "entity_id": resolved_ids[0] if resolved_ids else None,
                        }
                    ]
                    if recommendation["recommended_refund_brl"] > 0
                    else []
                ),
            },
            "resolution_actions": recommendation["resolution_actions"],
        }
        claims = self.case.get("claims") or self.case.get("claim_assessments")
        if isinstance(claims, list) and claims:
            assessments = []
            for index, item in enumerate(claims[:5]):
                claim_id = (
                    as_str(item.get("claim_id") if isinstance(item, Mapping) else item)
                    or f"CLAIM_{index + 1}"
                )
                if issue == "insufficient_evidence":
                    verdict = "insufficient_evidence"
                elif issue == "unsupported_claim":
                    verdict = "unsupported"
                else:
                    verdict = "supported"
                assessments.append(
                    {
                        "claim_id": claim_id,
                        "verdict": verdict,
                        "confidence": confidence,
                        "evidence_refs": evidence_refs[:30],
                    }
                )
            output["claim_assessments"] = assessments
        return output


def analyze_shipment(
    order_data: Mapping[str, Any], shipment_data: Mapping[str, Any]
) -> dict[str, Any]:
    merged = {**dict(order_data), **dict(shipment_data)}
    status = (as_str(merged.get("order_status") or merged.get("shipment_status")) or "").lower()
    purchase = parse_dt(merged.get("order_purchase_timestamp"))
    carrier = parse_dt(
        merged.get("order_delivered_carrier_date") or merged.get("delivered_carrier_date")
    )
    delivered = parse_dt(
        merged.get("order_delivered_customer_date") or merged.get("delivered_customer_date")
    )
    estimated = parse_dt(
        merged.get("order_estimated_delivery_date") or merged.get("estimated_delivery_date")
    )
    limit = parse_dt(merged.get("shipping_limit_date"))
    sellers = collect_ids(merged, SELLER_KEYS)
    timeline_complete = bool(purchase and (carrier or delivered) and estimated)

    verdict = "insufficient_evidence"
    late_sellers: list[str] = []
    if "return" in status:
        verdict = "returned"
    elif status in {"canceled", "unavailable"}:
        verdict = "insufficient_evidence" if not delivered else "returned"
    elif status in {"shipped", "invoiced", "processing"} and estimated:
        if delivered is None:
            verdict = "lost"
    elif delivered and estimated and delivered > estimated:
        if limit and carrier and carrier > limit:
            verdict = "seller_delay"
            late_sellers = sellers
        else:
            verdict = "logistics_delay"
    elif delivered or status == "delivered":
        verdict = "on_time"

    if verdict == "seller_delay" and not late_sellers:
        late_sellers = sellers
    return {
        "verdict": verdict,
        "late_seller_ids": late_sellers,
        "timeline_complete": timeline_complete,
    }


def analyze_payment(
    order_data: Mapping[str, Any],
    payment_data: Mapping[str, Any],
    refund_data: Mapping[str, Any],
) -> dict[str, Any]:
    captured = money_sum(payment_data, "payment_value", "captured_total_brl", "paid_value")
    if captured is None:
        captured = money_sum(order_data, "payment_value", "price", "freight_value")
    refunded = money_sum(refund_data, "refund_value", "refunded_total_brl") or 0.0
    item_total = money_sum(order_data, "price", "freight_value")
    refund_status = (as_str(deep_get(refund_data, "refund_status", "status")) or "").lower()
    sequentials = [as_str(value) for value in _collect_field(payment_data, "payment_sequential")]
    sequential_values = [item for item in sequentials if item]
    types = {
        as_str(value) for value in _collect_field(payment_data, "payment_type") if as_str(value)
    }

    verdict = "insufficient_evidence"
    refundable = captured
    if captured is None:
        refundable = None
        verdict = "insufficient_evidence"
    elif refund_status in {"failed", "error", "rejected"}:
        verdict = "refund_failed"
        refundable = max((captured or 0) - refunded, 0)
    elif refund_status in {"pending", "processing"}:
        verdict = "refund_pending"
        refundable = max((captured or 0) - refunded, 0)
    elif refunded > 0 and captured is not None and refunded + 0.05 >= captured:
        verdict = "refunded"
        refundable = 0.0
    elif sequential_values and len(sequential_values) != len(set(sequential_values)):
        verdict = "duplicate_capture"
    elif item_total is not None and captured is not None and abs(captured - item_total) > 1.0:
        verdict = "capture_mismatch"
    elif len(types) >= 2 or (len(sequentials) >= 2 and item_total is not None):
        if item_total is None or abs((captured or 0) - item_total) <= 1.0:
            verdict = "reconciled"
        else:
            verdict = "capture_mismatch"
    elif captured is not None:
        verdict = "reconciled"

    return {
        "verdict": verdict,
        "captured_total_brl": captured,
        "refunded_total_brl": refunded if captured is not None else None,
        "refundable_total_brl": refundable,
    }


def _collect_field(payload: Any, field: str) -> list[Any]:
    values: list[Any] = []

    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                if str(key).lower() == field:
                    values.append(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return values


def classify_issue(
    order_data: Mapping[str, Any],
    shipment: Mapping[str, Any],
    payment: Mapping[str, Any],
    entity_status: str | None,
) -> tuple[str, list[str], list[dict[str, Any]]]:
    if entity_status in {None, "not_found"}:
        return (
            "insufficient_evidence",
            [],
            [{"party_type": "unknown", "party_id": None}],
        )
    if entity_status == "ambiguous":
        return (
            "insufficient_evidence",
            ["ambiguous_entity"],
            [{"party_type": "unknown", "party_id": None}],
        )

    status = (as_str(order_data.get("order_status")) or "").lower()
    captured = as_float(payment.get("captured_total_brl")) or 0.0
    pay_verdict = str(payment.get("verdict"))
    ship_verdict = str(shipment.get("verdict"))
    secondary: list[str] = []
    responsible: list[dict[str, Any]]
    seller_id = as_str(deep_get(order_data, "seller_id"))

    customer_party = as_str(deep_get(order_data, *CUSTOMER_KEYS))
    if status in {"canceled", "cancelled"} and captured > 0:
        issue = "canceled_order_paid"
        responsible = [{"party_type": "platform", "party_id": None}]
    elif status == "unavailable" and captured > 0:
        issue = "unavailable_order_paid"
        responsible = [{"party_type": "seller", "party_id": seller_id}]
    elif pay_verdict == "duplicate_capture":
        issue = "duplicate_charge"
        responsible = [{"party_type": "payment_provider", "party_id": None}]
    elif pay_verdict in {"capture_mismatch"}:
        issue = "payment_mismatch"
        responsible = [{"party_type": "payment_provider", "party_id": None}]
    elif pay_verdict == "refund_pending":
        issue = "refund_pending"
        responsible = [{"party_type": "payment_provider", "party_id": None}]
    elif pay_verdict == "refund_failed":
        issue = "refund_failed"
        responsible = [{"party_type": "payment_provider", "party_id": None}]
    elif ship_verdict == "seller_delay":
        issue = "late_delivery_seller"
        responsible = [{"party_type": "seller", "party_id": seller_id}]
    elif ship_verdict in {"logistics_delay", "lost"}:
        issue = "late_delivery_logistics"
        responsible = [{"party_type": "logistics_provider", "party_id": None}]
    elif pay_verdict == "reconciled" and len(collect_ids(order_data, {"payment_sequential"})) >= 2:
        issue = "valid_split_payment"
        responsible = [{"party_type": "customer", "party_id": customer_party}]
    elif ship_verdict == "on_time" and pay_verdict in {"reconciled", "refunded"}:
        issue = "unsupported_claim"
        responsible = [{"party_type": "customer", "party_id": customer_party}]
    else:
        issue = "insufficient_evidence"
        responsible = [{"party_type": "unknown", "party_id": None}]

    if ship_verdict in {"seller_delay", "logistics_delay"} and pay_verdict == "capture_mismatch":
        secondary.append("payment_mismatch")
    return issue, unique_ids(secondary, limit=10), responsible
