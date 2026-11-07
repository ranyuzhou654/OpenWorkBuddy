"""A browser for the assistant through the official Playwright MCP server (M2).

Not a new browser engine: the existing MCP plumbing starts `@playwright/mcp` (version pinned) as the built-in
server "browser". What this module adds is the boundary around it:

  - its own profile (~/.nwb/browser/profile): the user logs in there by hand once where needed; the
    everyday Chrome profile and its cookies are never touched;
  - permissions by what a tool does: reading the page runs freely, opening a URL is approved per site
    (like web_fetch); clicking and typing are approved per call. Scripts and local-file uploads are unavailable;
  - one owner at a time: the browser is shared by the process, so a session that is using it holds it
    until its run ends, and another session gets a clear "in use" error instead of fighting over tabs.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..config import McpServer
from ..tools.registry import AnyArgs, Result, Tool, ToolCtx, ToolError
from ..types import new_id

PACKAGE = "@playwright/mcp@0.0.83"
NAME = "browser"
HIDDEN = {"browser_run_code_unsafe", "browser_evaluate", "browser_set_config", "browser_set_storage_state",
          "browser_storage_state", "browser_file_upload"}  # uploads need a separate frozen-file approval path
READ = {"browser_" + n for n in ("snapshot", "find", "take_screenshot", "console_messages", "network_requests",
                                "network_request", "wait_for")}
INTERACT = {"browser_" + n for n in ("click", "type", "press_key", "fill_form", "select_option", "drag", "hover",
                                    "handle_dialog", "resize", "emulate_media", "close", "navigate_back")}
NAVIGATION = "mcp__browser__browser_navigate"
INTERACTION = "mcp__browser__interaction"


def navigate_url(value: str) -> str:
    if not isinstance(value, str):
        raise ToolError("browser URL must be a string")
    if value == "about:blank":
        return value
    u = urlsplit(value)
    if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password:
        raise ToolError("browser navigation requires an http(s) URL without embedded credentials")
    return value


def _output(filename: str, ctx: ToolCtx) -> Path:
    if not isinstance(filename, str):
        raise ToolError("browser filename must be a string")
    rel = Path(filename)
    root = ctx.spill_dir.resolve() / "browser-output"
    if root.is_symlink():
        raise ToolError("browser output folder cannot be a symbolic link")
    if rel.is_absolute() or ".." in rel.parts or not rel.name:
        raise ToolError("browser filename must be relative to this session's browser output folder")
    dest = (root / rel).resolve()
    if not dest.is_relative_to(root) or dest.exists():
        raise ToolError("browser output escapes the session folder or would overwrite an existing file")
    return dest


def scope(name: str, args: AnyArgs, ctx: ToolCtx) -> tuple[str, str]:
    values = args.model_extra or {}
    if values.get("filename") is not None:
        _output(values["filename"], ctx)
    if name == "browser_tabs":
        match values.get("action"):
            case "new":
                return NAVIGATION, navigate_url(values.get("url") or "about:blank")
            case "list":
                if values.get("url") is not None:
                    raise ToolError("tabs/list cannot contain a navigation URL")
                return "mcp__browser__browser_tabs", "list"
            case "close" | "select":
                if values.get("url") is not None:
                    raise ToolError("tabs/close or select cannot contain a navigation URL")
                return INTERACTION, name + ":" + values["action"]
            case _:
                raise ToolError("browser_tabs action must be list, new, close or select")
    if name == "browser_navigate":
        return NAVIGATION, navigate_url(values.get("url", ""))
    return ("mcp__browser__" + name, "") if name in READ else (INTERACTION, name)


def spec(home: Path, headless: bool = False) -> McpServer:
    """The built-in server. Unsandboxed: a browser needs the network, a window and its own profile folder."""
    folder = home / "browser"
    args = ["-y", PACKAGE, "--user-data-dir", str(folder / "profile"), "--output-dir", str(folder / "output"),
            "--image-responses", "omit"]  # screenshots only when asked: every step's image costs time and tokens
    if headless:
        args.append("--headless")
    return McpServer(NAME, "npx", tuple(args), sandbox=False, exposure="auto", timeout=180,
                     description="A web browser with its own profile; the user may have logged in to sites there.",
                     cwd=str(home.resolve()))  # MCP's workspace root must include controlled session output


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
        short = t.name.rsplit("__", 1)[-1]
        if short in HIDDEN or short not in READ | INTERACT | {"browser_navigate", "browser_tabs"}:
            continue

        def leased(fn: Any, name: str) -> Any:
            async def call(args: AnyArgs, ctx: ToolCtx) -> Result:
                scope(name, args, ctx)  # revalidate controlled paths even when called without a runtime gate
                lease.take(ctx.session_id, running)
                values = dict(args.model_extra or {})
                filename = values.get("filename")
                if name == "browser_take_screenshot" and not filename:
                    filename = new_id("page") + ".png"
                if filename:
                    dest = _output(filename, ctx)
                    dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    args = args.model_copy(update={"filename": str(dest)})
                return await fn(args, ctx)

            return call

        out.append(dataclasses.replace(t, fn=leased(t.fn, short), effect="read" if short in READ else "exec",
                                       parallel=False, recover=None,
                                       scope=lambda args, ctx, n=short: scope(n, args, ctx)))
    return out


NOTICE = ("A web browser is available through the mcp__browser__* tools. It has its own profile: sites the user "
          "logged in to there stay logged in, other sites are not. Read pages with browser_snapshot (cheaper than "
          "screenshots). Navigation and tabs/new share the same site permission. Interactions (including "
          "tabs/close/select and history back) require confirmation for each call, even in full trust. "
          "Scripts, local-file uploads, browser reconfiguration and cookie export are unavailable. "
          "Output filenames must be relative "
          "and are confined to this session's browser output folder. "
          "If a site needs a login, a CAPTCHA or two-factor confirmation, stop and ask the user to do it in the "
          "browser window (ask_user), then continue; never try to get past them yourself. Prefer web_fetch for "
          "plain reading of public pages; use the browser for pages that need a login or interaction.")
