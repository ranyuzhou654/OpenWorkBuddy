"""web_fetch (D11) and web_search (M1).

web_search asks the configured search backend and records each hit as a numbered source ([S#], kind
"search": a snippet only). web_fetch reads a page and records it as a source read in full (kind "page"),
which upgrades a search hit for the same URL. The records travel in the results' meta.

web_fetch: fetch a URL and return its readable text.

The permission resource is the URL, so "always allow" grants one site (scheme://host/*). Redirects are
followed only within the same site: a redirect elsewhere is reported to the model, whose next fetch is
then checked for that site. HTML becomes plain text with headings, lists and links kept; images come
back as images; anything else (PDF, archives) is saved to the session's private folder.
"""

from __future__ import annotations

import base64
import hashlib
import os
import time
from html.parser import HTMLParser
from typing import Literal
from urllib.parse import urljoin, urlsplit

import httpx
from pydantic import Field

from .. import artifacts, documents
from ..search import SearchError
from ..types import text
from .builtin import MAX_IMAGE_BYTES, Args
from .registry import Result, ToolCtx, ToolError, tool

MAX_BYTES = 10 * 1024 * 1024
MAX_REDIRECTS = 5
USER_AGENT = "Mozilla/5.0 (compatible; nano-workbuddy; +https://github.com)"
TEXT_TYPES = ("text/", "application/json", "application/xml", "application/javascript", "+json", "+xml")
SKIP = {"script", "style", "noscript", "svg", "template", "iframe", "head"}
BLOCKS = {"p", "div", "section", "article", "main", "header", "footer", "nav", "aside", "table", "tr", "form",
          "blockquote", "figure", "figcaption", "br", "hr", "ul", "ol", "dl", "dt", "dd"}


def site(url: str) -> str:
    u = urlsplit(url)
    return f"{u.scheme}://{u.netloc}"


def normalize(url: str) -> str:
    u = urlsplit(url.strip())
    if u.scheme not in ("http", "https") or not u.netloc:
        raise ToolError("url must start with http:// or https://")
    return u._replace(path=u.path or "/", fragment="").geturl()


class _Text(HTMLParser):
    """Readable text from HTML: block structure as line breaks, headings as #, links as [text](url)."""

    def __init__(self, base: str):
        super().__init__(convert_charrefs=True)
        self.base, self.out, self.skip, self.title = base, [], 0, ""
        self.href: str | None = None
        self.in_title = self.in_pre = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in SKIP:
            self.skip += 1
        elif tag == "title":
            self.in_title = True
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.out.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "li":
            self.out.append("\n- ")
        elif tag == "pre":
            self.in_pre = True
            self.out.append("\n```\n")
        elif tag == "a" and a.get("href") and not a["href"].startswith(("#", "javascript:")):
            self.href = urljoin(self.base, a["href"])
            self.out.append("[")
        elif tag == "img" and a.get("alt"):
            self.out.append(f"[image: {a['alt']}]")
        elif tag in BLOCKS:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag == "title":
            self.in_title = False
        elif tag == "pre":
            self.in_pre = False
            self.out.append("\n```\n")
        elif tag == "a" and self.href:
            self.out.append(f"]({self.href})")
            self.href = None
        elif tag in BLOCKS or tag.startswith("h") and tag[1:].isdigit():
            self.out.append("\n")

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        elif not self.skip:
            self.out.append(data if self.in_pre else " ".join(data.split()) + (" " if data[-1:].isspace() else ""))

    def result(self) -> str:
        body, blank, fence = [], False, False
        for ln in "".join(self.out).splitlines():
            if ln.strip() == "```":
                fence = not fence
            ln = ln.rstrip() if fence else ln.strip()  # code keeps its indentation
            if ln or fence or not blank:  # collapse runs of empty lines outside code
                body.append(ln)
            blank = not ln
        title = " ".join(self.title.split())
        return (f"# {title}\n\n" if title else "") + "\n".join(body).strip()


def html_to_text(html: str, base: str) -> str:
    p = _Text(base)
    p.feed(html)
    p.close()
    return p.result()


class FetchArgs(Args):
    url: str = Field(description="http(s) URL to fetch")


@tool(effect="read", parallel=True, scope=lambda args, ctx: ("web_fetch", normalize(args.url)))
async def web_fetch(args: FetchArgs, ctx: ToolCtx) -> Result:
    """Fetch a web page or file over HTTP(S) and return its readable text (HTML is converted to text with
    links kept). Images come back as images; other binary files are saved and their path returned."""
    url = normalize(args.url)
    async with httpx.AsyncClient(timeout=httpx.Timeout(30), headers={"User-Agent": USER_AGENT}) as client:
        for _ in range(MAX_REDIRECTS + 1):
            async with client.stream("GET", url) as r:
                if r.is_redirect and (to := r.headers.get("location")):
                    nxt = urljoin(url, to)
                    if site(nxt) != site(url):  # a new site needs its own permission check
                        return Result.ok(f"{url} redirects to another site: {nxt}\nFetch that URL to continue.")
                    url = normalize(nxt)
                    continue
                body = bytearray()
                async for chunk in r.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_BYTES:
                        raise ToolError(f"response is larger than {MAX_BYTES} bytes")
                return _result(r, bytes(body), url, ctx)
    raise ToolError(f"more than {MAX_REDIRECTS} redirects")


def _result(r: httpx.Response, body: bytes, url: str, ctx: ToolCtx) -> Result:
    kind = r.headers.get("content-type", "").split(";")[0].strip().lower()
    if r.status_code >= 400:
        return Result.error(f"HTTP {r.status_code} for {url}\n" + body[:2000].decode("utf-8", "replace"))

    def cited(title: str, source_kind: str = "page") -> tuple[str, dict]:
        rec = artifacts.cite(ctx.sources, url, source_kind, title, retrieved_at=_now(),
                             sha256=hashlib.sha256(body).hexdigest())
        return f"[S{rec['n']}] {url} ({r.status_code}, {kind or 'unknown type'})\n\n", {"sources": [rec]}

    if kind.startswith("image/") and len(body) <= MAX_IMAGE_BYTES:
        head, meta = cited("", "file")
        return Result([text(head.strip()), {"type": "image", "mime": kind, "data": base64.b64encode(body).decode()}],
                      meta=meta)
    if kind == "text/html" or kind == "application/xhtml+xml":
        page = html_to_text(body.decode(r.encoding or "utf-8", "replace"), url)
        head, meta = cited(page[2:page.index("\n")] if page.startswith("# ") and "\n" in page else "")
        return Result([text(head + page)], meta=meta)
    if any(t in kind for t in TEXT_TYPES) or not kind and b"\0" not in body[:8192]:
        head, meta = cited("")
        return Result([text(head + body.decode(r.encoding or "utf-8", "replace"))], meta=meta)
    name = (urlsplit(url).path.rsplit("/", 1)[-1] or "download").replace("..", "_")
    ctx.spill_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    f = ctx.spill_dir / f"{ctx.call_id or 'web'}-{name}"
    with os.fdopen(os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600), "wb") as out:
        out.write(body)
    head, meta = cited(name, "file")
    how = ("Read it with the read tool (pages=... for long documents)." if documents.is_document(f)
           else "Inspect it with bash, e.g. `file` or a converter for its format.")
    return Result([text(f"{head}Binary content ({len(body)} bytes) saved to {f}. {how}")], meta=meta)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class SearchArgs(Args):
    query: str = Field(description="What to search for; specific queries work best")
    max_results: int = Field(5, ge=1, le=10)
    recency: Literal["any", "day", "week", "month", "year"] = Field(
        "any", description="Only results published within this period")
    topic: Literal["general", "news"] = Field("general", description="news: recent reporting, with publication dates")


@tool(effect="read", parallel=True, scope=lambda args, ctx: ("web_search", args.query))
async def web_search(args: SearchArgs, ctx: ToolCtx) -> Result:
    """Search the web. Returns numbered sources [S#] with snippets; snippets are not the page: fetch a page
    with web_fetch before relying on its details. Cite the numbers in answers and documents."""
    if ctx.search is None:
        raise ToolError("web search is not configured; the user can add a key in Settings → 联网搜索")
    try:
        hits = await ctx.search.search(args.query, args.max_results, None if args.recency == "any" else args.recency,
                                       args.topic)
    except SearchError as e:
        raise ToolError(str(e)) from e
    if not hits:
        return Result.ok(f"No results for {args.query!r}.")
    when, recs, lines = _now(), [], []
    for h in hits:
        rec = artifacts.cite(ctx.sources, h.url, "search", h.title, snippet=h.snippet[:500], published=h.published,
                             retrieved_at=when, provider=ctx.search.name)
        recs.append(rec)
        date = f"published {h.published}" if h.published else "publication date unknown"
        lines.append(f"[S{rec['n']}] {h.title}\n{h.url}\n{date}\n{h.snippet[:800]}")
    head = (f"Results for {args.query!r} ({ctx.search.name}, retrieved {when}). These are search snippets; "
            "fetch a page before relying on details.\n\n")
    return Result([text(head + "\n\n".join(lines))], meta={"sources": recs})
