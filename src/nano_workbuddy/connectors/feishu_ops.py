"""Five controlled Feishu writes. No model-controlled CLI flags enter execution.

Text is frozen in memory and passed on stdin; imported bytes use a private snapshot.
The small operation journal is local to the session. It is not an exactly-once protocol.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, TypeAdapter, model_validator

from ..tools.builtin import Args, atomic_write, resolve
from ..tools.registry import Result, ToolCtx, ToolError, Verdict


class Body(Args):
    content: str = ""
    content_file: str | None = None
    format: Literal["text", "markdown", "xml"] = "markdown"

    @model_validator(mode="after")
    def one_body(self):
        if self.content_file is not None and self.content:
            raise ValueError("use content or content_file, not both")
        return self


class Send(Body):
    kind: Literal["send_message"]
    chat_id: str = ""
    user_id: str = ""

    @model_validator(mode="after")
    def recipient(self):
        if bool(self.chat_id) == bool(self.user_id):
            raise ValueError("set exactly one of chat_id and user_id")
        if self.format == "xml":
            raise ValueError("messages support text or markdown")
        return self


class Reply(Body):
    kind: Literal["reply_message"]
    message_id: str = Field(min_length=1)
    reply_in_thread: bool = False

    @model_validator(mode="after")
    def message_format(self):
        if self.format == "xml":
            raise ValueError("messages support text or markdown")
        return self


class Create(Body):
    kind: Literal["create_document"]
    title: str = ""
    parent_token: str = ""
    parent_position: str = ""

    @model_validator(mode="after")
    def document(self):
        if self.format == "text" or self.parent_token and self.parent_position:
            raise ValueError("documents use markdown/xml; parent_token and parent_position are exclusive")
        return self


class Update(Body):
    kind: Literal["update_document"]
    doc: str = Field(min_length=1)
    command: Literal["append", "overwrite", "str_replace"] = "append"
    pattern: str = ""
    revision_id: int = Field(-1, ge=-1)

    @model_validator(mode="after")
    def document(self):
        if self.format == "text" or self.command == "str_replace" and not self.pattern:
            raise ValueError("documents use markdown/xml; str_replace requires pattern")
        return self


class Import(Args):
    kind: Literal["import_document"]
    file: str = Field(min_length=1)
    type: Literal["docx", "sheet", "bitable", "slides"]
    name: str = ""
    folder_token: str = ""
    target_token: str = ""

    @model_validator(mode="after")
    def target(self):
        if self.target_token and self.type != "bitable":
            raise ValueError("target_token applies only to bitable")
        return self


type Operation = Annotated[Send | Reply | Create | Update | Import, Field(discriminator="kind")]
ADAPTER = TypeAdapter(Operation)
MODELS = {"send_message": Send, "reply_message": Reply, "create_document": Create,
          "update_document": Update, "import_document": Import}
COMMANDS = {"send_message": ("im", "+messages-send"), "reply_message": ("im", "+messages-reply"),
            "create_document": ("docs", "+create"), "update_document": ("docs", "+update"),
            "import_document": ("drive", "+import")}
KINDS = {v: k for k, v in COMMANDS.items()}
HOST_FLAGS = {"--as", "--profile", "--dry-run", "--idempotency-key", "--format", "--json", "--jq", "-q"}


def flags(argv: list[str]) -> dict[str, str]:
    """Normalize legacy argv once. Reject duplicates, positionals and host-owned switches."""
    out, i = {}, 2
    while i < len(argv):
        key, sep, value = argv[i].partition("=")
        if key in HOST_FLAGS:
            raise ToolError(f"{key} is host-controlled; use mode=preview for a dry run")
        if not key.startswith("--") or key == "--" or key in out:
            raise ToolError(f"invalid or repeated flag: {key}")
        if not sep:
            i += 1
            if i >= len(argv) or argv[i].startswith("--"):
                raise ToolError(f"missing value for {key}")
            value = argv[i]
        out[key] = value
        i += 1
    return out


def legacy(argv: list[str]) -> Operation:
    kind = KINDS[tuple(argv[:2])]
    values = flags(argv)
    body = {}
    if kind != "import_document":
        options = [f for f in ("--text", "--markdown", "--content") if f in values]
        if len(options) > 1:
            raise ToolError("choose one content format")
        flag = options[0] if options else "--content"
        content = values.pop(flag, "")
        body = {"content_file": content[1:]} if content.startswith("@") else {"content": content}
        body["format"] = values.pop("--doc-format", "xml") if kind.endswith("document") else (
            "markdown" if flag == "--markdown" else "text")
        if flag == "--content" and kind in ("send_message", "reply_message") and options:
            raise ToolError("raw message JSON is not supported; use text/markdown")
    allowed = set(MODELS[kind].model_fields) - {"kind", "content", "content_file", "format"}
    converted = {k[2:].replace("-", "_"): v for k, v in values.items()}
    if set(converted) - allowed:
        raise ToolError(f"unsupported flags for {kind}: {sorted(set(converted) - allowed)}")
    try:
        return ADAPTER.validate_python({"kind": kind, **body, **converted})
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def directory(ctx: ToolCtx) -> Path:
    return ctx.spill_dir / "feishu-operations"


def record_path(ctx: ToolCtx) -> Path:
    return directory(ctx) / ("op_" + digest([ctx.session_id, ctx.call_key])[:24] + ".json")


def save(path: Path, record: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    atomic_write(path, json.dumps(record, ensure_ascii=False, indent=2))
    path.chmod(0o600)


def read_local(ctx: ToolCtx, name: str) -> tuple[Path, bytes]:
    p = resolve(ctx, name)
    if not p.is_relative_to(ctx.root.resolve()) and not p.is_relative_to(ctx.spill_dir.resolve()):
        raise ToolError(f"{p} is outside the project and session attachments")
    if not ctx.can_read(p):
        raise ToolError(f"reading {p} is not allowed")
    if not p.is_file():
        raise ToolError(f"{p} is not a regular file")
    return p, p.read_bytes()


def snapshot(ctx: ToolCtx, data: bytes, suffix: str) -> Path:
    folder = directory(ctx) / "snapshots"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    p = folder / (hashlib.sha256(data).hexdigest() + suffix)
    try:
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
    except FileExistsError:
        if p.is_symlink() or p.read_bytes() != data:
            raise ToolError("frozen attachment snapshot was changed") from None
    else:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
    return p


async def account(cli) -> dict:
    status = await cli.status(max_age=0)
    user = status.get("identities", {}).get("user", {})
    if not user.get("available") or not user.get("openId") or not status.get("appId"):
        raise ToolError("cannot bind a write to a verified Feishu user/app; run lark-cli auth login")
    return {"app_id": status["appId"], "user_id": user["openId"], "brand": status.get("brand"),
            "name": user.get("userName") or user["openId"]}


async def cli_identity(cli) -> dict:
    p = Path(cli.cli).resolve()
    return {"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest(), "version": await cli.version()}


@dataclass
class Prepared:
    path: Path
    record: dict
    argv: tuple[str, ...]
    stdin: bytes | None
    approved: bool = False


async def prepare(op: Operation, args_hash: str, ctx: ToolCtx) -> Prepared:
    path = record_path(ctx)
    if path.is_file() and json.loads(path.read_bytes())["status"] == "confirmed":
        raise ToolError("this operation already has a confirmed receipt; recover it instead of overwriting it")
    cli = ctx.service("feishu")
    argv = [*COMMANDS[op.kind]]
    if await cli.risk(*argv) != "write":
        raise ToolError("the configured CLI does not recognize this controlled write as Risk: write")
    who, binary = await account(cli), await cli_identity(cli)
    payload = op.model_dump()
    files, stdin = [], None
    if isinstance(op, Import):
        source, data = read_local(ctx, op.file)
        frozen = snapshot(ctx, data, source.suffix)
        name = op.name or source.stem
        meta = {"name": source.name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
                "snapshot": str(frozen)}
        files.append(meta)
        payload.update(file={k: meta[k] for k in ("sha256", "bytes")}, name=name)
        argv += [f"--file={frozen}", f"--type={op.type}", f"--name={name}"]
        argv += [f"--{k.replace('_', '-')}={getattr(op, k)}" for k in ("folder_token", "target_token")
                 if getattr(op, k)]
    else:
        if op.content_file is not None:
            _, data = read_local(ctx, op.content_file)
            try:
                content = data.decode("utf-8")
            except UnicodeError as exc:
                raise ToolError("content_file must be UTF-8 text") from exc
        else:
            content, data = op.content, op.content.encode()
        if isinstance(op, Send | Reply) and not content:
            raise ToolError("messages need nonempty text or markdown")
        payload.pop("content_file")
        payload["content"] = content
        stdin = data
        body_flag = op.format if isinstance(op, Send | Reply) else "content"
        argv += [f"--{body_flag}=-"]
        if isinstance(op, Send):
            argv += [f"--chat-id={op.chat_id}"] if op.chat_id else [f"--user-id={op.user_id}"]
        elif isinstance(op, Reply):
            argv += [f"--message-id={op.message_id}", f"--reply-in-thread={str(op.reply_in_thread).lower()}"]
        elif isinstance(op, Create):
            argv += [f"--{k.replace('_', '-')}={getattr(op, k)}" for k in ("title", "parent_token", "parent_position")
                     if getattr(op, k)]
        else:
            argv += [f"--doc={op.doc}", f"--command={op.command}", f"--revision-id={op.revision_id}"]
            if op.pattern:
                argv += [f"--pattern={op.pattern}"]
        if not isinstance(op, Send | Reply):
            argv += [f"--doc-format={op.format}"]
        snapshot(ctx, data, ".txt")  # durable evidence; execution uses the immutable in-memory bytes on stdin
    payload_hash = digest({"account": {k: who[k] for k in ("app_id", "user_id", "brand")}, "payload": payload})
    for old in directory(ctx).glob("op_*.json"):
        previous = json.loads(old.read_bytes())
        if previous["payload_hash"] == payload_hash and previous["status"] in ("started", "unknown"):
            raise ToolError(f"operation {previous['operation_id']} has an UNKNOWN outcome; verify it before any retry")
    if op.kind in ("send_message", "reply_message"):
        argv += ["--idempotency-key=" + digest([ctx.session_id, ctx.call_key, payload_hash])[:40]]
    argv += ["--as=user"]
    record = {"operation_id": path.stem, "call_key": ctx.call_key, "input_hash": args_hash,
              "payload_hash": payload_hash, "payload": payload, "account": who, "cli": binary,
              "files": files, "argv": argv, "status": "prepared", "created_at": time.time(),
              "approval_id": None, "receipt": None}
    save(path, record)
    result = Prepared(path, record, tuple(argv), stdin)
    ctx.prepared[ctx.call_key] = result
    return result


async def decision(args_hash: str, ctx: ToolCtx, status: str, approval_id: str) -> None:
    prepared = ctx.prepared.get(ctx.call_key)
    if prepared is None or prepared.record["input_hash"] != args_hash:
        raise ToolError("approval does not match a prepared operation")
    prepared.approved = status == "approved"
    prepared.record.update(status=status, approval_id=approval_id)
    save(prepared.path, prepared.record)


def recover(is_write: bool, ctx: ToolCtx, args_hash: str) -> Verdict | Result:
    if not is_write:
        return "not_done"
    p = record_path(ctx)
    if p.is_file():
        record = json.loads(p.read_bytes())
        if record.get("input_hash") != args_hash:
            return "unknown"
        if record["status"] == "confirmed":
            return Result([{"type": "text", "text": "Recovered the recorded Feishu receipt."}],
                          meta={"receipt": record["receipt"]})
        if record["status"] in ("prepared", "approved", "denied", "expired"):
            return "not_done"  # no CLI call started; a fresh approval is still required
    return "unknown"  # no auto-replay, including message idempotency keys with unknown validity periods
