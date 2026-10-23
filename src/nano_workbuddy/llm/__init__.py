"""Provider adapters. Each adapter turns neutral Messages into one wire protocol and back (D7, D8)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from ..config import ModelSpec, Provider
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


def connect(spec: ModelSpec, api_key: str) -> Model:
    match spec.provider.protocol:
        case "openai-chat":
            from .openai_chat import OpenAIChat

            return OpenAIChat(spec, api_key)
        case "anthropic":
            from .anthropic import AnthropicMessages

            return AnthropicMessages(spec, api_key)
    raise ValueError(f"unsupported protocol {spec.provider.protocol!r}")


async def list_models(provider: Provider, api_key: str) -> list[dict[str, Any]]:
    """Model ids the endpoint offers. Anthropic also reports context window and output limit;
    OpenAI-compatible endpoints only list ids, so those limits must be filled in by hand."""
    if provider.protocol == "anthropic":
        import anthropic

        client = anthropic.AsyncAnthropic(base_url=provider.base_url, api_key=api_key, max_retries=1)
        return [{"id": m.id, "context_window": getattr(m, "max_input_tokens", None),
                 "max_output": getattr(m, "max_tokens", None)} async for m in client.models.list()]
    import openai

    client = openai.AsyncOpenAI(base_url=provider.base_url, api_key=api_key, max_retries=1)
    return sorted([{"id": m.id} async for m in client.models.list()], key=lambda m: m["id"])
