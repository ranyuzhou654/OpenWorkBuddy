"""Provider adapters. Each adapter turns neutral Messages into one wire protocol and back (D7, D8)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from ..config import ModelSpec
from ..types import Block, Message, Stop


@dataclass(slots=True)
class Delta:
    kind: Literal["text", "thinking"]
    text: str


@dataclass(slots=True)
class Response:
    content: list[Block]
    stop: Stop
    usage: dict[str, int] = field(default_factory=dict)  # input, output, cache_read
    meta: dict[str, Any] = field(default_factory=dict)  # provider, model


class ContextOverflow(Exception):
    """The request did not fit the model's context window."""


class Model(Protocol):
    spec: ModelSpec

    def stream(self, system: str, messages: list[Message], tools: list[dict[str, Any]],
               tool_choice: str | None = None) -> AsyncIterator[Delta | Response]:
        """Yield deltas while streaming, then exactly one final Response."""
        ...


def connect(spec: ModelSpec) -> Model:
    match spec.provider.protocol:
        case "openai-chat":
            from .openai_chat import OpenAIChat

            return OpenAIChat(spec)
    raise ValueError(f"unsupported protocol {spec.provider.protocol!r}")
