"""Wires config, index, logs, models and tools together. Shared by the CLI and (later) the HTTP server."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import llm, prompt
from .config import Config, load
from .index import Index
from .log import SessionLog
from .loop import Run, close_pending, drive
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
        return log

    def open_session(self, session_id: str) -> SessionLog:
        row = self.index.session(session_id)
        if row is None:
            raise KeyError(f"no session {session_id}")
        return SessionLog.open(Path(row["file_path"]))

    def fork(self, log: SessionLog, entry_id: str) -> SessionLog:
        sid = new_id("s")
        new = log.fork(entry_id, self.sessions_dir / log.header["project_id"] / f"{sid}.jsonl", sid)
        self.index.sync_session(new, self.index.session(log.id)["model_ref"])
        return new

    def root_of(self, log: SessionLog) -> Path:
        row = self.index.db.execute("select root_path from projects where id=?", (log.header["project_id"],))
        return Path(row.fetchone()[0])

    def set_model(self, log: SessionLog, model: str) -> None:
        """Takes effect on the next run; a running run keeps the model it started with."""
        self.index.sync_session(log, self.cfg.model(model).name)

    # runs

    async def submit(self, log: SessionLog, content: str, *, request_id: str | None = None,
                     emit: Emit = lambda e: None) -> str:
        model_ref = self.index.session(log.id)["model_ref"]
        run, created = self.index.start_run(log.id, request_id, model_ref)
        if not created:  # a retried request: never start the same work twice
            return run["status"]
        close_pending(log, run["id"], "Interrupted; the user sent a new message instead.")
        log.append(USER, {"content": [text(content)]}, run_id=run["id"])
        return await self._drive(log, run, emit)

    async def resume(self, log: SessionLog, emit: Emit = lambda e: None) -> str | None:
        """Continue runs left `running` by a crash (D6). Gives up after MAX_RESUMES attempts."""
        status = None
        for run in self.index.running_runs(log.id):
            if self.index.bump_resume(run["id"]) > MAX_RESUMES:
                self.index.finish_run(run["id"], "error")
                status = "error"
                continue
            emit({"type": "run.resumed", "run_id": run["id"]})
            status = await self._drive(log, run, emit)
        return status

    async def _drive(self, log: SessionLog, run_row: dict[str, Any], emit: Emit) -> str:
        """Unknown errors propagate to the caller; the run is still marked so it is not resumed."""
        status, usage = "error", {}
        emit({"type": "run.started", "run_id": run_row["id"]})
        try:
            ctx = ToolCtx(root=self.root_of(log), session_id=log.id, spill_dir=self.cfg.home / "spill" / log.id,
                          emit=emit, read_stamps=self._stamps.setdefault(log.id, {}))
            run = Run(log=log, model=self.connect(self.cfg.model(run_row["model_ref"])), tools=self.tools, ctx=ctx,
                      run_id=run_row["id"], emit=emit, max_turns=self.cfg.max_turns, usage=usage)
            status = await drive(run)
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        finally:
            self.index.finish_run(run_row["id"], status, usage)
            self.index.sync_session(log)
            emit({"type": "run.finished", "run_id": run_row["id"], "status": status, "usage": usage})
        return status
