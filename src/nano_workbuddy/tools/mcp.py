"""MCP servers (D14): one long-lived connection per configured server, shared by every session.

The SDK's transports must be entered and exited by the same task, so each connection lives in a task
of its own and runs only call tools through it. A server that fails or dies is started again at the
next run. stdio servers run inside the OS sandbox (network allowed) unless the config says otherwise.
Every MCP tool goes through the same permission gate as built-in tools: action `mcp__server__tool`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import tempfile
from pathlib import Path
from typing import IO, Any

import httpx2
import mcp_types
from mcp import Client
from mcp.client.stdio import StdioServerParameters, get_default_environment, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp_types.jsonrpc import CONNECTION_CLOSED
from pydantic import BaseModel, Field

from ..config import McpServer, resolve
from ..policy import server_sandbox
from ..sandbox import wrap
from ..types import text
from .registry import AnyArgs, Result, Tool, ToolCtx, ToolError, tool

log = logging.getLogger(__name__)

START_TIMEOUT = 60  # seconds: npx/uvx may download the server on first start
INSTRUCTIONS_CHARS = 1_000


def tool_name(server: str, name: str) -> str:
    """Provider function names allow [A-Za-z0-9_-], at most 64 characters. A longer name is cut and ends in a
    hash of the full one, so two long names that share their first 64 characters stay apart."""
    full = f"mcp__{server}__{name}"
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", full)
    return safe if len(safe) <= 64 else f"{safe[:55]}_{_hash(full)}"


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:8]


def describe(e: BaseException) -> str:
    """The innermost message of an exception group: what actually went wrong."""
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        e = e.exceptions[0]
    return f"{type(e).__name__}: {e}"[:500]


class Server:
    def __init__(self, spec: McpServer, home: Path):
        self.spec, self.home = spec, home
        self.client: Client | None = None
        self.tools: list[mcp_types.Tool] = []
        self.instructions = ""
        self.error = ""
        self._task: asyncio.Task[None] | None = None
        self._ready = self._stop = asyncio.Event()

    @property
    def tmp(self) -> Path:
        return Path(tempfile.gettempdir()).resolve() / f"nwb-mcp-{self.spec.name}"

    async def start(self) -> None:
        """Connect if not connected; otherwise refresh the tool list. Never raises: see .error."""
        if self.client is not None:
            try:
                self.tools = await list_tools(self.client)
                return
            except Exception as e:  # the server went away between runs: start it again
                self.error = describe(e)
                await self.stop()
        if self._task is None or self._task.done():
            self._ready, self._stop = asyncio.Event(), asyncio.Event()
            self._task = asyncio.create_task(self._main(), name=f"mcp-{self.spec.name}")
        try:
            await asyncio.wait_for(self._ready.wait(), START_TIMEOUT)
        except TimeoutError:
            self.error = f"no answer within {START_TIMEOUT}s"
            await self.stop()

    async def stop(self) -> None:
        self._stop.set()
        if self._task and not self._task.done():
            if self.client is None:  # still starting: it is not waiting for _stop yet
                self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self.client = None

    async def _main(self) -> None:
        errlog: IO[str] | None = None
        try:
            if self.spec.url:
                headers = {k: resolve(v, self.home) for k, v in self.spec.headers.items()}
                async with httpx2.AsyncClient(headers=headers, timeout=httpx2.Timeout(30, read=300)) as http:
                    await self._serve(streamable_http_client(self.spec.url, http_client=http))
            else:
                (self.home / "logs").mkdir(exist_ok=True)
                errlog = open(self.home / "logs" / f"mcp-{self.spec.name}.log", "a", encoding="utf-8")  # noqa: SIM115
                await self._serve(stdio_client(self._params(), errlog=errlog))
        except Exception as e:  # spawn or handshake failures, a missing key, the server dying later
            self.error = describe(e)
            log.warning("MCP server %s: %s", self.spec.name, self.error)
        finally:
            self.client = None
            self._ready.set()
            if errlog:
                errlog.close()

    async def _serve(self, transport: Any) -> None:
        async with Client(transport) as client:
            self.instructions = (client.instructions or "")[:INSTRUCTIONS_CHARS]
            self.tools = await list_tools(client)
            self.client, self.error = client, ""
            self._ready.set()
            await self._stop.wait()

    def _params(self) -> StdioServerParameters:
        s = self.spec
        argv = [s.command, *s.args]
        env = get_default_environment() | {k: resolve(v, self.home) for k, v in s.env.items()}
        if s.sandbox:  # SandboxUnavailable propagates: never fall back to running unconfined
            self.tmp.mkdir(parents=True, exist_ok=True)
            writable = (self.tmp, *(Path(w).expanduser().resolve() for w in s.writable))
            argv = wrap(argv, server_sandbox(writable, s.network, self.home.resolve()))
            env["TMPDIR"] = str(self.tmp)
        cwd = Path(s.cwd).expanduser().resolve() if s.cwd else Path.home()
        return StdioServerParameters(command=argv[0], args=argv[1:], env=env, cwd=cwd)

    def as_tools(self) -> list[Tool]:
        out, taken = [], set()
        for t in self.tools:
            name = tool_name(self.spec.name, t.name)
            if name in taken:  # "a.b" and "a_b" both become a_b: the later one gets a stable suffix
                name = f"{name[:55]}_{_hash(t.name)}"
            taken.add(name)
            out.append(self._tool(t, name))
        return out

    def _tool(self, t: mcp_types.Tool, name: str) -> Tool:
        # readOnlyHint is the server's own claim. It may let calls run side by side, but never makes a call
        # safe to replay after a crash: effect stays "exec", so an interrupted call is reported as unknown.
        read_only = bool(t.annotations and t.annotations.read_only_hint)

        async def call(args: AnyArgs, ctx: ToolCtx) -> Result:
            return await self.call(t.name, args.model_dump())

        return Tool(name, t.description or t.title or t.name, AnyArgs, call, "exec", read_only, None,
                    lambda args, ctx: (name, ""), input_schema=t.input_schema)

    async def call(self, name: str, args: dict[str, Any]) -> Result:
        if self.client is None:
            raise ToolError(f"MCP server {self.spec.name} is not running ({self.error or 'stopped'})")
        try:
            return to_result(await self.client.call_tool(name, args, read_timeout_seconds=self.spec.timeout))
        except MCPError as e:
            if e.code == CONNECTION_CLOSED:
                self.error = "connection closed"
                await self.stop()  # restarted at the next run
            raise ToolError(f"MCP server {self.spec.name}: {e.error.message}") from e


async def list_tools(client: Client) -> list[mcp_types.Tool]:
    tools, cursor = [], None
    while True:
        page = await client.list_tools(cursor=cursor)
        tools += page.tools
        if not (cursor := page.next_cursor):
            return tools


def to_result(r: mcp_types.CallToolResult) -> Result:
    blocks: list[Any] = []
    for c in r.content:
        if c.type == "text":
            blocks.append(text(c.text))
        elif c.type == "image":
            blocks.append({"type": "image", "mime": c.mime_type, "data": c.data})
        elif c.type == "resource" and isinstance(c.resource, mcp_types.TextResourceContents):
            blocks.append(text(f"[resource {c.resource.uri}]\n{c.resource.text}"))
        elif c.type == "resource":
            blocks.append(text(f"[binary resource {c.resource.uri} ({c.resource.mime_type}) not shown]"))
        elif c.type == "resource_link":
            blocks.append(text(f"[resource link {c.uri} {c.name}]"))
        else:
            blocks.append(text(f"[{c.type} content not shown]"))
    meta: dict[str, Any] = {}
    if r.structured_content is not None:  # kept whole in the log even when the text blocks summarise it
        meta["structured"] = r.structured_content
        if not blocks:
            blocks.append(text(json.dumps(r.structured_content, ensure_ascii=False)))
    return Result(blocks or [text("(no output)")], r.is_error, meta)


class McpHub:
    """The process's MCP connections, kept in step with [mcp.*] in config.toml."""

    def __init__(self, home: Path):
        self.home = home
        self.servers: dict[str, Server] = {}
        self._lock = asyncio.Lock()

    async def connect(self, specs: dict[str, McpServer]) -> list[Server]:
        async with self._lock:
            return await self._connect(specs)

    async def _connect(self, specs: dict[str, McpServer]) -> list[Server]:
        """Stop servers whose config changed or went away, then start every enabled one."""
        for name, srv in list(self.servers.items()):
            if specs.get(name) != srv.spec:
                await srv.stop()
                del self.servers[name]
        for name, spec in specs.items():
            if spec.enabled and name not in self.servers:
                self.servers[name] = Server(spec, self.home)
        await asyncio.gather(*(s.start() for s in self.servers.values()))
        return list(self.servers.values())

    async def close(self) -> None:
        await asyncio.gather(*(s.stop() for s in self.servers.values()))
        self.servers.clear()

    def status(self, specs: dict[str, McpServer]) -> dict[str, dict[str, Any]]:
        out = {}
        for name, spec in specs.items():
            srv = self.servers.get(name)
            if srv and srv.spec != spec:  # edited since it started: restarts with the new config at the next run
                srv = None
            state = ("disabled" if not spec.enabled else "connected" if srv and srv.client
                     else "error" if srv and srv.error else "idle")
            out[name] = {"status": state, "error": srv.error if srv else "",
                         "tools": [{"name": t.name, "description": (t.description or "")[:200]}
                                   for t in (srv.tools if srv and srv.client else [])]}
        return out


class SearchArgs(BaseModel):
    query: str = Field(description='Keywords describing the capability you need, or "select:name1,name2" '
                       "to load tools by exact name")
    limit: int = Field(5, ge=1, le=20)


@tool(effect="read", parallel=True)
async def tool_search(args: SearchArgs, ctx: ToolCtx) -> Result:
    """Find and load deferred tools. Matching tools become callable from your next step."""
    found = ctx.tools.search(args.query, args.limit) if ctx.tools else []
    if not found:
        return Result.ok("No deferred tool matches. Try other keywords; the deferred servers are listed in the "
                         "latest system notice.")
    lines = [f"- {t.name}: {t.description.strip().splitlines()[0][:200] if t.description.strip() else ''}"
             for t in found]
    return Result([text("Loaded these tools; call them from your next step:\n" + "\n".join(lines))],
                  meta={"load": [t.name for t in found]})
