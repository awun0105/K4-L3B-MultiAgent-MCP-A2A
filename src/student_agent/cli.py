from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import httpx

from .a2a import A2ABus
from .cases import load_case_set
from .config import Settings
from .contracts import Contracts
from .local_tools import LOCAL_TOOL_NAMES
from .mcp_gateway import connect_gateway
from .mcp_server import LocalEvidenceGateway, create_mock_mcp_server
from .submission import package_submission, validate_artifacts
from .tooling import attach_catalog
from .trace import TraceWriter
from .workflow import solve_case


def _root(value: str) -> Path:
    return Path(value).resolve()


async def _show_tools(root: Path) -> None:
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    try:
        async with connect_gateway(
            settings.mcp_endpoint, settings.team_api_key, contracts
        ) as gateway:
            specs = await gateway.list_tool_specs()
            for spec in specs:
                print(json.dumps(spec, ensure_ascii=False, sort_keys=True))
    except Exception as exc:
        print(f"Warning: remote MCP gateway unavailable ({exc}), listing local tools:")
        mock = LocalEvidenceGateway(contracts)
        for tool in await mock.list_tools():
            print(f"{tool} (mock)")


async def _run(root: Path, resume: bool = False) -> None:
    settings = Settings.load(root)
    case_set = load_case_set(root)
    contracts = Contracts(root / "contracts" / "schemas")
    output_root = root / "outputs"
    trace_path = root / "traces" / "trace.jsonl"
    output_root.mkdir(parents=True, exist_ok=True)
    trace_path.parent.mkdir(parents=True, exist_ok=True)

    # Ensure competition run session is active
    try:
        url = f"{settings.competition_api_url}/api/v2/runs"
        headers = {"Authorization": f"Bearer {settings.team_api_key}"}
        resp = httpx.post(url, json={"variant_id": "l3b"}, headers=headers, timeout=10.0)
        if resp.status_code in (200, 201):
            expires = resp.json().get("expires_at")
            print(f"Competition run session active (expires {expires})")
    except Exception as exc:
        print(f"Warning checking run session: {exc}")

    if not resume:
        for stale in output_root.glob("*.json"):
            stale.unlink()
        trace_path.unlink(missing_ok=True)

    existing_cases = {p.stem for p in output_root.glob("*.json")}
    remaining_cases = [cid for cid in case_set.case_ids if cid not in existing_cases]

    if not remaining_cases:
        print("All cases have already been processed.")
        return

    trace = TraceWriter(trace_path, contracts)
    max_reconnects = 15
    started_cases: set[str] = set()

    for _attempt in range(max_reconnects):
        try:
            async with connect_gateway(
                settings.mcp_endpoint, settings.team_api_key, contracts
            ) as gateway:
                discovered_tools = await gateway.list_tools()
                if not discovered_tools:
                    raise RuntimeError("MCP Gateway returned no tools")
                attach_catalog(gateway, discovered_tools)

                total_cases = len(case_set.case_ids)
                while remaining_cases:
                    case_id = remaining_cases[0]
                    case = case_set.cases[case_id]
                    if case_id not in started_cases:
                        trace.emit(
                            case_id=case_id, event_type="case_received", actor="coordinator"
                        )
                        started_cases.add(case_id)
                    output = await solve_case(case, gateway, trace)
                    contracts.validate_output(output, f"outputs/{case_id}.json")
                    if output.get("case_id") != case_id:
                        raise ValueError(f"solver returned a mismatched case_id for {case_id}")
                    target = output_root / f"{case_id}.json"
                    temporary = target.with_suffix(".json.tmp")
                    temporary.write_text(
                        json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                    )
                    temporary.replace(target)
                    trace.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")
                    started_cases.discard(case_id)
                    remaining_cases.pop(0)
                    done_count = total_cases - len(remaining_cases)
                    issue = output["assessment"]["primary_issue"]
                    refs = len(output["evidence_refs"])
                    conf = output["assessment"]["confidence"]
                    summary = f"[{done_count:03d}/{total_cases}] {case_id}: {issue}"
                    print(f"{summary} (conf={conf:.2f}, ev={refs})")
            break
        except Exception as exc:
            if not remaining_cases:
                break
            print(f"Network drop ({exc}), reconnecting in 2s... ({len(remaining_cases)} remaining)")
            await asyncio.sleep(2.0)


async def _inspect_case(root: Path, case_id: str) -> None:
    case_file = root / "inputs" / f"{case_id}.json"
    if not case_file.exists():
        raise FileNotFoundError(f"Case file not found: {case_file}")

    case = json.loads(case_file.read_text(encoding="utf-8"))
    contracts = Contracts(root / "contracts" / "schemas")

    # Use existing output or solve locally using mock gateway
    output_file = root / "outputs" / f"{case_id}.json"
    if output_file.exists():
        output = json.loads(output_file.read_text(encoding="utf-8"))
        print(f"=== Inspecting pre-computed output: {case_id} ===")
    else:
        print(f"=== Executing Multi-Agent workflow on: {case_id} ===")
        mock_trace_path = root / "traces" / f"inspect_{case_id}.jsonl"
        mock_trace = TraceWriter(mock_trace_path, contracts)
        mock_gateway = LocalEvidenceGateway(contracts)
        mock_trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
        output = await solve_case(case, mock_gateway, mock_trace)  # type: ignore[arg-type]
        contracts.validate_output(output, f"inspect/{case_id}.json")
        mock_trace.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")
        mock_trace_path.unlink(missing_ok=True)

    assessment = output.get("assessment", {})
    entity_res = output.get("entity_resolution", {})
    shipment = output.get("shipment_analysis", {})
    payment = output.get("payment_analysis", {})
    fin_res = output.get("financial_resolution", {})
    root_cause = output.get("root_cause_analysis", {})

    print(f"\n[Case Overview] ID: {case_id}")
    print(f"  Primary Issue       : {assessment.get('primary_issue')}")
    print(f"  Case Status         : {assessment.get('case_status')}")
    print(f"  Confidence          : {assessment.get('confidence'):.4f}")
    orders = entity_res.get("resolved_order_ids")
    print(f"  Entity Resolution   : {entity_res.get('status')} -> {orders}")
    late_sellers = shipment.get("late_seller_ids")
    print(f"  Shipment Verdict    : {shipment.get('verdict')} (Late: {late_sellers})")
    captured = payment.get("captured_total_brl")
    print(f"  Payment Verdict     : {payment.get('verdict')} (Captured: {captured} BRL)")
    refund_val = fin_res.get("recommended_refund_brl")
    print(f"  Recommended Refund  : {refund_val} {fin_res.get('currency')}")
    print(f"  Resolution Actions  : {', '.join(output.get('resolution_actions', []))}")
    print(f"  Evidence Refs Count : {len(output.get('evidence_refs', []))}")
    print(f"  Data Conflicts Count: {len(output.get('data_conflicts', []))}")
    causes = root_cause.get("ranked_causes", [])
    if causes:
        print(f"  Root Cause Code     : {causes[0].get('cause_code')}")


async def _run_benchmark(root: Path, limit: int = 10) -> None:
    contracts = Contracts(root / "contracts" / "schemas")
    case_files = sorted((root / "inputs").glob("*.json"))[:limit]
    if not case_files:
        raise RuntimeError("No input cases found in inputs/")

    print(f"=== Multi-Agent Benchmark: Running on {len(case_files)} cases ===")
    mock_trace_path = root / "traces" / "benchmark_trace.jsonl"
    mock_trace = TraceWriter(mock_trace_path, contracts)
    mock_gateway = LocalEvidenceGateway(contracts)

    start_time = time.perf_counter()
    passed = 0
    total_refs = 0

    for file_path in case_files:
        case = json.loads(file_path.read_text(encoding="utf-8"))
        case_id = case["case_id"]
        mock_trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
        output = await solve_case(case, mock_gateway, mock_trace)  # type: ignore[arg-type]
        contracts.validate_output(output, f"benchmark/{case_id}.json")
        mock_trace.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")
        passed += 1
        total_refs += len(output.get("evidence_refs", []))

    elapsed = time.perf_counter() - start_time
    mock_trace_path.unlink(missing_ok=True)

    print("\n--- Benchmark Results ---")
    print(f"Total Cases Evaluated : {len(case_files)}")
    pct = passed / len(case_files) * 100
    print(f"Schema Validation Rate: {pct:.1f}% ({passed}/{len(case_files)})")
    print(f"Total Processing Time : {elapsed:.3f} s")
    speed = len(case_files) / elapsed
    ms_per_case = elapsed / len(case_files) * 1000
    print(f"Average Speed         : {speed:.2f} cases/sec ({ms_per_case:.1f} ms/case)")
    print(f"Avg Evidence per Case : {total_refs / len(case_files):.1f}")
    print("Status                : PASS (High-performance Multi-Agent DAG execution)")


def _print_a2a_graph() -> None:
    bus = A2ABus()
    bus.request(
        sender="coordinator",
        recipient="entity-agent",
        intent="resolve_entities",
        correlation_id="CASE_DEMO",
    )
    bus.inform(
        sender="entity-agent",
        recipient="coordinator",
        intent="entities_resolved",
        correlation_id="CASE_DEMO",
    )
    bus.request(
        sender="coordinator",
        recipient="shipment-agent",
        intent="analyze_shipment",
        correlation_id="CASE_DEMO",
    )
    bus.request(
        sender="coordinator",
        recipient="payment-agent",
        intent="analyze_payment",
        correlation_id="CASE_DEMO",
    )
    bus.propose(
        sender="shipment-agent",
        recipient="payment-agent",
        intent="negotiate_delay_compensation",
        correlation_id="CASE_DEMO",
    )
    bus.confirm(
        sender="payment-agent",
        recipient="shipment-agent",
        intent="compensation_terms_confirmed",
        correlation_id="CASE_DEMO",
    )
    bus.inform(
        sender="shipment-agent",
        recipient="coordinator",
        intent="shipment_analyzed",
        correlation_id="CASE_DEMO",
    )
    bus.inform(
        sender="payment-agent",
        recipient="coordinator",
        intent="payment_analyzed",
        correlation_id="CASE_DEMO",
    )
    bus.request(
        sender="coordinator",
        recipient="conflict-agent",
        intent="resolve_conflicts",
        correlation_id="CASE_DEMO",
    )
    bus.inform(
        sender="conflict-agent",
        recipient="coordinator",
        intent="conflicts_resolved",
        correlation_id="CASE_DEMO",
    )
    bus.request(
        sender="coordinator",
        recipient="verifier",
        intent="verify_output_invariants",
        correlation_id="CASE_DEMO",
    )
    bus.inform(
        sender="verifier",
        recipient="coordinator",
        intent="verification_completed",
        correlation_id="CASE_DEMO",
    )

    print(bus.to_mermaid())


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Day09 L3B Multi-Agent MCP + A2A System")
    result.add_argument("--root", default=".", help="repository root (default: current directory)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-inputs", help="validate case-set.json and all 100 inputs")
    commands.add_parser("mcp-tools", help="authenticate and list discovered MCP tools")
    commands.add_parser("local-tools", help="list student-authored local analysis tools")
    run_parser = commands.add_parser("run", help="run the implemented workflow for all cases")
    run_parser.add_argument("--resume", action="store_true", help="resume from remaining cases")
    commands.add_parser("validate", help="validate outputs and observable trace")
    package = commands.add_parser("package", help="validate and build the submission ZIP")
    package.add_argument("--output", default="dist/submission.zip")

    # Enhanced subcommands
    inspect = commands.add_parser("inspect", help="inspect multi-agent reasoning for a case")
    inspect.add_argument("case_id", help="case identifier, e.g. L3B_CASE_001")

    bench = commands.add_parser("benchmark", help="run high-speed offline multi-agent benchmark")
    bench.add_argument(
        "--limit", type=int, default=10, help="number of cases to benchmark (default: 10)"
    )

    commands.add_parser(
        "a2a-graph", help="export Mermaid sequence diagram of multi-agent interactions"
    )
    dev_server = commands.add_parser("dev-server", help="start standalone mock MCP server")
    dev_server.add_argument("--port", type=int, default=8001, help="port to bind (default: 8001)")

    return result


def main() -> None:
    args = parser().parse_args()
    root = _root(args.root)
    try:
        if args.command == "validate-inputs":
            case_set = load_case_set(root)
            print(
                f"OK: {case_set.variant_id} / {case_set.version} / {len(case_set.case_ids)} cases"
            )
        elif args.command == "mcp-tools":
            asyncio.run(_show_tools(root))
        elif args.command == "local-tools":
            for name in LOCAL_TOOL_NAMES:
                print(name)
        elif args.command == "run":
            asyncio.run(_run(root, resume=getattr(args, "resume", False)))
        elif args.command == "validate":
            case_set = load_case_set(root)
            contracts = Contracts(root / "contracts" / "schemas")
            _, trace = validate_artifacts(root, case_set, contracts)
            print(f"OK: {len(case_set.case_ids)} outputs / {len(trace)} trace events")
        elif args.command == "package":
            destination = package_submission(root, root / args.output)
            print(f"OK: {destination}")
        elif args.command == "inspect":
            asyncio.run(_inspect_case(root, args.case_id))
        elif args.command == "benchmark":
            asyncio.run(_run_benchmark(root, args.limit))
        elif args.command == "a2a-graph":
            _print_a2a_graph()
        elif args.command == "dev-server":
            server = create_mock_mcp_server()
            print(f"Mock MCP Server initialized: {server.name}")
            print("Tools available: get_order, get_order_items, get_customer_history,")
            print("                 get_shipment, get_payment, get_refund, get_policy,")
            print("                 search_orders")
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
