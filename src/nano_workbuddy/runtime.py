"""Wires config, index, logs, models and tools together. Shared by the CLI and the HTTP server."""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from . import compaction, llm, policy, prompt
from .config import Config, ModelSpec, load, secret
from .context import budget, request_tokens, to_messages
from .index import Index
from .log import SessionLog
from .loop import SHUTDOWN, Inbox, Run, close_pending, drive
from .tools import BUILTIN, Registry, Result, Tool, ToolCtx, ToolError
from .tools.registry import parse_args
from .types import SYSTEM_PROMPT, USER, ToolCall, new_id, text

MAX_RESUMES = 3

type Emit = Callable[[dict[str, Any]], None]
type Ask = Callable[[policy.Check, Tool, ToolCall], Awaitable[Result | None]]  # asks the user; None = allowed


class Workspace:
    """Process-wide state. One instance per process; no globals."""

    def __init__(self, cfg: Config, connect: Callable[[ModelSpec], llm.Model] | None = None,
                 tools: Registry | None = None):
        self.cfg = cfg
        self.index = Index(cfg.home / "index.db")
        self.connect = connect or self._connect
        self.tools = tools or Registry(BUILTIN)
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
        return {"tokens": request_tokens(system, messages, self.tools.schemas()), "budget": budget(spec)}

    # permissions (D12)

    def preset(self, log: SessionLog) -> str:
        return self.index.session(log.id)["settings"]["preset"]

    def rules(self, log: SessionLog, preset: str) -> list[policy.Rule]:
        """Last match wins: preset defaults < config.toml < approved for this session."""
        return [*policy.PRESETS[preset].rules, *self.cfg.rules, *self.index.session_rules(log.id)]

    # runs

    async def submit(self, log: SessionLog, content: str, *, request_id: str | None = None,
                     parent_id: str | None = None, emit: Emit = lambda e: None,
                     inbox: Inbox | None = None, ask: Ask | None = None) -> str:
        """Append a user message (as a child of parent_id when editing an earlier message) and run."""
        run, created = self.index.start_run(log.id, request_id, self.model_ref(log))
        if not created:  # a retried request: never start the same work twice
            return run["status"]
        if parent_id is not None:
            log.checkout(parent_id)
        close_pending(log, run["id"], "Interrupted; the user sent a new message instead.")
        log.append(USER, {"content": [text(content)]}, run_id=run["id"])
        return await self._drive(log, run, emit, inbox, ask)

    async def resume(self, log: SessionLog, emit: Emit = lambda e: None,
                     inbox: Inbox | None = None, ask: Ask | None = None) -> str | None:
        """Continue runs a crash or restart left unfinished (D6). Gives up after MAX_RESUMES attempts."""
        status = None
        for run in self.index.unfinished_runs(log.id):
            if self.index.bump_resume(run["id"]) > MAX_RESUMES:
                self.index.finish_run(run["id"], "error")
                status = "error"
                continue
            emit({"type": "run.resumed", "run_id": run["id"]})
            status = await self._drive(log, run, emit, inbox, ask)
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

    def make_run(self, log: SessionLog, model_ref: str, run_id: str | None, emit: Emit,
                 inbox: Inbox | None = None, ask: Ask | None = None) -> Run:
        """Freeze one run's settings: model and preset are read once (D12); approved rules are read live."""
        preset_name = self.preset(log)
        preset = policy.PRESETS[preset_name]
        root, home = self.root_of(log), self.cfg.home.resolve()
        spill = home / "spill" / log.id
        ctx = ToolCtx(root=root, session_id=log.id, spill_dir=spill, emit=emit,
                      read_stamps=self._stamps.setdefault(log.id, {}),
                      sandbox=policy.sandbox_policy(preset, root, tmp_dir(log.id), home, spill),
                      tmp=tmp_dir(log.id))
        places = {"root": str(root), "home": str(home), "spill": str(spill)}

        async def gate(tool: Tool, call: ToolCall) -> Result | None:
            try:
                args = parse_args(tool, call)
            except ToolError:
                return None  # execute() reports the invalid arguments
            action, resource = tool.permission_scope(args, ctx)
            check = policy.check(action, resource, self.rules(log, preset_name), places)
            if check.effect == "allow":
                return None
            if check.effect == "deny":
                return Result.error(f"Blocked by a permission rule: {action} on {resource} is not allowed.")
            if ask is None:
                return Result.error(f"{action} on {resource} needs the user's approval, which this client "
                                    "cannot ask for.")
            return await ask(check, tool, call)

        model_ref = model_ref if model_ref in self.cfg.models else self.cfg.default_model
        return Run(log=log, model=self.connect(self.cfg.model(model_ref)), tools=self.tools, ctx=ctx, run_id=run_id,
                   emit=emit, max_turns=self.cfg.max_turns, inbox=inbox or Inbox(), gate=gate,
                   notices={"permissions": preset.notice.format(root=root)})

    async def _drive(self, log: SessionLog, run_row: dict[str, Any], emit: Emit,
                     inbox: Inbox | None, ask: Ask | None) -> str:
        """Unknown errors propagate to the caller; the run is still marked so it is not resumed."""
        status, usage = "error", {}
        emit({"type": "run.started", "run_id": run_row["id"]})
        try:
            run = self.make_run(log, run_row["model_ref"], run_row["id"], emit, inbox, ask)
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


def tmp_dir(session_id: str) -> Path:
    """Scratch space for sandboxed commands, outside ~/.nwb (which the sandbox hides)."""
    return Path(tempfile.gettempdir()).resolve() / f"nwb-{session_id}"
