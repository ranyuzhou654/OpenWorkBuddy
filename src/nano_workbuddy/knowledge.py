"""Rebuildable FTS5 cache. Incremental JSONL tails and document parsing run in a single background worker.

Queries always carry an authorized session set, project/session filters and access scope. The cache
never decides permissions: the host filters with current project settings at each search/read.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import re
import sqlite3
from pathlib import Path
from typing import Any

from . import artifacts, documents
from .types import ASSISTANT, TOOL_RESULT, USER, Entry, text_of

log = logging.getLogger(__name__)
SCHEMA = """
create virtual table if not exists items using fts5(
 title, body, kind unindexed, ref unindexed, session_id unindexed, at unindexed,
 project_id unindexed, access_scope unindexed, record_id unindexed, tokenize='trigram');
create table if not exists progress(session_id text primary key, lines integer not null, bytes integer not null);
create table if not exists files(sha256 text not null, session_id text not null, primary key(sha256, session_id));
"""
MAX_FILE_CHARS = 60_000
MAX_ENTRIES = 256
KINDS = {"messages": ("message",), "sources": ("source",), "files": ("file", "upload")}


class Knowledge:
    def __init__(self, path: Path, store: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("pragma journal_mode=wal")
        if self.db.execute("pragma user_version").fetchone()[0] != 2:
            self.db.executescript("drop table if exists items; drop table if exists progress; "
                                 "drop table if exists files;")
            self.db.executescript(SCHEMA)
            self.db.execute("pragma user_version=2")
        self.store = store

    def sync(self, sessions: list[dict[str, Any]]) -> int:
        """At most MAX_ENTRIES complete lines per session per batch; rollback preserves the old cursor."""
        n = 0
        for row in sessions:
            try:
                n += self._sync(row)
            except Exception:
                # Includes a corrupt JSONL line or failed insert; other sessions still make progress.
                log.exception("record indexing failed for session %s", row["id"])
                raise
        return n

    def _sync(self, row: dict[str, Any]) -> int:
        f = Path(row["file_path"])
        if not f.is_file():
            self.forget(row["id"])
            return 0
        done = self.db.execute("select lines, bytes from progress where session_id=?", (row["id"],)).fetchone()
        size = f.stat().st_size
        if done and done["bytes"] == size:
            return 0
        start = done["bytes"] if done and done["bytes"] < size else 0
        seq = done["lines"] if start else 0
        self.db.execute("begin")
        try:
            if not start and done:
                self.forget(row["id"])
            with f.open("rb") as stream:
                stream.seek(start)
                n = 0
                while n < MAX_ENTRIES:
                    raw = stream.readline()
                    if not raw or not raw.endswith(b"\n"):
                        break  # never advance past a torn append
                    data = json.loads(raw)
                    if "id" in data and "parent_id" in data:
                        self._entry(row, Entry.from_json(data, seq))
                    seq += 1
                    n += 1
                    start = stream.tell()
            self.db.execute("insert or replace into progress values (?,?,?)", (row["id"], seq, start))
            self.db.execute("commit")
            return n
        except BaseException:
            self.db.execute("rollback")
            raise

    def _entry(self, row: dict[str, Any], e: Entry) -> None:
        if e.type in (USER, ASSISTANT):
            body = text_of([b for b in e.data.get("content", []) if "attachment" not in b]).strip()
            if body and not e.data.get("synthetic"):
                self._add(row, row.get("title") or "", body, "message", e.id, e.ts)
            for a in e.data.get("attachments", ()):
                self._file(row, a["sha256"], a["name"], a["name"], "upload", e.ts)
        elif e.type == TOOL_RESULT:
            for s in e.data.get("meta", {}).get("sources", ()):
                identity = f"{s['url']}@S{s['n']}"
                body = s.get("snippet", "")
                if digest := s.get("text_ref"):
                    with (contextlib.suppress(OSError, ValueError, UnicodeError),
                          artifacts.blob(self.store, digest).open("r", encoding="utf-8") as stream):
                        body = stream.read(MAX_FILE_CHARS)
                self._add(row, s.get("title") or s["url"], f"{body}\n{s['url']}", "source", s["url"], e.ts,
                          identity)
            if a := e.data.get("meta", {}).get("artifact"):
                self._file(row, a["sha256"], a.get("title") or a["name"], a["path"], "file", e.ts, a["name"])

    def _add(self, row, title, body, kind, ref, at, identity=None) -> None:
        rid = "rec_" + hashlib.sha256(f"{row['id']}:{kind}:{identity or ref}".encode()).hexdigest()[:24]
        self.db.execute("delete from items where record_id=?", (rid,))
        self.db.execute("insert into items values (?,?,?,?,?,?,?,?,?)",
                        (title, body, kind, ref, row["id"], at, row["project_id"], "local", rid))

    def _file(self, row, digest, title, ref, kind, at, name=None) -> None:
        cur = self.db.execute("insert or ignore into files values (?,?)", (digest, row["id"]))
        if not cur.rowcount:
            return
        blob = artifacts.blob(self.store, digest)
        body = ""
        suffix = Path(name or title).suffix.lower()
        if blob.is_file():
            try:
                if suffix in documents.KINDS:
                    body = documents.parse(blob, documents.KINDS[suffix]).text()[:MAX_FILE_CHARS]
                elif suffix in artifacts.TEXT_SUFFIXES:
                    with blob.open("r", encoding="utf-8", errors="replace") as stream:
                        body = stream.read(MAX_FILE_CHARS)
            except Exception as exc:
                # A bad parser/file still produces a searchable filename; it does not hold the cursor.
                log.warning("attachment parse failed (%s): %s", digest[:12], type(exc).__name__)
        self._add(row, title, f"{name or title}\n{body}", kind, ref, at, f"{ref}:{digest}")

    def forget(self, session_id: str) -> None:
        for table in ("items", "progress", "files"):
            self.db.execute(f"delete from {table} where session_id=?", (session_id,))

    @staticmethod
    def _scope(session_ids, project_id, session_id, access_scope):
        if access_scope != "local":
            raise ValueError("only local record access is supported")
        if not session_ids:
            return ["0"], []
        where = [f"session_id in ({','.join('?' * len(session_ids))})", "access_scope=?"]
        args = [*session_ids, access_scope]
        if project_id:
            where.append("project_id=?")
            args.append(project_id)
        if session_id:
            where.append("session_id=?")
            args.append(session_id)
        return where, args

    def search(self, query: str, kind: str = "all", limit: int = 20, *, session_ids: list[str],
               project_id: str | None = None, session_id: str | None = None,
               access_scope: str = "local") -> list[dict[str, Any]]:
        terms = [t for t in re.split(r"\s+", query.strip()) if t]
        if not terms:
            return []
        long_terms = [t for t in terms if len(t) >= 3]
        where, args = self._scope(session_ids, project_id, session_id, access_scope)
        if long_terms:
            where.append("items match ?")
            args.append(" AND ".join('"' + t.replace('"', '""') + '"' for t in long_terms))
        for t in (t for t in terms if len(t) < 3):
            where.append("(body like ? escape '\\' or title like ? escape '\\')")
            escaped = t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            args += [f"%{escaped}%", f"%{escaped}%"]
        if kind != "all":
            kinds = KINDS[kind]
            where.append(f"kind in ({','.join('?' * len(kinds))})")
            args += list(kinds)
        order = "bm25(items)" if long_terms else "at desc"
        rows = self.db.execute(f"select title,body,kind,ref,session_id,at,project_id,access_scope,record_id "
                               f"from items where {' and '.join(where)} order by {order} limit ?",
                               (*args, max(1, min(limit, 100)))).fetchall()
        return [{**dict(r), "body": None, "snippet": snippet(r["body"], terms)} for r in rows]

    def read_record(self, ref: str, start: int, end: int, *, session_ids: list[str],
                    project_id: str | None = None, session_id: str | None = None,
                    access_scope: str = "local") -> dict[str, Any] | None:
        where, args = self._scope(session_ids, project_id, session_id, access_scope)
        if not 0 <= start < end <= start + 12_000:
            raise ValueError("range must use zero-based character offsets, at most 12000 characters")
        row = self.db.execute(f"select * from items where record_id=? and {' and '.join(where)}",
                              (ref, *args)).fetchone()
        if row is None:
            return None
        if start >= len(row["body"]) and start:
            raise ValueError("range starts beyond indexed text")
        result = dict(row)
        result.update(body=row["body"][start:end], start=start, end=min(end, len(row["body"])),
                      total_chars=len(row["body"]))
        return result


def snippet(body: str, terms: list[str], width: int = 160) -> str:
    flat = " ".join(body.split())
    low = flat.lower()
    at = min((i for t in terms if (i := low.find(t.lower())) >= 0), default=0)
    start = max(0, at - width // 3)
    return ("…" if start else "") + flat[start:start + width] + ("…" if start + width < len(flat) else "")
