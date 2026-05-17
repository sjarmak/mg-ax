"""Anthropic LLMClient adapter for the live recorder.

The harness records cost from per-record `cost_usd` aggregates (R7, R12); the
pricing table below is used to attach a USD estimate to each LLM turn from
returned token usage. It is an approximation — the API account is the source of
truth for billing. Numbers represent USD per million tokens (input, output).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .live_agent import LLMTurn, LLMUsage, LLMClient, ToolCall, ToolSpec


# Per the harness contract, run.json references capability profiles not model
# IDs. The default live model is the test-agent (Haiku) for cost reasons;
# operators can override via the constructor.
DEFAULT_LIVE_MODEL = "claude-haiku-4-5"
DEFAULT_MAX_OUTPUT_TOKENS = 2048

_PRICING_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (0.80, 4.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-4-7": (15.00, 75.00),
}


class AnthropicCredentialsMissing(RuntimeError):
    pass


def estimate_cost_usd(model: str, tokens_in: int, tokens_out: int) -> float:
    pricing = _PRICING_USD_PER_MTOK.get(model)
    if pricing is None:
        # Unknown models: refuse to estimate; recorder will record 0.0 and the
        # budget cap will under-count. Operators should add a pricing entry.
        return 0.0
    in_per_mtok, out_per_mtok = pricing
    return (tokens_in / 1_000_000.0) * in_per_mtok + (tokens_out / 1_000_000.0) * out_per_mtok


@dataclass(frozen=True)
class AnthropicClient(LLMClient):
    """Thin LLMClient wrapper around anthropic.Anthropic.messages.create."""

    api_key: str
    model: str = DEFAULT_LIVE_MODEL
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS

    def __post_init__(self) -> None:
        if not self.api_key:
            raise AnthropicCredentialsMissing(
                "ANTHROPIC_API_KEY is empty — required for `mcp-ax trace --record` "
                "live mode. Set the environment variable and retry."
            )

    def send(
        self,
        *,
        system: str,
        messages: Sequence[dict],
        tools: Sequence[ToolSpec],
    ) -> LLMTurn:
        import anthropic  # local import: anthropic is only required for live mode

        client = anthropic.Anthropic(api_key=self.api_key)
        api_tools = [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
            }
            for t in tools
        ]
        response = client.messages.create(
            model=self.model,
            max_tokens=self.max_output_tokens,
            system=system,
            messages=list(messages),
            tools=api_tools or None,
        )
        return _turn_from_anthropic_response(response, model=self.model)


def _turn_from_anthropic_response(response, *, model: str) -> LLMTurn:
    text_chunks: list[str] = []
    tool_calls: list[ToolCall] = []
    for block in getattr(response, "content", []) or []:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            text_chunks.append(getattr(block, "text", "") or "")
        elif block_type == "tool_use":
            tool_calls.append(
                ToolCall(
                    id=getattr(block, "id", "") or "",
                    name=getattr(block, "name", "") or "",
                    arguments=dict(getattr(block, "input", {}) or {}),
                )
            )
    usage = getattr(response, "usage", None)
    tokens_in = int(getattr(usage, "input_tokens", 0) or 0) if usage else 0
    tokens_out = int(getattr(usage, "output_tokens", 0) or 0) if usage else 0
    return LLMTurn(
        text="".join(text_chunks),
        tool_calls=tuple(tool_calls),
        stop_reason=str(getattr(response, "stop_reason", "") or ""),
        usage=LLMUsage(
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=estimate_cost_usd(model, tokens_in, tokens_out),
        ),
    )
