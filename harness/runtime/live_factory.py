"""Factories for live-recording clients — kept separate so tests can monkeypatch.

`cli/trace.py` imports this module and calls `build_llm_client` /
`build_mcp_session` by attribute lookup. Tests replace the attributes with
fakes that return canned `LLMTurn` / `ToolResult` values, exercising the
recorder without requiring real ANTHROPIC_API_KEY / MCP_SERVER_ENDPOINT.
"""

from __future__ import annotations

import os
from typing import Mapping

from .anthropic_client import AnthropicClient, AnthropicCredentialsMissing, DEFAULT_LIVE_MODEL
from .live_agent import LLMClient, MCPClient
from .mcp_session import ConnectionSpec, MCPTransportError, SyncMCPSession


ENV_ANTHROPIC_API_KEY = "ANTHROPIC_API_KEY"
ENV_MCP_ENDPOINT = "MCP_SERVER_ENDPOINT"
ENV_LIVE_MODEL = "MCP_AX_LIVE_MODEL"


class LiveCredentialsMissing(RuntimeError):
    """Raised when required env vars for live mode are absent or empty."""


def require_live_env(env: Mapping[str, str] | None = None) -> tuple[str, str]:
    """Return (api_key, endpoint) or raise with a clear remediation message."""
    env = os.environ if env is None else env
    api_key = (env.get(ENV_ANTHROPIC_API_KEY) or "").strip()
    endpoint = (env.get(ENV_MCP_ENDPOINT) or "").strip()
    missing = []
    if not api_key:
        missing.append(ENV_ANTHROPIC_API_KEY)
    if not endpoint:
        missing.append(ENV_MCP_ENDPOINT)
    if missing:
        raise LiveCredentialsMissing(
            "live recording requires environment variables: "
            + ", ".join(missing)
            + ". Set them and retry, or omit --record / --no-cassette to use "
            "cassette replay."
        )
    return api_key, endpoint


def build_llm_client(env: Mapping[str, str] | None = None) -> LLMClient:
    env = os.environ if env is None else env
    api_key, _ = require_live_env(env)
    model = (env.get(ENV_LIVE_MODEL) or DEFAULT_LIVE_MODEL).strip() or DEFAULT_LIVE_MODEL
    return AnthropicClient(api_key=api_key, model=model)


def build_mcp_session(env: Mapping[str, str] | None = None) -> MCPClient:
    """Return an MCPClient. Returned object may also be a context manager;
    callers should close it via .close() (or `with` for SyncMCPSession)."""
    env = os.environ if env is None else env
    _, endpoint = require_live_env(env)
    spec = ConnectionSpec.from_endpoint(endpoint)
    return SyncMCPSession(spec).open()
