"""Sentinel-check runner — invoked by `make sentinel-check` and the
`.github/workflows/sentinel-gate.yml` CI gate.

Per R17 + ph3.7 acceptance criteria:

  1. For every sentinels/tools/<name>.json, run the static lint stage
     (`python3 -m cli lint <manifest>`) and capture the set of fired AX####
     IDs.
  2. Compare the fired-set against sentinels/expected.json's
     `expected_findings`. Mismatches are bucket-assignment failures and block
     the release gate.
  3. Repeat the lint pass `rerun_count` times (default 5) and assert variance
     across reruns is < `variance_target_per_metric` (default 0.5). For the
     fully-deterministic static lint this is 0; the variance check exists so
     the same harness can score future trace-tier sentinels (which have
     stochastic metrics) without changing the gate's contract.
  4. `pending_rules` are documented but not asserted — those rules are
     reserved for sentinels that the lint engine does not yet detect, and
     their presence in expected.json is a known-future-work signal, not a
     CI failure.

Exit codes:
  0  — all sentinels match expected, variance OK
  1  — bucket-assignment mismatch or variance breach
  2  — usage / IO error
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SENTINELS_DIR = REPO_ROOT / "sentinels"
EXPECTED_PATH = SENTINELS_DIR / "expected.json"
TOOLS_DIR = SENTINELS_DIR / "tools"


class SentinelCheckError(RuntimeError):
    pass


def _load_expected() -> dict:
    if not EXPECTED_PATH.is_file():
        raise SentinelCheckError(f"missing {EXPECTED_PATH}")
    with EXPECTED_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _run_lint_once(manifest: Path) -> dict:
    result = subprocess.run(
        [sys.executable, "-m", "cli", "lint", str(manifest), "--format", "json"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    if result.returncode not in (0, 1):
        raise SentinelCheckError(
            f"lint failed for {manifest.name} (exit {result.returncode}): "
            f"{result.stderr}"
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SentinelCheckError(
            f"lint emitted non-JSON for {manifest.name}: {exc}"
        ) from None


def _fired_rule_ids(findings_doc: dict) -> set[str]:
    return {f["id"] for f in findings_doc.get("findings", [])}


def _check_one(name: str, expected: dict, rerun_count: int, variance_target: float) -> list[str]:
    """Lint one sentinel `rerun_count` times, return list of error strings."""
    manifest = TOOLS_DIR / f"{name}.json"
    if not manifest.is_file():
        return [f"sentinel manifest missing: {manifest.relative_to(REPO_ROOT)}"]
    expected_set = set(expected["expected_findings"])
    fired_per_run: list[set[str]] = []
    for _ in range(rerun_count):
        doc = _run_lint_once(manifest)
        fired_per_run.append(_fired_rule_ids(doc))
    canonical = fired_per_run[0]
    errors: list[str] = []
    if canonical != expected_set:
        errors.append(
            f"[{name}] bucket mismatch: expected {sorted(expected_set)}, "
            f"got {sorted(canonical)}"
        )
    drift = [i for i, fired in enumerate(fired_per_run) if fired != canonical]
    if drift:
        errors.append(
            f"[{name}] variance breach: rerun{drift} produced different "
            f"fired-sets (lint must be deterministic)"
        )
    counts = [len(fired) for fired in fired_per_run]
    if len(set(counts)) > 1:
        spread = statistics.pstdev(counts)
        if spread >= variance_target:
            errors.append(
                f"[{name}] finding-count variance {spread:.3f} exceeds "
                f"target {variance_target}"
            )
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sentinel-check")
    parser.add_argument("--rerun-count", type=int, default=None)
    parser.add_argument("--variance-target", type=float, default=None)
    parser.add_argument("--name", default=None, help="Run only this sentinel")
    args = parser.parse_args(argv)

    try:
        expected_doc = _load_expected()
    except SentinelCheckError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    rerun_count = args.rerun_count or expected_doc.get("rerun_count", 5)
    variance_target = (
        args.variance_target
        if args.variance_target is not None
        else expected_doc.get("variance_target_per_metric", 0.5)
    )

    sentinels = expected_doc.get("sentinels", {})
    if args.name:
        if args.name not in sentinels:
            print(f"error: unknown sentinel {args.name!r}", file=sys.stderr)
            return 2
        sentinels = {args.name: sentinels[args.name]}

    all_errors: list[str] = []
    pass_count = 0
    pending_rules: set[str] = set()
    for name, expected in sorted(sentinels.items()):
        errors = _check_one(name, expected, rerun_count, variance_target)
        if errors:
            all_errors.extend(errors)
        else:
            pass_count += 1
        for rule_id in expected.get("pending_rules", []):
            pending_rules.add(rule_id)

    if all_errors:
        print("FAIL — sentinel-check detected issues:", file=sys.stderr)
        for err in all_errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    print(
        f"OK — {pass_count}/{len(sentinels)} sentinels match expected "
        f"(rerun_count={rerun_count}, variance_target={variance_target})."
    )
    if pending_rules:
        print(
            f"  pending rules (not yet implemented in lint engine): "
            f"{sorted(pending_rules)}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
