"""Tests for `mcp-ax trace --record` live recording (bead mcp-ax-ph3.14, R27).

Acceptance mapping:

  AC1 — `mcp-ax trace --record` connects to MCP server + LLM client and writes
        a JSONL cassette for each scenario.
  AC2 — `--no-cassette` runs live without writing cassettes.
  AC3 — ANTHROPIC_API_KEY + MCP_SERVER_ENDPOINT env vars are required.

Unit-level coverage uses fakes injected into `record_scenario_live`; CLI-level
coverage monkeypatches `live_factory.build_*` so the trace.main() entrypoint is
exercised without real network or API calls.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from itertools import count
from pathlib import Path
from typing import Iterator, Sequence
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"

sys.path.insert(0, str(REPO_ROOT))

from harness.runtime.cassette import (
    CassetteTooLargeError,
    cassette_path,
    load_cassette,
    serialise_records,
    write_cassette,
)
from harness.runtime.live_agent import (
    LiveAgentConfig,
    LiveRecordingFailure,
    LLMTurn,
    LLMUsage,
    Scenario,
    ToolCall,
    ToolResult,
    ToolSpec,
    record_scenario_live,
)


# ---------- fake clients ----------------------------------------------------


class FakeLLM:
    """Returns a queued sequence of LLMTurn objects; raises if exhausted."""

    def __init__(self, turns: Sequence[LLMTurn]):
        self._turns = list(turns)
        self.calls: list[dict] = []

    def send(self, *, system, messages, tools):
        self.calls.append({"system": system, "messages": list(messages), "tools": list(tools)})
        if not self._turns:
            raise AssertionError("FakeLLM exhausted: more LLM calls than queued turns")
        return self._turns.pop(0)


class FakeMCP:
    def __init__(self, tools: Sequence[ToolSpec], results: dict):
        self._tools = tuple(tools)
        self._results = dict(results)
        self.calls: list[tuple[str, dict]] = []

    def list_tools(self) -> Sequence[ToolSpec]:
        return self._tools

    def call_tool(self, *, name: str, arguments: dict) -> ToolResult:
        self.calls.append((name, dict(arguments)))
        if name not in self._results:
            raise RuntimeError(f"unknown tool: {name}")
        return self._results[name]


def _fixed_clock() -> "callable":
    counter = count(start=1_700_000_000)
    return lambda: float(next(counter))


def _scenario(prompt: str = "Find references to fooBar.") -> Scenario:
    return Scenario(id="sc01", mode="direct", prompt=prompt)


def _config() -> LiveAgentConfig:
    return LiveAgentConfig(system_prompt="test-agent-prompt", clock=_fixed_clock())


def _usage(in_tok: int = 100, out_tok: int = 25, cost: float = 0.001) -> LLMUsage:
    return LLMUsage(tokens_in=in_tok, tokens_out=out_tok, cost_usd=cost)


# ---------- unit tests for the recorder ------------------------------------


class TestRecorderHappyPath(unittest.TestCase):
    """AC1 — single-turn tool call + final answer produces well-formed records."""

    def test_records_match_cassette_shape(self) -> None:
        tools = [
            ToolSpec(
                name="deepsearch",
                description="search",
                input_schema={"type": "object", "required": ["query", "repo"]},
            )
        ]
        turns = [
            LLMTurn(
                text="",
                tool_calls=(
                    ToolCall(id="t1", name="deepsearch", arguments={"query": "fooBar", "repo": "X"}),
                ),
                stop_reason="tool_use",
                usage=_usage(),
            ),
            LLMTurn(
                text="Found 1 reference at src/foo.ts:18.",
                tool_calls=(),
                stop_reason="end_turn",
                usage=_usage(in_tok=64, out_tok=18, cost=0.0008),
            ),
        ]
        results = {
            "deepsearch": ToolResult(
                id="t1",
                content={"ok": True, "results": [{"file": "src/foo.ts", "line": 18}]},
            )
        }
        llm = FakeLLM(turns)
        mcp = FakeMCP(tools, results)

        out = record_scenario_live(_scenario(), llm=llm, mcp=mcp, config=_config())

        kinds = [r["kind"] for r in out.records]
        self.assertEqual(
            kinds,
            ["system_message", "agent_message", "tool_call", "tool_result", "agent_message"],
        )
        # Every record carries scenario_id + mode.
        for rec in out.records:
            self.assertEqual(rec["scenario_id"], "sc01")
            self.assertEqual(rec["mode"], "direct")
            self.assertIn("ts", rec)
            self.assertIn("payload", rec)
        # First tool_call: schema_valid + first_call true.
        tc = next(r for r in out.records if r["kind"] == "tool_call")
        self.assertTrue(tc["payload"]["schema_valid"])
        self.assertTrue(tc["payload"]["first_call"])
        self.assertEqual(tc["tool_name"], "deepsearch")
        # Final agent_message has cost + tokens.
        final = [r for r in out.records if r["kind"] == "agent_message"][-1]
        self.assertEqual(final["payload"]["role"], "assistant")
        self.assertEqual(final["tokens_in"], 64)
        self.assertEqual(final["tokens_out"], 18)
        # Total cost aggregates both turns.
        self.assertAlmostEqual(out.cost_usd, 0.0018, places=6)


class TestRecorderTextThenToolUseSameTurn(unittest.TestCase):
    """Assistant-text emitted in the same turn as tool_use lands BEFORE the tool_call."""

    def test_text_record_precedes_tool_call_record(self) -> None:
        tools = [
            ToolSpec(
                name="list_repos",
                description="list",
                input_schema={"type": "object", "required": []},
            )
        ]
        turns = [
            LLMTurn(
                text="Let me enumerate first.",
                tool_calls=(ToolCall(id="t1", name="list_repos", arguments={}),),
                stop_reason="tool_use",
                usage=_usage(),
            ),
            LLMTurn(text="Done.", tool_calls=(), stop_reason="end_turn", usage=_usage()),
        ]
        results = {"list_repos": ToolResult(id="t1", content=[{"name": "R1"}])}
        out = record_scenario_live(
            _scenario(), llm=FakeLLM(turns), mcp=FakeMCP(tools, results), config=_config()
        )
        kinds = [r["kind"] for r in out.records]
        self.assertEqual(
            kinds,
            [
                "system_message",
                "agent_message",  # user prompt
                "agent_message",  # assistant leading text
                "tool_call",
                "tool_result",
                "agent_message",  # final answer
            ],
        )


class TestRecorderEmptyResultDetected(unittest.TestCase):
    """is_empty: true must be set for empty results so metrics can detect graceful handling."""

    def test_empty_results_flagged(self) -> None:
        tools = [
            ToolSpec(name="search", description="x", input_schema={"type": "object", "required": []})
        ]
        turns = [
            LLMTurn(
                text="",
                tool_calls=(ToolCall(id="t1", name="search", arguments={}),),
                stop_reason="tool_use",
                usage=_usage(),
            ),
            LLMTurn(text="Nothing found.", tool_calls=(), stop_reason="end_turn", usage=_usage()),
        ]
        results = {"search": ToolResult(id="t1", content={"results": []})}
        out = record_scenario_live(
            _scenario(), llm=FakeLLM(turns), mcp=FakeMCP(tools, results), config=_config()
        )
        tr = next(r for r in out.records if r["kind"] == "tool_result")
        self.assertTrue(tr["payload"]["is_empty"])


class TestRecorderSchemaInvalidArgs(unittest.TestCase):
    """schema_valid is False when required fields are missing — surfaces R7 metric."""

    def test_missing_required_field_marks_schema_invalid(self) -> None:
        tools = [
            ToolSpec(
                name="deepsearch",
                description="x",
                input_schema={"type": "object", "required": ["query", "repo"]},
            )
        ]
        turns = [
            LLMTurn(
                text="",
                tool_calls=(ToolCall(id="t1", name="deepsearch", arguments={"query": "x"}),),
                stop_reason="tool_use",
                usage=_usage(),
            ),
            LLMTurn(text="done.", tool_calls=(), stop_reason="end_turn", usage=_usage()),
        ]
        results = {"deepsearch": ToolResult(id="t1", content=[])}
        out = record_scenario_live(
            _scenario(), llm=FakeLLM(turns), mcp=FakeMCP(tools, results), config=_config()
        )
        tc = next(r for r in out.records if r["kind"] == "tool_call")
        self.assertFalse(tc["payload"]["schema_valid"])


class TestRecorderToolFailureCaptured(unittest.TestCase):
    """A raising tool call becomes a tool_result with ok=false; the loop continues."""

    def test_tool_exception_becomes_error_result(self) -> None:
        tools = [
            ToolSpec(name="bad", description="x", input_schema={"type": "object", "required": []})
        ]
        results = {"bad": "raise"}  # sentinel — FakeMCP will be replaced

        class RaisingMCP(FakeMCP):
            def call_tool(self, *, name, arguments):
                raise RuntimeError("upstream 500")

        turns = [
            LLMTurn(
                text="",
                tool_calls=(ToolCall(id="t1", name="bad", arguments={}),),
                stop_reason="tool_use",
                usage=_usage(),
            ),
            LLMTurn(
                text="I couldn't complete the task.",
                tool_calls=(),
                stop_reason="end_turn",
                usage=_usage(),
            ),
        ]
        out = record_scenario_live(
            _scenario(),
            llm=FakeLLM(turns),
            mcp=RaisingMCP(tools, results),
            config=_config(),
        )
        tr = next(r for r in out.records if r["kind"] == "tool_result")
        self.assertFalse(tr["payload"]["ok"])
        self.assertIn("upstream 500", tr["payload"]["error"])


class TestRecorderMaxTurnsAbort(unittest.TestCase):
    """An LLM that never yields end_turn within max_turns raises LiveRecordingFailure."""

    def test_infinite_tool_loop_aborts(self) -> None:
        tools = [
            ToolSpec(name="loop", description="x", input_schema={"type": "object", "required": []})
        ]
        results = {"loop": ToolResult(id="t1", content="result")}
        turn = LLMTurn(
            text="",
            tool_calls=(ToolCall(id="t1", name="loop", arguments={}),),
            stop_reason="tool_use",
            usage=_usage(),
        )
        llm = FakeLLM([turn] * 50)
        cfg = LiveAgentConfig(system_prompt="x", max_turns=3, clock=_fixed_clock())
        with self.assertRaises(LiveRecordingFailure):
            record_scenario_live(_scenario(), llm=llm, mcp=FakeMCP(tools, results), config=cfg)


# ---------- cassette write / load round trip --------------------------------


class TestCassetteRoundTrip(unittest.TestCase):
    """write_cassette emits the same bytes that load_cassette reads back."""

    def test_round_trip_byte_equal(self) -> None:
        records = [
            {"ts": 1.0, "scenario_id": "sc99", "mode": "direct", "kind": "system_message",
             "payload": {"role": "system", "content": "x"}},
            {"ts": 2.0, "scenario_id": "sc99", "mode": "direct", "kind": "agent_message",
             "payload": {"role": "user", "content": "go"}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            cas_dir = Path(tmp)
            written = write_cassette(cas_dir, "sc99", "direct", records)
            self.assertTrue(written.path.exists())
            loaded = load_cassette(cas_dir, "sc99", "direct")
            self.assertEqual(written.bytes, loaded.bytes)
            # Each line is canonical JSON.
            for line in loaded.bytes.decode().splitlines():
                json.loads(line)

    def test_oversize_cassette_rejected(self) -> None:
        big_payload = "x" * (550 * 1024)
        records = [{"ts": 1.0, "scenario_id": "sc99", "mode": "direct",
                    "kind": "agent_message",
                    "payload": {"role": "assistant", "content": big_payload}}]
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(CassetteTooLargeError):
                write_cassette(Path(tmp), "sc99", "direct", records)
            self.assertFalse((Path(tmp) / "sc99.direct.jsonl").exists())


# ---------- CLI integration tests ------------------------------------------


def _run_trace_subprocess(*args: str, env_overrides: dict | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    for k in ("ANTHROPIC_API_KEY", "MCP_SERVER_ENDPOINT", "MCP_AX_LIVE_MODEL"):
        env.pop(k, None)
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-m", "cli", "trace", *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        env=env,
    )


class TestCliRecordRequiresEnvVars(unittest.TestCase):
    """AC3 — missing ANTHROPIC_API_KEY or MCP_SERVER_ENDPOINT fails loud."""

    def test_record_without_credentials_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            r = _run_trace_subprocess(
                str(FIXTURES_DIR / "manifest.clean.json"),
                "--record", "--tier", "smoke", "--out", str(Path(tmp) / "out"),
            )
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("ANTHROPIC_API_KEY", r.stderr)
            self.assertIn("MCP_SERVER_ENDPOINT", r.stderr)

    def test_no_cassette_without_credentials_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            r = _run_trace_subprocess(
                str(FIXTURES_DIR / "manifest.clean.json"),
                "--no-cassette", "--tier", "smoke", "--out", str(Path(tmp) / "out"),
            )
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("ANTHROPIC_API_KEY", r.stderr)

    def test_record_with_only_api_key_errors_on_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            r = _run_trace_subprocess(
                str(FIXTURES_DIR / "manifest.clean.json"),
                "--record", "--tier", "smoke", "--out", str(Path(tmp) / "out"),
                env_overrides={"ANTHROPIC_API_KEY": "sk-test"},
            )
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("MCP_SERVER_ENDPOINT", r.stderr)


class TestCliRecordAndNoCassetteMutuallyExclusive(unittest.TestCase):
    """`--record` + `--no-cassette` together is a usage error (not a silent contradiction)."""

    def test_both_flags_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            r = _run_trace_subprocess(
                str(FIXTURES_DIR / "manifest.clean.json"),
                "--record", "--no-cassette",
                "--tier", "smoke", "--out", str(Path(tmp) / "out"),
                env_overrides={
                    "ANTHROPIC_API_KEY": "sk-test",
                    "MCP_SERVER_ENDPOINT": "https://mcp.example/v1",
                },
            )
            self.assertEqual(r.returncode, 2)
            self.assertIn("mutually exclusive", r.stderr)


class TestCliLiveHappyPathInProcess(unittest.TestCase):
    """AC1/AC2 — exercise _run_live in-process with monkeypatched factories.

    Subprocess can't see test monkeypatching, so we import trace.main() and
    swap the factory module attributes directly.
    """

    def setUp(self) -> None:
        # Avoid the existing trace module pulling in a stale process state.
        for mod in list(sys.modules):
            if mod.startswith("cli.") or mod == "cli":
                # importing cli/trace lazily is fine — let the test fresh-import
                pass

    @contextmanager
    def _patched_factories(self, llm, mcp):
        from harness.runtime import live_factory
        with mock.patch.object(live_factory, "build_llm_client", lambda env=None: llm), \
             mock.patch.object(live_factory, "build_mcp_session", lambda env=None: mcp):
            yield

    def _two_scenario_turns(self) -> list[LLMTurn]:
        # Two scenarios in smoke tier (sc01, sc02), each needs one tool call + final answer.
        return [
            LLMTurn(
                text="",
                tool_calls=(ToolCall(id="t1", name="search_code", arguments={"pattern": "fooBar", "repo": "X"}),),
                stop_reason="tool_use",
                usage=_usage(in_tok=100, out_tok=25, cost=0.0005),
            ),
            LLMTurn(text="Found.", tool_calls=(), stop_reason="end_turn",
                    usage=_usage(in_tok=50, out_tok=10, cost=0.0002)),
            LLMTurn(
                text="",
                tool_calls=(ToolCall(id="t2", name="search_code", arguments={"pattern": "barBaz", "repo": "Y"}),),
                stop_reason="tool_use",
                usage=_usage(in_tok=100, out_tok=25, cost=0.0005),
            ),
            LLMTurn(text="Done.", tool_calls=(), stop_reason="end_turn",
                    usage=_usage(in_tok=50, out_tok=10, cost=0.0002)),
        ]

    def _tools_and_results(self) -> tuple[Sequence[ToolSpec], dict]:
        tools = [
            ToolSpec(
                name="search_code",
                description="search",
                input_schema={"type": "object", "required": ["pattern", "repo"]},
            )
        ]
        results = {
            "search_code": ToolResult(id="x", content={"ok": True, "results": [{"f": "a"}]}),
        }
        return tools, results

    def test_record_writes_cassettes_and_metrics(self) -> None:
        tools, results = self._tools_and_results()
        llm = FakeLLM(self._two_scenario_turns())
        mcp = FakeMCP(tools, results)
        with tempfile.TemporaryDirectory() as tmp_repo:
            # Copy repo into tmp so cassettes are written to an isolated dir.
            shutil.copytree(REPO_ROOT, tmp_repo, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns(
                                "__pycache__", ".pytest_cache", ".beads", ".git",
                                "runs", "*.pyc",
                            ))
            tmp_root = Path(tmp_repo)
            (tmp_root / "fixtures" / "cassettes").mkdir(parents=True, exist_ok=True)
            # Wipe pre-existing cassettes in the tmp copy so we observe fresh writes.
            for old in (tmp_root / "fixtures" / "cassettes").glob("*.jsonl"):
                old.unlink()

            out = tmp_root / "out"
            from harness.runtime import live_factory
            from cli import trace as trace_mod
            # Make trace.py see the tmp repo root.
            with mock.patch("cli.trace.find_repo_root", return_value=tmp_root), \
                 self._patched_factories(llm, mcp):
                rc = trace_mod.main([
                    str(tmp_root / "tests" / "fixtures" / "manifest.clean.json"),
                    "--record", "--tier", "smoke", "--modes", "direct",
                    "--max-usd", "10.0",
                    "--out", str(out),
                ])
            self.assertEqual(rc, 0)

            # Cassettes for sc01 and sc02 are present in fixtures/.
            self.assertTrue((tmp_root / "fixtures" / "cassettes" / "sc01.direct.jsonl").is_file())
            self.assertTrue((tmp_root / "fixtures" / "cassettes" / "sc02.direct.jsonl").is_file())
            # Trace files are present in out/.
            self.assertTrue((out / "traces" / "sc01.direct.jsonl").is_file())
            self.assertTrue((out / "traces" / "sc02.direct.jsonl").is_file())
            # metrics.json + run.json exist.
            self.assertTrue((out / "metrics.json").is_file())
            run_dirs = list((out / "runs").iterdir())
            self.assertEqual(len(run_dirs), 1)
            run_doc = json.loads((run_dirs[0] / "run.json").read_text())
            self.assertFalse(run_doc["replay"])  # live, not replay

    def test_no_cassette_runs_live_without_writing_cassette(self) -> None:
        tools, results = self._tools_and_results()
        llm = FakeLLM(self._two_scenario_turns())
        mcp = FakeMCP(tools, results)
        with tempfile.TemporaryDirectory() as tmp_repo:
            shutil.copytree(REPO_ROOT, tmp_repo, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns(
                                "__pycache__", ".pytest_cache", ".beads", ".git",
                                "runs", "*.pyc",
                            ))
            tmp_root = Path(tmp_repo)
            (tmp_root / "fixtures" / "cassettes").mkdir(parents=True, exist_ok=True)
            for old in (tmp_root / "fixtures" / "cassettes").glob("*.jsonl"):
                old.unlink()

            out = tmp_root / "out"
            from cli import trace as trace_mod
            with mock.patch("cli.trace.find_repo_root", return_value=tmp_root), \
                 self._patched_factories(llm, mcp):
                rc = trace_mod.main([
                    str(tmp_root / "tests" / "fixtures" / "manifest.clean.json"),
                    "--no-cassette", "--tier", "smoke", "--modes", "direct",
                    "--max-usd", "10.0",
                    "--out", str(out),
                ])
            self.assertEqual(rc, 0)
            # Crucially: NO cassettes written.
            cassettes = list((tmp_root / "fixtures" / "cassettes").glob("*.jsonl"))
            self.assertEqual(cassettes, [])
            # But traces ARE written.
            self.assertTrue((out / "traces" / "sc01.direct.jsonl").is_file())

    def test_live_budget_cap_fails_loud_before_writing_metrics(self) -> None:
        tools, results = self._tools_and_results()
        # Push cost above the cap on the first scenario.
        turns = [
            LLMTurn(
                text="",
                tool_calls=(ToolCall(id="t1", name="search_code", arguments={"pattern": "x", "repo": "Y"}),),
                stop_reason="tool_use",
                usage=_usage(in_tok=10_000, out_tok=10_000, cost=5.0),
            ),
            LLMTurn(text="ok", tool_calls=(), stop_reason="end_turn",
                    usage=_usage(in_tok=0, out_tok=0, cost=0.0)),
        ]
        llm = FakeLLM(turns)
        mcp = FakeMCP(tools, results)
        with tempfile.TemporaryDirectory() as tmp_repo:
            shutil.copytree(REPO_ROOT, tmp_repo, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns(
                                "__pycache__", ".pytest_cache", ".beads", ".git",
                                "runs", "*.pyc",
                            ))
            tmp_root = Path(tmp_repo)
            (tmp_root / "fixtures" / "cassettes").mkdir(parents=True, exist_ok=True)
            for old in (tmp_root / "fixtures" / "cassettes").glob("*.jsonl"):
                old.unlink()

            out = tmp_root / "out"
            from cli import trace as trace_mod
            with mock.patch("cli.trace.find_repo_root", return_value=tmp_root), \
                 self._patched_factories(llm, mcp):
                rc = trace_mod.main([
                    str(tmp_root / "tests" / "fixtures" / "manifest.clean.json"),
                    "--no-cassette", "--tier", "smoke", "--modes", "direct",
                    "--max-usd", "0.10",
                    "--out", str(out),
                ])
            self.assertEqual(rc, 3)
            self.assertFalse((out / "metrics.json").exists())


if __name__ == "__main__":
    unittest.main()
