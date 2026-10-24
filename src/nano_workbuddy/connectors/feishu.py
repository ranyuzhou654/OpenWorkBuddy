"""Feishu (飞书) through the official lark-cli (M2).

The CLI already covers documents, calendar, tasks and messages, keeps the OAuth tokens in the OS keychain,
and labels every command with a risk level in its help ("Risk: read" / "write" / "high-risk-write").
This module adds the boundary the assistant needs on top:

  - one tool, `feishu`, whose argument is the CLI's own argv (so the official lark skills' examples apply
    directly), run with a fixed argv and never through a shell;
  - an allowlist: read commands of the enabled domains run without asking; a short list of write commands
    needs an approval for that exact call (it can never be "always allowed"); everything else, including
    every high-risk-write (delete, permissions) and the raw `api` escape hatch, is refused;
  - the user's own identity always (--as user), so the assistant sees exactly what the user can see;
  - values that make the CLI read local files (@file, --file ...) must stay inside the project;
  - message sends carry an idempotency key derived from the call, so a replay after a crash does not
    send twice; every write's result is kept as a receipt in the tool result's meta.

Tokens never reach the model, the log or a sandboxed shell: the CLI reads them from the keychain itself,
and bash runs sandboxed without network or keychain access.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import json
import os
import re
import shutil
import signal
import time
from pathlib import Path
from typing import Any

from pydantic import Field

from .. import artifacts
from ..tools.builtin import Args, _env, resolve
from ..tools.registry import Result, ToolCtx, ToolError, Verdict, tool
from ..types import text

# What the user enabled (2026-10-24): documents (search, read, create, update), calendar and tasks (read),
# messages (read, send, reply), plus the lookups those need (contacts, wiki, sheets, drive search/download).
READ_DOMAINS = ("docs", "drive", "wiki", "sheets", "calendar", "task", "im", "contact")
WRITES = {
    ("im", "+messages-send"): "send a message",
    ("im", "+messages-reply"): "reply to a message",
    ("docs", "+create"): "create a document",
    ("docs", "+update"): "change a document",
    ("drive", "+import"): "import a local file as a cloud document",
}
IDEMPOTENT = {("im", "+messages-send"), ("im", "+messages-reply")}  # take --idempotency-key
FORBIDDEN_FLAGS = ("--as", "--profile")  # identity is fixed; profiles would switch app or account
PATH_FLAGS = ("--file", "--image", "--audio", "--video", "--video-cover", "--output", "--output-dir")
TARGET_FLAGS = ("--chat-id", "--user-id", "--message-id", "--doc", "--parent-token", "--folder-token", "--file")
TIMEOUT = 120
LOGIN_HINT = ("The user's Feishu authorisation is missing or expired. Ask the user to run `lark-cli auth login` "
              "in a terminal (it opens Feishu to approve), then try again. Do not try to log in yourself.")


class Feishu:
    """Runs lark-cli. One per process; what it learns about commands (risk labels) and the account is cached."""

    def __init__(self, cli: str = "lark-cli"):
        self.cli = shutil.which(cli) or cli
        self._risk: dict[tuple[str, str], str | None] = {}
        self._status: tuple[float, dict[str, Any]] | None = None

    @property
    def available(self) -> bool:
        return Path(self.cli).is_file() and os.access(self.cli, os.X_OK)

    async def run(self, argv: list[str], cwd: Path, timeout: int = TIMEOUT) -> tuple[int, str]:
        """(exit code, stdout+stderr). Network and keychain are needed, so this never runs in the sandbox;
        the environment is the same whitelist bash gets, so no other secret leaks into the CLI."""
        proc = await asyncio.create_subprocess_exec(
            self.cli, *argv, cwd=cwd, env=_env(), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except (TimeoutError, asyncio.CancelledError):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()
            raise
        return proc.returncode or 0, out.decode(errors="replace")

    async def risk(self, domain: str, command: str) -> str | None:
        """The CLI's own risk label for a command ("read", "write", "high-risk-write"), or None if it has none
        (unknown commands, raw resource operations)."""
        key = (domain, command)
        if key not in self._risk:
            try:
                _, out = await self.run([domain, command, "--help"], Path.home(), timeout=30)
            except TimeoutError:
                return None
            m = re.search(r"^Risk:\s*([a-z-]+)", out, re.M)
            self._risk[key] = m.group(1) if m else None
        return self._risk[key]

    async def status(self, max_age: float = 60) -> dict[str, Any]:
        """`lark-cli auth status`: the app and whether the user identity is logged in."""
        if self._status and time.monotonic() - self._status[0] < max_age:
            return self._status[1]
        try:
            code, out = await self.run(["auth", "status"], Path.home(), timeout=30)
            data = json.loads(out[out.index("{"):]) if "{" in out else {"error": out.strip()[:300]}
        except (TimeoutError, ValueError, OSError) as e:
            data = {"error": f"{type(e).__name__}: {e}"}
        self._status = (time.monotonic(), data)
        return data

    async def version(self) -> str:
        try:
            _, out = await self.run(["--version"], Path.home(), timeout=15)
        except (TimeoutError, OSError):
            return ""
        return out.strip().rsplit(" ", 1)[-1]


def user_identity(status: dict[str, Any]) -> dict[str, Any]:
    return status.get("identities", {}).get("user", {})


def notice(status: dict[str, Any]) -> str:
    """What the model is told about the connection, when the tool is offered."""
    user = user_identity(status)
    who = (f"as {user.get('userName') or 'the user'} (the user's own identity)" if user.get("available")
           else "but the user's authorisation is missing: calls will fail until the user runs "
                "`lark-cli auth login` in a terminal")
    writes = "; ".join(f"{d} {c} ({what})" for (d, c), what in WRITES.items())
    return (f"Feishu (飞书) is connected through the `feishu` tool {who}. Pass lark-cli arguments as a list, "
            f"exactly as the lark-* skills show them without the leading `lark-cli`. Read commands of these "
            f"domains run directly: {', '.join(READ_DOMAINS)}. These writes need the user's approval of the exact "
            f"call: {writes}. Everything else (deleting, permissions, other domains, `api`) is not available. "
            "Never run lark-cli through bash. Resolve names to IDs (contact +search-user, im +chat-search, "
            "docs +search) before writing; when a name is ambiguous, ask the user instead of guessing.")


# --- the tool ---


class FeishuArgs(Args):
    command: list[str] = Field(description='lark-cli arguments, e.g. ["docs", "+search", "--query", "季度规划"] or '
                                           '["im", "+messages-send", "--chat-id", "oc_…", "--markdown", "…"]')


def classify(argv: list[str]) -> tuple[str, str, bool]:
    """(domain, command, is_write) from the argv alone; raises ToolError for what is never allowed."""
    if len(argv) < 2:
        raise ToolError("give at least a domain and a command, e.g. [\"docs\", \"+search\", ...]")
    domain, command = argv[0], argv[1]
    if domain not in READ_DOMAINS:
        raise ToolError(f"Feishu domain {domain!r} is not enabled; enabled: {', '.join(READ_DOMAINS)}")
    if not command.startswith("+"):
        raise ToolError(f"only lark-cli shortcuts (commands starting with +) are allowed, not {command!r}")
    for flag in argv[2:]:
        name = flag.split("=", 1)[0]
        if name in FORBIDDEN_FLAGS:
            raise ToolError(f"{name} cannot be set: the connector always acts as the user, with the configured app")
    write = (domain, command) in WRITES and "--dry-run" not in argv
    return domain, command, write


def _flag(argv: list[str], name: str) -> str | None:
    for i, a in enumerate(argv):
        if a == name and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None


def target(argv: list[str]) -> str:
    """The object a write acts on, for the approval card and the permission rule's resource."""
    return next((f"{f[2:]}={v}" for f in TARGET_FLAGS if (v := _flag(argv, f))), "(new)")


def _scope(args: FeishuArgs, ctx: ToolCtx) -> tuple[str, str]:
    try:
        domain, command, write = classify(args.command)
    except ToolError:
        return "feishu", "invalid"  # execute() reports why
    return ("feishu.write" if write else "feishu"), f"{domain} {command} {target(args.command)}"


def _confine(argv: list[str], ctx: ToolCtx) -> None:
    """Values that make the CLI read or write local files must stay inside the project (or this session's
    attachments): `--content @~/.ssh/id_rsa` would otherwise send any readable file to Feishu."""
    allowed = [ctx.root.resolve(), ctx.spill_dir.resolve()]

    def check(path: str, flag: str) -> None:
        p = resolve(ctx, path)
        if not any(p.is_relative_to(a) for a in allowed):
            raise ToolError(f"{flag} refers to {p}, outside the project; copy the file into the project first")

    for i, a in enumerate(argv[2:], 2):
        value = argv[i + 1] if a.startswith("--") and "=" not in a and i + 1 < len(argv) else None
        if "=" in a and a.startswith("--"):
            a, value = a.split("=", 1)
        if value is None:
            continue
        if value == "-":
            raise ToolError(f"{a} cannot read standard input here; pass the value directly or as @file")
        if value.startswith("@"):
            check(value[1:], a)
        elif a in PATH_FLAGS and not re.match(r"^(https?://|file_|img_)", value):
            check(value, a)


def _recover(args: FeishuArgs, ctx: ToolCtx) -> Verdict:
    """After a crash mid-call: reads and idempotent sends (same key) are safe to run again; any other write
    has an unknown outcome and is never replayed automatically."""
    try:
        domain, command, write = classify(args.command)
    except ToolError:
        return "not_done"
    return "not_done" if not write or (domain, command) in IDEMPOTENT else "unknown"


async def _preview(args: FeishuArgs, ctx: ToolCtx) -> dict[str, Any] | None:
    """For the approval card: the account, the target and the CLI's own dry run of the exact request."""
    if ctx.feishu is None:
        return None
    argv = list(args.command)
    try:
        classify(argv)
        _confine(argv, ctx)
        code, out = await ctx.feishu.run([*argv, "--as", "user", "--dry-run"], ctx.root, timeout=30)
    except (ToolError, TimeoutError, OSError) as e:
        return {"error": str(e)}
    user = user_identity(await ctx.feishu.status())
    return {"account": user.get("userName") or user.get("openId") or "?", "action": WRITES.get(tuple(argv[:2])),
            "target": target(argv), "dry_run": _parse(out) if code == 0 else out[-2000:]}


def _parse(out: str) -> Any:
    try:
        return json.loads(out[out.index("{"):]) if "{" in out else out
    except ValueError:
        return out


def _key(ctx: ToolCtx, argv: list[str]) -> str:
    """Stable for one tool call (also after a crash and replay), different for every other call."""
    seed = f"{ctx.session_id}\0{ctx.call_key or ctx.call_id}\0{json.dumps(argv)}"
    return hashlib.sha256(seed.encode()).hexdigest()[:40]


@tool(effect="exec", recover=_recover, scope=_scope)
async def feishu(args: FeishuArgs, ctx: ToolCtx) -> Result:
    """Use Feishu (飞书) through lark-cli as the user: search and read documents, wiki pages and sheets, check the
    calendar and tasks, read chats; with the user's approval, send or reply to messages and create or update
    documents. Pass the CLI arguments as a list (without "lark-cli"); add "--dry-run" to preview a write."""
    if ctx.feishu is None:
        raise ToolError("Feishu is not enabled; the user can enable it in Settings → 连接器")
    argv = list(args.command)
    domain, command, write = classify(argv)
    _confine(argv, ctx)
    risk = await ctx.feishu.risk(domain, command)
    if risk is None:
        raise ToolError(f"unknown command {domain} {command}; check the lark-{domain} skill for the right "
                        "command name")
    if risk == "high-risk-write":
        raise ToolError(f"{domain} {command} is a high-risk operation (delete, permissions); it is not available. "
                        "The user can do it in Feishu.")
    if risk != "read" and (domain, command) not in WRITES:
        raise ToolError(f"{domain} {command} changes data and is not among the enabled writes "
                        f"({', '.join(f'{d} {c}' for d, c in WRITES)})")
    if (domain, command) in IDEMPOTENT and write and _flag(argv, "--idempotency-key") is None:
        argv += ["--idempotency-key", _key(ctx, argv)]
    try:
        code, out = await ctx.feishu.run([*argv, "--as", "user"], ctx.root)
    except TimeoutError:
        if write:
            raise ToolError(f"no answer within {TIMEOUT}s. The outcome is UNKNOWN: check in Feishu (or with a read "
                            "command) before trying again; do not simply repeat the call") from None
        raise ToolError(f"no answer within {TIMEOUT}s") from None
    data = _parse(out)
    if code != 0 or isinstance(data, dict) and data.get("ok") is False:
        message = out.strip()[-3000:]
        if re.search(r"auth login|token|unauthori[sz]ed|identity.*missing", message, re.I):
            message += "\n" + LOGIN_HINT
        return Result.error(f"lark-cli failed (exit {code}):\n{message}")
    meta: dict[str, Any] = {}
    if write:
        meta["receipt"] = {"command": f"{domain} {command}", "target": target(argv), "what": WRITES[(domain, command)],
                           "result": data.get("data", data) if isinstance(data, dict) else str(data)[:2000],
                           "links": sorted(set(re.findall(r"https://[^\s\"']+(?:feishu|larksuite)[^\s\"']*", out)))[:5],
                           "at": time.time()}
    elif (domain, command) == ("docs", "+fetch") and (doc := _flag(argv, "--doc")):
        body = data.get("data") if isinstance(data, dict) else None
        title = body.get("title", "") if isinstance(body, dict) else ""
        url = doc if doc.startswith("http") else f"feishu:doc/{doc}"
        rec = artifacts.cite(ctx.sources, url, "page", title or doc,
                             retrieved_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        meta["sources"] = [rec]
        out = f"[S{rec['n']}] Feishu document {doc}\n\n{out}"
    return Result([text(out.strip() or "(no output)")], meta=meta)


feishu = dataclasses.replace(feishu, preview=_preview)
