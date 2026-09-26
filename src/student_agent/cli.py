from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .cases import load_case_set
from .config import Settings
from .contracts import Contracts
from .mcp_gateway import connect_gateway
from .submission import package_submission, validate_artifacts
from .trace import TraceWriter
from .workflow import solve_case


def _root(value: str) -> Path:
    return Path(value).resolve()


async def _show_tools(root: Path) -> None:
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        for tool in await gateway.list_tools():
            print(tool)


async def _run(root: Path, limit: int | None = None, clean: bool = False) -> None:
    settings = Settings.load(root)
    case_set = load_case_set(root)
    contracts = Contracts(root / "contracts" / "schemas")
    output_root = root / "outputs"
    trace_path = root / "traces" / "trace.jsonl"
    output_root.mkdir(parents=True, exist_ok=True)
    trace_path.parent.mkdir(parents=True, exist_ok=True)

    if clean:
        for stale in output_root.glob("*.json"):
            stale.unlink()
        trace_path.unlink(missing_ok=True)

    trace = TraceWriter(trace_path, contracts)
    selected_ids = case_set.case_ids[:limit] if limit else case_set.case_ids
    total = len(selected_ids)

    # Check which cases are already successfully processed
    completed_ids = set()
    for p in output_root.glob("*.json"):
        try:
            val = json.loads(p.read_text(encoding="utf-8"))
            contracts.validate_output(val, p.name)
            completed_ids.add(p.stem)
        except Exception:
            p.unlink(missing_ok=True)

    remaining_ids = [cid for cid in selected_ids if cid not in completed_ids]
    if not remaining_ids:
        print(f"All {total} cases already completed and verified!", flush=True)
        return

    processed_count = len(completed_ids)
    while remaining_ids:
        case_id = remaining_ids[0]
        processed_count += 1
        print(f"[{processed_count}/{total}] Processing {case_id}...", flush=True)
        case = case_set.cases[case_id]

        max_attempts = 4
        solved = False
        last_error = None

        for attempt in range(1, max_attempts + 1):
            try:
                async with connect_gateway(
                    settings.mcp_endpoint, settings.team_api_key, contracts
                ) as gateway:
                    trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
                    output = await solve_case(case, gateway, trace)
                    contracts.validate_output(output, f"outputs/{case_id}.json")
                    if output.get("case_id") != case_id:
                        raise ValueError(f"solver returned mismatched case_id for {case_id}")
                    target = output_root / f"{case_id}.json"
                    temporary = target.with_suffix(".json.tmp")
                    temporary.write_text(
                        json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                    )
                    temporary.replace(target)
                    trace.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")
                    print(
                        f"[{processed_count}/{total}] Completed {case_id} -> "
                        f"{output['assessment']['primary_issue']} "
                        f"(status={output['assessment']['case_status']})",
                        flush=True,
                    )
                    solved = True
                    remaining_ids.pop(0)
                    break
            except Exception as exc:
                last_error = exc
                print(
                    f"Warning: attempt {attempt}/{max_attempts} for {case_id} failed: {exc}",
                    flush=True,
                )
                await asyncio.sleep(2 * attempt)

        if not solved:
            raise RuntimeError(f"Case {case_id} failed after {max_attempts} attempts: {last_error}")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Day09 L3B student workflow")
    result.add_argument("--root", default=".", help="repository root (default: current directory)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-inputs", help="validate case-set.json and all 100 inputs")
    commands.add_parser("mcp-tools", help="authenticate and list discovered MCP tools")
    run_cmd = commands.add_parser("run", help="run the implemented workflow for all cases")
    run_cmd.add_argument("--limit", type=int, default=None, help="limit number of cases to run")
    run_cmd.add_argument(
        "--clean", action="store_true", help="delete all existing outputs before running"
    )
    commands.add_parser("validate", help="validate outputs and observable trace")
    package = commands.add_parser("package", help="validate and build the submission ZIP")
    package.add_argument("--output", default="dist/submission.zip")
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
        elif args.command == "run":
            asyncio.run(_run(root, args.limit, args.clean))
        elif args.command == "validate":
            case_set = load_case_set(root)
            contracts = Contracts(root / "contracts" / "schemas")
            _, trace = validate_artifacts(root, case_set, contracts)
            print(f"OK: {len(case_set.case_ids)} outputs / {len(trace)} trace events")
        elif args.command == "package":
            destination = package_submission(root, root / args.output)
            print(f"OK: {destination}")
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
