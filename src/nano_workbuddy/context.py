"""Project the current log path into a model request.

Phase 1 covers the first tier of context control (D10): large tool results are spilled to files
when they are written. Pruning old results and summary compaction arrive in phase 3.
"""

from __future__ import annotations

import os
from pathlib import Path

from .tools.registry import Result
from .types import ASSISTANT, SYSTEM_PROMPT, TOOL_RESULT, USER, Entry, Message, text

SPILL_CHARS = 30_000  # roughly 8k tokens
KEEP_CHARS = 4_000  # head and tail kept inline


def to_messages(path: list[Entry]) -> tuple[str, list[Message]]:
    system, messages = "", []
    for e in path:
        if e.type == SYSTEM_PROMPT:
            system = e.data["text"]
        elif e.type == USER:
            messages.append(Message(role="user", content=e.data["content"]))
        elif e.type == ASSISTANT and e.data["content"]:
            messages.append(Message(role="assistant", content=e.data["content"]))
        elif e.type == TOOL_RESULT:
            messages.append(Message(role="tool", content=e.data["content"], call_id=e.data["call_id"],
                                    is_error=e.data["is_error"]))
    return system, messages


def spill(r: Result, spill_dir: Path, call_id: str) -> Result:
    """Keep the head and tail inline and save the full text privately (0700 dir, 0600 file, no symlink follow)."""
    full = "".join(b["text"] for b in r.content if b["type"] == "text")
    if len(full) <= SPILL_CHARS:
        return r
    spill_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    f = spill_dir / f"{call_id}.txt"
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(full)
    omitted = len(full) - 2 * KEEP_CHARS
    note = (f"\n\n[... {omitted} characters omitted. Full output saved to {f} — "
            f"inspect it with read (offset/limit) or bash grep ...]\n\n")
    images = [b for b in r.content if b["type"] == "image"]
    return Result([text(full[:KEEP_CHARS] + note + full[-KEEP_CHARS:]), *images], r.is_error)
