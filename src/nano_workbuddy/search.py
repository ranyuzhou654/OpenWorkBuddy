"""Web search backends (M1). One small interface, so the provider can be swapped without touching the tool.

A backend returns normalised hits: title, url, snippet, and the publication date only when the provider
knows it. The date a page was retrieved is never passed off as its publication date.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx


class SearchError(Exception):
    """The search could not be done (bad key, quota, network); the message says which."""


@dataclass(frozen=True, slots=True)
class Hit:
    title: str
    url: str
    snippet: str
    published: str | None = None


class SearchProvider(Protocol):
    name: str

    async def search(self, query: str, max_results: int, recency: str | None, topic: str) -> list[Hit]: ...


class Tavily:
    """https://docs.tavily.com: a search API made for LLM agents; snippets are extracted page content."""

    name = "tavily"
    URL = "https://api.tavily.com/search"

    def __init__(self, api_key: str, transport: httpx.AsyncBaseTransport | None = None):
        self.api_key = api_key
        self.transport = transport  # tests inject a mock transport

    async def search(self, query: str, max_results: int, recency: str | None, topic: str) -> list[Hit]:
        body = {"query": query, "max_results": max_results, "search_depth": "basic", "topic": topic,
                "include_answer": False}
        if recency:
            body["time_range"] = recency
        try:
            async with httpx.AsyncClient(timeout=30, transport=self.transport) as client:
                r = await client.post(self.URL, json=body, headers={"Authorization": f"Bearer {self.api_key}"})
        except httpx.HTTPError as e:
            raise SearchError(f"cannot reach Tavily: {type(e).__name__}: {e}. If you need a proxy, set "
                              "HTTPS_PROXY for the server.") from e
        if r.status_code in (401, 403):
            raise SearchError("Tavily rejected the API key; check it in Settings → 联网搜索")
        if r.status_code in (429, 432, 433):
            raise SearchError(f"Tavily quota or rate limit reached (HTTP {r.status_code}); try again later")
        if r.status_code >= 400:
            raise SearchError(f"Tavily returned HTTP {r.status_code}: {r.text[:300]}")
        return [Hit(x.get("title") or x["url"], x["url"], " ".join((x.get("content") or "").split()),
                    x.get("published_date") or None)
                for x in r.json().get("results", []) if x.get("url")]


def provider(name: str, api_key: str) -> SearchProvider:
    if name == "tavily":
        return Tavily(api_key)
    raise ValueError(f"unknown search provider {name!r}")
