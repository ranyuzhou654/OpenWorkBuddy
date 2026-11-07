"""Deliverables, sources and attachments (M1): create_document / present, web_search, uploads."""

import asyncio
import json

import httpx
import pytest
from conftest import FakeModel, call, reply
from test_api import Server, Stream, finished

from nano_workbuddy import artifacts, search
from nano_workbuddy.config import load
from nano_workbuddy.runtime import Workspace
from nano_workbuddy.tools import BUILTIN, Registry, ToolCtx, execute
from nano_workbuddy.types import TOOL_RESULT, USER, text_of

TOOLS = Registry(BUILTIN)


def user_message(request):
    """The user's own message in a model request (notices may follow it)."""
    return next(m for m in reversed(request["messages"]) if m["role"] == "user"
                and not text_of(m["content"]).startswith("<system-notice>"))["content"]


def ctx(project, tmp_path, **kw):
    return ToolCtx(root=project, session_id="s", spill_dir=tmp_path / "spill", store=tmp_path / "store", **kw)


async def run(c, name, **args):
    r = await execute(TOOLS.get(name), {"type": "tool_call", "id": "c", "name": name, "args": args}, c)
    return r, text_of(r.content)


async def test_create_document_versions_and_overwrite_guard(project, tmp_path):
    c = ctx(project, tmp_path)
    r, out = await run(c, "create_document", path="out/brief.docx", content="# 简报\n\n正文", title="简报")
    a = r.meta["artifact"]
    assert not r.is_error and (project / "out/brief.docx").is_file() and a["version"] == 1 and a["kept"]
    assert all(ch["ok"] for ch in a["checks"] if ch["layer"] == "structure") and "Delivered" in out
    assert all(ch["ok"] is None for ch in a["checks"] if ch["layer"] in ("numbers", "human"))
    assert artifacts.blob(tmp_path / "store", a["sha256"]).read_bytes() == (project / "out/brief.docx").read_bytes()

    r, out = await run(c, "create_document", path="out/brief.docx", content="# 第二版")
    assert r.is_error and "already exists" in out
    r, _ = await run(c, "create_document", path="out/brief.docx", content="# 第二版", overwrite=True)
    assert r.meta["artifact"]["version"] == 2

    (project / "out/brief.docx").write_bytes(b"edited by the user")  # changed behind our back
    r, out = await run(c, "create_document", path="out/brief.docx", content="# 第三版", overwrite=True)
    assert r.is_error and "changed after you delivered it" in out
    (project / "mine.pdf").write_bytes(b"%PDF the user's own")
    r, out = await run(c, "create_document", path="mine.pdf", content="x", overwrite=True)
    assert r.is_error and "may be the user's work" in out
    r, out = await run(c, "create_document", path="x.pptx", content="x")
    assert r.is_error and "unsupported format" in out
    assert [p.name for p in (project / "out").iterdir()] == ["brief.docx"]  # no temporary files left behind


async def test_present_checks_what_it_delivers(project, tmp_path):
    c = ctx(project, tmp_path)
    r, out = await run(c, "present", path="missing.pdf")
    assert r.is_error and "nothing was delivered" in out
    (project / "broken.pdf").write_bytes(b"not really a pdf")
    r, out = await run(c, "present", path="broken.pdf", title="Report")
    a = r.meta["artifact"]
    assert not r.is_error and "PROBLEM" in out and not all(ch["ok"] for ch in a["checks"])
    (project / "notes.md").write_text("# Notes\n")
    r, _ = await run(c, "present", path="notes.md")
    assert r.meta["artifact"]["mime"] == "text/markdown" and r.meta["artifact"]["info"]["headings"] == 1


def tavily(results, status=200, seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(json.loads(request.content))
        return httpx.Response(status, json={"results": results})

    return search.Tavily("tvly-test", httpx.MockTransport(handler))


HITS = [{"title": "Pi agent", "url": "https://pi.dev/docs#intro", "content": "A minimal agent.",
         "published_date": "2026-09-01"},
        {"title": "OpenCode", "url": "https://opencode.ai/", "content": "An agent product."}]


async def test_web_search_numbers_sources(project, tmp_path):
    seen = []
    c = ctx(project, tmp_path, search=tavily(HITS, seen=seen))
    r, out = await run(c, "web_search", query="agent harness", recency="month")
    assert seen[0]["query"] == "agent harness" and seen[0]["time_range"] == "month"
    assert "[S1] Pi agent\nhttps://pi.dev/docs#intro\npublished 2026-09-01" in out
    assert "[S2] OpenCode" in out and "publication date unknown" in out  # never the retrieval date
    assert [(s["n"], s["kind"], s["url"]) for s in r.meta["sources"]] == [
        (1, "search", "https://pi.dev/docs"), (2, "search", "https://opencode.ai/")]
    r, out = await run(c, "web_search", query="again")
    assert "[S1] Pi agent" in out  # the same URL keeps its number

    r, out = await run(ctx(project, tmp_path), "web_search", query="x")
    assert r.is_error and "not configured" in out
    r, out = await run(ctx(project, tmp_path, search=tavily([], status=401)), "web_search", query="x")
    assert r.is_error and "rejected the API key" in out


async def test_citations_are_checked_against_sources(project, tmp_path):
    c = ctx(project, tmp_path, search=tavily(HITS))
    await run(c, "web_search", query="agents")
    artifacts.cite(c.sources, "https://opencode.ai/", "page", "OpenCode")  # read in full since
    r, out = await run(c, "create_document", path="r.md", content="Pi is small [S1]. OpenCode [S2]. Made up [S9].")
    checks = {ch["name"]: ch for ch in r.meta["artifact"]["checks"]}
    assert not checks["citations refer to recorded sources"]["ok"] and "S9" in checks[
        "citations refer to recorded sources"]["detail"]
    detail = checks["cited source excerpts entered model context"]["detail"]
    assert "S1" in detail and "S2" in detail  # neither a snippet nor a legacy 'page' proves model exposure


async def test_sources_and_versions_survive_a_restart(home, project, monkeypatch):
    """Numbering continues from the log in a new process; an overwrite is still recognised as ours."""
    monkeypatch.setattr(search, "provider", lambda name, key: tavily(HITS))
    with open(load(home).home / "config.toml", "a") as f:
        f.write('\n[search]\nprovider = "tavily"\napi_key = "env:NWB_TEST_SEARCH"\n')
    monkeypatch.setenv("NWB_TEST_SEARCH", "tvly-x")
    model = FakeModel(reply(None, call("web_search", query="agents")),
                      reply(None, call("create_document", path="r.md", content="# R\n[S1]")), reply("done"))
    ws = Workspace(load(home), connect=model.connect)
    log = ws.new_session(project)
    await ws.submit(log, "research")
    assert "web_search" in [t["name"] for t in model.requests[0]["tools"]]

    ws2 = Workspace(load(home), connect=FakeModel().connect)
    sources, versions = ws2.records(ws2.open_session(log.id))
    assert sources["https://opencode.ai/"]["n"] == 2 and len(versions[str(project / "r.md")]) == 1


async def test_attachments_reach_the_model_and_the_log(home, project):
    model = FakeModel(reply("got them"), reply(None, call("create_document", path="a.md", content="# A")),
                      reply("delivered"))
    async with Server(Workspace(load(home), connect=model.connect)) as s:
        sid = await s.session(project)
        stream = Stream(s.client, sid)
        await stream.ready.wait()
        up = lambda name, data: s.client.put(f"/api/sessions/{sid}/attachments", params={"name": name},  # noqa: E731
                                             content=data)
        note = (await up("../../笔记.md", "# 会议纪要\n要点一".encode())).json()
        image = (await up("shot.png", b"\x89PNG fake")).json()
        assert note["name"] == "笔记.md" and note["kind"] == "text" and image["kind"] == "image"
        assert (await up("big.bin", b"x" * (artifacts.MAX_UPLOAD_BYTES + 1))).status_code == 413

        r = await s.client.post(f"/api/sessions/{sid}/messages",
                                json={"text": "看看这些", "attachments": [note["id"], image["id"]]})
        assert r.json()["delivery"] == "run"
        await stream.until(finished)
        bad = await s.client.post(f"/api/sessions/{sid}/messages", json={"text": "x", "attachments": ["0" * 16]})
        assert bad.status_code == 400

        sent = user_message(model.requests[0])
        assert sent[0] == {"type": "text", "text": "看看这些"}
        assert "# 会议纪要\n要点一" in sent[1]["text"] and sent[1]["attachment"] == note["id"]
        assert "cannot view images" in sent[2]["text"]  # the default DeepSeek model is text-only
        log = s.ws.open_session(sid)
        user = next(e for e in log.path() if e.type == USER)
        assert [a["name"] for a in user.data["attachments"]] == ["笔记.md", "shot.png"]
        assert s.ws.index.session(sid)["title"] == "看看这些"

        f = await s.client.get(f"/api/sessions/{sid}/files/{note['sha256']}", params={"name": "笔记.md"})
        assert f.status_code == 200 and f.content.decode() == "# 会议纪要\n要点一"
        assert (await s.client.get(f"/api/sessions/{sid}/files/{'0' * 64}")).status_code == 404

        await s.client.post(f"/api/sessions/{sid}/messages", json={"text": "make a file"})
        await stream.until(lambda e: finished(e) and sum(x["type"] == "run.finished" for x in stream.events) == 2)
        stream.close()
        a = next(e.data["meta"]["artifact"] for e in log.path() if e.type == TOOL_RESULT)
        f = await s.client.get(f"/api/sessions/{sid}/files/{a['sha256']}", params={"name": "a.md", "download": 1})
        assert f.content == b"# A" and "attachment" in f.headers["content-disposition"]
        r = await s.client.post(f"/api/sessions/{sid}/reveal", json={"path": "/etc/passwd"})
        assert r.status_code == 404  # only files delivered in this session


async def test_attachment_only_message_and_vision_model(home, project):
    with open(load(home).home / "config.toml", "a") as f:
        f.write("\n[models.seer]\nprovider = \"deepseek\"\nid = \"v\"\ncontext_window = 100000\nmax_output = 8000\n"
                "vision = true\n")
    model = FakeModel(reply("ok"))
    ws = Workspace(load(home), connect=model.connect)
    log = ws.new_session(project, "seer")
    up = ws.upload(log, "shot.png", b"\x89PNG fake")
    files, blocks = ws.attachments(log, [up["id"]])
    assert [b["type"] for b in blocks] == ["text", "image"]
    from nano_workbuddy.loop import Input

    await ws.submit(log, Input("", None, tuple(files), tuple(blocks)))
    assert [b["type"] for b in user_message(model.requests[0])] == ["text", "image"]
    assert ws.index.session(log.id)["title"] == "shot.png"


@pytest.mark.parametrize("name", ["../etc/passwd", ".hidden", "a\x00b"])
def test_upload_names_are_made_safe(name):
    safe = artifacts.safe_name(name)
    assert "/" not in safe and "\x00" not in safe and not safe.startswith(".")


async def test_pending_message_keeps_its_attachments_across_a_restart(home, project):
    gate = asyncio.Event()

    async def never(msgs):
        await gate.wait()

    async with Server(Workspace(load(home), connect=FakeModel(never).connect)) as s:
        sid = await s.session(project)
        stream = Stream(s.client, sid)
        await stream.ready.wait()
        await s.client.post(f"/api/sessions/{sid}/messages", json={"text": "long job"})
        await stream.until(lambda e: e["type"] == "run.started")
        up = (await s.client.put(f"/api/sessions/{sid}/attachments", params={"name": "n.txt"}, content=b"NOTE")).json()
        await s.client.post(f"/api/sessions/{sid}/messages",
                            json={"text": "later", "delivery": "queue", "attachments": [up["id"]]})
        stream.close()
    model = FakeModel(reply("resumed"), reply("read it"))
    async with Server(Workspace(load(home), connect=model.connect)) as s2:
        for _ in range(100):
            if sid not in s2.runs.live and not s2.ws.index.pending_inputs(sid):
                break
            await asyncio.sleep(0.05)
        assert "NOTE" in user_message(model.requests[-1])[1]["text"]
