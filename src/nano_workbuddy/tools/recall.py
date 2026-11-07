"""Scoped search and range reading of the assistant's own records (knowledge.py)."""

from __future__ import annotations

import time
from typing import Literal

from pydantic import Field

from .builtin import Args
from .registry import Result, ToolCtx, ToolError, tool


class RecallArgs(Args):
    query: str = Field(description="Words to look for; several words must all match. Chinese works without spaces.")
    kind: Literal["all", "messages", "sources", "files"] = "all"
    limit: int = Field(10, ge=1, le=50)


LABEL = {"message": "conversation", "source": "source", "file": "delivered file", "upload": "attached file"}


@tool(effect="read", parallel=True, scope=lambda args, ctx: ("recall", ""))
async def recall(args: RecallArgs, ctx: ToolCtx) -> Result:
    """Search earlier work in the host-authorized scope (default current project; projectless: current session).
    Cross-project recall requires the user's explicit scope setting. Search past conversations, sources cited, files
    delivered and files the user attached. Use it when the user refers to something from before ("the brief
    from last week", "what did we decide about ..."). Results point to where the record is."""
    if ctx.recall is None:
        raise ToolError("search over past records is not available here")
    hits = ctx.recall(args.query, args.kind, args.limit)  # on the loop thread: SQLite and the logs live there
    if not hits:
        return Result.ok(f"Scope: {ctx.recall_scope()}. Nothing found for {args.query!r}.")
    lines = []
    for h in hits:
        when = time.strftime("%Y-%m-%d", time.localtime(h["at"]))
        if h["kind"] == "message":
            head = f"- [conversation] {when} · session {h['session_id']} ({h['title'] or 'untitled'})"
        else:
            head = f"- [{LABEL[h['kind']]}] {when} · {h['title']} · {h['ref']}"
        lines.append(f"{head}\n  record={h['record_id']}\n  {h['snippet']}")
    return Result.ok(f"Scope: {ctx.recall_scope()}. {len(hits)} result(s) for {args.query!r}:\n" + "\n".join(lines))


class RecordArgs(Args):
    ref: str = Field(description="The stable rec_ identifier from recall")
    start: int = Field(0, ge=0, description="Zero-based character offset in the indexed text")
    end: int = Field(6000, ge=1, description="Exclusive character offset, at most start+12000")


@tool(effect="read", parallel=True, scope=lambda args, ctx: ("recall", ""))
async def read_record(args: RecordArgs, ctx: ToolCtx) -> Result:
    """Read a recall hit's indexed context by character range. Uses the same host-selected scope as recall.
    Indexed files may be truncated to 60000 characters; use the original file for omitted text."""
    if ctx.read_record is None:
        raise ToolError("record reading is not available here")
    try:
        record = ctx.read_record(args.ref, args.start, args.end)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    if record is None:
        raise ToolError("record not found in the current authorized scope")
    return Result.ok(f"Scope: {ctx.recall_scope()}; {record['title']}\nSession {record['session_id']}; "
                     f"{record['ref']}\nCharacters {record['start']}-{record['end']} of {record['total_chars']}\n\n"
                     + record["body"])
