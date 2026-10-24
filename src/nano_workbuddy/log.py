"""Append-only session log: one JSONL file per session; entries form a tree through parent_id.

Line 0 is the session header. Lines with an "id" are tree entries. Lines of type "checkout"
move the current leaf and are not part of the tree. A crash can leave a partial last line;
opening the file truncates it. Anything else that fails to parse is corruption and raises.

One writer at a time: every write happens under an exclusive flock on the file, and a run holds it from
start to end (writing()). A second process (the CLI next to the web server) that wants to write the same
session gets SessionBusy instead of interleaving its lines. When a process takes the lock it first reads
whatever another process appended since, so its in-memory tree is never stale when it writes. Readers
need no lock. The OS drops the lock when a process dies, so a crash never leaves a session stuck.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from .types import STATE, Entry, new_id

_LEAF = object()  # sentinel: attach to the current leaf


class SessionBusy(Exception):
    """Another process is writing this session."""


class SessionLog:
    def __init__(self, path: Path, header: dict[str, Any]):
        self.file = path
        self.header = header
        self.entries: dict[str, Entry] = {}
        self.leaf_id: str | None = None
        self.lines = 1  # header occupies line 0
        self._fd: int | None = None  # open, and locked, only while writing
        self._holds = 0  # nested writing() blocks
        self._size = 0  # bytes of the file already applied to this object
        self.listeners: list[Callable[[dict[str, Any]], None]] = []  # told about every durable change

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
        log._size = path.stat().st_size
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
        log._size = end
        return log

    def _apply(self, d: dict[str, Any], seq: int) -> None:
        if "id" in d:
            e = Entry.from_json(d, seq)
            self.entries[e.id] = e
            self.leaf_id = e.id
        elif d["type"] == "checkout":
            self.leaf_id = d["leaf_id"]

    # writing

    @contextlib.contextmanager
    def writing(self) -> Iterator[None]:
        """Hold the write lock for a whole block (a run); re-entrant. Raises SessionBusy at once if another
        process holds it."""
        if self._holds == 0:
            self._lock()
        self._holds += 1
        try:
            yield
        finally:
            if self._holds > 0:  # close() may already have released it
                self._holds -= 1
            if self._holds == 0:
                self.close()

    def _lock(self) -> None:
        fd = os.open(self.file, os.O_WRONLY | os.O_APPEND)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise SessionBusy(f"session {self.id} is being written by another process (the CLI or another "
                              "server); try again when it has finished") from None
        self._fd = fd
        self._catch_up()

    def _catch_up(self) -> None:
        """Apply lines another process appended since this object last wrote or read the file."""
        size = os.fstat(self._fd).st_size
        if size <= self._size:
            return
        with open(self.file, "rb") as f:
            f.seek(self._size)
            raw = f.read(size - self._size)
        end = raw.rfind(b"\n") + 1
        if end < len(raw):  # that process died mid-line: the same repair as open()
            os.truncate(self.file, self._size + end)
        for line in raw[:end].decode("utf-8").splitlines():
            d, seq = json.loads(line), self.lines
            self._apply(d, seq)
            self.lines += 1
            if "id" in d:
                self._notify({"type": "entry", "seq": seq, "entry": entry_json(self.entries[d["id"]])})
            elif d["type"] == "checkout":
                self._notify({"type": "checkout", "seq": seq, "leaf_id": d["leaf_id"]})
        self._size += end

    def _write(self, d: dict[str, Any], durable: bool) -> int:
        line = (json.dumps(d, ensure_ascii=False) + "\n").encode("utf-8")
        with self.writing():
            os.write(self._fd, line)
            if durable:
                os.fsync(self._fd)
            self._size += len(line)
            seq, self.lines = self.lines, self.lines + 1
        return seq

    def append(self, type: str, data: dict[str, Any], *, parent_id: Any = _LEAF,
               run_id: str | None = None, durable: bool = True) -> Entry:
        with self.writing():  # lock (and catch up) before choosing the parent
            e = Entry(new_id("e"), self.leaf_id if parent_id is _LEAF else parent_id, type, data, run_id)
            e.seq = self._write(e.to_json(), durable)
        self.entries[e.id] = e
        self.leaf_id = e.id
        self._notify({"type": "entry", "seq": e.seq, "entry": entry_json(e)})
        return e

    def checkout(self, entry_id: str | None) -> None:
        with self.writing():
            if entry_id is not None and entry_id not in self.entries:
                raise KeyError(entry_id)
            seq = self._write({"type": "checkout", "leaf_id": entry_id, "ts": time.time()}, durable=False)
        self.leaf_id = entry_id
        self._notify({"type": "checkout", "seq": seq, "leaf_id": entry_id})

    def _notify(self, event: dict[str, Any]) -> None:
        for listener in self.listeners:
            listener(event)

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

    def latest_leaf(self, entry_id: str) -> str:
        """The most recently written entry in entry_id's subtree: where the user last was on that branch."""
        kids: dict[str | None, list[Entry]] = {}
        for e in self.entries.values():
            kids.setdefault(e.parent_id, []).append(e)
        best, stack = self.entries[entry_id], [self.entries[entry_id]]
        while stack:
            e = stack.pop()
            best = e if e.seq > best.seq else best
            stack.extend(kids.get(e.id, []))
        return best.id

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
        with new.writing():
            for e in self.path(entry_id):
                new._write(e.to_json(), durable=False)
                new._apply(e.to_json(), new.lines - 1)
            os.fsync(new._fd)
        return new

    def close(self) -> None:
        """Release the write lock (if held)."""
        self._holds = 0
        if self._fd is not None:
            os.close(self._fd)  # closing the descriptor drops its flock
            self._fd = None


def entry_json(e: Entry) -> dict[str, Any]:
    """Wire form of an entry for clients: the stored form plus its seq."""
    return {**e.to_json(), "seq": e.seq}
