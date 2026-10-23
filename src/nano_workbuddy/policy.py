"""Permission modes for phase 2. Phase 3 replaces this with rule tables and sandbox presets (D12)."""

from __future__ import annotations

from typing import Any, Literal

from .tools import Tool

MODES = {
    "confirm": "Ask before writing files or running commands",
    "trust": "Never ask (full trust, no sandbox)",
}


def decide(settings: dict[str, Any], tool: Tool) -> Literal["allow", "ask"]:
    if settings["mode"] == "trust" or tool.effect == "read" or tool.name in settings["allow"]:
        return "allow"
    return "ask"
