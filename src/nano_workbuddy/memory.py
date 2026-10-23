"""Long-term memory (D18): two Markdown files the user can read and edit, and the model can update.

~/.nwb/USER.md holds what is true of the user everywhere; the project's AGENTS.md holds what is true of
this project. Both are frozen into the system prompt when a session starts (prompt.py), so an update
takes effect from the next session and never invalidates the running session's prompt cache.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field

from .prompt import MAX_FILE_CHARS, PROJECT_FILES
from .tools.builtin import Args
from .tools.registry import Result, Tool, ToolCtx, ToolError, Verdict, tool


def project_file(root: Path) -> Path:
    """The instructions file prompt.py reads: AGENTS.md, or CLAUDE.md when only that one exists."""
    return next((root / n for n in PROJECT_FILES if (root / n).is_file()), root / PROJECT_FILES[0])


class MemoryArgs(Args):
    action: Literal["add", "replace", "remove"]
    file: Literal["user", "project"] = Field(description="user: USER.md, facts about the user that hold in "
                                             "every project. project: this project's AGENTS.md")
    text: str = Field("", description="add: the text to append. replace: the new text")
    old_text: str = Field("", description="replace/remove: existing text, quoted exactly; must match once")


def memory_tool(home: Path) -> Tool:
    def target(args: MemoryArgs, ctx: ToolCtx) -> Path:
        return home / "USER.md" if args.file == "user" else project_file(ctx.root)

    def apply(args: MemoryArgs, current: str) -> str:
        if args.action == "add":
            if not args.text.strip():
                raise ToolError("text is required to add")
            return current.rstrip("\n") + ("\n\n" if current.strip() else "") + args.text.strip() + "\n"
        n = current.count(args.old_text) if args.old_text else 0
        if n != 1:
            raise ToolError(f"old_text must match exactly once; it matches {n} times. Quote it exactly as it appears")
        if args.action == "replace":
            return current.replace(args.old_text, args.text.strip())
        lines = [ln for ln in current.replace(args.old_text, "\0").splitlines() if ln.strip() != "\0"]
        return "\n".join(lines).replace("\0", "").rstrip("\n") + "\n"

    def recover(args: MemoryArgs, ctx: ToolCtx) -> Verdict:
        f = target(args, ctx)
        s = f.read_text(encoding="utf-8") if f.is_file() else ""
        if args.action == "remove":
            return "done" if args.old_text not in s else "not_done"
        return "done" if args.text.strip() in s and (args.action == "add" or args.old_text not in s) else "not_done"

    @tool(effect="write", recover=recover, scope=lambda args, ctx: ("memory", str(target(args, ctx))))
    async def memory(args: MemoryArgs, ctx: ToolCtx) -> Result:
        """Save durable knowledge for future sessions: the user's preferences, facts about them, or this
        project's conventions. Keep entries short and specific; update or remove outdated ones instead of
        adding duplicates. Never store secrets. Changes take effect from the next session."""
        f = target(args, ctx)
        current = f.read_text(encoding="utf-8") if f.is_file() else ""
        new = apply(args, current)
        if len(new) > MAX_FILE_CHARS:
            raise ToolError(f"{f.name} would exceed {MAX_FILE_CHARS} characters; consolidate or remove entries first")
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(new, encoding="utf-8")
        return Result.ok(f"Updated {f}. It takes effect from the next session; this session keeps its prompt.")

    return memory
