"""A browser for the assistant through the official Playwright MCP server (M2).

Not a new browser engine: the existing MCP plumbing starts `@playwright/mcp` (version pinned) as the built-in
server "browser". What this module adds is the boundary around it:

  - its own profile (~/.nwb/browser/profile): the user logs in there by hand once where needed; the
    everyday Chrome profile and its cookies are never touched;
  - permissions by what a tool does: reading the page runs freely, opening a URL is approved per site
    (like web_fetch), clicking, typing, uploading and running page scripts are approved per call, and
    browser_run_code_unsafe (arbitrary code in the browser) is not offered at all;
  - one owner at a time: the browser is shared by the process, so a session that is using it holds it
    until its run ends, and another session gets a clear "in use" error instead of fighting over tabs.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

from ..config import McpServer
from ..tools.registry import AnyArgs, Result, Tool, ToolCtx, ToolError

PACKAGE = "@playwright/mcp@0.0.83"
NAME = "browser"
HIDDEN = {"browser_run_code_unsafe"}  # arbitrary code with the browser's access: never offered


def spec(home: Path, headless: bool = False) -> McpServer:
    """The built-in server. Unsandboxed: a browser needs the network, a window and its own profile folder."""
    folder = home / "browser"
    args = ["-y", PACKAGE, "--user-data-dir", str(folder / "profile"), "--output-dir", str(folder / "output"),
            "--image-responses", "omit"]  # screenshots only when asked: every step's image costs time and tokens
    if headless:
        args.append("--headless")
    return McpServer(NAME, "npx", tuple(args), sandbox=False, exposure="auto", timeout=180,
                     description="A web browser with its own profile; the user may have logged in to sites there.")


class Lease:
    """Which session may drive the browser now. Taken by the first browser call of a run, released when
    that run ends; a call from another session meanwhile is refused."""

    def __init__(self) -> None:
        self.owner: str | None = None

    def take(self, session_id: str, running: set[str]) -> None:
        if self.owner and self.owner != session_id and self.owner in running:
            raise ToolError(f"the browser is being used by another session ({self.owner}); try again when that "
                            "task has finished, or tell the user")
        self.owner = session_id

    def release(self, session_id: str) -> None:
        if self.owner == session_id:
            self.owner = None


def wrap(tools: list[Tool], lease: Lease, running: set[str]) -> list[Tool]:
    """The server's tools with the lease check, minus hidden ones; navigation is checked per site."""
    out = []
    for t in tools:
        if t.name.endswith(tuple(f"__{h}" for h in HIDDEN)):
            continue

        def leased(fn: Any) -> Any:
            async def call(args: AnyArgs, ctx: ToolCtx) -> Result:
                lease.take(ctx.session_id, running)
                return await fn(args, ctx)

            return call

        scope = t.scope
        if t.name.endswith("__browser_navigate"):
            name = t.name

            def scope(args: AnyArgs, ctx: ToolCtx, name: str = name) -> tuple[str, str]:
                return name, str((args.model_extra or {}).get("url", ""))  # "always allow" = this site

        out.append(dataclasses.replace(t, fn=leased(t.fn), scope=scope))
    return out


NOTICE = ("A web browser is available through the mcp__browser__* tools. It has its own profile: sites the user "
          "logged in to there stay logged in, other sites are not. Read pages with browser_snapshot (cheaper than "
          "screenshots). Opening a site, clicking, typing, uploading and running page scripts ask the user first. "
          "If a site needs a login, a CAPTCHA or two-factor confirmation, stop and ask the user to do it in the "
          "browser window (ask_user), then continue; never try to get past them yourself. Prefer web_fetch for "
          "plain reading of public pages; use the browser for pages that need a login or interaction.")
