"""grep and glob (D11). ripgrep when it is installed (fast, honours .gitignore), otherwise a Python walk
with the same output. Both skip hidden files and directories, which also keeps them out of ~/.ssh and
friends; an explicit path into such a place is still checked by the permission rules as a read."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

from pydantic import Field

from ..sandbox import SandboxUnavailable, wrap
from .builtin import Args, resolve
from .registry import Result, ToolCtx, ToolError, tool

SKIP_DIRS = {"node_modules", "__pycache__"}
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_LINE = 300


def _scope(args, ctx: ToolCtx) -> tuple[str, str]:
    return "read", str(resolve(ctx, args.path))  # searching is reading: the read rules apply


def glob_regex(pattern: str) -> re.Pattern[str]:
    """Shell glob to regex over "/"-separated relative paths: ** crosses directories, * and ? do not."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        else:
            c = pattern[i]
            out += {"*": "[^/]*", "?": "[^/]"}.get(c, re.escape(c))
            i += 1
    return re.compile(out + r"\Z")


def walk(base: Path) -> Iterator[Path]:
    if base.is_file():
        yield base
        return
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS)
        yield from (Path(dirpath, f) for f in sorted(filenames) if not f.startswith("."))


def _shown(p: Path, ctx: ToolCtx) -> str:
    """Paths inside the project are shown relative to it, the way the model will use them."""
    return str(p.relative_to(ctx.root)) if p.is_relative_to(ctx.root) else str(p)


def _matches_glob(rel: str, glob: re.Pattern[str] | None, has_slash: bool) -> bool:
    return glob is None or bool(glob.match(rel if has_slash else rel.rsplit("/", 1)[-1]))


class GlobArgs(Args):
    pattern: str = Field(description='Glob over paths relative to path, e.g. "**/*.py" or "src/**/test_*.py"')
    path: str = Field(".", description="Directory to search in")
    limit: int = Field(200, ge=1, le=2000)


@tool(effect="read", parallel=True, scope=_scope)
async def glob(args: GlobArgs, ctx: ToolCtx) -> Result:
    """Find files by name pattern. Newest first. Skips hidden files and node_modules."""
    base = resolve(ctx, args.path)
    if not base.is_dir():
        raise ToolError(f"{base} is not a directory")
    rx = glob_regex(args.pattern)
    hits = await asyncio.to_thread(lambda: [p for p in walk(base) if rx.match(p.relative_to(base).as_posix())])
    if not hits:
        return Result.ok(f"No files match {args.pattern} under {base}")
    hits.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    more = f"\n[{len(hits) - args.limit} more not shown; narrow the pattern]" if len(hits) > args.limit else ""
    return Result.ok("\n".join(_shown(p, ctx) for p in hits[: args.limit]) + more)


class GrepArgs(Args):
    pattern: str = Field(description="Regular expression to search file contents for")
    path: str = Field(".", description="File or directory to search")
    glob: str | None = Field(None, description='Only search files matching this glob, e.g. "*.py" or "src/**/*.ts"')
    ignore_case: bool = False
    mode: Literal["files", "content", "count"] = Field(
        "files", description="files: paths that match; content: matching lines with numbers; count: matches per file")
    limit: int = Field(100, ge=1, le=2000, description="Maximum files (files, count) or lines (content)")


@tool(effect="read", parallel=True, scope=_scope)
async def grep(args: GrepArgs, ctx: ToolCtx) -> Result:
    """Search file contents with a regular expression. Prefer this over running grep or rg through bash.
    Skips hidden files, node_modules and binary files."""
    base = resolve(ctx, args.path)
    if not base.exists():
        raise ToolError(f"{base} does not exist")
    rg = shutil.which("rg")
    lines = await (_rg(rg, args, base, ctx) if rg else asyncio.to_thread(_py, args, base, ctx))
    if not lines:
        return Result.ok(f"No matches for {args.pattern} under {base}")
    more = f"\n[{len(lines) - args.limit} more not shown; narrow the search]" if len(lines) > args.limit else ""
    return Result.ok("\n".join(lines[: args.limit]) + more)


def _py(args: GrepArgs, base: Path, ctx: ToolCtx) -> list[str]:
    try:
        rx = re.compile(args.pattern, re.IGNORECASE if args.ignore_case else 0)
    except re.error as e:
        raise ToolError(f"invalid regular expression: {e}") from e
    glob = glob_regex(args.glob) if args.glob else None
    out: list[str] = []
    for p in walk(base):
        rel = p.relative_to(base).as_posix() if p != base else p.name
        if not _matches_glob(rel, glob, "/" in (args.glob or "")) or p.stat().st_size > MAX_FILE_BYTES:
            continue
        raw = p.read_bytes()
        if b"\0" in raw[:8192]:
            continue  # binary
        found = [(n, line) for n, line in enumerate(raw.decode("utf-8", "replace").splitlines(), 1) if rx.search(line)]
        if not found:
            continue
        shown = _shown(p, ctx)
        if args.mode == "files":
            out.append(shown)
        elif args.mode == "count":
            out.append(f"{shown}:{len(found)}")
        else:
            out += [f"{shown}:{n}:{line[:MAX_LINE]}" for n, line in found]
        if len(out) > args.limit:
            break  # enough to say there are more
    return out


async def _rg(rg: str, args: GrepArgs, base: Path, ctx: ToolCtx) -> list[str]:
    mode = {"files": ["--files-with-matches"], "count": ["--count"], "content": ["--line-number", "--no-heading",
                                                                                   f"--max-columns={MAX_LINE}",
                                                                                   "--max-columns-preview"]}
    argv = [rg, "--color=never", "--sort=path", "--with-filename", *mode[args.mode], f"--max-filesize={MAX_FILE_BYTES}",
            *(["-i"] if args.ignore_case else []), *(["--glob", args.glob] if args.glob else []),
            "--glob", "!node_modules", "--", args.pattern, str(base)]
    if ctx.sandbox:
        try:
            argv = wrap(argv, ctx.sandbox)
        except SandboxUnavailable as e:
            raise ToolError(str(e)) from e
    proc = await asyncio.create_subprocess_exec(*argv, cwd=ctx.root, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), 60)
    except TimeoutError:
        proc.kill()
        raise ToolError("search took longer than 60s; narrow the path or the glob") from None
    if proc.returncode == 2 and not stdout:
        raise ToolError(stderr.decode(errors="replace").strip()[:500])
    root = f"{ctx.root}/"
    return [line.removeprefix(root) for line in stdout.decode(errors="replace").splitlines()]
