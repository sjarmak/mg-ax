"""Synchronous MCPClient adapter wrapping the async `mcp` Python SDK.

The recorder loop is sync (matches the rest of the trace stage). The official
mcp client is async-only, so we own one event loop in a background thread and
marshal each `list_tools` / `call_tool` through it via
`run_coroutine_threadsafe`.

Transport is chosen from the connection spec:

  http(s)://host/path     → streamable HTTP
  sse://host/path         → SSE (legacy)
  stdio:<command line>    → spawn subprocess, talk MCP over stdio

The connection lifecycle is owned by `SyncMCPSession`; callers should use it as
a context manager so the subprocess (if any) and event loop are torn down on
every exit path.
"""

from __future__ import annotations

import asyncio
import shlex
import threading
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Sequence

from .live_agent import MCPClient, ToolResult, ToolSpec


DEFAULT_CALL_TIMEOUT_S = 60.0


class MCPTransportError(RuntimeError):
    pass


@dataclass(frozen=True)
class ConnectionSpec:
    scheme: str   # "http" | "sse" | "stdio"
    target: str   # URL or command line
    timeout_s: float = DEFAULT_CALL_TIMEOUT_S

    @classmethod
    def from_endpoint(cls, endpoint: str, *, timeout_s: float = DEFAULT_CALL_TIMEOUT_S) -> "ConnectionSpec":
        if not endpoint:
            raise MCPTransportError(
                "MCP_SERVER_ENDPOINT is empty — set http(s)://, sse://, or "
                "stdio:<command> to point at the upstream MCP server."
            )
        if endpoint.startswith(("http://", "https://")):
            return cls(scheme="http", target=endpoint, timeout_s=timeout_s)
        if endpoint.startswith("sse://"):
            return cls(scheme="sse", target=endpoint.removeprefix("sse://"), timeout_s=timeout_s)
        if endpoint.startswith("stdio:"):
            return cls(scheme="stdio", target=endpoint.removeprefix("stdio:"), timeout_s=timeout_s)
        raise MCPTransportError(
            f"unrecognised MCP endpoint scheme: {endpoint!r}; "
            "must be http(s)://, sse://, or stdio:<command>"
        )


class SyncMCPSession(MCPClient):
    """Holds an event loop in a worker thread and proxies sync calls onto it."""

    def __init__(self, spec: ConnectionSpec) -> None:
        self._spec = spec
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stack: AsyncExitStack | None = None
        self._session = None
        self._opened = False

    def open(self) -> "SyncMCPSession":
        if self._opened:
            return self
        self._loop = asyncio.new_event_loop()
        ready = threading.Event()

        def _runner() -> None:
            asyncio.set_event_loop(self._loop)
            ready.set()
            self._loop.run_forever()

        self._thread = threading.Thread(target=_runner, daemon=True, name="mcp-session-loop")
        self._thread.start()
        ready.wait(timeout=5.0)
        try:
            self._stack, self._session = self._submit(self._connect_async())
        except Exception:
            self.close()
            raise
        self._opened = True
        return self

    def close(self) -> None:
        if self._loop is None:
            return
        if self._stack is not None:
            try:
                self._submit(self._stack.aclose())
            except Exception:
                pass
            self._stack = None
            self._session = None
        loop, thread = self._loop, self._thread
        self._loop = None
        self._thread = None
        self._opened = False
        try:
            loop.call_soon_threadsafe(loop.stop)
        except RuntimeError:
            pass
        if thread is not None:
            thread.join(timeout=5.0)
        try:
            loop.close()
        except Exception:
            pass

    def __enter__(self) -> "SyncMCPSession":
        return self.open()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def list_tools(self) -> Sequence[ToolSpec]:
        self._require_open()
        result = self._submit(self._session.list_tools())
        out: list[ToolSpec] = []
        for tool in getattr(result, "tools", []) or []:
            out.append(
                ToolSpec(
                    name=getattr(tool, "name", "") or "",
                    description=getattr(tool, "description", "") or "",
                    input_schema=dict(getattr(tool, "inputSchema", {}) or {}),
                )
            )
        return tuple(out)

    def call_tool(self, *, name: str, arguments: dict) -> ToolResult:
        self._require_open()
        result = self._submit(self._session.call_tool(name, arguments=arguments))
        content = _flatten_call_tool_content(result)
        is_error = bool(getattr(result, "isError", False))
        return ToolResult(id="", content=content, is_error=is_error)

    def _require_open(self) -> None:
        if not self._opened or self._session is None:
            raise MCPTransportError(
                "SyncMCPSession not opened — call .open() or use as a context manager."
            )

    def _submit(self, coro):
        assert self._loop is not None
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=self._spec.timeout_s)

    async def _connect_async(self):
        from mcp import ClientSession

        stack = AsyncExitStack()
        try:
            if self._spec.scheme == "http":
                from mcp.client.streamable_http import streamablehttp_client

                transport = await stack.enter_async_context(streamablehttp_client(self._spec.target))
                read, write = transport[0], transport[1]
            elif self._spec.scheme == "sse":
                from mcp.client.sse import sse_client

                read, write = await stack.enter_async_context(sse_client(self._spec.target))
            elif self._spec.scheme == "stdio":
                from mcp.client.stdio import StdioServerParameters, stdio_client

                parts = shlex.split(self._spec.target)
                if not parts:
                    raise MCPTransportError("stdio: endpoint must include a command")
                params = StdioServerParameters(command=parts[0], args=parts[1:])
                read, write = await stack.enter_async_context(stdio_client(params))
            else:
                raise MCPTransportError(f"unsupported scheme: {self._spec.scheme!r}")

            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            return stack, session
        except Exception:
            await stack.aclose()
            raise


def _flatten_call_tool_content(result) -> object:
    """Coerce CallToolResult.content into a JSON-friendly payload.

    MCP returns a list of content blocks (text/image/resource). For the
    recorder we keep text blocks as-is, surface structured data verbatim, and
    fall back to a list of dicts otherwise.
    """
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return structured
    content = getattr(result, "content", None)
    if content is None:
        return None
    text_chunks: list[str] = []
    has_only_text = True
    for block in content:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            text_chunks.append(getattr(block, "text", "") or "")
        else:
            has_only_text = False
            break
    if has_only_text:
        joined = "".join(text_chunks)
        return joined if joined else None
    # Fall back: serialise blocks with whatever attributes are exposed.
    return [
        {k: v for k, v in getattr(block, "__dict__", {}).items() if not k.startswith("_")}
        for block in content
    ]
