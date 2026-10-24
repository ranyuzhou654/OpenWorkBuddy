"""create_document and present (M1): files handed to the user as deliverables.

create_document lays out Markdown the model wrote as .md, .docx or .pdf; present delivers a file made any
other way (a script, bash, write). Both re-open the result to check it (artifacts.check), keep a copy of
this version, and return an artifact record in the result's meta, which the UI shows as a file card.
A missing or unreadable file is never announced as delivered.
"""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from pydantic import Field

from .. import artifacts, render
from ..types import text
from .builtin import Args, resolve
from .registry import Result, ToolCtx, ToolError, tool

FORMATS = (".md", ".docx", ".pdf")
CITATION = re.compile(r"\[S(\d+)\]")


class CreateArgs(Args):
    path: str = Field(description="Output file; the extension picks the format: .md, .docx or .pdf")
    content: str = Field(description="The document in Markdown: headings, paragraphs, lists, tables, code, links. "
                                     "Cite sources as [S1], [S2] where facts come from them.")
    title: str = Field("", description="Document title (file metadata)")
    overwrite: bool = Field(False, description="Replace a file this tool created earlier in this session")


def _create_scope(args: CreateArgs, ctx: ToolCtx) -> tuple[str, str]:
    return "write", str(resolve(ctx, args.path))


@tool(effect="write", scope=_create_scope)
async def create_document(args: CreateArgs, ctx: ToolCtx) -> Result:
    """Create a document (Markdown, Word or PDF) from Markdown and deliver it to the user. Use this for
    reports, briefs and other files the user asked for; the result is checked and shown as a file card."""
    p = resolve(ctx, args.path)
    if p.suffix.lower() not in FORMATS:
        raise ToolError(f"unsupported format {p.suffix or '(none)'}; use one of {', '.join(FORMATS)}")
    if p.exists():
        history = ctx.versions.get(str(p), [])
        if not args.overwrite:
            raise ToolError(f"{p} already exists; choose another name, or set overwrite=true to replace a file "
                            "you created")
        if not history:
            raise ToolError(f"{p} was not created by create_document in this session; it may be the user's "
                            "work. Choose another name.")
        if not p.is_file() or artifacts.sha256_of(p) != history[-1]:
            raise ToolError(f"{p} changed after you delivered it (the user may have edited it); choose another "
                            "name instead of overwriting their changes")
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.", suffix=p.suffix)
    os.close(fd)
    try:
        if p.suffix.lower() == ".md":
            Path(tmp).write_text(args.content, encoding="utf-8")
        elif p.suffix.lower() == ".docx":
            render.to_docx(render.parse(args.content), Path(tmp), args.title)
        else:
            render.to_pdf(render.parse(args.content), Path(tmp), args.title)
        os.chmod(tmp, 0o644)
        os.replace(tmp, p)  # the file appears complete or not at all
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
    return _deliver(p, ctx, args.title, citation_checks(args.content, ctx))


def citation_checks(markdown: str, ctx: ToolCtx) -> list[dict[str, Any]]:
    """Every [S#] the document cites must be a source recorded in this session."""
    cited = sorted({int(n) for n in CITATION.findall(markdown)})
    if not cited:
        return []
    known = {s["n"]: s for s in ctx.sources.values()}
    unknown = [n for n in cited if n not in known]
    snippets = [n for n in cited if n in known and known[n]["kind"] == "search"]
    out = [{"name": "citations refer to recorded sources", "ok": not unknown,
            "detail": f"unknown: {', '.join(f'S{n}' for n in unknown)}" if unknown else f"{len(cited)} cited"}]
    if snippets:
        out.append({"name": "cited sources were read in full", "ok": False,
                    "detail": f"only search snippets seen for {', '.join(f'S{n}' for n in snippets)}; "
                              "fetch them before relying on details"})
    return out


class PresentArgs(Args):
    path: str = Field(description="The file to deliver")
    title: str = Field("", description="A short title for the file card")


def _present_scope(args: PresentArgs, ctx: ToolCtx) -> tuple[str, str]:
    return "read", str(resolve(ctx, args.path))  # it reads the file to keep a copy; nothing else changes


@tool(effect="read", scope=_present_scope)
async def present(args: PresentArgs, ctx: ToolCtx) -> Result:
    """Deliver a finished file you made another way (with a script, bash or write) to the user: it is checked,
    a copy of this version is kept, and the user sees it as a file card they can open or download."""
    p = resolve(ctx, args.path)
    if not p.is_file():
        raise ToolError(f"{p} does not exist or is not a file; nothing was delivered")
    return _deliver(p, ctx, args.title)


def _deliver(p: Path, ctx: ToolCtx, title: str, extra: list[dict[str, Any]] | None = None) -> Result:
    checks, info = artifacts.check(p)
    a = artifacts.record(ctx.store, p, ctx.versions, title, checks + (extra or []), info)
    return Result([text(artifacts.summary(a))], meta={"artifact": a})
