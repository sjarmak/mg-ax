"""Live recorder glue for `mcp-ax trace --record` and `--no-cassette` (R27).

This module is pure orchestration: it accepts an LLM client and an MCP session,
runs the agent loop for one scenario, and emits trace records matching the
trace.jsonl schema. All reasoning is delegated to the LLM (ZFC); the recorder
only validates structure, tracks wall clock / token / cost, and shapes records.

Sister modules:

  harness.runtime.anthropic_client — concrete LLMClient implementation
  harness.runtime.mcp_session      — concrete MCPClient implementation
  harness.runtime.cassette         — load / replay / write JSONL cassettes
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Protocol, Sequence, runtime_checkable


DEFAULT_MAX_TURNS = 8


class LiveRecordingConfigError(RuntimeError):
    """Live recording requested but required credentials / transport missing."""


class LiveRecordingFailure(RuntimeError):
    """Live recording started but failed mid-run (e.g. max_turns exhausted)."""


@dataclass(frozen=True)
class Scenario:
    id: str
    mode: str
    prompt: str


@dataclass(frozen=True)
class ToolSpec:
    """Tool advertised by the upstream MCP server and forwarded to the LLM."""
    name: str
    description: str
    input_schema: dict


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass(frozen=True)
class ToolResult:
    id: str
    content: object
    is_error: bool = False


@dataclass(frozen=True)
class LLMUsage:
    tokens_in: int
    tokens_out: int
    cost_usd: float


@dataclass(frozen=True)
class LLMTurn:
    text: str
    tool_calls: tuple[ToolCall, ...]
    stop_reason: str
    usage: LLMUsage


@runtime_checkable
class LLMClient(Protocol):
    def send(
        self,
        *,
        system: str,
        messages: Sequence[dict],
        tools: Sequence[ToolSpec],
    ) -> LLMTurn:
        ...


@runtime_checkable
class MCPClient(Protocol):
    def list_tools(self) -> Sequence[ToolSpec]:
        ...

    def call_tool(self, *, name: str, arguments: dict) -> ToolResult:
        ...


@dataclass(frozen=True)
class LiveAgentConfig:
    system_prompt: str
    max_turns: int = DEFAULT_MAX_TURNS
    clock: Callable[[], float] = field(default=time.time)


@dataclass(frozen=True)
class LiveAgentRunResult:
    records: tuple[dict, ...]
    cost_usd: float
    wall_clock_ms: int


def record_scenario_live(
    scenario: Scenario,
    *,
    llm: LLMClient,
    mcp: MCPClient,
    config: LiveAgentConfig,
) -> LiveAgentRunResult:
    """Drive one scenario end-to-end and return trace records + aggregates.

    Records follow harness/schemas/trace.jsonl.schema.json. The recorder owns
    structural fields (ts, scenario_id, mode, kind, tool_name, tokens, cost,
    wall_clock_ms); the payload is whatever the LLM / MCP server produced.
    """
    records: list[dict] = []
    cost_total = 0.0
    started_perf = time.perf_counter()

    def emit(
        kind: str,
        payload: dict,
        *,
        tool_name: str | None = None,
        tokens_in: int = 0,
        tokens_out: int = 0,
        cost_usd: float = 0.0,
        wall_clock_ms: int = 0,
    ) -> None:
        rec: dict = {
            "ts": float(config.clock()),
            "scenario_id": scenario.id,
            "mode": scenario.mode,
            "kind": kind,
            "payload": payload,
        }
        if tool_name is not None:
            rec["tool_name"] = tool_name
        if tokens_in:
            rec["tokens_in"] = int(tokens_in)
        if tokens_out:
            rec["tokens_out"] = int(tokens_out)
        if cost_usd:
            rec["cost_usd"] = float(cost_usd)
        if wall_clock_ms:
            rec["wall_clock_ms"] = int(wall_clock_ms)
        records.append(rec)

    emit("system_message", {"role": "system", "content": config.system_prompt})
    emit("agent_message", {"role": "user", "content": scenario.prompt})

    tools = tuple(mcp.list_tools())
    tools_by_name = {t.name: t for t in tools}

    msg_history: list[dict] = [{"role": "user", "content": scenario.prompt}]
    saw_first_tool_call = False

    for _ in range(config.max_turns):
        llm_started = time.perf_counter()
        turn = llm.send(system=config.system_prompt, messages=msg_history, tools=tools)
        llm_elapsed_ms = int((time.perf_counter() - llm_started) * 1000)
        cost_total += turn.usage.cost_usd

        assistant_blocks: list[dict] = []
        if turn.text:
            assistant_blocks.append({"type": "text", "text": turn.text})
        for tc in turn.tool_calls:
            assistant_blocks.append(
                {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.arguments}
            )
        msg_history.append({"role": "assistant", "content": assistant_blocks})

        if turn.text:
            emit(
                "agent_message",
                {"role": "assistant", "content": turn.text},
                tokens_in=turn.usage.tokens_in,
                tokens_out=turn.usage.tokens_out,
                cost_usd=turn.usage.cost_usd,
                wall_clock_ms=llm_elapsed_ms,
            )

        if not turn.tool_calls:
            return LiveAgentRunResult(
                records=tuple(records),
                cost_usd=cost_total,
                wall_clock_ms=int((time.perf_counter() - started_perf) * 1000),
            )

        tool_blocks: list[dict] = []
        for tc in turn.tool_calls:
            tool_started = time.perf_counter()
            emit(
                "tool_call",
                {
                    "args": tc.arguments,
                    "schema_valid": _args_satisfy_schema(tc.arguments, tools_by_name.get(tc.name)),
                    "first_call": not saw_first_tool_call,
                },
                tool_name=tc.name,
            )
            saw_first_tool_call = True

            try:
                result = mcp.call_tool(name=tc.name, arguments=tc.arguments)
                tool_elapsed_ms = int((time.perf_counter() - tool_started) * 1000)
                emit(
                    "tool_result",
                    {
                        "ok": not result.is_error,
                        "result": result.content,
                        "is_empty": _is_empty_result(result.content),
                    },
                    tool_name=tc.name,
                    wall_clock_ms=tool_elapsed_ms,
                )
                tool_blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tc.id,
                        "is_error": bool(result.is_error),
                        "content": _to_text_content(result.content),
                    }
                )
            except Exception as exc:  # noqa: BLE001 — surface MCP failures into the trace
                tool_elapsed_ms = int((time.perf_counter() - tool_started) * 1000)
                emit(
                    "tool_result",
                    {"ok": False, "error": str(exc), "is_empty": False},
                    tool_name=tc.name,
                    wall_clock_ms=tool_elapsed_ms,
                )
                tool_blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tc.id,
                        "is_error": True,
                        "content": str(exc),
                    }
                )

        msg_history.append({"role": "user", "content": tool_blocks})

    raise LiveRecordingFailure(
        f"scenario {scenario.id!r}: max_turns={config.max_turns} reached "
        "without the LLM yielding end_turn — possible infinite tool loop."
    )


def _args_satisfy_schema(args: object, tool: ToolSpec | None) -> bool:
    if tool is None or not isinstance(args, dict):
        return False
    required = tool.input_schema.get("required", []) or []
    return all(field_name in args for field_name in required)


def _is_empty_result(content: object) -> bool:
    if content is None:
        return True
    if isinstance(content, (list, str, bytes)) and len(content) == 0:
        return True
    if isinstance(content, dict):
        results = content.get("results")
        if isinstance(results, list) and len(results) == 0:
            return True
        if not content:
            return True
    return False


def _to_text_content(content: object) -> str:
    if isinstance(content, str):
        return content
    return json.dumps(content, sort_keys=True)
