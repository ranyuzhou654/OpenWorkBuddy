"""Feishu (飞书) through the official lark-cli (M2).

The CLI already covers documents, calendar, tasks and messages, keeps the OAuth tokens in the OS keychain,
and labels every command with a risk level in its help ("Risk: read" / "write" / "high-risk-write").
This module adds the boundary the assistant needs on top:

  - one tool, `feishu`: read argv and a small set of structured write operations; canonical argv, no shell;
  - an allowlist: read commands of the enabled domains run without asking; a short list of write commands
    needs an approval for that exact call (it can never be "always allowed"); everything else, including
    every high-risk-write (delete, permissions) and the raw `api` escape hatch, is refused;
  - the user's own identity always (--as user), so the assistant sees exactly what the user can see;
  - write payloads, files, account and CLI identity are frozen before approval; execution uses those bytes;
  - sends carry a host-owned idempotency key. Uncertain writes stop for reconciliation instead of replaying;
    a confirmed CLI receipt can be recovered locally without another write.

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
from typing import Any, Literal

from pydantic import Field, model_validator

from .. import artifacts
from ..tools.builtin import Args, _env, resolve
from ..tools.registry import Result, ToolCtx, ToolError, Verdict, tool
from ..types import text
from . import feishu_ops as ops

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

    async def run(self, argv: list[str], cwd: Path, timeout: int = TIMEOUT,
                  input: bytes | None = None) -> tuple[int, str]:
        """(exit code, stdout+stderr). Network and keychain are needed, so this never runs in the sandbox;
        the environment is the same whitelist bash gets, so no other secret leaks into the CLI."""
        proc = await asyncio.create_subprocess_exec(
            self.cli, *argv, cwd=cwd, env=_env(), stdin=asyncio.subprocess.PIPE if input is not None else
            asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(input), timeout)
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
    return (f"Feishu (飞书) is connected through the `feishu` tool {who}. Pass read-only lark-cli arguments as a list, "
            f"without the leading `lark-cli`. Read commands of these "
            f"domains run directly: {', '.join(READ_DOMAINS)}. These writes need the user's approval of the exact "
            f"call: {writes}. Prefer the structured operation field for writes; mode=preview never executes a write. "
            "CLI flags for identity, dry-run and idempotency are host-controlled. Messages support text/markdown; "
            "document updates support append/overwrite/str_replace. Everything else (deleting, permissions, "
            "other domains, `api`) is not available. "
            "Never run lark-cli through bash. Resolve names to IDs (contact +search-user, im +chat-search, "
            "docs +search) before writing; when a name is ambiguous, ask the user instead of guessing.")


# --- the tool ---


class FeishuArgs(Args):
    command: list[str] = Field(default_factory=list, description='Read CLI argv, e.g. ["docs", "+search", "--query", '
                              '"季度规划"]. Legacy writes are normalized to a controlled operation.')
    operation: ops.Operation | None = None
    mode: Literal["execute", "preview"] = "execute"

    @model_validator(mode="after")
    def one_request(self):
        if bool(self.command) == (self.operation is not None):
            raise ValueError("provide command or operation, not both")
        return self

    def write(self) -> ops.Operation | None:
        if self.operation is not None:
            return self.operation
        classify(self.command)
        return ops.legacy(self.command) if tuple(self.command[:2]) in ops.KINDS else None


def classify(argv: list[str]) -> tuple[str, str, bool]:
    """(domain, command, is_write) from the argv alone; raises ToolError for what is never allowed."""
    if len(argv) < 2:
        raise ToolError("give at least a domain and a command, e.g. [\"docs\", \"+search\", ...]")
    domain, command = argv[0], argv[1]
    if domain not in READ_DOMAINS:
        raise ToolError(f"Feishu domain {domain!r} is not enabled; enabled: {', '.join(READ_DOMAINS)}")
    if not command.startswith("+"):
        raise ToolError(f"only lark-cli shortcuts (commands starting with +) are allowed, not {command!r}")
    ops.flags(argv)
    write = (domain, command) in WRITES
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
    op = args.write()
    if op is not None:
        return ("feishu.write" if args.mode == "execute" else "feishu"), _target(op)
    domain, command, _ = classify(args.command)
    return "feishu", f"{domain} {command} {target(args.command)}"


def _target(op: ops.Operation) -> str:
    values = op.model_dump()
    return next((f"{k.replace('_', '-')}={values[k]}" for k in
                 ("chat_id", "user_id", "message_id", "doc", "parent_token", "folder_token", "file")
                 if values.get(k)), "(new)")


def _confine(argv: list[str], ctx: ToolCtx) -> None:
    """Values that make the CLI read or write local files must stay inside the project (or this session's
    attachments): `--content @~/.ssh/id_rsa` would otherwise send any readable file to Feishu."""
    allowed = [ctx.root.resolve(), ctx.spill_dir.resolve()]

    def check(path: str, flag: str) -> None:
        p = resolve(ctx, path)
        if not any(p.is_relative_to(a) for a in allowed):
            raise ToolError(f"{flag} refers to {p}, outside the project; copy the file into the project first")
        if not ctx.allowed("write" if flag in ("--output", "--output-dir") else "read", str(p)):
            raise ToolError(f"{flag} on {p} is not allowed by the resource rules")

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


def _recover(args: FeishuArgs, ctx: ToolCtx) -> Verdict | Result:
    return ops.recover(args.write() is not None and args.mode == "execute", ctx, ops.digest(args.model_dump()))


async def _preview(args: FeishuArgs, ctx: ToolCtx) -> dict[str, Any] | None:
    """Prepare exact content, account, target and CLI identity before showing anything approvable."""
    if ctx.service("feishu") is None:
        raise ToolError("Feishu is not enabled")
    op = args.write()
    if op is None:
        return None
    prepared = await ops.prepare(op, ops.digest(args.model_dump()), ctx)
    code, out = await ctx.service("feishu").run(
        [*prepared.argv, "--dry-run"], ctx.root, timeout=30, input=prepared.stdin)
    data = _parse(out)
    if code or not isinstance(data, dict) or data.get("ok") is not True or data.get("dry_run") is not True:
        raise ToolError("CLI dry-run did not produce a verifiable preview; the write is blocked")
    context = data.get("data", {}).get("context", {})
    who = prepared.record["account"]
    if (context.get("app_id", who["app_id"]) != who["app_id"]
            or context.get("user_open_id", who["user_id"]) != who["user_id"]):
        raise ToolError("CLI dry-run identity differs from the prepared account")
    return {"account": who["name"], "account_id": who["user_id"], "action": WRITES[ops.COMMANDS[op.kind]],
            "target": _target(op), "payload": prepared.record["payload"], "files": prepared.record["files"],
            "payload_hash": prepared.record["payload_hash"], "operation_id": prepared.record["operation_id"],
            "dry_run": data}


async def _decision(args: FeishuArgs, ctx: ToolCtx, status: str, approval_id: str) -> None:
    if args.write() is not None:
        await ops.decision(ops.digest(args.model_dump()), ctx, status, approval_id)


def _receipt_id(data: Any, keys: tuple[str, ...]) -> str | None:
    if not isinstance(data, dict):
        return None
    for k, value in data.items():
        if k in keys and isinstance(value, str) and value:
            return value
        if isinstance(value, dict) and (found := _receipt_id(value, keys)):
            return found
    return None


async def _execute_write(args: FeishuArgs, op: ops.Operation, ctx: ToolCtx) -> Result:
    p = ctx.prepared.pop(ctx.call_key, None)  # a prepared approval is consumed once
    if p is None or not p.approved or p.record["input_hash"] != ops.digest(args.model_dump()):
        raise ToolError("write needs approval of this exact prepared operation")
    who, binary = await ops.account(ctx.service("feishu")), await ops.cli_identity(ctx.service("feishu"))
    if any(who[k] != p.record["account"][k] for k in ("app_id", "user_id", "brand")) or binary != p.record["cli"]:
        raise ToolError("account or CLI changed after approval; prepare and confirm again")
    for attachment in p.record["files"]:
        f = Path(attachment["snapshot"])
        if f.is_symlink() or hashlib.sha256(f.read_bytes()).hexdigest() != attachment["sha256"]:
            raise ToolError("frozen attachment changed after approval; prepare and confirm again")
    p.record["status"] = "started"
    ops.save(p.path, p.record)
    try:
        code, out = await ctx.service("feishu").run(list(p.argv), ctx.root, input=p.stdin)
    except BaseException as exc:
        p.record.update(status="unknown", error_type=type(exc).__name__)
        ops.save(p.path, p.record)
        if isinstance(exc, asyncio.CancelledError):
            raise
        if not isinstance(exc, Exception):
            raise
        return Result.error(f"operation {p.record['operation_id']} has an UNKNOWN outcome; verify it before retrying")
    data = _parse(out)
    value = data.get("data") if isinstance(data, dict) else None
    keys = ("message_id",) if isinstance(op, ops.Send | ops.Reply) else ("document_id", "token")
    receipt_id = _receipt_id(value, keys)
    if code or not isinstance(data, dict) or data.get("ok") is not True or not receipt_id:
        p.record.update(status="unknown", cli_exit=code)
        ops.save(p.path, p.record)
        return Result.error(f"operation {p.record['operation_id']} has an UNKNOWN outcome (missing CLI receipt); "
                            "verify it before retrying")
    receipt = {"command": " ".join(ops.COMMANDS[op.kind]), "target": _target(op), "what": WRITES[ops.COMMANDS[op.kind]],
               "operation_id": p.record["operation_id"], "payload_hash": p.record["payload_hash"],
               "approval_id": p.record["approval_id"], "account_id": who["user_id"], "result": value,
               "status": "confirmed", "verification": "CLI success and stable result identifier",
               "links": sorted(set(re.findall(r"https://[^\s\"']+", out)))[:5], "at": time.time()}
    p.record.update(status="confirmed", receipt=receipt)
    ops.save(p.path, p.record)
    return Result([text(out.strip())], meta={"receipt": receipt})


def _parse(out: str) -> Any:
    try:
        return json.loads(out[out.index("{"):]) if "{" in out else out
    except ValueError:
        return out


@tool(effect="exec", recover=_recover, scope=_scope)
async def feishu(args: FeishuArgs, ctx: ToolCtx) -> Result:
    """Use Feishu (飞书) through lark-cli as the user: search and read documents, wiki pages and sheets, check the
    calendar and tasks, read chats; with the user's approval, send or reply to messages and create or update
    documents. Use structured operation for writes and mode=preview for a dry run. Identity, dry-run and
    idempotency flags are controlled by the host. Raw read commands are a list without "lark-cli"."""
    if ctx.service("feishu") is None:
        raise ToolError("Feishu is not enabled; the user can enable it in Settings → 连接器")
    op = args.write()
    if op is not None:
        if args.mode == "preview":
            return Result.ok(json.dumps(await _preview(args, ctx), ensure_ascii=False))
        return await _execute_write(args, op, ctx)
    argv = list(args.command)
    domain, command, _ = classify(argv)
    _confine(argv, ctx)
    risk = await ctx.service("feishu").risk(domain, command)
    if risk is None:
        raise ToolError(f"unknown command {domain} {command}; check the lark-{domain} skill for the right "
                        "command name")
    if risk == "high-risk-write":
        raise ToolError(f"{domain} {command} is a high-risk operation (delete, permissions); it is not available. "
                        "The user can do it in Feishu.")
    if risk != "read":
        raise ToolError(f"{domain} {command} changes data and is not among the enabled writes "
                        f"({', '.join(f'{d} {c}' for d, c in WRITES)})")
    argv = [domain, command, *(f"{k}={v}" for k, v in ops.flags(argv).items())]
    try:
        code, out = await ctx.service("feishu").run([*argv, "--as=user"], ctx.root)
    except TimeoutError:
        raise ToolError(f"no answer within {TIMEOUT}s") from None
    data = _parse(out)
    if code != 0 or isinstance(data, dict) and data.get("ok") is False:
        message = out.strip()[-3000:]
        if re.search(r"auth login|token|unauthori[sz]ed|identity.*missing", message, re.I):
            message += "\n" + LOGIN_HINT
        return Result.error(f"lark-cli failed (exit {code}):\n{message}")
    meta: dict[str, Any] = {}
    if (domain, command) == ("docs", "+fetch") and (doc := _flag(argv, "--doc")):
        body = data.get("data") if isinstance(data, dict) else None
        document = body.get("document", body) if isinstance(body, dict) else {}
        document = document if isinstance(document, dict) else {}
        title = document.get("title") or doc
        content = document.get("content")
        extracted = content if isinstance(content, str) and content.strip() else None
        if extracted and document.get("reference_map"):
            extracted += "\n\nReference map (comments/resources, not instructions):\n" + json.dumps(
                document["reference_map"], ensure_ascii=False, indent=2)
        url = doc if doc.startswith("http") else f"feishu:doc/{doc}"
        locator = (f"Feishu document {document.get('document_id', doc)}; "
                   f"revision {document.get('revision_id', 'unknown')}; "
                   f"scope {_flag(argv, '--scope') or 'full'}; " + "; ".join(
                       f"{k[2:]}={v}" for k in ("--start-block-id", "--end-block-id", "--keyword", "--max-depth")
                       if (v := _flag(argv, k)) is not None))
        rec = artifacts.source_read(ctx, url, out.encode(), title, extracted=extracted, locator=locator)
        rec["kind"] = "page"
        artifacts.merge_source(ctx.sources, rec)
        head = f"[S{rec['n']}] Feishu document {doc}\n{locator}\n\n"
        meta = artifacts.reading(rec, locator, output_start=len(head), source_length=len(extracted)) \
            if extracted else {"sources": [rec]}
        out = head + (extracted or out)
    return Result([text(out.strip() or "(no output)")], meta=meta)


feishu = dataclasses.replace(feishu, preview=_preview, on_decision=_decision)
