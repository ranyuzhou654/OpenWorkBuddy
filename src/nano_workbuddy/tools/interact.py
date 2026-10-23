"""todo and ask_user (D11): tools whose effect is on the conversation, not on the computer.

todo keeps the plan in the log: each call replaces the whole list, the latest one is what the UI shows,
and it is pinned so a compaction cannot lose it. ask_user puts a question card in front of the user and
waits for the answer, the way approvals do.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ..types import text
from .builtin import Args
from .registry import Result, ToolCtx, ToolError, tool

MARKS = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}


class TodoItem(BaseModel):
    content: str = Field(description="What to do, as a short imperative sentence")
    status: Literal["pending", "in_progress", "completed"] = "pending"


class TodoArgs(Args):
    todos: list[TodoItem] = Field(description="The complete list; it replaces the previous one")


@tool(effect="read", scope=lambda args, ctx: ("todo", ""))
async def todo(args: TodoArgs, ctx: ToolCtx) -> Result:
    """Keep a visible plan for work with three or more steps. Send the whole list every time, mark exactly
    one item in_progress while you work on it, and mark items completed as soon as they are done."""
    if sum(t.status == "in_progress" for t in args.todos) > 1:
        raise ToolError("only one item may be in_progress at a time")
    done = sum(t.status == "completed" for t in args.todos)
    body = "\n".join(f"{MARKS[t.status]} {t.content}" for t in args.todos)
    return Result([text(f"Todo list ({done}/{len(args.todos)} done):\n{body}")],
                  meta={"todos": [t.model_dump() for t in args.todos], "pin": "todo"})


class AskArgs(Args):
    question: str = Field(description="One clear question")
    options: list[str] = Field([], max_length=6, description="Suggested answers to pick from (the user may also "
                               "type their own); leave empty for an open question")


@tool(effect="read", scope=lambda args, ctx: ("ask_user", ""))
async def ask_user(args: AskArgs, ctx: ToolCtx) -> Result:
    """Ask the user a question and wait for the answer. Use it only when a decision is genuinely theirs or the
    request is ambiguous in a way you cannot resolve by looking; otherwise make a reasonable choice."""
    if ctx.question is None:
        raise ToolError("this client cannot ask questions; state your assumption and continue")
    answer = await ctx.question({"call_id": ctx.call_id, "question": args.question, "options": args.options})
    return Result.ok(f"The user answered: {answer}")
