"""A tool is an async function plus a Pydantic argument model. No class hierarchy."""

from __future__ import annotations

import inspect
import logging
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from ..sandbox import SandboxPolicy
from ..types import Image, Text, ToolCall, text

log = logging.getLogger(__name__)

type Effect = Literal["read", "write", "exec"]
type Verdict = Literal["done", "not_done", "unknown"]


@dataclass(slots=True)
class ToolCtx:
    root: Path  # project root; relative paths resolve here
    session_id: str
    spill_dir: Path
    emit: Callable[[dict[str, Any]], None] = lambda event: None
    read_stamps: dict[str, float] = field(default_factory=dict)  # path -> mtime at last read
    call_id: str = ""
    sandbox: SandboxPolicy | None = None  # None: commands run unconfined (full-trust preset)
    tmp: Path | None = None  # writable scratch directory for sandboxed commands
    tools: Registry | None = None  # the run's own registry, for tool_search


@dataclass(slots=True)
class Result:
    content: list[Text | Image]
    is_error: bool = False
    meta: dict[str, Any] = field(default_factory=dict)  # kept in the log entry, never shown to the model

    @classmethod
    def ok(cls, s: str) -> Result:
        return cls([text(s)])

    @classmethod
    def error(cls, s: str) -> Result:
        return cls([text(s if s.startswith("Error") else f"Error: {s}")], is_error=True)


class ToolError(Exception):
    """An expected failure the model should see and can react to."""


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    args: type[BaseModel]
    fn: Callable[[Any, ToolCtx], Awaitable[Result]]
    effect: Effect
    parallel: bool  # safe to run concurrently with other parallel tools
    recover: Callable[[Any, ToolCtx], Verdict] | None  # after a crash: did the call already take effect?
    scope: Callable[[Any, ToolCtx], tuple[str, str]] | None  # (action, resource) for permission rules
    input_schema: dict[str, Any] | None = None  # given by the tool's owner (MCP) instead of derived from args

    def permission_scope(self, args: Any, ctx: ToolCtx) -> tuple[str, str]:
        return self.scope(args, ctx) if self.scope else (self.name, "")

    def schema(self) -> dict[str, Any]:
        if self.input_schema is not None:
            return {"name": self.name, "description": self.description, "parameters": self.input_schema}
        params = self.args.model_json_schema()
        params.pop("title", None)
        for p in params.get("properties", {}).values():
            p.pop("title", None)
        return {"name": self.name, "description": self.description, "parameters": params}


def tool(*, effect: Effect, parallel: bool = False, recover: Callable[[Any, ToolCtx], Verdict] | None = None,
         scope: Callable[[Any, ToolCtx], tuple[str, str]] | None = None):
    def wrap(fn: Callable[[Any, ToolCtx], Awaitable[Result]]) -> Tool:
        args = next(iter(inspect.signature(fn, eval_str=True).parameters.values())).annotation
        return Tool(fn.__name__, inspect.cleandoc(fn.__doc__ or ""), args, fn, effect, parallel, recover, scope)

    return wrap


class AnyArgs(BaseModel):
    """Arguments of a tool whose schema we only pass through; its owner validates them."""

    model_config = ConfigDict(extra="allow")


class Registry:
    """All tools a run may call. Deferred tools are callable but declared to the model only after
    tool_search loaded them (D14), so dozens of rarely used schemas do not ride along every request."""

    def __init__(self, tools: list[Tool], deferred: Iterable[str] = ()):
        self.tools = {t.name: t for t in tools}  # insertion order is the declared order (stable prefix, D9)
        self.deferred = set(deferred)

    def get(self, name: str) -> Tool | None:
        return self.tools.get(name)

    def schemas(self, loaded: Iterable[str] = ()) -> list[dict[str, Any]]:
        """Direct tools in declared order, then loaded deferred tools in the order they were loaded,
        so loading one more tool only appends to the declaration."""
        direct = [t.schema() for n, t in self.tools.items() if n not in self.deferred]
        return direct + [self.tools[n].schema() for n in dict.fromkeys(loaded) if n in self.deferred]

    def search(self, query: str, limit: int = 5) -> list[Tool]:
        """Deferred tools ranked by keyword hits; "select:a,b" picks tools by exact name."""
        pool = [self.tools[n] for n in self.tools if n in self.deferred]
        if query.startswith("select:"):
            names = {n.strip() for n in query.removeprefix("select:").split(",")}
            return [t for t in pool if t.name in names]
        terms = [w for w in re.split(r"[^\w]+", query.lower()) if w]

        def score(t: Tool) -> int:
            name, desc = t.name.lower(), t.description.lower()
            return sum(3 * (w in name) + desc.count(w) for w in terms)

        ranked = sorted((t for t in pool if score(t)), key=score, reverse=True)
        return ranked[:limit]


def parse_args(t: Tool, call: ToolCall) -> BaseModel:
    if "error" in call:
        raise ToolError(call["error"])
    try:
        return t.args.model_validate(call["args"])
    except ValidationError as e:
        raise ToolError(f"invalid arguments for {t.name}: {e.errors(include_url=False)}") from e


async def execute(t: Tool, call: ToolCall, ctx: ToolCtx) -> Result:
    """Run one call. Tool failures become error results; cancellation propagates."""
    try:
        return await t.fn(parse_args(t, call), ctx)
    except ToolError as e:
        return Result.error(str(e))
    except Exception as e:  # a broken tool must not kill the run; the model sees the failure
        log.exception("tool %s failed", t.name)
        return Result.error(f"{type(e).__name__}: {e}")

