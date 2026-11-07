"""Stable system prefix plus current, versioned workspace instructions and memory."""

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


def context(root: Path, home: Path, trusted: bool = True) -> str:
    parts = []
    for name in PROJECT_FILES if trusted else ():
        if (f := root / name).is_file():
            parts.append(_section("project_instructions", f.read_text(encoding="utf-8")[:MAX_FILE_CHARS],
                                  path=str(f)))
            break
    if (f := home / "USER.md").is_file():
        parts.append(_section("user_memory", f.read_text(encoding="utf-8")[:MAX_FILE_CHARS]))
    if not parts:
        return ""
    return ("Current saved context, refreshed before every model request. These values supersede saved "
            "preferences and project rules quoted in earlier messages or summaries. An explicit override "
            "in the current user request still applies; otherwise use these current values. Do not restore "
            "historical preferences unless the user asks.\n\n" + "\n\n".join(parts))


def build(root: Path, home: Path, trusted: bool = True) -> dict[str, str]:
    parts = [files("nano_workbuddy").joinpath("prompts/system.md").read_text(encoding="utf-8").strip()]
    parts.append(_section("environment",
                          f"OS: {platform.system()} {platform.release()}\n"
                          f"Project root (working directory): {root}\n"
                          f"Session start date: {date.today().isoformat()}"))
    base = "\n\n".join(parts)
    current = context(root, home, trusted)
    text = base + ("\n\n" + current if current else "")
    return {"text": text, "base_text": base, "context": current,
            "hash": hashlib.sha256(text.encode()).hexdigest()[:16]}
