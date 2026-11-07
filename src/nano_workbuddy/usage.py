"""Append-only model-call evidence, separate from the conversation projection.

A started call without a terminal event is deliberately unknown after a crash. Token totals
include cached input; raw provider usage is kept for later billing reconciliation.
"""

from __future__ import annotations

import contextvars
import fcntl
import json
import os
import time
from collections.abc import AsyncIterator, Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .llm import Delta, Model, Response
from .types import Message, new_id

PURPOSE = contextvars.ContextVar("model_call_purpose", default="agent")


@contextmanager
def purpose(name: str):
    token = PURPOSE.set(name)
    try:
        yield
    finally:
        PURPOSE.reset(token)


def append(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw = (json.dumps({"ts": time.time(), **event}, ensure_ascii=False) + "\n").encode()
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a+b") as out:
        fcntl.flock(out, fcntl.LOCK_EX)
        out.seek(0, os.SEEK_END)
        if out.tell():
            out.seek(-1, os.SEEK_END)
            if out.read(1) != b"\n":
                out.write(b"\n")  # keep a killed append as evidence; isolate it from the new event
        out.write(raw)
        out.flush()
        os.fsync(out.fileno())


def events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    lines = path.read_bytes().splitlines(keepends=True)
    # A killed append may leave one partial tail; never mutate it while reading.
    out = []
    for line in lines:
        if not line.endswith(b"\n"):
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            out.append({"type": "ledger.corrupt", "bytes": len(line)})
    return out


def totals(path: Path, run_id: str | None = None) -> dict[str, int]:
    out: dict[str, int] = {}
    for e in events(path):
        if e["type"] == "model.response" and (run_id is None or e.get("run_id") == run_id):
            for k, v in e["usage"].items():
                out[k] = out.get(k, 0) + v
    return out


class MeasuredModel:
    def __init__(self, model: Model, path: Path, run_id: str | None, before_call: Callable[[], None] | None = None):
        self.model, self.path, self.run_id = model, path, run_id
        self.spec = model.spec
        self.before_call = before_call

    async def stream(self, system: str, messages: list[Message], tools: list[dict[str, Any]],
                     tool_choice: str | None = None) -> AsyncIterator[Delta | Response]:
        if self.before_call:
            self.before_call()
        call_id = new_id("mc")
        base = {"call_id": call_id, "run_id": self.run_id, "purpose": PURPOSE.get(),
                "provider": self.spec.provider.name, "model": self.spec.id}
        retries = getattr(getattr(self.model, "client", None), "max_retries", None)
        append(self.path, {**base, "type": "model.started", "sdk_max_retries": retries})
        start, answered = time.monotonic(), False
        try:
            async for item in self.model.stream(system, messages, tools, tool_choice):
                if isinstance(item, Response):
                    if answered:
                        raise RuntimeError("model returned more than one final Response")
                    answered = True
                    seconds = time.monotonic() - start
                    item.meta["seconds"] = seconds
                    append(self.path, {**base, "type": "model.response", "stop": item.stop,
                                       "seconds": seconds, "usage": item.usage,
                                       "raw_usage": item.meta.get("raw_usage"),
                                       "request_id": item.meta.get("request_id"),
                                       "response_id": item.meta.get("response_id"),
                                       "model_version": item.meta.get("model", self.spec.id),
                                       "usage_complete": item.meta.get("usage_complete", False)})
                yield item
            if not answered:
                raise RuntimeError("model stream ended without a Response")
        except BaseException as exc:
            append(self.path, {**base, "type": "model.error", "error_type": type(exc).__name__,
                               "request_id": getattr(exc, "request_id", None),
                               "seconds": time.monotonic() - start})
            raise


def wire_usage(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    return json.loads(json.dumps(value, default=lambda obj: obj.model_dump(mode="json")
                                 if hasattr(obj, "model_dump") else vars(obj)))
