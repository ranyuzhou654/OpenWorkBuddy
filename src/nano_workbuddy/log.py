"""Append-only session log: one JSONL file per session; entries form a tree through parent_id.

Line 0 is the session header. Lines with an "id" are tree entries. Lines of type "checkout"
move the current leaf and are not part of the tree. A crash can leave a partial last line;
opening the file truncates it. Anything else that fails to parse is corruption and raises.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from .types import STATE, Entry, new_id

_LEAF = object()  # sentinel: attach to the current leaf


class SessionLog:
    def __init__(self, path: Path, header: dict[str, Any]):
        self.file = path
        self.header = header
        self.entries: dict[str, Entry] = {}
        self.leaf_id: str | None = None
        self.lines = 1  # header occupies line 0
        self._fd: int | None = None

    @property
    def id(self) -> str:
        return self.header["id"]

    @classmethod
    def create(cls, path: Path, session_id: str, project_id: str, forked_from: str | None = None) -> SessionLog:
        path.parent.mkdir(parents=True, exist_ok=True)
        header = {"type": "session", "v": 1, "id": session_id, "project_id": project_id,
                  "forked_from": forked_from, "ts": time.time()}
        log = cls(path, header)
        with open(path, "x", encoding="utf-8") as f:
            f.write(json.dumps(header, ensure_ascii=False) + "\n")
        return log

    @classmethod
    def open(cls, path: Path) -> SessionLog:
        raw = path.read_bytes()
        end = raw.rfind(b"\n") + 1
        if end < len(raw):  # partial line from a crash mid-write
            with open(path, "r+b") as f:
                f.truncate(end)
            raw = raw[:end]
        lines = raw.decode("utf-8").splitlines()
        log = cls(path, json.loads(lines[0]))
        for seq, line in enumerate(lines[1:], start=1):
            log._apply(json.loads(line), seq)
        log.lines = len(lines)
        return log

    def _apply(self, d: dict[str, Any], seq: int) -> None:
        if "id" in d:
            e = Entry.from_json(d, seq)
            self.entries[e.id] = e
            self.leaf_id = e.id
        elif d["type"] == "checkout":
            self.leaf_id = d["leaf_id"]

    def _write(self, d: dict[str, Any], durable: bool) -> int:
        if self._fd is None:
            self._fd = os.open(self.file, os.O_WRONLY | os.O_APPEND)
        os.write(self._fd, (json.dumps(d, ensure_ascii=False) + "\n").encode("utf-8"))
        if durable:
            os.fsync(self._fd)
        seq, self.lines = self.lines, self.lines + 1
        return seq

    def append(self, type: str, data: dict[str, Any], *, parent_id: Any = _LEAF,
               run_id: str | None = None, durable: bool = True) -> Entry:
        e = Entry(new_id("e"), self.leaf_id if parent_id is _LEAF else parent_id, type, data, run_id)
        e.seq = self._write(e.to_json(), durable)
        self.entries[e.id] = e
        self.leaf_id = e.id
        return e

    def checkout(self, entry_id: str | None) -> None:
        if entry_id is not None and entry_id not in self.entries:
            raise KeyError(entry_id)
        self._write({"type": "checkout", "leaf_id": entry_id, "ts": time.time()}, durable=False)
        self.leaf_id = entry_id

    def path(self, leaf_id: str | None = None) -> list[Entry]:
        """Entries from the root to leaf_id (default: the current leaf)."""
        out, cur = [], leaf_id or self.leaf_id
        while cur is not None:
            e = self.entries[cur]
            out.append(e)
            cur = e.parent_id
        out.reverse()
        return out

    def children(self, entry_id: str | None) -> list[Entry]:
        return [e for e in self.entries.values() if e.parent_id == entry_id]

    def resolve(self, prefix: str) -> str:
        """Find an entry by id or unique id prefix."""
        hits = [i for i in self.entries if i == prefix or i.startswith(prefix)]
        if len(hits) != 1:
            raise KeyError(f"{prefix!r} matches {len(hits)} entries")
        return hits[0]

    def state(self) -> dict[str, Any]:
        """Latest state entry on the current path."""
        return next((e.data for e in reversed(self.path()) if e.type == STATE), {})

    def fork(self, entry_id: str, path: Path, session_id: str) -> SessionLog:
        """Copy the root→entry_id path into a new session file."""
        new = SessionLog.create(path, session_id, self.header["project_id"], forked_from=self.id)
        for e in self.path(entry_id):
            new._write(e.to_json(), durable=False)
            new._apply(e.to_json(), new.lines - 1)
        if new._fd is not None:
            os.fsync(new._fd)
        return new

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
