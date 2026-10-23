"""Wires config, index, logs, models and tools together. Shared by the CLI and the HTTP server."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from . import compaction, hooks, llm, policy, prompt, skills
from .config import Config, ModelSpec, load, secret
from .context import budget, estimate_tokens, in_view, loaded_tools, request_tokens, to_messages
from .index import Index
from .log import SessionLog
from .loop import SHUTDOWN, Inbox, Run, close_pending, drive
from .memory import memory_tool
from .tools import BUILTIN, Registry, Result, Tool, ToolCtx, ToolError
from .tools.mcp import McpHub, Server, tool_search
from .tools.registry import parse_args
from .types import NOTICE, SYSTEM_PROMPT, USER, ToolCall, new_id, text

MAX_RESUMES = 3
DEFER_SHARE = 0.05  # exposure "auto": defer MCP tools when their schemas would take more of the window than this

type Emit = Callable[[dict[str, Any]], None]
type Ask = Callable[[policy.Check, Tool, ToolCall], Awaitable[Result | None]]  # asks the user; None = allowed
type Question = Callable[[dict[str, Any]], Awaitable[str]]  # ask_user: shows a question, returns the answer


class Workspace:
    """Process-wide state. One instance per process; no globals."""

    def __init__(self, cfg: Config, connect: Callable[[ModelSpec], llm.Model] | None = None,
                 tools: Registry | None = None):
        self.cfg = cfg
        self.index = Index(cfg.home / "index.db")
        self.connect = connect or self._connect
        self.tools = tools or Registry([*BUILTIN, memory_tool(cfg.home)])  # skill and MCP tools are added per run
        self.mcp = McpHub(cfg.home)
        self.last_tools = self.tools  # the most recent run's tools, for context estimates between runs
        self._logs: dict[str, SessionLog] = {}  # one live object per session, so listeners see every append
        self._stamps: dict[str, dict[str, float]] = {}  # per-session read stamps survive across runs
        self.index.reindex(self.sessions_dir)

    @classmethod
    def open(cls, home: Path | None = None) -> Workspace:
        return cls(load(home))

    def _connect(self, spec: ModelSpec) -> llm.Model:
        return llm.connect(spec, secret(spec.provider.api_key, self.cfg.home))

    def model_ref(self, log: SessionLog) -> str:
        """The session's model, or the default if it was deleted from the settings since."""
        ref = self.index.session(log.id)["model_ref"]
        return ref if ref in self.cfg.models else self.cfg.default_model

    @property
    def sessions_dir(self) -> Path:
        return self.cfg.home / "sessions"

    # sessions

    def new_session(self, root: Path, model: str | None = None) -> SessionLog:
        project = self.index.project_for(root)
        sid = new_id("s")
        log = SessionLog.create(self.sessions_dir / project["id"] / f"{sid}.jsonl", sid, project["id"])
        log.append(SYSTEM_PROMPT, prompt.build(Path(project["root_path"]), self.cfg.home))
        self.index.sync_session(log, self.cfg.model(model).name)
        self._logs[sid] = log
        return log

    def open_session(self, session_id: str) -> SessionLog:
        if log := self._logs.get(session_id):
            return log
        row = self.index.session(session_id)
        if row is None:
            raise KeyError(f"no session {session_id}")
        log = self._logs[session_id] = SessionLog.open(Path(row["file_path"]))
        return log

    def fork(self, log: SessionLog, entry_id: str) -> SessionLog:
        sid = new_id("s")
        new = log.fork(entry_id, self.sessions_dir / log.header["project_id"] / f"{sid}.jsonl", sid)
        row = self.index.session(log.id)
        self.index.sync_session(new, row["model_ref"])
        self.index.update_settings(sid, row["settings"])
        self._logs[sid] = new
        return new

    def root_of(self, log: SessionLog) -> Path:
        return Path(self.index.project(log.header["project_id"])["root_path"])

    def set_model(self, log: SessionLog, model: str) -> None:
        """Takes effect on the next run; a running run keeps the model it started with."""
        self.index.sync_session(log, self.cfg.model(model).name)

    def context_usage(self, log: SessionLog) -> dict[str, int]:
        """Estimated size of the next request versus what the session's model allows."""
        system, messages = to_messages(log.path())
        spec = self.cfg.model(self.model_ref(log))
        tools = self.last_tools.schemas(loaded_tools(log.path()))
        return {"tokens": request_tokens(system, messages, tools), "budget": budget(spec)}

    # tools (D14)

    def skills(self, root: Path | None, enabled: bool = True) -> dict[str, skills.Skill]:
        """Skills found for a project; by default minus those switched off in the settings."""
        found = skills.discover(root, self.cfg.home, self.cfg.skill_dirs)
        return {n: s for n, s in found.items() if not (enabled and n in self.cfg.skills_disabled)}

    async def toolset(self, spec: ModelSpec, root: Path) -> tuple[Registry, dict[str, str]]:
        """Built-in tools, the skill tool and the tools of every enabled MCP server, read fresh at each run
        start so that a skill or server switched off in the settings is gone from the next run.
        Also returns the notices that tell the model what is available."""
        notices: dict[str, str] = {}
        base = list(self.tools.tools.values())
        if found := self.skills(root, enabled=False):  # then keep the model's list current, even if empty
            enabled = {n: s for n, s in found.items() if n not in self.cfg.skills_disabled}
            notices["skills"] = skills.listing(enabled) or "No skills are enabled."
            base += [skills.skill_tool(enabled)] if enabled else []
        if not self.cfg.mcp:
            self.last_tools = Registry(base)
            return self.last_tools, notices
        servers = await self.mcp.connect(self.cfg.mcp)
        live = [s for s in servers if s.client]
        auto = sum(estimate_tokens(json.dumps([t.schema() for t in s.as_tools()])) for s in live
                   if s.spec.exposure == "auto")
        lazy = {s.spec.name for s in live
                if s.spec.exposure == "deferred" or (s.spec.exposure == "auto" and auto > DEFER_SHARE *
                                                     spec.context_window)}
        mcp_tools = {s.spec.name: s.as_tools() for s in live}
        deferred = [t.name for s in live if s.spec.name in lazy for t in mcp_tools[s.spec.name]]
        tools = [*base, *([tool_search] if deferred else []), *(t for ts in mcp_tools.values() for t in ts)]
        self.last_tools = Registry(tools, deferred)
        return self.last_tools, notices | {"mcp": mcp_notice(self.cfg.mcp, servers, lazy)}

    # permissions (D12)

    def preset(self, log: SessionLog) -> str:
        return self.index.session(log.id)["settings"]["preset"]

    def rules(self, log: SessionLog, preset: str) -> list[policy.Rule]:
        """Last match wins: preset defaults < config.toml < approved for this session."""
        return [*policy.PRESETS[preset].rules, *self.cfg.rules, *self.index.session_rules(log.id)]

    # runs

    async def submit(self, log: SessionLog, content: str, *, request_id: str | None = None,
                     parent_id: str | None = None, emit: Emit = lambda e: None,
                     inbox: Inbox | None = None, ask: Ask | None = None, question: Question | None = None) -> str:
        """Append a user message (as a child of parent_id when editing an earlier message) and run."""
        run, created = self.index.start_run(log.id, request_id, self.model_ref(log))
        if not created:  # a retried request: never start the same work twice
            return run["status"]
        hook = await self._hook(log, "UserPromptSubmit", {"prompt": content}, emit)
        if hook.block:  # the message is refused before anything is written; the text goes back to the composer
            self.index.finish_run(run["id"], "blocked")
            emit({"type": "run.error", "message": f"A UserPromptSubmit hook refused this message: {hook.reason}"})
            emit({"type": "inbox", "steer": [], "queue": [], "dropped": [content]})
            return "blocked"
        if parent_id is not None:
            log.checkout(parent_id)
        close_pending(log, run["id"], "Interrupted; the user sent a new message instead.")
        if hook.context:
            log.append(NOTICE, {"kind": "hook", "text": "\n".join(hook.context)}, run_id=run["id"])
        if forced := self._invoked_skill(log, content):  # "/name ...": load the skill for the model (D15)
            body, key = skills.render(forced), f"skill:{forced.name}"
            if in_view(log.path()).get(key) != body:  # unless the model already sees this exact text
                log.append(NOTICE, {"kind": key, "text": body, "pin": True}, run_id=run["id"])
        log.append(USER, {"content": [text(content)]}, run_id=run["id"])
        return await self._drive(log, run, emit, inbox, ask, question)

    async def _hook(self, log: SessionLog, event: str, payload: dict[str, Any], emit: Emit,
                    tool: str | None = None) -> hooks.Outcome:
        """Run the configured hooks for an event; their failures are shown to the user, not the model."""
        base = {"session_id": log.id, "transcript_path": str(log.file), "permission_mode": self.preset(log)}
        out = await hooks.run(self.cfg.hooks, event, base | payload, self.root_of(log), tool)
        for message in out.errors:
            emit({"type": "hook.error", "message": message})
        return out

    def _invoked_skill(self, log: SessionLog, content: str) -> skills.Skill | None:
        if not content.startswith("/"):
            return None
        return self.skills(self.root_of(log)).get(content[1:].split(maxsplit=1)[0] if content[1:].strip() else "")

    async def resume(self, log: SessionLog, emit: Emit = lambda e: None,
                     inbox: Inbox | None = None, ask: Ask | None = None,
                     question: Question | None = None) -> str | None:
        """Continue runs a crash or restart left unfinished (D6). Gives up after MAX_RESUMES attempts."""
        status = None
        for run in self.index.unfinished_runs(log.id):
            if self.index.bump_resume(run["id"]) > MAX_RESUMES:
                self.index.finish_run(run["id"], "error")
                status = "error"
                continue
            emit({"type": "run.resumed", "run_id": run["id"]})
            status = await self._drive(log, run, emit, inbox, ask, question)
        return status

    async def compact(self, log: SessionLog, instructions: str | None = None, emit: Emit = lambda e: None) -> str:
        """Manual compaction (/compact): summarise now, whatever the budget says."""
        run = self.make_run(log, self.model_ref(log), None, emit)
        emit({"type": "run.started", "run_id": None, "kind": "compaction"})
        status = "error"
        try:
            status = "ok" if await compaction.compact(run, instructions) else "nothing"
        finally:
            emit({"type": "run.finished", "run_id": None, "status": status, "usage": run.usage})
        return status

    async def switch_branch(self, log: SessionLog, target_id: str, emit: Emit = lambda e: None) -> str:
        """Checkout with a summary of the branch being left (Pi's branch summary)."""
        run = self.make_run(log, self.model_ref(log), None, emit)
        emit({"type": "run.started", "run_id": None, "kind": "branch_summary"})
        status = "error"
        try:
            status = "ok" if await compaction.summarize_branch(run, target_id) else "nothing"
        finally:
            self.index.sync_session(log)
            emit({"type": "run.finished", "run_id": None, "status": status, "usage": run.usage})
        return status

    def make_run(self, log: SessionLog, model_ref: str, run_id: str | None, emit: Emit,
                 inbox: Inbox | None = None, ask: Ask | None = None, tools: Registry | None = None,
                 notices: dict[str, str] | None = None, question: Question | None = None) -> Run:
        """Freeze one run's settings: model, preset and tools are read once (D12); approved rules are read live."""
        tools = tools or self.last_tools
        preset_name = self.preset(log)
        preset = policy.PRESETS[preset_name]
        root, home = self.root_of(log), self.cfg.home.resolve()
        spill = home / "spill" / log.id
        ctx = ToolCtx(root=root, session_id=log.id, spill_dir=spill, emit=emit,
                      read_stamps=self._stamps.setdefault(log.id, {}),
                      sandbox=policy.sandbox_policy(preset, root, tmp_dir(log.id), home, spill),
                      tmp=tmp_dir(log.id), tools=tools, question=question)
        places = {"root": str(root), "home": str(home), "spill": str(spill)}

        async def gate(tool: Tool, call: ToolCall) -> Result | None:
            try:
                args = parse_args(tool, call)
            except ToolError:
                return None  # execute() reports the invalid arguments
            action, resource = tool.permission_scope(args, ctx)
            check = policy.check(action, resource, self.rules(log, preset_name), places)
            if check.effect == "deny":
                return Result.error(f"Blocked by a permission rule: {action} on {resource} is not allowed.")
            if self.cfg.hooks:  # a PreToolUse hook can block, or turn ask into allow and allow into ask
                hook = await self._hook(log, "PreToolUse", {**hooks.tool_payload(tool.name, call["args"]),
                                                            "tool_use_id": call["id"]}, emit, tool.name)
                if hook.block:
                    return Result.error(f"Blocked by a PreToolUse hook: {hook.reason}")
                if hook.decision in ("allow", "ask"):
                    check = dataclasses.replace(check, effect=hook.decision)
            if check.effect == "allow":
                return None
            if ask is None:
                return Result.error(f"{action} on {resource} needs the user's approval, which this client "
                                    "cannot ask for.")
            return await ask(check, tool, call)

        async def after_tool(tool: Tool, call: ToolCall, r: Result) -> Result:
            response = {"content": "".join(b["text"] for b in r.content if b["type"] == "text"), "is_error": r.is_error}
            hook = await self._hook(log, "PostToolUse", {**hooks.tool_payload(tool.name, call["args"]),
                                                         "tool_use_id": call["id"], "tool_response": response},
                                    emit, tool.name)
            if feedback := (hook.reasons if hook.block else []) + hook.context:
                return dataclasses.replace(r, content=[*r.content, text("[Hook feedback]\n" + "\n".join(feedback))])
            return r

        async def on_stop(asked: bool) -> str | None:
            hook = await self._hook(log, "Stop", {"stop_hook_active": asked}, emit)
            return f"[Stop hook] {hook.reason}" if hook.block else None

        has = {h.event for h in self.cfg.hooks}
        model_ref = model_ref if model_ref in self.cfg.models else self.cfg.default_model
        return Run(log=log, model=self.connect(self.cfg.model(model_ref)), tools=tools, ctx=ctx, run_id=run_id,
                   emit=emit, max_turns=self.cfg.max_turns, inbox=inbox or Inbox(), gate=gate,
                   after_tool=after_tool if "PostToolUse" in has else None,
                   on_stop=on_stop if "Stop" in has else None,
                   notices={"permissions": preset.notice.format(root=root), **(notices or {})})

    async def _drive(self, log: SessionLog, run_row: dict[str, Any], emit: Emit, inbox: Inbox | None,
                     ask: Ask | None, question: Question | None = None) -> str:
        """Unknown errors propagate to the caller; the run is still marked so it is not resumed."""
        status, usage = "error", {}
        emit({"type": "run.started", "run_id": run_row["id"]})
        try:
            ref = run_row["model_ref"] if run_row["model_ref"] in self.cfg.models else self.cfg.default_model
            tools, notices = await self.toolset(self.cfg.model(ref), self.root_of(log))
            run = self.make_run(log, ref, run_row["id"], emit, inbox, ask, tools, notices, question)
            usage = run.usage
            status = await drive(run)
        except asyncio.CancelledError as e:
            status = "interrupted" if SHUTDOWN in e.args else "cancelled"  # interrupted runs resume on restart
            raise
        finally:
            self.index.finish_run(run_row["id"], status, usage)
            self.index.sync_session(log)
            emit({"type": "run.finished", "run_id": run_row["id"], "status": status, "usage": usage})
        return status


def mcp_notice(specs: dict[str, Any], servers: list[Server], lazy: set[str]) -> str:
    """What the model is told about MCP servers. It changes only when servers do, so it is appended
    as a new notice (D9) instead of rewriting the system prompt."""
    by_name = {s.spec.name: s for s in servers}
    lines = []
    for name in specs:
        s = by_name.get(name)
        if s is None:
            continue  # disabled: say nothing, its tools are simply gone
        if s.client is None:
            lines.append(f"- {name}: unavailable ({s.error or 'not started'}); its tools cannot be used now.")
            continue
        about = s.spec.description or s.instructions.strip()
        how = "deferred: load its tools with tool_search" if name in lazy else "tools declared directly"
        lines.append(f"- {name} ({len(s.tools)} tools, {how})" + (f": {about}" if about else ""))
    if not lines:
        return "No MCP servers are enabled."
    text = "MCP servers (their tools are named mcp__<server>__<tool>):\n" + "\n".join(lines)
    if lazy:
        text += ("\nDeferred tools are not declared yet. Call tool_search with keywords, or \"select:<name>\", "
                 "to load the ones you need; they become callable from your next step.")
    return text


def tmp_dir(session_id: str) -> Path:
    """Scratch space for sandboxed commands, outside ~/.nwb (which the sandbox hides)."""
    return Path(tempfile.gettempdir()).resolve() / f"nwb-{session_id}"
