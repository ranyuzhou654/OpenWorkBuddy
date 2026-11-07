"""Document creation, deterministic format export and delivery.

create_document lays out Markdown the model wrote as .md, .docx or .pdf; present delivers a file made any
other way (a script, bash, write). export_document converts one delivered Markdown without rewriting.
All re-open the result to check it (artifacts.check), keep a copy of
this version, and return an artifact record in the result's meta, which the UI shows as a file card.
A missing or unreadable file is never announced as delivered.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import os
import re
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import Field

from .. import artifacts, render
from ..types import text
from .builtin import Args, resolve
from .registry import Result, ToolCtx, ToolError, tool

FORMATS = (".md", ".docx", ".pdf")
CITATION = re.compile(r"\[(S\d+(?:[ \t,;，；][^\]\n]*)?)\]")


def cited_numbers(markdown: str) -> list[int]:
    """Keep source IDs in page-qualified and grouped citations; page numbers are not source IDs."""
    return sorted({int(n) for bracket in CITATION.findall(markdown) for n in re.findall(r"\bS(\d+)\b", bracket)})


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
    content, sources = bibliography(args.content, ctx)
    _write_document(p, content, args.title, args.overwrite, ctx)
    result = _deliver(p, ctx, args.title, citation_checks(args.content, ctx))
    result.meta["artifact"]["sources"] = sources  # immutable evidence bound to this delivered version
    ctx.document_records[str(p)] = copy.deepcopy(result.meta["artifact"])
    return result


def _write_document(p: Path, content: str, title: str, overwrite: bool, ctx: ToolCtx) -> None:
    if p.suffix.lower() not in FORMATS:
        raise ToolError(f"unsupported format {p.suffix or '(none)'}; use one of {', '.join(FORMATS)}")
    if p.exists():
        history = ctx.versions.get(str(p), [])
        if not overwrite:
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
            Path(tmp).write_text(content, encoding="utf-8")
        elif p.suffix.lower() == ".docx":
            render.to_docx(render.parse(content), Path(tmp), title)
        else:
            render.to_pdf(render.parse(content), Path(tmp), title)
        os.chmod(tmp, 0o644)
        os.replace(tmp, p)  # the file appears complete or not at all
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)


class ExportArgs(Args):
    source: str = Field(description="An unchanged .md created by create_document in this session")
    path: str = Field(description="Output .docx or .pdf path; content is read from source, not generated again")
    title: str = Field("", description="Optional title override; otherwise preserve the source title")
    overwrite: bool = Field(False, description="Replace an unchanged file this session created earlier")


@tool(effect="write", scope=_create_scope)
async def export_document(args: ExportArgs, ctx: ToolCtx) -> Result:
    """Export a delivered Markdown document to Word or PDF without rewriting its content. First use
    create_document for .md, then call this with source/path only, optionally in the same ordered batch.
    Preserves the source version's bibliography and citation checks; never appends another bibliography."""
    source, target = resolve(ctx, args.source), resolve(ctx, args.path)
    if not ctx.can_read(source):
        raise ToolError(f"read on {source} is not allowed")
    original = ctx.document_records.get(str(source))
    if source.suffix.lower() != ".md" or original is None:
        raise ToolError("source must be a Markdown created by create_document in this session")
    if target.suffix.lower() not in (".docx", ".pdf"):
        raise ToolError("export path must end in .docx or .pdf")
    if not source.is_file():
        raise ToolError("source is missing; restore or create the document again before exporting")
    data = source.read_bytes()
    if hashlib.sha256(data).hexdigest() != original["sha256"]:
        raise ToolError("source changed after delivery; create a new document version before exporting")
    title = args.title or original.get("title", "")
    _write_document(target, data.decode("utf-8"), title, args.overwrite, ctx)
    checks = [copy.deepcopy(c) for c in original["checks"] if c.get("layer") == "citations"]
    result = _deliver(target, ctx, title, checks)
    result.meta["artifact"].update(sources=copy.deepcopy(original["sources"]), derived_from={
        "path": str(source), "sha256": original["sha256"], "version": original["version"]})
    return result


def bibliography(markdown: str, ctx: ToolCtx) -> tuple[str, list[dict[str, Any]]]:
    known = {s["n"]: s for s in ctx.sources.values()}
    sources = [copy.deepcopy(known[n]) for n in cited_numbers(markdown) if n in known]
    if not sources:
        return markdown, []
    lines = [markdown.rstrip(), "", "## 参考文献", ""]
    for s in sources:
        label = (s.get("title") or s["url"]).replace("\n", " ")
        title = re.sub(r"([\\`*_\[\]<>])", r"\\\1", label)
        uri = s["url"].replace(")", "%29").replace("(", "%28").replace(" ", "%20")
        if urlsplit(s["url"]).scheme in ("http", "https"):
            reference = f"[{title}]({uri}) · {s['url']}" if label != s["url"] else f"<{uri}>"
        else:
            # CommonMark intentionally rejects file: links. Keep local/opaque URIs readable once;
            # do not relax link validation or turn other schemes into clickable links.
            address = re.sub(r"([\\`*_\[\]<>])", r"\\\1", s["url"])
            reference = f"{title} · {address}" if label != s["url"] else address
        when = s.get("retrieved_at") or "unknown"
        locator = "; ".join(x["locator"] for x in s.get("inspections", [])) or s.get("locator", "not inspected")
        lines.append(f"- [S{s['n']}] {reference} · 访问 {when} · "
                     f"{s.get('state', 'legacy state unknown')} · {locator} · "
                     f"SHA256 {s.get('sha256') or s.get('discovery_sha256', 'unknown')}")
    lines += ["", "来源状态仅表示获取、提取或上下文读取进度；不代表事实或计算已经核验。", ""]
    return "\n".join(lines), sources


def citation_checks(markdown: str, ctx: ToolCtx) -> list[dict[str, Any]]:
    """Every [S#] the document cites must be a source recorded in this session."""
    cited = cited_numbers(markdown)
    if not cited:
        return []
    known = {s["n"]: s for s in ctx.sources.values()}
    unknown = [n for n in cited if n not in known]
    unread = [n for n in cited if n in known and known[n].get("state") != "inspected"]
    out = [{"name": "citations refer to recorded sources", "ok": not unknown,
            "layer": "citations",
            "detail": f"unknown: {', '.join(f'S{n}' for n in unknown)}" if unknown else f"{len(cited)} cited"}]
    out.append({"name": "cited source excerpts entered model context", "ok": not unread,
                "layer": "citations", "detail": f"not inspected: {', '.join(f'S{n}' for n in unread)}" if unread
                else "inspected excerpts only; claim support and factual correctness are not assessed"})
    missing = [n for n in cited if n in known and not known[n].get("snapshot")]
    out.append({"name": "cited versions have retained snapshots", "ok": not missing, "layer": "citations",
                "detail": "missing snapshot: " + ", ".join(f"S{n}" for n in missing) if missing
                else "content-addressed snapshots retained; no factual verification implied"})
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
