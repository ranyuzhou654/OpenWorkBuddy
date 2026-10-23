"""Wires config, index, logs, models and tools together. Shared by the CLI and the HTTP server."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import llm, prompt
from .config import Config, load
from .index import Index
from .log import SessionLog
from .loop import SHUTDOWN, Gate, Inbox, Run, close_pending, drive
from .tools import BUILTIN, Registry, ToolCtx
from .types import SYSTEM_PROMPT, USER, new_id, text

MAX_RESUMES = 3

type Emit = Callable[[dict[str, Any]], None]


class Workspace:
    """Process-wide state. One instance per process; no globals."""

    def __init__(self, cfg: Config, connect: Callable[..., llm.Model] = llm.connect,
                 tools: Registry | None = None):
        self.cfg = cfg
        self.index = Index(cfg.home / "index.db")
        self.connect = connect
        self.tools = tools or Registry(BUILTIN)
        self._logs: dict[str, SessionLog] = {}  # one live object per session, so listeners see every append
        self._stamps: dict[str, dict[str, float]] = {}  # per-session read stamps survive across runs
        self.index.reindex(self.sessions_dir)

    @classmethod
    def open(cls, home: Path | None = None) -> Workspace:
        return cls(load(home))

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

    # runs

    async def submit(self, log: SessionLog, content: str, *, request_id: str | None = None,
                     parent_id: str | None = None, emit: Emit = lambda e: None,
                     inbox: Inbox | None = None, gate: Gate | None = None) -> str:
        """Append a user message (as a child of parent_id when editing an earlier message) and run."""
        model_ref = self.index.session(log.id)["model_ref"]
        run, created = self.index.start_run(log.id, request_id, model_ref)
        if not created:  # a retried request: never start the same work twice
            return run["status"]
        if parent_id is not None:
            log.checkout(parent_id)
        close_pending(log, run["id"], "Interrupted; the user sent a new message instead.")
        log.append(USER, {"content": [text(content)]}, run_id=run["id"])
        return await self._drive(log, run, emit, inbox, gate)

    async def resume(self, log: SessionLog, emit: Emit = lambda e: None,
                     inbox: Inbox | None = None, gate: Gate | None = None) -> str | None:
        """Continue runs a crash or restart left unfinished (D6). Gives up after MAX_RESUMES attempts."""
        status = None
        for run in self.index.unfinished_runs(log.id):
            if self.index.bump_resume(run["id"]) > MAX_RESUMES:
                self.index.finish_run(run["id"], "error")
                status = "error"
                continue
            emit({"type": "run.resumed", "run_id": run["id"]})
            status = await self._drive(log, run, emit, inbox, gate)
        return status

    async def _drive(self, log: SessionLog, run_row: dict[str, Any], emit: Emit,
                     inbox: Inbox | None, gate: Gate | None) -> str:
        """Unknown errors propagate to the caller; the run is still marked so it is not resumed."""
        status, usage = "error", {}
        emit({"type": "run.started", "run_id": run_row["id"]})
        try:
            ctx = ToolCtx(root=self.root_of(log), session_id=log.id, spill_dir=self.cfg.home / "spill" / log.id,
                          emit=emit, read_stamps=self._stamps.setdefault(log.id, {}))
            run = Run(log=log, model=self.connect(self.cfg.model(run_row["model_ref"])), tools=self.tools, ctx=ctx,
                      run_id=run_row["id"], emit=emit, max_turns=self.cfg.max_turns, usage=usage,
                      inbox=inbox or Inbox(), gate=gate)
            status = await drive(run)
        except asyncio.CancelledError as e:
            status = "interrupted" if SHUTDOWN in e.args else "cancelled"  # interrupted runs resume on restart
            raise
        finally:
            self.index.finish_run(run_row["id"], status, usage)
            self.index.sync_session(log)
            emit({"type": "run.finished", "run_id": run_row["id"], "status": status, "usage": usage})
        return status
