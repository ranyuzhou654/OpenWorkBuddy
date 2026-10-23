"""Shell hooks, in Claude Code's format: the same event names, JSON on stdin and stdout, exit code 2 to block.

    [[hooks.PreToolUse]]           # also PostToolUse, UserPromptSubmit, Stop
    matcher = "Bash|mcp__github__.*"   # regex over the tool name; empty matches every tool
    command = "~/bin/check.sh"
    timeout = 60

So that scripts written for Claude Code work unchanged, built-in tools are reported under Claude Code's
names (Bash, Read, Write, Edit) and tool_input also carries file_path next to our path. Hooks are the
user's own commands: they run unsandboxed, in the project root, with a whitelisted environment.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .tools.builtin import ENV_ALLOW

log = logging.getLogger(__name__)

EVENTS = ("PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop")
ALIASES = {"bash": "Bash", "read": "Read", "write": "Write", "edit": "Edit"}
BLOCK = 2


@dataclass(frozen=True)
class Hook:
    event: str
    command: str
    matcher: str = ""
    timeout: int = 60

    def matches(self, tool: str | None) -> bool:
        if tool is None or self.matcher in ("", "*"):
            return True
        return any(re.fullmatch(self.matcher, n) for n in (tool, ALIASES.get(tool, tool)))


@dataclass
class Outcome:
    """What all hooks of one event decided together. block wins; reasons and context are joined."""

    block: bool = False
    reasons: list[str] = field(default_factory=list)
    decision: str | None = None  # PreToolUse only: "allow", "deny" or "ask"
    context: list[str] = field(default_factory=list)  # text to add for the model
    errors: list[str] = field(default_factory=list)  # failures to show the user, not the model

    @property
    def reason(self) -> str:
        return "\n".join(self.reasons)


def parse(raw: dict[str, Any]) -> tuple[Hook, ...]:
    hooks = []
    for event, items in raw.items():
        if event not in EVENTS:
            raise ValueError(f"hooks: unknown event {event!r}; supported: {', '.join(EVENTS)}")
        for h in items:
            re.compile(h.get("matcher", ""))  # a bad pattern fails at load time, not mid-run
            hooks.append(Hook(event, h["command"], h.get("matcher", ""), int(h.get("timeout", 60))))
    return tuple(hooks)


def tool_payload(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    extra = {"file_path": args["path"]} if "path" in args else {}
    return {"tool_name": ALIASES.get(tool, tool), "tool_input": {**args, **extra}}


async def run(hooks: tuple[Hook, ...], event: str, payload: dict[str, Any], cwd: Path,
              tool: str | None = None) -> Outcome:
    """Run the matching hooks one after another (so their effects are ordered) and merge their answers."""
    out = Outcome()
    for h in hooks:
        if h.event == event and h.matches(tool):
            await _one(h, {"hook_event_name": event, "cwd": str(cwd), **payload}, cwd, out)
    return out


async def _one(h: Hook, payload: dict[str, Any], cwd: Path, out: Outcome) -> None:
    env = {k: v for k, v in os.environ.items() if k in ENV_ALLOW or k.startswith("LC_")}
    env |= {"NWB_PROJECT_DIR": str(cwd), "CLAUDE_PROJECT_DIR": str(cwd)}
    proc = await asyncio.create_subprocess_exec(
        "/bin/bash", "-c", h.command, cwd=cwd, env=env, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(json.dumps(payload).encode()), h.timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        out.errors.append(f"{h.event} hook timed out after {h.timeout}s: {h.command}")
        return
    except asyncio.CancelledError:
        proc.kill()
        raise
    so, se = stdout.decode(errors="replace").strip(), stderr.decode(errors="replace").strip()
    if proc.returncode == BLOCK:
        out.block = True
        out.decision = "deny"
        out.reasons.append(se or f"blocked by hook: {h.command}")
    elif proc.returncode != 0:
        out.errors.append(f"{h.event} hook failed (exit {proc.returncode}): {se or h.command}")
    elif so.startswith("{"):
        _json(so, out)
    elif so and h.event == "UserPromptSubmit":
        out.context.append(so)  # plain stdout of a prompt hook is added as context, as in Claude Code


def _json(so: str, out: Outcome) -> None:
    try:
        d = json.loads(so)
    except ValueError:
        out.errors.append(f"hook printed invalid JSON: {so[:200]}")
        return
    specific = d.get("hookSpecificOutput") or {}
    decision = specific.get("permissionDecision") or {"approve": "allow", "block": "deny"}.get(d.get("decision", ""))
    reason = specific.get("permissionDecisionReason") or d.get("reason") or d.get("stopReason") or ""
    if decision == "deny" or d.get("decision") == "block" or d.get("continue") is False:
        out.block = True
        out.reasons.append(reason or "blocked by hook")
    if decision and out.decision != "deny":
        out.decision = decision
    if ctx := specific.get("additionalContext"):
        out.context.append(ctx)
