"""SQLite index: projects and runs are authoritative here; the sessions table is a cache
that can always be rebuilt from the session JSONL files (D4)."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from .log import SessionLog
from .types import USER, new_id, text_of

OWNER = "local"  # single-user for now; every query still scopes by owner (D16)

SCHEMA = """
create table if not exists projects(
  id text primary key, owner_id text not null, name text not null, root_path text not null,
  settings_json text not null default '{}', trusted integer not null default 0, created_at real not null,
  unique(owner_id, root_path));
create table if not exists sessions(
  id text primary key, project_id text not null, title text, file_path text not null,
  indexed_bytes integer not null default 0, leaf_id text, model_ref text, updated_at real not null);
create table if not exists runs(
  id text primary key, session_id text not null, request_id text unique, status text not null,
  model_ref text, resume_count integer not null default 0, started_at real not null, ended_at real,
  usage_json text);
"""


class Index:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("pragma journal_mode=wal")
        self.db.executescript(SCHEMA)

    def _one(self, sql: str, *args: Any) -> dict[str, Any] | None:
        row = self.db.execute(sql, args).fetchone()
        return dict(row) if row else None

    def _all(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute(sql, args)]

    # projects

    def project_for(self, root: Path) -> dict[str, Any]:
        root_s = str(root.resolve())
        sql = "select * from projects where owner_id=? and root_path=?"
        if p := self._one(sql, OWNER, root_s):
            return p
        self.db.execute("insert into projects(id, owner_id, name, root_path, created_at) values (?,?,?,?,?)",
                        (new_id("p"), OWNER, root.name, root_s, time.time()))
        return self._one(sql, OWNER, root_s)

    # sessions

    def sync_session(self, log: SessionLog, model_ref: str | None = None) -> None:
        first_user = next((e for e in log.entries.values() if e.type == USER), None)
        title = text_of(first_user.data["content"])[:80] if first_user else None
        self.db.execute(
            """insert into sessions(id, project_id, title, file_path, indexed_bytes, leaf_id, model_ref, updated_at)
               values (?,?,?,?,?,?,?,?)
               on conflict(id) do update set title=excluded.title, indexed_bytes=excluded.indexed_bytes,
                 leaf_id=excluded.leaf_id, model_ref=coalesce(excluded.model_ref, sessions.model_ref),
                 updated_at=excluded.updated_at""",
            (log.id, log.header["project_id"], title, str(log.file), log.file.stat().st_size,
             log.leaf_id, model_ref, time.time()))

    def session(self, session_id: str) -> dict[str, Any] | None:
        return self._one("select * from sessions where id=?", session_id)

    def sessions(self, project_id: str) -> list[dict[str, Any]]:
        return self._all("select * from sessions where project_id=? order by updated_at desc", project_id)

    def reindex(self, sessions_dir: Path) -> int:
        """Bring the sessions table up to date with JSONL files written after the last sync."""
        known = {r["file_path"]: r["indexed_bytes"] for r in self._all("select file_path, indexed_bytes from sessions")}
        n = 0
        for f in sessions_dir.glob("*/*.jsonl"):
            if known.get(str(f)) != f.stat().st_size:
                log = SessionLog.open(f)
                self.sync_session(log)
                n += 1
        return n

    # runs

    def start_run(self, session_id: str, request_id: str | None, model_ref: str) -> tuple[dict[str, Any], bool]:
        """Returns (run, created). A repeated request_id returns the existing run."""
        if request_id and (r := self._one("select * from runs where request_id=?", request_id)):
            return r, False
        run_id = new_id("r")
        self.db.execute("insert into runs(id, session_id, request_id, status, model_ref, started_at)"
                        " values (?,?,?,?,?,?)", (run_id, session_id, request_id, "running", model_ref, time.time()))
        return self._one("select * from runs where id=?", run_id), True

    def finish_run(self, run_id: str, status: str, usage: dict[str, int] | None = None) -> None:
        self.db.execute("update runs set status=?, ended_at=?, usage_json=? where id=?",
                        (status, time.time(), json.dumps(usage or {}), run_id))

    def running_runs(self, session_id: str | None = None) -> list[dict[str, Any]]:
        if session_id:
            return self._all("select * from runs where status='running' and session_id=?", session_id)
        return self._all("select * from runs where status='running'")

    def bump_resume(self, run_id: str) -> int:
        self.db.execute("update runs set resume_count=resume_count+1 where id=?", (run_id,))
        return self._one("select resume_count from runs where id=?", run_id)["resume_count"]
