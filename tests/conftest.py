from __future__ import annotations

import asyncio
import copy
import inspect
from pathlib import Path
from typing import Any

import pytest

from nano_workbuddy.config import load
from nano_workbuddy.llm import Delta, Response
from nano_workbuddy.runtime import Workspace
from nano_workbuddy.types import ToolCall, new_id, text

HANG = "hang"  # script item: stream some text, then block until cancelled


def call(name: str, **args: Any) -> ToolCall:
    return {"type": "tool_call", "id": new_id("call"), "name": name, "args": args}


def reply(s: str | None = None, *calls: ToolCall, stop: str | None = None) -> Response:
    content = ([text(s)] if s else []) + list(calls)
    return Response(content, stop or ("tool_use" if calls else "end"), {"input": 10, "output": 5},
                    {"provider": "fake", "model": "fake"})


class FakeModel:
    """Plays a script of responses. Items: Response, callable(messages) -> Response or awaitable, exception, HANG."""

    def __init__(self, *script: Any):
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []

    async def stream(self, system, messages, tools, tool_choice=None):
        self.requests.append({"system": system, "messages": copy.deepcopy(messages), "tools": tools,
                              "tool_choice": tool_choice})
        item = self.script.pop(0)
        if callable(item):
            item = item(messages)
        if inspect.isawaitable(item):  # async items let a test hold the model until it is ready
            item = await item
        if isinstance(item, BaseException):
            raise item
        if item == HANG:
            yield Delta("text", "partial answer")
            await asyncio.sleep(3600)
        for b in item.content:
            if b["type"] == "text":
                yield Delta("text", b["text"])
        yield item


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "home"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    p = tmp_path / "project"
    p.mkdir()
    return p


@pytest.fixture
def make_ws(home: Path):
    def make(model: FakeModel) -> Workspace:
        return Workspace(load(home), connect=lambda spec: model)

    return make
