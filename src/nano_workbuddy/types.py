"""Neutral message and log types shared by every layer."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal, NotRequired, TypedDict


class Text(TypedDict):
    type: Literal["text"]
    text: str


class Image(TypedDict):
    type: Literal["image"]
    mime: str
    data: str  # base64


class Thinking(TypedDict):
    type: Literal["thinking"]
    text: str
    provider: str  # replayed only to the provider that produced it
    meta: NotRequired[dict[str, Any]]  # opaque provider fields (signature, wire field name, ...)
    stale: NotRequired[bool]  # produced before a prune/compaction edited history (set in projection only)


class ToolCall(TypedDict):
    type: Literal["tool_call"]
    id: str
    name: str
    args: dict[str, Any]
    error: NotRequired[str]  # set when the provider sent unparseable arguments


type Block = Text | Image | Thinking | ToolCall
type Stop = Literal["end", "tool_use", "length", "error", "aborted"]


class Message(TypedDict):
    """What the model layer consumes: a projection of log entries."""

    role: Literal["user", "assistant", "tool"]
    content: list[Block]
    call_id: NotRequired[str]
    is_error: NotRequired[bool]


# Log entry types. Only these form the conversation tree.
SYSTEM_PROMPT = "system_prompt"
CONTEXT_STATE = "context_state"  # host snapshot of current instructions, memory and loaded skill versions
USER = "user"
ASSISTANT = "assistant"
TOOL_STARTED = "tool_started"
TOOL_RESULT = "tool_result"
STATE = "state"
EVIDENCE = "evidence"  # host-recorded source excerpts exposed in a model request; never a factual verdict
NOTICE = "notice"  # context the harness gives the model mid-session (permissions...), sent as user-role text
PRUNE = "prune"  # old tool results up to a seq are shown as pointers to saved copies
COMPACTION = "compaction"  # a summary that stands in for everything before first_kept_id
BRANCH_SUMMARY = "branch_summary"  # what happened on the branch the user just left (Pi's branch summary)


def text(s: str) -> Text:
    return {"type": "text", "text": s}


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def text_of(blocks: list[Block]) -> str:
    return "".join(b["text"] for b in blocks if b["type"] == "text")


@dataclass(slots=True)
class Entry:
    id: str
    parent_id: str | None
    type: str
    data: dict[str, Any]
    run_id: str | None = None
    ts: float = field(default_factory=time.time)
    seq: int = 0  # line number in the session file; derived, never written

    def to_json(self) -> dict[str, Any]:
        d = {"id": self.id, "parent_id": self.parent_id, "type": self.type, "data": self.data, "ts": self.ts}
        if self.run_id:
            d["run_id"] = self.run_id
        return d

    @classmethod
    def from_json(cls, d: dict[str, Any], seq: int) -> Entry:
        return cls(d["id"], d["parent_id"], d["type"], d["data"], d.get("run_id"), d["ts"], seq)

    @property
    def tool_calls(self) -> list[ToolCall]:
        if self.type != ASSISTANT:
            return []
        return [b for b in self.data["content"] if b["type"] == "tool_call"]
