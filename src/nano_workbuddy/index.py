"""SQLite index (D4). Authoritative here: projects, runs, approvals, and each session's model, settings,
name and archived state. Cache rebuilt from the JSONL files: session titles, leaves and indexed sizes."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from .log import SessionLog
from .policy import Rule
from .types import USER, new_id, text_of

OWNER = "local"  # single-user for now; every query still scopes by owner (D16)
DEFAULT_SETTINGS = {"preset": "workspace"}

SCHEMA = """
create table if not exists projects(
  id text primary key, owner_id text not null, name text not null, root_path text not null,
  settings_json text not null default '{}', trusted integer not null default 0, created_at real not null,
  unique(owner_id, root_path));
create table if not exists sessions(
  id text primary key, project_id text not null, title text, file_path text not null,
  indexed_bytes integer not null default 0, leaf_id text, model_ref text, settings_json text not null default '{}',
  updated_at real not null, name text, archived_at real);
create table if not exists runs(
  id text primary key, session_id text not null, request_id text unique, status text not null,
  model_ref text, resume_count integer not null default 0, started_at real not null, ended_at real,
  usage_json text);
create table if not exists rules(
  id text primary key, scope text not null, session_id text, action text not null, resource text not null,
  effect text not null, created_at real not null);
create table if not exists approvals(
  id text primary key, session_id text not null, run_id text, call_id text not null, tool text not null,
  args_json text not null, status text not null, feedback text, created_at real not null, resolved_at real);
"""

# Append only: MIGRATIONS[i] upgrades schema version i to i + 1. SCHEMA always describes the latest shape.
MIGRATIONS = [
    "alter table sessions add column settings_json text not null default '{}'",  # phase 2
    "alter table sessions add column name text",  # renamed by the user; shown instead of the title
    "alter table sessions add column archived_at real",
    "update projects set trusted=1",  # projects added before trust existed were all chosen by the user
]


class Index:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("pragma journal_mode=wal")
        self._migrate()

    def _migrate(self) -> None:
        existed = self._one("select 1 from sqlite_master where type='table' and name='sessions'") is not None
        self.db.executescript(SCHEMA)  # creates whatever is missing, at the latest shape
        version = self.db.execute("pragma user_version").fetchone()[0] if existed else len(MIGRATIONS)
        for sql in MIGRATIONS[version:]:
            try:
                self.db.execute(sql)
            except sqlite3.OperationalError as e:  # a database written by a build that had the column already
                if "duplicate column" not in str(e):
                    raise
        self.db.execute(f"pragma user_version={len(MIGRATIONS)}")

    def _one(self, sql: str, *args: Any) -> dict[str, Any] | None:
        row = self.db.execute(sql, args).fetchone()
        return dict(row) if row else None

    def _all(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute(sql, args)]

    # projects

    def project_at(self, root: Path) -> dict[str, Any] | None:
        return self._one("select * from projects where owner_id=? and root_path=?", OWNER, str(root.resolve()))

    def project_for(self, root: Path, trusted: bool = False) -> dict[str, Any]:
        """The project at root, created (with that trust) if it is new; an existing one keeps its trust."""
        if p := self.project_at(root):
            return p
        self.db.execute("insert into projects(id, owner_id, name, root_path, trusted, created_at) values (?,?,?,?,?,?)",
                        (new_id("p"), OWNER, root.resolve().name, str(root.resolve()), int(trusted), time.time()))
        return self.project_at(root)

    def project(self, project_id: str) -> dict[str, Any] | None:
        return self._one("select * from projects where id=? and owner_id=?", project_id, OWNER)

    def projects(self) -> list[dict[str, Any]]:
        return self._all("select * from projects where owner_id=? order by created_at", OWNER)

    def update_project(self, project_id: str, name: str | None = None, trusted: bool | None = None) -> None:
        self.db.execute("update projects set name=coalesce(?, name), trusted=coalesce(?, trusted) where id=?",
                        (name, None if trusted is None else int(trusted), project_id))

    def delete_project(self, project_id: str) -> None:
        """Only the index row; the caller deletes the sessions first."""
        self.db.execute("delete from projects where id=?", (project_id,))

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

    @staticmethod
    def _session(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is not None:
            stored = json.loads(row.pop("settings_json"))
            if "mode" in stored:  # phase 2 settings: confirm / trust
                stored = {"preset": "trust" if stored["mode"] == "trust" else "workspace"}
            row["settings"] = {**DEFAULT_SETTINGS, **stored}
            row["title"] = row.pop("name") or row["title"]
        return row

    def session(self, session_id: str) -> dict[str, Any] | None:
        return self._session(self._one("select * from sessions where id=?", session_id))

    def sessions(self, project_id: str) -> list[dict[str, Any]]:
        rows = self._all("select * from sessions where project_id=? order by updated_at desc", project_id)
        return [self._session(r) for r in rows]

    def rename_session(self, session_id: str, name: str | None) -> None:
        """None goes back to the title taken from the first message."""
        self.db.execute("update sessions set name=? where id=?", (name, session_id))

    def archive_session(self, session_id: str, archived: bool) -> None:
        self.db.execute("update sessions set archived_at=? where id=?", (time.time() if archived else None, session_id))

    def delete_session(self, session_id: str) -> None:
        for table, key in (("sessions", "id"), ("runs", "session_id"), ("rules", "session_id"),
                           ("approvals", "session_id")):
            self.db.execute(f"delete from {table} where {key}=?", (session_id,))

    def update_settings(self, session_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        settings = {**self.session(session_id)["settings"], **patch}
        self.db.execute("update sessions set settings_json=? where id=?", (json.dumps(settings), session_id))
        return settings

    def reindex(self, sessions_dir: Path) -> int:
        """Bring the sessions table up to date with JSONL files written after the last sync."""
        known = {r["file_path"]: r["indexed_bytes"] for r in self._all("select file_path, indexed_bytes from sessions")}
        n = 0
        for f in sessions_dir.glob("*/*.jsonl"):
            if known.get(str(f)) != f.stat().st_size:
                self.sync_session(SessionLog.open(f))
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

    def unfinished_runs(self, session_id: str | None = None) -> list[dict[str, Any]]:
        """Runs a crash ('running') or a server shutdown ('interrupted') left behind."""
        sql = "select * from runs where status in ('running', 'interrupted')"
        if session_id:
            return self._all(sql + " and session_id=? order by started_at", session_id)
        return self._all(sql + " order by started_at")

    def bump_resume(self, run_id: str) -> int:
        self.db.execute("update runs set resume_count=resume_count+1 where id=?", (run_id,))
        return self._one("select resume_count from runs where id=?", run_id)["resume_count"]

    # rules approved by the user ("always allow")

    def add_rule(self, session_id: str, rule: Rule) -> None:
        self.db.execute("insert into rules(id, scope, session_id, action, resource, effect, created_at)"
                        " values (?,?,?,?,?,?,?)", (new_id("rule"), "session", session_id, rule.action, rule.resource,
                                                     rule.effect, time.time()))

    def session_rules(self, session_id: str) -> list[Rule]:
        rows = self._all("select * from rules where scope='session' and session_id=? order by created_at", session_id)
        return [Rule(r["action"], r["resource"], r["effect"]) for r in rows]

    # approvals

    def add_approval(self, approval_id: str, session_id: str, run_id: str | None, call: dict[str, Any]) -> None:
        self.db.execute("insert into approvals(id, session_id, run_id, call_id, tool, args_json, status, created_at)"
                        " values (?,?,?,?,?,?,?,?)", (approval_id, session_id, run_id, call["id"], call["name"],
                                                      json.dumps(call["args"], ensure_ascii=False), "pending",
                                                      time.time()))

    def resolve_approval(self, approval_id: str, status: str, feedback: str | None = None) -> None:
        self.db.execute("update approvals set status=?, feedback=?, resolved_at=? where id=? and status='pending'",
                        (status, feedback, time.time(), approval_id))

    def expire_pending_approvals(self) -> None:
        """At startup nobody is waiting on old approvals; resumed runs ask again."""
        self.db.execute("update approvals set status='expired', resolved_at=? where status='pending'", (time.time(),))
