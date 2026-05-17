"""mcp-ax trace <target> --tier=smoke|full --modes=direct[,…] (R7,R8,R12,R13,R26,R27).

Produces:
  traces/<scenario>.<mode>.jsonl  — replayed cassettes, byte-identical to source
  metrics.json                    — deterministic per-scenario aggregates (R7)
  runs/<run_id>/run.json          — reproducibility metadata (R13)

Default behaviour is **replay**: cassettes already exist under
`fixtures/cassettes/<scenario>.<mode>.jsonl`. Pass `--record` to capture a fresh
cassette (live mode — see harness.runtime.cassette for details on what's
wired). Pass `--no-cassette` to force live without overwriting cassettes.

R12 budget cap: the run estimates total cost from cassettes (or, in live mode,
from the tier estimator) and exits **non-zero before completing** when the
estimate exceeds `--max-usd`. Premortem mitigation: this is a hard precondition,
not a circuit-breaker.

R8 enforcement: every `agent_message` payload is scanned for forbidden
self-score keys via harness.assertions.no_self_score. The first hit aborts
the run with a SelfScoreEmitted error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

import yaml

from ._paths import find_repo_root
from extract.metrics import extract_metrics, load_jsonl, render_metrics_json
from harness.assertions.no_self_score import assert_no_self_score, SelfScoreEmitted
from harness.runtime import live_factory
from harness.runtime.budget import BudgetExceededError, assert_within_cap
from harness.runtime.capability_profile import (
    DEFAULT_JUDGE,
    DEFAULT_ORCHESTRATOR,
    DEFAULT_TEST_AGENT,
)
from harness.runtime.baseline import baseline_exists
from harness.runtime.cassette import (
    CassetteNotFoundError,
    CassetteTooLargeError,
    LiveRecordingNotConfiguredError,
    estimated_cost,
    load_cassette,
    replay_to,
    serialise_records,
    write_cassette,
)
from harness.runtime.live_agent import (
    LiveAgentConfig,
    LiveRecordingConfigError,
    LiveRecordingFailure,
    Scenario,
    record_scenario_live,
)
from harness.runtime.live_factory import LiveCredentialsMissing
from harness.runtime.mcp_session import MCPTransportError
from harness.runtime.run_writer import (
    McpServerInfo,
    RunMetadata,
    hash_bytes,
    hash_path_text,
    harness_git_sha,
    make_run_id,
    utc_now_iso,
    write_run_json,
)


CASSETTES_DIR_NAME = "fixtures/cassettes"
SCENARIOS_DIR_NAME = "harness/scenarios"
PROMPT_DIR_NAME = "prompts"
DEFAULT_SYSTEM_PROMPT_NAME = "test-agent"
DEFAULT_MAX_USD = 0.20  # R12 smoke-tier ceiling


def _load_tier_scenarios(repo_root: Path, tier: str) -> tuple[str, ...]:
    tiers_path = repo_root / SCENARIOS_DIR_NAME / "_tiers.yaml"
    with tiers_path.open("r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    tiers = doc.get("tiers") or {}
    if tier not in tiers:
        raise ValueError(
            f"tier {tier!r} not in {tiers_path.relative_to(repo_root)}; "
            f"valid: {sorted(tiers)}"
        )
    return tuple(tiers[tier])


def _parse_modes(spec: str) -> tuple[str, ...]:
    modes = tuple(m.strip() for m in spec.split(",") if m.strip())
    valid = {"direct", "fuzzy", "distractor", "noise"}
    bad = [m for m in modes if m not in valid]
    if bad:
        raise ValueError(f"invalid mode(s) {bad}; valid: {sorted(valid)}")
    return modes


def _scan_self_score(records: list[dict], source_label: str) -> None:
    for rec in records:
        if rec.get("kind") != "agent_message":
            continue
        payload = rec.get("payload") or {}
        content = payload.get("content")
        if not isinstance(content, str):
            continue
        # Use a per-record source label so the violation message points at the
        # exact trace line.
        assert_no_self_score(content, source=source_label)


def _collect_prompt_template_shas(repo_root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    prompts_dir = repo_root / PROMPT_DIR_NAME
    if not prompts_dir.is_dir():
        return out
    for path in sorted(prompts_dir.glob("*.md")):
        out[path.stem] = hash_path_text(path)
    return out


def _tool_manifest_hash(target: Path) -> str:
    if target.is_file():
        return hash_path_text(target)
    raise FileNotFoundError(f"target manifest not found: {target}")


def _load_scenario_doc(repo_root: Path, scenario_id: str) -> dict:
    path = repo_root / SCENARIOS_DIR_NAME / f"{scenario_id}.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"scenario file not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    if not isinstance(doc, dict):
        raise ValueError(f"scenario {scenario_id!r}: root must be a mapping")
    return doc


def _load_system_prompt(repo_root: Path, name: str = DEFAULT_SYSTEM_PROMPT_NAME) -> str:
    path = repo_root / PROMPT_DIR_NAME / f"{name}.md"
    if not path.is_file():
        raise FileNotFoundError(f"system prompt not found: {path}")
    return path.read_text(encoding="utf-8")


def _run_replay(
    *,
    repo_root: Path,
    target: Path,
    tier: str,
    modes: tuple[str, ...],
    max_usd: float,
    out_root: Path,
    rng_seed: int,
    mcp_server: McpServerInfo,
) -> int:
    cassettes_dir = repo_root / CASSETTES_DIR_NAME
    scenarios = _load_tier_scenarios(repo_root, tier)

    cassettes = []
    for scenario in scenarios:
        for mode in modes:
            try:
                cas = load_cassette(cassettes_dir, scenario, mode)
            except CassetteNotFoundError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
            except CassetteTooLargeError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
            cassettes.append(cas)

    # R12 — estimate cost and assert before doing any work.
    estimate = sum(estimated_cost(c) for c in cassettes)
    try:
        assert_within_cap(estimate, max_usd)
    except BudgetExceededError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3

    # Replay each cassette into traces/.
    started_at = utc_now_iso()
    start_perf = time.perf_counter()
    run_id = make_run_id(started_at)
    run_dir = out_root / "runs" / run_id
    traces_dir = out_root / "traces"
    traces_dir.mkdir(parents=True, exist_ok=True)

    all_records: list[dict] = []
    for cas in cassettes:
        replay_to(cas, traces_dir)
        records = load_jsonl(cas.path)
        # R8 — runtime self-score check.
        try:
            _scan_self_score(
                records, source_label=f"{cas.scenario_id}.{cas.mode}.jsonl",
            )
        except SelfScoreEmitted as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 4
        all_records.extend(records)

    # R7 — extract metrics deterministically.
    metrics_doc = extract_metrics(all_records)
    metrics_path = out_root / "metrics.json"
    metrics_path.write_text(render_metrics_json(metrics_doc), encoding="utf-8")

    completed_at = utc_now_iso()
    elapsed_ms = int((time.perf_counter() - start_perf) * 1000)

    # R13 — write run.json.
    metadata = RunMetadata(
        run_id=run_id,
        started_at=started_at,
        completed_at=completed_at,
        harness_git_sha=harness_git_sha(repo_root),
        tool_manifest_hash=_tool_manifest_hash(target),
        mcp_server=mcp_server,
        tested_repo_commit_sha=harness_git_sha(repo_root),  # placeholder when target is local
        rng_seed=rng_seed,
        total_cost_usd=round(estimate, 6),
        budget_usd_cap=max_usd,
        budget_usd_used=round(estimate, 6),
        replay=True,
        orchestrator_capability_profile=DEFAULT_ORCHESTRATOR,
        test_capability_profile=DEFAULT_TEST_AGENT,
        judge_capability_profile=DEFAULT_JUDGE,
        prompt_template_shas=_collect_prompt_template_shas(repo_root),
        baseline_missing=not baseline_exists(target),
    )
    write_run_json(run_dir, metadata)

    print(
        f"trace ok — run_id={run_id} cassettes={len(cassettes)} "
        f"estimated=${estimate:.4f} elapsed_ms={elapsed_ms} traces={traces_dir} "
        f"metrics={metrics_path} run={run_dir / 'run.json'}",
        file=sys.stderr,
    )
    return 0


def _run_live(
    *,
    repo_root: Path,
    target: Path,
    tier: str,
    modes: tuple[str, ...],
    max_usd: float,
    out_root: Path,
    rng_seed: int,
    mcp_server: McpServerInfo,
    write_cassettes: bool,
) -> int:
    cassettes_dir = repo_root / CASSETTES_DIR_NAME
    try:
        llm = live_factory.build_llm_client()
        mcp = live_factory.build_mcp_session()
    except (LiveCredentialsMissing, MCPTransportError, LiveRecordingConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 5

    system_prompt = _load_system_prompt(repo_root)
    scenarios = _load_tier_scenarios(repo_root, tier)
    config = LiveAgentConfig(system_prompt=system_prompt)

    started_at = utc_now_iso()
    start_perf = time.perf_counter()
    run_id = make_run_id(started_at)
    run_dir = out_root / "runs" / run_id
    traces_dir = out_root / "traces"
    traces_dir.mkdir(parents=True, exist_ok=True)

    all_records: list[dict] = []
    total_cost = 0.0
    try:
        for scenario_id in scenarios:
            try:
                doc = _load_scenario_doc(repo_root, scenario_id)
            except (FileNotFoundError, ValueError) as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
            prompt = doc.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip():
                print(
                    f"error: scenario {scenario_id!r}: missing `prompt:` string field",
                    file=sys.stderr,
                )
                return 2
            for mode in modes:
                scenario = Scenario(id=scenario_id, mode=mode, prompt=prompt)
                try:
                    result = record_scenario_live(scenario, llm=llm, mcp=mcp, config=config)
                except LiveRecordingFailure as exc:
                    print(f"error: {exc}", file=sys.stderr)
                    return 6
                total_cost += result.cost_usd
                # R12 — fail loud the moment we cross the cap; do not write partials.
                try:
                    assert_within_cap(total_cost, max_usd)
                except BudgetExceededError as exc:
                    print(f"error: {exc}", file=sys.stderr)
                    return 3
                records = list(result.records)
                # R8 — runtime self-score check on every recorded agent_message.
                try:
                    _scan_self_score(records, source_label=f"{scenario_id}.{mode}.jsonl")
                except SelfScoreEmitted as exc:
                    print(f"error: {exc}", file=sys.stderr)
                    return 4
                trace_path = traces_dir / f"{scenario_id}.{mode}.jsonl"
                trace_path.write_bytes(serialise_records(records))
                if write_cassettes:
                    try:
                        write_cassette(cassettes_dir, scenario_id, mode, records)
                    except CassetteTooLargeError as exc:
                        print(f"error: {exc}", file=sys.stderr)
                        return 2
                all_records.extend(records)
    finally:
        close = getattr(mcp, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    metrics_doc = extract_metrics(all_records)
    metrics_path = out_root / "metrics.json"
    metrics_path.write_text(render_metrics_json(metrics_doc), encoding="utf-8")

    completed_at = utc_now_iso()
    elapsed_ms = int((time.perf_counter() - start_perf) * 1000)

    metadata = RunMetadata(
        run_id=run_id,
        started_at=started_at,
        completed_at=completed_at,
        harness_git_sha=harness_git_sha(repo_root),
        tool_manifest_hash=_tool_manifest_hash(target),
        mcp_server=mcp_server,
        tested_repo_commit_sha=harness_git_sha(repo_root),
        rng_seed=rng_seed,
        total_cost_usd=round(total_cost, 6),
        budget_usd_cap=max_usd,
        budget_usd_used=round(total_cost, 6),
        replay=False,
        orchestrator_capability_profile=DEFAULT_ORCHESTRATOR,
        test_capability_profile=DEFAULT_TEST_AGENT,
        judge_capability_profile=DEFAULT_JUDGE,
        prompt_template_shas=_collect_prompt_template_shas(repo_root),
        baseline_missing=not baseline_exists(target),
    )
    write_run_json(run_dir, metadata)

    print(
        f"trace ok (live) — run_id={run_id} scenarios={len(scenarios)} "
        f"modes={len(modes)} cost=${total_cost:.4f} elapsed_ms={elapsed_ms} "
        f"traces={traces_dir} metrics={metrics_path} run={run_dir / 'run.json'} "
        f"cassettes_written={write_cassettes}",
        file=sys.stderr,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mcp-ax trace",
        description="Run the trace stage (replay-by-default cassettes + metrics + run.json).",
    )
    parser.add_argument("target", help="Path to MCP tool manifest JSON file")
    parser.add_argument(
        "--tier", choices=("smoke", "full"), default="smoke",
        help="Scenario tier to run (default: smoke).",
    )
    parser.add_argument(
        "--modes", default="direct",
        help="Comma-separated R26 modes (default: direct).",
    )
    parser.add_argument(
        "--max-usd", type=float, default=DEFAULT_MAX_USD,
        help="R12 hard budget cap. Run exits non-zero before completing if estimate > cap.",
    )
    parser.add_argument(
        "--rng-seed", type=int, default=42,
        help="Deterministic RNG seed (recorded in run.json).",
    )
    parser.add_argument(
        "--out", default="runs",
        help="Output root directory (default: runs/).",
    )
    parser.add_argument(
        "--record", action="store_true",
        help="Capture cassettes from a live run instead of replaying.",
    )
    parser.add_argument(
        "--no-cassette", action="store_true",
        help="Force live without writing cassettes (smoke a live server).",
    )
    parser.add_argument(
        "--mcp-endpoint", default="https://mcp.example.test/v1",
        help="Recorded MCP server endpoint (run.json field).",
    )
    parser.add_argument(
        "--mcp-version", default="0.0.0",
        help="Recorded MCP server version (run.json field).",
    )
    parser.add_argument(
        "--mcp-build-sha", default="0000000",
        help="Recorded MCP server build SHA (run.json field).",
    )
    args = parser.parse_args(argv)

    try:
        repo_root = find_repo_root()
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    target = Path(args.target)
    try:
        modes = _parse_modes(args.modes)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.record and args.no_cassette:
        print(
            "error: --record and --no-cassette are mutually exclusive; "
            "--record captures cassettes, --no-cassette skips capture.",
            file=sys.stderr,
        )
        return 2

    out_root = Path(args.out)
    if not out_root.is_absolute():
        out_root = (Path.cwd() / out_root).resolve()
    mcp_server = McpServerInfo(
        endpoint=args.mcp_endpoint,
        version=args.mcp_version,
        build_sha=args.mcp_build_sha,
    )

    if args.record or args.no_cassette:
        return _run_live(
            repo_root=repo_root,
            target=target,
            tier=args.tier,
            modes=modes,
            max_usd=args.max_usd,
            out_root=out_root,
            rng_seed=args.rng_seed,
            mcp_server=mcp_server,
            write_cassettes=args.record,
        )

    return _run_replay(
        repo_root=repo_root,
        target=target,
        tier=args.tier,
        modes=modes,
        max_usd=args.max_usd,
        out_root=out_root,
        rng_seed=args.rng_seed,
        mcp_server=mcp_server,
    )


if __name__ == "__main__":
    raise SystemExit(main())
