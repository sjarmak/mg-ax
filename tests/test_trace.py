"""Tests for `mcp-ax trace` (bead mcp-ax-ph3.9 — R7,R8,R12,R13,R26,R27).

Acceptance mapping:

  AC1 — `mcp-ax trace --tier=smoke --modes=direct <target>` produces
        traces/*.jsonl + metrics.json + runs/<id>/run.json
  AC2 — `--record` captures cassettes; default replay-when-cassette-exists;
        `--no-cassette` forces live (R27)
  AC3 — Smoke-tier replay completes in ≤ 60 s wallclock and ≤ $0.20
  AC4 — `--tier=full --max-usd=0.01` exits non-zero **before** completing
  AC5 — metrics.json records all R7 metrics
  AC6 — Re-running extract on unchanged traces produces byte-equal metrics.json
  AC7 — Every trace record has a `mode` field
  AC8 — Runtime asserts no agent self-score YAML block
  AC9 — runs/<id>/run.json includes capability-profile (NOT model_id), MCP
        server build SHA, replay flag
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from jsonschema import Draft7Validator

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMAS_DIR = REPO_ROOT / "harness" / "schemas"
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"


def _run_trace(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    # Strip live-mode credentials so the replay/error tests are deterministic
    # regardless of the developer's shell environment.
    for k in ("ANTHROPIC_API_KEY", "MCP_SERVER_ENDPOINT", "MCP_AX_LIVE_MODEL"):
        env.pop(k, None)
    return subprocess.run(
        [sys.executable, "-m", "cli", "trace", *args],
        cwd=str(cwd or REPO_ROOT),
        capture_output=True,
        text=True,
        env=env,
    )


def _validator(name: str) -> Draft7Validator:
    schema = json.loads((SCHEMAS_DIR / name).read_text(encoding="utf-8"))
    return Draft7Validator(schema)


def _trace_validator() -> Draft7Validator:
    return _validator("trace.jsonl.schema.json")


def _metrics_validator() -> Draft7Validator:
    return _validator("metrics.json")


def _run_validator() -> Draft7Validator:
    return _validator("run.json")


class TestTraceSmokeProducesArtifacts(unittest.TestCase):
    """AC1 — smoke run produces traces/, metrics.json, runs/<id>/run.json."""

    def test_smoke_run_emits_all_three_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            target = FIXTURES_DIR / "manifest.clean.json"
            t0 = time.perf_counter()
            r = _run_trace(
                str(target), "--tier", "smoke", "--modes", "direct",
                "--out", str(out),
            )
            elapsed = time.perf_counter() - t0
            self.assertEqual(r.returncode, 0, r.stderr)

            traces = sorted((out / "traces").glob("*.jsonl"))
            self.assertGreaterEqual(len(traces), 2)
            self.assertTrue((out / "metrics.json").is_file())
            run_dirs = list((out / "runs").iterdir())
            self.assertEqual(len(run_dirs), 1)
            self.assertTrue((run_dirs[0] / "run.json").is_file())

            # AC3 — smoke replay must complete fast.
            self.assertLess(elapsed, 60.0)


class TestTracesValidateSchema(unittest.TestCase):
    """AC7 — every trace record has a mode field; trace records validate."""

    def test_replayed_trace_records_validate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            target = FIXTURES_DIR / "manifest.clean.json"
            r = _run_trace(
                str(target), "--tier", "smoke", "--out", str(out),
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            validator = _trace_validator()
            for trace in (out / "traces").glob("*.jsonl"):
                for line in trace.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    rec = json.loads(line)
                    errors = list(validator.iter_errors(rec))
                    self.assertEqual(errors, [], f"{trace.name}: {errors}")
                    self.assertIn("mode", rec)


class TestMetricsValidateAndComplete(unittest.TestCase):
    """AC5 — metrics.json records all R7 metrics and validates the schema."""

    def test_metrics_validates_and_includes_every_r7_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            r = _run_trace(
                str(FIXTURES_DIR / "manifest.clean.json"),
                "--tier", "full", "--out", str(out),
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            doc = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
            errors = list(_metrics_validator().iter_errors(doc))
            self.assertEqual(errors, [], errors)
            required = {
                "scenario_id", "mode",
                "first_call_schema_valid", "tool_call_count", "retry_count",
                "fallback_tool_count", "polling_retry_count", "wall_clock_ms",
                "tokens_in", "tokens_out", "cost_usd", "empty_result_handled",
                "anti_pattern_redirects",
            }
            for sc in doc["scenarios"]:
                missing = required - set(sc.keys())
                self.assertEqual(missing, set(), f"missing R7 fields: {missing}")


class TestMetricsByteEqualOnRerun(unittest.TestCase):
    """AC6 — extract produces byte-equal metrics.json on rerun."""

    def test_two_runs_emit_byte_equal_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_a = Path(tmp) / "a"
            out_b = Path(tmp) / "b"
            target = FIXTURES_DIR / "manifest.clean.json"
            r1 = _run_trace(str(target), "--tier", "full", "--out", str(out_a))
            r2 = _run_trace(str(target), "--tier", "full", "--out", str(out_b))
            self.assertEqual(r1.returncode, 0, r1.stderr)
            self.assertEqual(r2.returncode, 0, r2.stderr)
            self.assertEqual(
                (out_a / "metrics.json").read_bytes(),
                (out_b / "metrics.json").read_bytes(),
            )


class TestBudgetCapHardExits(unittest.TestCase):
    """AC4 — --max-usd below estimate exits non-zero before completing."""

    def test_max_usd_below_estimate_blocks_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            r = _run_trace(
                str(FIXTURES_DIR / "manifest.clean.json"),
                "--tier", "full",
                "--max-usd", "0.0001",
                "--out", str(out),
            )
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("max-usd", r.stderr)
            # Crucially: NOT a partial report. nothing should be written.
            self.assertFalse((out / "metrics.json").exists())
            self.assertFalse((out / "traces").exists())

    def test_max_usd_above_estimate_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            r = _run_trace(
                str(FIXTURES_DIR / "manifest.clean.json"),
                "--tier", "smoke",
                "--max-usd", "10.0",
                "--out", str(out),
            )
            self.assertEqual(r.returncode, 0, r.stderr)


class TestRunJsonReproducibility(unittest.TestCase):
    """AC9 — run.json validates and uses capability_profile, not model_id."""

    def test_run_json_validates_and_has_capability_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            r = _run_trace(
                str(FIXTURES_DIR / "manifest.clean.json"),
                "--tier", "smoke", "--out", str(out),
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            run_dirs = list((out / "runs").iterdir())
            run_doc = json.loads((run_dirs[0] / "run.json").read_text(encoding="utf-8"))
            errors = list(_run_validator().iter_errors(run_doc))
            self.assertEqual(errors, [], errors)
            self.assertIn("orchestrator_capability_profile", run_doc)
            self.assertIn("test_capability_profile", run_doc)
            self.assertIn("judge_capability_profile", run_doc)
            self.assertNotIn("model_id", run_doc)
            self.assertTrue(run_doc["replay"])
            self.assertIn("build_sha", run_doc["mcp_server"])


class TestRecordModeNotConfigured(unittest.TestCase):
    """AC2 — `--record` is wired but errors cleanly until live mode is plugged in."""

    def test_record_flag_errors_with_clear_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            r = _run_trace(
                str(FIXTURES_DIR / "manifest.clean.json"),
                "--record", "--tier", "smoke", "--out", str(out),
            )
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("live recording", r.stderr.lower())

    def test_no_cassette_flag_errors_with_clear_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            r = _run_trace(
                str(FIXTURES_DIR / "manifest.clean.json"),
                "--no-cassette", "--tier", "smoke", "--out", str(out),
            )
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("live recording", r.stderr.lower())


class TestDefaultIsReplay(unittest.TestCase):
    """AC2 — default behaviour is replay when cassette exists."""

    def test_default_invocation_replays_existing_cassettes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            r = _run_trace(
                str(FIXTURES_DIR / "manifest.clean.json"),
                "--tier", "smoke", "--out", str(out),
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            # Replay produces byte-equal cassette content in traces/.
            cassette = (REPO_ROOT / "fixtures" / "cassettes" / "sc01.direct.jsonl").read_bytes()
            replayed = (out / "traces" / "sc01.direct.jsonl").read_bytes()
            self.assertEqual(replayed, cassette)


class TestRuntimeSelfScoreAssertion(unittest.TestCase):
    """AC8 — runtime aborts when a cassette contains a self-score block."""

    def test_self_score_in_cassette_fails_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp) / "repo"
            shutil.copytree(REPO_ROOT, tmp_root, ignore=shutil.ignore_patterns(
                "__pycache__", ".pytest_cache", ".beads", ".git", "runs", "*.pyc",
            ))
            cassette = tmp_root / "fixtures" / "cassettes" / "sc01.direct.jsonl"
            lines = cassette.read_text(encoding="utf-8").splitlines()
            # Inject a self-score YAML block into the agent_message content.
            for i, line in enumerate(lines):
                rec = json.loads(line)
                if rec.get("kind") == "agent_message" and rec.get("payload", {}).get("role") == "assistant":
                    rec["payload"]["content"] = (
                        rec["payload"]["content"]
                        + "\n```yaml\nscore: 5\ndimension: comprehension\n```"
                    )
                    lines[i] = json.dumps(rec)
                    break
            cassette.write_text("\n".join(lines) + "\n", encoding="utf-8")

            out = Path(tmp) / "out"
            env = os.environ.copy()
            env["PYTHONPATH"] = str(tmp_root) + os.pathsep + env.get("PYTHONPATH", "")
            r = subprocess.run(
                [sys.executable, "-m", "cli", "trace",
                 str(tmp_root / "tests" / "fixtures" / "manifest.clean.json"),
                 "--tier", "smoke", "--out", str(out)],
                cwd=str(tmp_root),
                capture_output=True, text=True, env=env,
            )
            self.assertNotEqual(r.returncode, 0, r.stdout)
            self.assertIn("R8 violation", r.stderr)


class TestModesParsing(unittest.TestCase):
    def test_unknown_mode_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            r = _run_trace(
                str(FIXTURES_DIR / "manifest.clean.json"),
                "--modes", "weird", "--out", str(out),
            )
            self.assertEqual(r.returncode, 2)
            self.assertIn("invalid mode", r.stderr)


if __name__ == "__main__":
    unittest.main()
