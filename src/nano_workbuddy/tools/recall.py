"""recall (M3): search the assistant's own records across all sessions (knowledge.py)."""

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
    """Search your own records of earlier work: past conversations in every session, sources cited, files
    delivered and files the user attached. Use it when the user refers to something from before ("the brief
    from last week", "what did we decide about ..."). Results point to where the record is."""
    if ctx.recall is None:
        raise ToolError("search over past records is not available here")
    hits = ctx.recall(args.query, args.kind, args.limit)  # on the loop thread: SQLite and the logs live there
    if not hits:
        return Result.ok(f"Nothing found for {args.query!r}.")
    lines = []
    for h in hits:
        when = time.strftime("%Y-%m-%d", time.localtime(h["at"]))
        if h["kind"] == "message":
            head = f"- [conversation] {when} · session {h['session_id']} ({h['title'] or 'untitled'})"
        else:
            head = f"- [{LABEL[h['kind']]}] {when} · {h['title']} · {h['ref']}"
        lines.append(f"{head}\n  {h['snippet']}")
    return Result.ok(f"{len(hits)} result(s) for {args.query!r}:\n" + "\n".join(lines))
