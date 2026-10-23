"""read / write / edit / bash. Phase 1 runs with full user permissions; the sandbox arrives in phase 3."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import os
import signal
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .registry import Result, ToolCtx, ToolError, Verdict, tool

IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
               ".webp": "image/webp"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_LINE = 2000
MAX_OUTPUT = 10 * 1024 * 1024
ENV_ALLOW = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TERM", "TMPDIR", "TZ")


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


def resolve(ctx: ToolCtx, path: str) -> Path:
    p = Path(path).expanduser()
    return (p if p.is_absolute() else ctx.root / p).resolve()


def _check_fresh(ctx: ToolCtx, p: Path) -> None:
    """Writes to an existing file require an unchanged earlier read, so the model never edits blind."""
    stamp = ctx.read_stamps.get(str(p))
    if stamp is None:
        raise ToolError(f"read {p} before modifying it")
    if p.stat().st_mtime != stamp:
        raise ToolError(f"{p} changed since it was last read; read it again")


class ReadArgs(Args):
    path: str = Field(description="File path, absolute or relative to the project root")
    offset: int = Field(1, ge=1, description="First line to read (1-based)")
    limit: int = Field(2000, ge=1, le=5000, description="Maximum number of lines")


@tool(effect="read", parallel=True)
async def read(args: ReadArgs, ctx: ToolCtx) -> Result:
    """Read a text file with line numbers, or an image. Use offset/limit for large files."""
    p = resolve(ctx, args.path)
    if not p.exists():
        raise ToolError(f"{p} does not exist")
    if p.is_dir():
        raise ToolError(f"{p} is a directory; list it with bash")
    if mime := IMAGE_TYPES.get(p.suffix.lower()):
        if p.stat().st_size > MAX_IMAGE_BYTES:
            raise ToolError(f"image larger than {MAX_IMAGE_BYTES} bytes")
        data = base64.b64encode(p.read_bytes()).decode()
        return Result([{"type": "text", "text": f"Image {p}"}, {"type": "image", "mime": mime, "data": data}])
    raw = p.read_bytes()
    if b"\0" in raw[:8192]:
        raise ToolError(f"{p} looks binary; inspect it with bash")
    ctx.read_stamps[str(p)] = p.stat().st_mtime
    lines = raw.decode("utf-8", errors="replace").splitlines()
    if not lines:
        return Result.ok(f"{p} is empty")
    start, end = args.offset - 1, min(len(lines), args.offset - 1 + args.limit)
    body = "\n".join(f"{n:6}\t{line[:MAX_LINE]}" for n, line in enumerate(lines[start:end], start=args.offset))
    if end < len(lines) or start > 0:
        body += f"\n[lines {args.offset}-{end} of {len(lines)}; use offset to read more]"
    return Result.ok(body)


class WriteArgs(Args):
    path: str
    content: str


def _write_recover(args: WriteArgs, ctx: ToolCtx) -> Verdict:
    p = resolve(ctx, args.path)
    if p.is_file() and p.read_bytes() == args.content.encode():
        return "done"
    return "not_done"


@tool(effect="write", recover=_write_recover)
async def write(args: WriteArgs, ctx: ToolCtx) -> Result:
    """Create or overwrite a file. An existing file must be read first. Prefer edit for changes."""
    p = resolve(ctx, args.path)
    if p.exists():
        _check_fresh(ctx, p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(args.content, encoding="utf-8")
    ctx.read_stamps[str(p)] = p.stat().st_mtime
    return Result.ok(f"Wrote {len(args.content.encode())} bytes to {p}")


class EditArgs(Args):
    path: str
    old_string: str = Field(description="Exact text to replace, including indentation")
    new_string: str
    replace_all: bool = False


def _edit_recover(args: EditArgs, ctx: ToolCtx) -> Verdict:
    p = resolve(ctx, args.path)
    if not p.is_file():
        return "unknown"
    s = p.read_text(encoding="utf-8")
    if args.old_string not in s and args.new_string in s:
        return "done"
    return "not_done" if s.count(args.old_string) == 1 or args.replace_all else "unknown"


@tool(effect="write", recover=_edit_recover)
async def edit(args: EditArgs, ctx: ToolCtx) -> Result:
    """Replace an exact string in a file. old_string must match exactly once unless replace_all is set.
    Read the file first."""
    p = resolve(ctx, args.path)
    if not p.is_file():
        raise ToolError(f"{p} does not exist")
    _check_fresh(ctx, p)
    if args.old_string == args.new_string:
        raise ToolError("old_string and new_string are identical")
    s = p.read_text(encoding="utf-8")
    n = s.count(args.old_string)
    if n == 0:
        raise ToolError("old_string not found; read the file again and copy the text exactly")
    if n > 1 and not args.replace_all:
        raise ToolError(f"old_string matches {n} places; include more context or set replace_all")
    p.write_text(s.replace(args.old_string, args.new_string), encoding="utf-8")
    ctx.read_stamps[str(p)] = p.stat().st_mtime
    return Result.ok(f"Edited {p} ({n if args.replace_all else 1} replacement(s))")


class BashArgs(Args):
    command: str
    timeout: int = Field(120, ge=1, le=600, description="Seconds before the command is killed")


def _env() -> dict[str, str]:
    """Child processes get a whitelist, never the server's secrets (D17)."""
    return {k: v for k, v in os.environ.items() if k in ENV_ALLOW or k.startswith("LC_")}


async def _kill(proc: asyncio.subprocess.Process) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, sig)
        try:
            await asyncio.wait_for(proc.wait(), 2)
            return
        except TimeoutError:
            continue


@tool(effect="exec")
async def bash(args: BashArgs, ctx: ToolCtx) -> Result:
    """Run a shell command in the project root and return combined stdout/stderr and the exit code."""
    proc = await asyncio.create_subprocess_exec(
        "/bin/bash", "-c", args.command, cwd=ctx.root, env=_env(), stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
    out = bytearray()

    async def pump() -> None:
        while chunk := await proc.stdout.read(65536):
            if len(out) < MAX_OUTPUT:
                out.extend(chunk[: MAX_OUTPUT - len(out)])
            ctx.emit({"type": "tool.output", "call_id": ctx.call_id, "text": chunk.decode(errors="replace")})
        await proc.wait()

    try:
        await asyncio.wait_for(pump(), args.timeout)
    except TimeoutError:
        await _kill(proc)
        return Result.error(f"timed out after {args.timeout}s\n{out.decode(errors='replace')}")
    except asyncio.CancelledError:
        await _kill(proc)  # the whole process group, not just bash
        raise
    body = out.decode(errors="replace").rstrip()
    if len(out) >= MAX_OUTPUT:
        body += f"\n[output truncated at {MAX_OUTPUT} bytes]"
    return Result.ok(f"{body}\n[exit code {proc.returncode}]".lstrip())


BUILTIN = [read, write, edit, bash]
