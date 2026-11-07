"""read / write / edit / bash: the file and shell tools. tools/__init__.py lists every built-in tool."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import os
import re
import signal
import tempfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .. import artifacts, documents
from ..sandbox import SandboxUnavailable, wrap
from ..types import text
from .registry import Result, ToolCtx, ToolError, Verdict, tool

IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
               ".webp": "image/webp"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_LINE = 2000
MAX_OUTPUT = 10 * 1024 * 1024
ENV_ALLOW = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TERM", "TMPDIR", "TZ",
             "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy")
SANDBOX_DENIAL = re.compile(r"Operation not permitted|Read-only file system|Could not resolve host|"
                            r"Failed to connect|Network is unreachable|Temporary failure in name resolution")
UMASK = os.umask(0)  # read once at import (setting it is process-wide, so never later from a running tool)
os.umask(UMASK)
SANDBOX_HINT = ("\n[This command ran in the sandbox. If it failed because it needs the network or must write "
                "outside the project, re-run it with unsandboxed=true; the user will be asked to approve.]")


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


def resolve(ctx: ToolCtx, path: str) -> Path:
    """Absolute and symlink-free, so permission rules see where a write really lands."""
    p = Path(path).expanduser()
    return (p if p.is_absolute() else ctx.root / p).resolve()


def _path_scope(action: str):
    return lambda args, ctx: (action, str(resolve(ctx, args.path)))


def stamp(p: Path) -> tuple[int, int]:
    """What "unchanged since read" compares: nanosecond mtime and size."""
    st = p.stat()
    return st.st_mtime_ns, st.st_size


def _check_fresh(ctx: ToolCtx, p: Path) -> None:
    """Writes to an existing file require an unchanged earlier read, so the model never edits blind."""
    seen = ctx.read_stamps.get(str(p))
    if seen is None:
        raise ToolError(f"read {p} before modifying it")
    if stamp(p) != seen:
        raise ToolError(f"{p} changed since it was last read; read it again")


def atomic_write(p: Path, content: str) -> None:
    """Write a sibling temporary file, then rename it over the target. A crash, a full disk or a reader at the
    wrong moment sees the old file or the new one, never a mix. An existing file keeps its permission bits."""
    mode = p.stat().st_mode & 0o7777 if p.exists() else 0o666 & ~UMASK
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.", suffix=".nwb-tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, p)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


class ReadArgs(Args):
    path: str = Field(description="File path, absolute or relative to the project root")
    offset: int = Field(1, ge=1, description="First line to read (1-based)")
    limit: int = Field(2000, ge=1, le=5000, description="Maximum number of lines")
    pages: str | None = Field(None, description='PDF pages, slides or sheets to read, e.g. "3" or "2-5"')


@tool(effect="read", parallel=True, scope=_path_scope("read"))
async def read(args: ReadArgs, ctx: ToolCtx) -> Result:
    """Read a text file with line numbers, an image, or the text of a PDF, Word, PowerPoint or Excel file
    (with page / slide / sheet markers to cite). Use offset/limit for large files, pages for long documents."""
    p = resolve(ctx, args.path)
    if not p.exists():
        raise ToolError(f"{p} does not exist")
    if p.is_dir():
        raise ToolError(f"{p} is a directory; list it with bash")
    if mime := IMAGE_TYPES.get(p.suffix.lower()):
        if p.stat().st_size > MAX_IMAGE_BYTES:
            raise ToolError(f"image larger than {MAX_IMAGE_BYTES} bytes")
        if not ctx.vision:
            return Result.ok(f"Image {p} ({p.stat().st_size} bytes). The current model cannot view images.")
        raw = p.read_bytes()
        data = base64.b64encode(raw).decode()
        rec = _file_source(p, ctx, raw, None, "image (visual)")
        return Result([{"type": "text", "text": f"[S{rec['n']}] Image {p}"},
                       {"type": "image", "mime": mime, "data": data}],
                      meta=artifacts.reading(rec, "image (visual)"))
    if documents.is_document(p):
        return await asyncio.to_thread(_read_document, p, args, ctx)
    before = stamp(p)
    raw = p.read_bytes()
    if stamp(p) != before:
        raise ToolError(f"{p} changed during the read; read it again")
    if b"\0" in raw[:8192]:
        raise ToolError(f"{p} looks binary; inspect it with bash")
    ctx.read_stamps[str(p)] = before
    body = raw.decode("utf-8", errors="replace")
    locator = f"lines {args.offset}-{min(len(body.splitlines()), args.offset - 1 + args.limit)} (line caps {MAX_LINE})"
    rec = _file_source(p, ctx, raw, body, locator)
    meta = artifacts.reading(rec, locator) if args.offset <= len(body.splitlines()) else {"sources": [rec]}
    return Result(
        [text(f"[S{rec['n']}] {p}\n" + _numbered(body.splitlines(), args, f"{p} is empty"))],
        meta=meta)


def _file_source(p: Path, ctx: ToolCtx, raw: bytes, extracted: str | None, locator: str) -> dict:
    for source in ctx.sources.values():
        if any(o["path"] == str(p) and o["sha256"] == hashlib.sha256(raw).hexdigest()
               for o in source.get("outputs", [])):
            return source  # an immutable spilled excerpt still belongs to its original source version
    known = next((s for s in ctx.sources.values() if s.get("path") == str(p)), None)
    return artifacts.source_read(ctx, known["url"] if known else p.as_uri(), raw, p.name,
                                 extracted=extracted, locator=locator, path=str(p))


def _numbered(lines: list[str], args: ReadArgs, empty: str) -> str:
    if not lines:
        return empty
    start, end = args.offset - 1, min(len(lines), args.offset - 1 + args.limit)
    body = "\n".join(f"{n:6}\t{line[:MAX_LINE]}" for n, line in enumerate(lines[start:end], start=args.offset))
    if end < len(lines) or start > 0:
        body += f"\n[lines {args.offset}-{end} of {len(lines)}; use offset to read more]"
    return body


def _read_document(p: Path, args: ReadArgs, ctx: ToolCtx) -> Result:
    try:
        raw = p.read_bytes()
        doc = documents.load_bytes(raw, p.suffix.lower(), ctx.store / "cache" / "docs" if ctx.store else None,
                                   name=p.name)
    except documents.DocumentError as e:
        raise ToolError(str(e)) from e
    first, last = 1, len(doc.units)
    if args.pages:
        if not doc.unit:
            raise ToolError(f"{doc.kind} documents have no pages here; use offset/limit")
        m = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+))?\s*", args.pages)
        if not m or not 1 <= int(m.group(1)) <= len(doc.units):
            raise ToolError(f"pages must look like 3 or 2-5, within 1-{len(doc.units)}")
        first, last = int(m.group(1)), min(int(m.group(2) or m.group(1)), len(doc.units))
        if last < first:
            raise ToolError("page range end must not be before its start")
    head = [f"{p}: {doc.describe()}" + (f", showing {doc.unit}s {first}-{last}" if args.pages else "")]
    head += [f"Note: {n}" for n in doc.notes]
    selected = doc.text(first, last).splitlines()
    body = _numbered(selected, args, "(no text)")
    locator = f"{doc.unit or 'document'} {first}-{last}; output lines {args.offset}-{args.offset + args.limit - 1}"
    rec = _file_source(p, ctx, raw, doc.text(), locator)
    readable = any(unit.strip() for unit in doc.units[first - 1:last]) and args.offset <= len(selected)
    return Result([text(f"[S{rec['n']}] " + "\n".join(head) + "\n\n" + body)],
                  meta=artifacts.reading(rec, locator) if readable else {"sources": [rec]})


class WriteArgs(Args):
    path: str
    content: str


def _write_recover(args: WriteArgs, ctx: ToolCtx) -> Verdict:
    p = resolve(ctx, args.path)
    if p.is_file() and p.read_bytes() == args.content.encode():
        return "done"
    return "not_done"


@tool(effect="write", recover=_write_recover, scope=_path_scope("write"))
async def write(args: WriteArgs, ctx: ToolCtx) -> Result:
    """Create or overwrite a file. An existing file must be read first. Prefer edit for changes."""
    p = resolve(ctx, args.path)
    if p.exists():
        _check_fresh(ctx, p)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(p, args.content)
    ctx.read_stamps[str(p)] = stamp(p)
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


@tool(effect="write", recover=_edit_recover, scope=_path_scope("edit"))
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
    atomic_write(p, s.replace(args.old_string, args.new_string))
    ctx.read_stamps[str(p)] = stamp(p)
    return Result.ok(f"Edited {p} ({n if args.replace_all else 1} replacement(s))")


class BashArgs(Args):
    command: str
    timeout: int = Field(120, ge=1, le=600, description="Seconds before the command is killed")
    unsandboxed: bool = Field(False, description="Run outside the sandbox, for commands that need the network or "
                              "must write outside the project. Requires the user's approval; use only when needed.")


def _bash_scope(args: BashArgs, ctx: ToolCtx) -> tuple[str, str]:
    return ("bash.unsandboxed" if args.unsandboxed and ctx.sandbox else "bash"), args.command


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


@tool(effect="exec", scope=_bash_scope)
async def bash(args: BashArgs, ctx: ToolCtx) -> Result:
    """Run a shell command in the project root and return combined stdout/stderr and the exit code."""
    argv, env = ["/bin/bash", "-c", args.command], _env()
    sandboxed = ctx.sandbox is not None and not args.unsandboxed
    if sandboxed:
        try:
            argv = wrap(argv, ctx.sandbox)
        except SandboxUnavailable as e:  # never fall back to running unconfined
            raise ToolError(f"{e}. Sandboxed commands cannot run here; ask the user to install the sandbox "
                            "or switch to the full-trust preset.") from e
        if ctx.tmp:
            ctx.tmp.mkdir(parents=True, exist_ok=True)
            env["TMPDIR"] = str(ctx.tmp)
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=ctx.root, env=env, stdin=asyncio.subprocess.DEVNULL,
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
        return Result([text(f"Error: timed out after {args.timeout}s\n{out.decode(errors='replace')}")], True,
                      {"exit_code": None, "timed_out": True})
    except asyncio.CancelledError:
        await _kill(proc)  # the whole process group, not just bash
        raise
    body = out.decode(errors="replace").rstrip()
    if len(out) >= MAX_OUTPUT:
        body += f"\n[output truncated at {MAX_OUTPUT} bytes]"
    if sandboxed and proc.returncode != 0 and SANDBOX_DENIAL.search(body):
        body += SANDBOX_HINT
    # A non-zero exit is often a legitimate answer (grep found nothing, a test failed), so the call itself
    # succeeded; the code is also kept structured for the UI and later checks.
    return Result([text(f"{body}\n[exit code {proc.returncode}]".lstrip())], meta={"exit_code": proc.returncode})

