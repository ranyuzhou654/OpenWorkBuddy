"""R-C regression evidence: source versions, projected excerpts, scope, incremental cache and budgets."""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import time
import zipfile
from pathlib import Path

import httpx
import pytest
from conftest import FakeModel, call, reply
from pypdf import PdfReader
from test_api import Server
from test_schedule import finish

from nano_workbuddy import artifacts, documents, usage
from nano_workbuddy.budgets import Limits, RunBudget
from nano_workbuddy.config import load
from nano_workbuddy.context import KEEP_CHARS, SPILL_CHARS, spill, to_messages
from nano_workbuddy.evidence import inspect_sources
from nano_workbuddy.knowledge import MAX_ENTRIES, Knowledge
from nano_workbuddy.log import SessionLog
from nano_workbuddy.runtime import Workspace
from nano_workbuddy.tools import BUILTIN, Registry, ToolCtx, execute
from nano_workbuddy.tools.docs import citation_checks
from nano_workbuddy.tools.web import _result
from nano_workbuddy.types import PRUNE, TOOL_RESULT, USER, text, text_of

TOOLS = Registry(BUILTIN)


def ctx(project, tmp_path):
    return ToolCtx(root=project, session_id="s", spill_dir=tmp_path / "spill", store=tmp_path / "store")


async def tool(ctx, name, **args):
    return await execute(TOOLS.get(name), call(name, **args), ctx)


def page(ctx, body=b"version one", url="https://example.test/report"):
    return _result(httpx.Response(200, headers={"content-type": "text/plain"}), body, url, ctx)


async def test_binary_download_is_fetched_not_extracted_or_inspected(project, tmp_path):
    c = ctx(project, tmp_path)
    result = _result(httpx.Response(200, headers={"content-type": "application/pdf"}),
                     b"synthetic-unparsed-pdf", "https://example.test/report.pdf", c)
    source = result.meta["sources"][0]
    assert source["state"] == "fetched" and not source.get("text_ref") and not result.meta.get("source_reads")
    assert artifacts.blob(c.store, source["snapshot"]).read_bytes() == b"synthetic-unparsed-pdf"
    checks = citation_checks("Downloaded [S1]", c)
    assert any(ch["ok"] is False and "not inspected: S1" in ch["detail"] for ch in checks)


@pytest.mark.parametrize("suffix", [".md", ".docx", ".pdf"])
async def test_exports_have_portable_bibliography_and_clickable_links(project, tmp_path, suffix):
    c = ctx(project, tmp_path)
    source = page(c).meta["sources"][0]
    result = await tool(c, "create_document", path=f"report{suffix}", content="# 报告\n\n事实候选 [S1].")
    a = result.meta["artifact"]
    assert a["sources"][0]["sha256"] == source["sha256"]
    assert {ch["layer"] for ch in a["checks"]} == {"structure", "citations", "numbers", "human"}
    assert all(ch["ok"] is None for ch in a["checks"] if ch["layer"] in ("numbers", "human"))
    path = Path(a["path"])
    if suffix == ".md":
        body = path.read_text()
        assert "参考文献" in body and source["url"] in body and source["retrieved_at"] in body
        assert source["sha256"] in body and "extracted" in body
    elif suffix == ".docx":
        with zipfile.ZipFile(path) as z:
            assert source["url"] in z.read("word/_rels/document.xml.rels").decode()
            assert source["sha256"] in z.read("word/document.xml").decode()
    else:
        pdf = PdfReader(path)
        assert any(a.get_object().get("/A", {}).get("/URI") == source["url"]
                   for p in pdf.pages for a in p.get("/Annots", []))
        assert source["sha256"] in "".join(p.extract_text() for p in pdf.pages).replace("\n", "")


async def test_same_url_changed_bytes_preserve_old_number_report_and_rebuild(project, tmp_path):
    c = ctx(project, tmp_path)
    first = page(c)
    report = await tool(c, "create_document", path="old.md", content="Old [S1]")
    frozen = json.dumps(report.meta["artifact"]["sources"], sort_keys=True)
    second = page(c, b"version two")
    assert second.meta["sources"][0]["n"] == 2
    assert page(c).meta["sources"][0]["n"] == 1  # re-observing old bytes reuses their identity
    assert json.dumps(report.meta["artifact"]["sources"], sort_keys=True) == frozen
    assert "version two" not in (project / "old.md").read_text()
    log = SessionLog.create(tmp_path / "s.jsonl", "s", "p")
    for result in [first, report, second]:
        log.append(TOOL_RESULT, {"meta": result.meta})
    sources, _ = artifacts.session_records(SessionLog.open(log.file))
    assert {s["n"] for s in sources.values()} == {1, 2}
    assert {s["sha256"] for s in sources.values()} == {hashlib.sha256(b"version one").hexdigest(),
                                                                     hashlib.sha256(b"version two").hexdigest()}


async def test_read_receipt_is_added_only_after_model_projection_and_is_partial(home, project):
    (project / "large.txt").write_text(("head\n" + "x" * 100 + "\n") * (SPILL_CHARS // 50))
    model = FakeModel(reply(None, call("read", path="large.txt", limit=5000)), reply("seen"))
    ws = Workspace(load(home), connect=model.connect)
    log = ws.new_session(project)
    await ws.submit(log, "read")
    source = next(iter(ws.records(log)[0].values()))
    assert source["state"] == "inspected"
    [inspection] = source["inspections"]
    assert inspection["partial_output"] and inspection["output_ranges"][0] == [0, KEEP_CHARS]
    excerpt = artifacts.blob(ws.store, inspection["excerpt_ref"]).read_text()
    assert "characters omitted" in excerpt and excerpt in text_of(model.requests[-1]["messages"][-1]["content"])
    assert inspection["excerpt_sha256"] == hashlib.sha256(excerpt.encode()).hexdigest()
    assert "lines 1-" in inspection["locator"]


async def test_pruned_output_pointer_is_not_inspected(home, project):
    ws = Workspace(load(home), connect=FakeModel().connect)
    log = ws.new_session(project)
    log.append(USER, {"content": [text("read")]})
    run = ws.make_run(log, ws.cfg.default_model, None, lambda e: None)
    rec = page(run.ctx, b"x" * 9000).meta["sources"][0]
    entry = log.append(TOOL_RESULT, {"call_id": "c", "name": "web_fetch", "is_error": False,
                       "content": [text("x" * 9000)], "meta": artifacts.reading(rec, "characters 0-9000")})
    log.append(PRUNE, {"upto_seq": entry.seq, "dir": str(run.ctx.spill_dir), "count": 1})
    _, messages = to_messages(log.path())
    inspect_sources(run, messages)
    assert next(iter(run.ctx.sources.values()))["state"] == "extracted"


async def test_pdf_page_selection_records_exact_excerpt_and_snapshot(home, project):
    from reportlab.pdfgen.canvas import Canvas

    p = project / "two.pdf"
    canvas = Canvas(str(p))
    canvas.drawString(20, 700, "PAGE_ONE_PRIVATE")
    canvas.showPage()
    canvas.drawString(20, 700, "PAGE_TWO_SELECTED")
    canvas.save()
    model = FakeModel(reply(None, call("read", path="two.pdf", pages="2")), reply("seen"))
    ws = Workspace(load(home), connect=model.connect)
    log = ws.new_session(project)
    await ws.submit(log, "read page two")
    source = next(iter(ws.records(log)[0].values()))
    [inspection] = source["inspections"]
    assert "page 2-2" in inspection["locator"]
    excerpt = artifacts.blob(ws.store, inspection["excerpt_ref"]).read_text()
    assert "PAGE_TWO_SELECTED" in excerpt and "PAGE_ONE_PRIVATE" not in excerpt
    assert source["snapshot"] == hashlib.sha256(p.read_bytes()).hexdigest()


async def test_short_attachment_inspected_long_attachment_only_extracted(home, project):
    ws = Workspace(load(home), connect=FakeModel(reply("ok")).connect)
    log = ws.new_session(project)
    short = ws.upload(log, "short.txt", b"SHORT")
    long = ws.upload(log, "long.txt", b"L" * (artifacts.INLINE_CHARS + 1))
    files, blocks = ws.attachments(log, [short["id"], long["id"]])
    from nano_workbuddy.loop import Input

    await ws.submit(log, Input("look", attachments=tuple(files), blocks=tuple(blocks)))
    states = {s["title"]: s["state"] for s in ws.records(log)[0].values()}
    assert states == {"short.txt": "inspected", "long.txt": "extracted"}
    rebuilt = artifacts.session_records(SessionLog.open(log.file))[0]
    assert {s["title"]: s["state"] for s in rebuilt.values()} == states


def test_document_cache_parses_the_same_bytes_it_hashes(tmp_path, monkeypatch):
    p = tmp_path / "a.docx"
    from docx import Document

    doc = Document()
    doc.add_paragraph("ORIGINAL")
    doc.save(p)
    raw = p.read_bytes()
    original = documents.parse

    def mutate(source, kind):
        p.write_bytes(b"changed behind reader")
        return original(source, kind)

    monkeypatch.setattr(documents, "parse", mutate)
    assert documents.load(p, tmp_path / "cache").text() == "ORIGINAL"
    assert (tmp_path / "cache" / f"{hashlib.sha256(raw).hexdigest()}.json").is_file()


async def test_default_recall_scope_selected_scope_and_read_record_recheck(home, project, tmp_path):
    ws = Workspace(load(home), connect=FakeModel(reply("one"), reply("two"), reply("three")).connect)
    one = ws.new_session(project)
    await ws.submit(one, "PROJECT_ONE_MARKER")
    other = tmp_path / "other"
    other.mkdir()
    two = ws.new_session(other)
    await ws.submit(two, "PROJECT_TWO_MARKER")
    scope = ws.record_scope(one.id)
    assert ws.recall("PROJECT_TWO_MARKER", scope=scope) == []
    hit = ws.recall("PROJECT_ONE_MARKER", scope=scope)[0]
    assert ws.read_record(hit["record_id"], 0, 100, scope=ws.record_scope(two.id)) is None
    ws.index.update_settings(one.id, {"recall_scope": "selected", "recall_sessions": [two.id]})
    assert ws.recall("PROJECT_TWO_MARKER", scope=ws.record_scope(one.id))
    scratch = ws.new_session(ws.scratch_project() and ws.cfg.scratch_dir)
    await ws.submit(scratch, "SCRATCH_MARKER")
    assert ws.record_scope(scratch.id)["session_ids"] == [scratch.id]
    assert not ws.recall("PROJECT_ONE_MARKER", scope=ws.record_scope(scratch.id))
    ws.index.update_settings(scratch.id, {"recall_scope": "all"})
    assert ws.recall("PROJECT_ONE_MARKER", scope=ws.record_scope(scratch.id))


async def test_search_never_syncs_or_scans_logs_and_external_appends_are_indexed(home, project, monkeypatch):
    ws = Workspace(load(home), connect=FakeModel(reply("ok")).connect)
    log = ws.new_session(project)
    await ws.submit(log, "original")
    external = SessionLog.open(log.file)
    external.append(USER, {"content": [text("EXTERNAL_MARKER")]})
    await ws.sync_records([log.id])  # reads the new JSONL tail, not the stale Workspace log object
    monkeypatch.setattr(ws.knowledge, "sync", lambda *a: pytest.fail("search tried to index"))
    monkeypatch.setattr(ws, "open_session", lambda *a: pytest.fail("search scanned a log"))
    hit = ws.recall("EXTERNAL_MARKER", scope=ws.record_scope(log.id))[0]
    assert "EXTERNAL_MARKER" in ws.read_record(hit["record_id"], 0, 100, scope=ws.record_scope(log.id))["body"]


async def test_project_exclusion_purges_and_revokes_previously_returned_record(home, project):
    ws = Workspace(load(home), connect=FakeModel(reply("ok")).connect)
    log = ws.new_session(project)
    await ws.submit(log, "EXCLUDE_MARKER")
    hit = ws.recall("EXCLUDE_MARKER")[0]
    async with Server(ws) as server:
        pid = ws.index.session(log.id)["project_id"]
        response = await server.client.patch(f"/api/projects/{pid}", json={"no_index": True})
        assert response.status_code == 200 and response.json()["no_index"]
        assert not ws.recall("EXCLUDE_MARKER")
        assert (await server.client.get(f"/api/records/{hit['record_id']}")).status_code == 404
        await ws.sync_records([log.id])
        assert ws.knowledge.db.execute("select count(*) from items").fetchone()[0] == 0
        await server.client.patch(f"/api/projects/{pid}", json={"no_index": False})
        assert ws.recall("EXCLUDE_MARKER")


def test_index_transaction_failure_rolls_back_and_next_batch_recovers(home, project, monkeypatch):
    ws = Workspace(load(home))
    log = ws.new_session(project)
    log.append(USER, {"content": [text("ROLLBACK_MARKER")]})
    row = ws.index.session(log.id)
    k = Knowledge(home / "knowledge.db", ws.store)
    real = k._entry

    def fail(row, entry):
        real(row, entry)
        if entry.type == USER:
            raise OSError("simulated disk problem")

    monkeypatch.setattr(k, "_entry", fail)
    with pytest.raises(OSError):
        k.sync([row])
    assert not k.db.in_transaction
    assert k.db.execute("select count(*) from items").fetchone()[0] == 0
    assert k.db.execute("select count(*) from progress").fetchone()[0] == 0
    monkeypatch.setattr(k, "_entry", real)
    k.sync([row])
    assert k.search("ROLLBACK_MARKER", session_ids=[log.id])


async def test_parser_failure_does_not_stall_following_records(home, project, monkeypatch):
    ws = Workspace(load(home))
    log = ws.new_session(project)
    raw = artifacts.save_upload(ws.store, ws.uploads_dir(log.id), "bad.docx", b"broken")
    log.append(USER, {"content": [text("FOLLOWING_MARKER")], "attachments": [raw]})
    monkeypatch.setattr(documents, "parse", lambda *a: (_ for _ in ()).throw(RuntimeError("parse failed")))
    await ws.sync_records([log.id])
    assert ws.recall("FOLLOWING_MARKER") and ws.recall("bad.docx", kind="files")
    assert ws._index_errors == []


def test_index_is_bounded_and_does_not_advance_past_partial_tail(home, project):
    ws = Workspace(load(home))
    log = ws.new_session(project)
    for i in range(MAX_ENTRIES + 3):
        log.append(USER, {"content": [text(f"bounded {i}")]})
    k = ws.knowledge
    row = ws.index.session(log.id)
    assert k.sync([row]) == MAX_ENTRIES
    assert k.sync([row]) == 5  # header + system prompt + user entries
    assert k.sync([row]) == 0
    with log.file.open("ab") as out:
        out.write(b'{"type":')
    assert k.sync([row]) == 0
    assert k.db.execute("select bytes from progress").fetchone()[0] < log.file.stat().st_size


async def test_deleted_session_cannot_be_read_by_old_record_id(home, project):
    ws = Workspace(load(home), connect=FakeModel(reply("ok")).connect)
    log = ws.new_session(project)
    await ws.submit(log, "DELETE_MARKER")
    hit = ws.recall("DELETE_MARKER")[0]
    ws.delete_session(log.id)
    assert not ws.recall("DELETE_MARKER")
    assert ws.read_record(hit["record_id"], 0, 100, scope=ws.record_scope()) is None


async def test_model_budget_counts_failed_attempts_and_never_marks_unsubmitted_read_as_inspected(home, project):
    (project / "a.txt").write_text("SOURCE")
    model = FakeModel(reply(None, call("read", path="a.txt")), reply("must not run"))
    cfg = dataclasses.replace(load(home), budget=Limits(model_calls=1))
    ws = Workspace(cfg, connect=model.connect)
    log = ws.new_session(project)
    assert await ws.submit(log, "go") == "limit"
    assert len(model.requests) == 1
    assert next(iter(ws.records(log)[0].values()))["state"] == "extracted"
    assert any(e["type"] == "budget.exhausted" and e["dimension"] == "model_calls"
               for e in usage.events(ws.usage_path(log)))


async def test_tool_batch_is_reserved_before_any_effect_and_returns_limit(home, project):
    model = FakeModel(reply(None, call("write", path="a", content="a"), call("write", path="b", content="b")))
    ws = Workspace(dataclasses.replace(load(home), budget=Limits(tool_calls=1)), connect=model.connect)
    log = ws.new_session(project)
    assert await ws.submit(log, "go") == "limit"
    assert not (project / "a").exists() and not (project / "b").exists()
    assert not any(e.type == "tool_started" for e in log.entries.values())


async def test_wall_budget_stops_stalled_provider_and_preserves_unknown_usage(home, project):
    model = FakeModel("hang")
    ws = Workspace(dataclasses.replace(load(home), budget=Limits(wall_seconds=1)), connect=model.connect)
    log = ws.new_session(project)
    assert await asyncio.wait_for(ws.submit(log, "go"), 3) == "limit"
    records = usage.events(ws.usage_path(log))
    assert any(e["type"] == "model.error" and e["error_type"] == "CancelledError" for e in records)
    assert not any(e["type"] == "model.response" for e in records)


async def test_budget_freeze_failed_call_count_and_elapsed_wall_survive_resume(home, project):
    model = FakeModel(RuntimeError("provider failed"), reply("must not run"))
    ws = Workspace(dataclasses.replace(load(home), budget=Limits(model_calls=1)), connect=model.connect)
    log = ws.new_session(project)
    with pytest.raises(RuntimeError):
        await ws.submit(log, "go")
    row = ws.index._one("select * from runs")
    ws.index.finish_run(row["id"], "interrupted")
    ws.cfg = dataclasses.replace(ws.cfg, budget=Limits(model_calls=100))
    assert await ws.resume(log) == "limit" and len(model.requests) == 1
    limits = RunBudget(ws.usage_path(log), row["id"], Limits(), row["started_at"], {})
    assert limits.limits.model_calls == 1 and limits.models == 1
    expired = RunBudget(home / "expired.jsonl", "expired", Limits(wall_seconds=1), time.time() - 5, {})
    with pytest.raises(Exception, match="wall_seconds"):
        expired.check_wall()


async def test_background_slots_preserve_pending_inputs_until_completion(home, project):
    gate = asyncio.Event()

    async def wait(messages):
        await gate.wait()
        return reply("done")

    ws = Workspace(dataclasses.replace(load(home), budget=Limits(background_concurrency=1)),
                   connect=FakeModel(wait, reply("done")).connect)
    async with Server(ws) as server:
        a, b = ws.new_session(project), ws.new_session(project)
        for log in (a, b):
            ws.index.accept_input(log.id, "run", "background", request_id=f"sched:{log.id}")
        server.runs.deliver_pending()
        assert sum(live.background for live in server.runs.live.values()) == 1
        assert ws.index.pending_inputs(b.id)
        gate.set()
        await finish(server.runs, a.id)
        await finish(server.runs, b.id)
        assert ws.index.run_for_request(f"sched:{b.id}")["status"] == "ok"


async def test_diagnostics_exposes_budget_provider_failure_and_unknown_price_without_prompts(home, project):
    model = FakeModel(reply(None, call("read", path="missing.txt")), RuntimeError("SECRET_PROMPT_MARKER"))
    ws = Workspace(load(home), connect=model.connect)
    log = ws.new_session(project)
    with pytest.raises(RuntimeError):
        await ws.submit(log, "SECRET_PROMPT_MARKER")
    async with Server(ws) as server:
        response = await server.client.get(f"/api/sessions/{log.id}/diagnostics")
        assert response.status_code == 200
        row = response.json()["runs"][0]
        assert row["model_calls"] == 2 and row["cost"] is None and row["failures"]
        assert row["tool_errors"] == 1 and row["tools"][0]["name"] == "read" and row["tools"][0]["is_error"]
        assert row["unknown_calls"] == 1
        assert "SECRET_PROMPT_MARKER" not in response.text
        invalid = await server.client.patch(f"/api/sessions/{log.id}", json={"budget": {"model_calls": 0}})
        assert invalid.status_code == 400


async def test_reading_spilled_output_extends_original_source_without_inventing_a_page_version(home, project):
    (project / "large.txt").write_text(("head\n" + "x" * 100 + "\n") * (SPILL_CHARS // 50))

    def read_saved(messages):
        result = next(m for m in reversed(messages) if m["role"] == "tool")
        import re

        saved = re.search(r"Full output saved to (.*?) —", text_of(result["content"]))[1]
        return reply(None, call("read", path=saved, offset=40, limit=5))

    model = FakeModel(reply(None, call("read", path="large.txt", limit=5000)), read_saved, reply("seen"))
    ws = Workspace(load(home), connect=model.connect)
    log = ws.new_session(project)
    await ws.submit(log, "read")
    sources = ws.records(log)[0]
    assert len(sources) == 1
    source = next(iter(sources.values()))
    assert len(source["inspections"]) == 2
    assert "lines 40-44" in source["inspections"][-1]["locator"]
    assert source["sha256"] == hashlib.sha256((project / "large.txt").read_bytes()).hexdigest()


async def test_out_of_range_read_does_not_assert_inspection(home, project):
    (project / "tiny.txt").write_text("ONE_LINE")
    model = FakeModel(reply(None, call("read", path="tiny.txt", offset=500)), reply("no source text"))
    ws = Workspace(load(home), connect=model.connect)
    log = ws.new_session(project)
    await ws.submit(log, "read")
    assert next(iter(ws.records(log)[0].values()))["state"] == "extracted"


async def test_scoped_recall_and_read_record_tools_cannot_self_enable_cross_project(home, project, tmp_path):
    other = tmp_path / "second"
    other.mkdir()
    ws = Workspace(load(home), connect=FakeModel(reply("ok")).connect)
    prior = ws.new_session(other)
    await ws.submit(prior, "OTHER_PROJECT_MARKER")
    current = ws.new_session(project)
    run = ws.make_run(current, ws.cfg.default_model, None, lambda e: None)
    hidden = ws.recall("OTHER_PROJECT_MARKER")[0]
    denied = await tool(run.ctx, "read_record", ref=hidden["record_id"], start=0, end=200)
    assert denied.is_error
    result = await tool(run.ctx, "recall", query="OTHER_PROJECT_MARKER")
    assert "Nothing found" in text_of(result.content) and "当前项目" in text_of(result.content)
    invalid = await tool(run.ctx, "recall", query="OTHER_PROJECT_MARKER", project_id="*")
    assert invalid.is_error
    ws.index.update_settings(current.id, {"recall_scope": "all"})
    result = await tool(run.ctx, "read_record", ref=hidden["record_id"], start=0, end=200)
    assert not result.is_error and "OTHER_PROJECT_MARKER" in text_of(result.content)
    assert "跨项目" in text_of(result.content)


async def test_summary_consumes_same_model_budget_as_agent_requests(home, project):
    from nano_workbuddy.compaction import complete
    from nano_workbuddy.loop import call_model

    ws = Workspace(dataclasses.replace(load(home), budget=Limits(model_calls=1)),
                   connect=FakeModel(reply("should not run"), summary="SUMMARY").connect)
    log = ws.new_session(project)
    log.append(USER, {"content": [text("work")]})
    row, _ = ws.index.start_run(log.id, "manual-test", ws.cfg.default_model)
    run = ws.make_run(log, ws.cfg.default_model, row["id"], lambda e: None)
    await complete(run.model, "You write handoff summaries", "summarize")
    from nano_workbuddy.budgets import BudgetExceeded

    with pytest.raises(BudgetExceeded, match="model_calls"):
        await call_model(run)
    starts = [e for e in usage.events(ws.usage_path(log)) if e["type"] == "model.started"]
    assert len(starts) == 1 and starts[0]["purpose"] == "compaction"


async def test_wall_timer_also_covers_toolset_startup(home, project, monkeypatch):
    model = FakeModel(reply("must not run"))
    ws = Workspace(dataclasses.replace(load(home), budget=Limits(wall_seconds=1)), connect=model.connect)

    async def stalled(*args, **kwargs):
        await asyncio.sleep(20)

    monkeypatch.setattr(ws, "toolset", stalled)
    log = ws.new_session(project)
    assert await asyncio.wait_for(ws.submit(log, "go"), 3) == "limit"
    assert not model.requests


async def test_source_text_ranges_are_relative_to_extracted_snapshot_after_spill(home, project):
    ws = Workspace(load(home), connect=FakeModel().connect)
    log = ws.new_session(project)
    log.append(USER, {"content": [text("read")]})
    run = ws.make_run(log, ws.cfg.default_model, None, lambda e: None)
    body = b"x" * (SPILL_CHARS + 100)
    result = spill(page(run.ctx, body), run.ctx.spill_dir, "fetch")
    log.append(TOOL_RESULT, {"call_id": "fetch", "name": "web_fetch", "is_error": False,
                            "content": result.content, "meta": result.meta})
    inspect_sources(run, to_messages(log.path())[1])
    source = next(iter(run.ctx.sources.values()))
    [inspection] = source["inspections"]
    assert len(inspection["source_ranges"]) == 2
    assert inspection["source_ranges"][0][0] == 0 and inspection["source_ranges"][-1][-1] == len(body)
    assert sum(b - a for a, b in inspection["source_ranges"]) < len(body)


async def test_no_index_revocation_is_live_even_if_an_old_scope_object_is_retained(home, project):
    ws = Workspace(load(home), connect=FakeModel(reply("ok")).connect)
    log = ws.new_session(project)
    await ws.submit(log, "REVOKED_MARKER")
    # Model callbacks ask record_scope anew for each call; they do not freeze permissions with the run.
    run = ws.make_run(log, ws.cfg.default_model, None, lambda e: None)
    ws.index.update_project(ws.index.session(log.id)["project_id"], no_index=True)
    result = await tool(run.ctx, "recall", query="REVOKED_MARKER")
    assert "Nothing found" in text_of(result.content)


def test_snapshot_copy_refuses_changed_bytes_instead_of_mislabeling_hash(tmp_path):
    p = tmp_path / "changing.txt"
    p.write_text("OLD")
    digest = artifacts.sha256_of(p)
    p.write_text("NEW")
    with pytest.raises(ValueError, match="changed"):
        artifacts.keep(tmp_path / "store", p, digest)
    assert not artifacts.blob(tmp_path / "store", digest).exists()


async def test_legacy_started_tools_are_not_reset_when_first_budget_is_frozen(home, project):
    from nano_workbuddy.budgets import BudgetExceeded

    ws = Workspace(load(home), connect=FakeModel().connect)
    log = ws.new_session(project)
    row, _ = ws.index.start_run(log.id, "legacy", ws.cfg.default_model)
    for i in range(2):
        log.append("tool_started", {"call_id": str(i), "name": "read"}, run_id=row["id"])
    ws.index.update_settings(log.id, {"budget": {"tool_calls": 2}})
    run = ws.make_run(log, ws.cfg.default_model, row["id"], lambda e: None)
    assert run.run_budget.tools == 2
    with pytest.raises(BudgetExceeded, match="tool_calls"):
        run.run_budget.tool("read")
    again = ws.make_run(log, ws.cfg.default_model, row["id"], lambda e: None)
    assert again.run_budget.tools == 2
