"""Search over the assistant's own records (M3): past conversations, cited sources, delivered files, attachments.

Scope chosen by the user (2026-10-24): only what the assistant itself produced or was given; no scanning of
other folders. The index is a cache in its own file (~/.nwb/knowledge.db), rebuilt from the session logs and
the content store at any time, and it follows deletions: a deleted session's rows go with it.

Chinese has no spaces, so SQLite's default tokenizer would treat a whole sentence as one token. The trigram
tokenizer indexes every 3-character window instead, which finds any substring of three or more characters;
shorter query terms (most two-character Chinese words) fall back to a substring scan, which is cheap at the
size of one person's records.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import artifacts, documents
from .log import SessionLog
from .types import ASSISTANT, TOOL_RESULT, USER, text_of

SCHEMA = """
create virtual table if not exists items using fts5(
  title, body, kind unindexed, ref unindexed, session_id unindexed, at unindexed, tokenize='trigram');
create table if not exists progress(session_id text primary key, lines integer not null, bytes integer not null);
create table if not exists files(sha256 text not null, session_id text not null, primary key(sha256, session_id));
"""
MAX_FILE_CHARS = 60_000
KINDS = {"messages": ("message",), "sources": ("source",), "files": ("file", "upload")}


class Knowledge:
    def __init__(self, path: Path, store: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.store = store

    # keeping up with the logs

    def sync(self, sessions: list[dict[str, Any]], open_log: Callable[[str], SessionLog]) -> int:
        """Index what was appended to each session since the last sync; returns how many entries were read.
        sessions: index rows (id, title, file_path); open_log: the process's live log object for a session."""
        n = 0
        for row in sessions:
            f = Path(row["file_path"])
            if not f.is_file():
                continue
            done = self.db.execute("select lines, bytes from progress where session_id=?", (row["id"],)).fetchone()
            size = f.stat().st_size
            if done and done["bytes"] == size:
                continue
            log = open_log(row["id"])
            start = done["lines"] if done and done["bytes"] < size else 0
            if start == 0 and done:  # the file shrank (rewritten): start over for this session
                self.forget(row["id"])
            self.db.execute("begin")
            for e in sorted(log.entries.values(), key=lambda e: e.seq):
                if e.seq >= start:
                    self._entry(row, e)
                    n += 1
            self.db.execute("insert or replace into progress(session_id, lines, bytes) values (?,?,?)",
                            (row["id"], log.lines, size))
            self.db.execute("commit")
        return n

    def _entry(self, row: dict[str, Any], e: Any) -> None:
        title = row.get("title") or ""
        if e.type in (USER, ASSISTANT):
            body = text_of([b for b in e.data.get("content", []) if "attachment" not in b]).strip()
            if body and not e.data.get("synthetic"):
                self._add(title, body, "message", e.id, row["id"], e.ts)
            for a in e.data.get("attachments", ()):
                self._file(row["id"], a["sha256"], a["name"], a["name"], "upload", e.ts)
        elif e.type == TOOL_RESULT and (meta := e.data.get("meta")):
            for s in meta.get("sources", ()):
                self.db.execute("delete from items where kind='source' and ref=? and session_id=?",
                                (s["url"], row["id"]))
                self._add(s.get("title") or s["url"], f"{s.get('snippet', '')}\n{s['url']}", "source", s["url"],
                          row["id"], e.ts)
            if a := meta.get("artifact"):
                self._file(row["id"], a["sha256"], a.get("title") or a["name"], a["path"], "file", e.ts, a["name"])

    def _add(self, title: str, body: str, kind: str, ref: str, session_id: str, at: float) -> None:
        self.db.execute("insert into items(title, body, kind, ref, session_id, at) values (?,?,?,?,?,?)",
                        (title, body, kind, ref, session_id, at))

    def _file(self, session_id: str, digest: str, title: str, ref: str, kind: str, at: float,
              name: str | None = None) -> None:
        """A delivered or attached file's text, once per content and session."""
        cur = self.db.execute("insert or ignore into files(sha256, session_id) values (?,?)", (digest, session_id))
        if not cur.rowcount:
            return
        blob = artifacts.blob(self.store, digest)
        body = ""
        suffix = Path(name or title).suffix.lower()
        if blob.is_file():
            try:
                if suffix in documents.KINDS:
                    body = documents.parse(blob, documents.KINDS[suffix]).text()
                elif suffix in artifacts.TEXT_SUFFIXES:
                    body = blob.read_bytes().decode("utf-8", "replace")
            except documents.DocumentError:
                body = ""
        self._add(title, f"{name or title}\n{body[:MAX_FILE_CHARS]}", kind, ref, session_id, at)

    def forget(self, session_id: str) -> None:
        for table in ("items", "progress", "files"):
            self.db.execute(f"delete from {table} where session_id=?", (session_id,))

    # searching

    def search(self, query: str, kind: str = "all", limit: int = 20) -> list[dict[str, Any]]:
        terms = [t for t in re.split(r"\s+", query.strip()) if t]
        if not terms:
            return []
        long_terms = [t for t in terms if len(t) >= 3]
        short_terms = [t for t in terms if len(t) < 3]
        where, args = [], []
        if long_terms:
            where.append("items match ?")
            args.append(" AND ".join('"' + t.replace('"', '""') + '"' for t in long_terms))
        for t in short_terms:  # below the trigram size: a substring scan
            where.append("(body like ? or title like ?)")
            args += [f"%{t}%", f"%{t}%"]
        if kind != "all":
            kinds = KINDS[kind]
            where.append(f"kind in ({','.join('?' * len(kinds))})")
            args += list(kinds)
        order = "bm25(items)" if long_terms else "at desc"
        rows = self.db.execute(f"select title, body, kind, ref, session_id, at from items where {' and '.join(where)} "
                               f"order by {order} limit ?", (*args, limit)).fetchall()
        return [{**dict(r), "body": None, "snippet": snippet(r["body"], terms)} for r in rows]


def snippet(body: str, terms: list[str], width: int = 160) -> str:
    """Around the first matching term, on one line."""
    flat = " ".join(body.split())
    low = flat.lower()
    at = min((i for t in terms if (i := low.find(t.lower())) >= 0), default=0)
    start = max(0, at - width // 3)
    return ("…" if start else "") + flat[start:start + width] + ("…" if start + width < len(flat) else "")
