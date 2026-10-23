"""System prompt: rendered once when a session starts, then frozen in the log (D9).

Later changes are appended as messages instead of rewriting the head, so provider prompt caches
keep hitting. Memory files are part of the frozen snapshot (D18).
"""

from __future__ import annotations

import hashlib
import platform
from datetime import date
from importlib.resources import files
from pathlib import Path

PROJECT_FILES = ("AGENTS.md", "CLAUDE.md")
MAX_FILE_CHARS = 20_000


def _section(tag: str, body: str, **attrs: str) -> str:
    attr = "".join(f' {k}="{v}"' for k, v in attrs.items())
    return f"<{tag}{attr}>\n{body.strip()}\n</{tag}>"


def build(root: Path, home: Path) -> dict[str, str]:
    parts = [files("nano_workbuddy").joinpath("prompts/system.md").read_text(encoding="utf-8").strip()]
    for name in PROJECT_FILES:
        if (f := root / name).is_file():
            parts.append(_section("project_instructions", f.read_text(encoding="utf-8")[:MAX_FILE_CHARS],
                                  path=str(f)))
            break
    if (f := home / "USER.md").is_file():
        parts.append(_section("user_memory", f.read_text(encoding="utf-8")[:MAX_FILE_CHARS]))
    parts.append(_section("environment",
                          f"OS: {platform.system()} {platform.release()}\n"
                          f"Project root (working directory): {root}\n"
                          f"Session start date: {date.today().isoformat()}"))
    text = "\n\n".join(parts)
    return {"text": text, "hash": hashlib.sha256(text.encode()).hexdigest()[:16]}
