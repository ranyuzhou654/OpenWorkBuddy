"""One authored body, frozen citations and read/write boundaries across conversion and restart."""
import dataclasses

import pytest
from conftest import FakeModel, call, reply

from nano_workbuddy import artifacts, documents, policy
from nano_workbuddy.config import load
from nano_workbuddy.runtime import Workspace
from nano_workbuddy.tools import BUILTIN, Registry, ToolCtx, execute
from nano_workbuddy.types import text_of

TOOLS = Registry(BUILTIN)


async def tool(ctx, name, **args):
    return await execute(TOOLS.get(name), call(name, **args), ctx)


@pytest.mark.parametrize("suffix", [".docx", ".pdf"])
async def test_export_preserves_original_body_citation_state_and_snapshot(project, tmp_path, suffix):
    ctx = ToolCtx(root=project, session_id="s", spill_dir=tmp_path / "spill", store=tmp_path / "store")
    url = "https://example.test/evidence"
    old = artifacts.source_read(ctx, url, b"ORIGINAL evidence", "Evidence", extracted="ORIGINAL evidence")
    result = await tool(ctx, "create_document", path="report.md", content="# Report\n\nFact [S1]", title="Report")
    original = result.meta["artifact"]
    # Later inspection and a changed page must not retroactively improve the original document's checks.
    ctx.sources[url]["state"] = "inspected"
    artifacts.source_read(ctx, url, b"CHANGED evidence", "Changed", extracted="CHANGED evidence")
    exported = await tool(ctx, "export_document", source="report.md", path="report" + suffix)
    assert not exported.is_error, text_of(exported.content)
    a = exported.meta["artifact"]
    assert a["sources"] == original["sources"]
    assert a["sources"][0]["sha256"] == old["sha256"] and a["sources"][0]["state"] == "extracted"
    assert a["derived_from"]["sha256"] == original["sha256"]
    assert any(c["ok"] is False for c in a["checks"] if c.get("layer") == "citations")
    body = documents.load(project / ("report" + suffix)).text().replace("\n", "")
    assert body.count("参考文献") == 1 and old["sha256"] in body and "CHANGED" not in body
    assert (project / "report.md").read_bytes() == artifacts.blob(ctx.store, original["sha256"]).read_bytes()


@pytest.mark.parametrize("case", ["edited_source", "unknown_source", "denied_read", "user_target", "edited_target"])
async def test_export_refuses_changed_or_unapproved_files(project, tmp_path, case):
    ctx = ToolCtx(root=project, session_id="s", spill_dir=tmp_path / "spill", store=tmp_path / "store")
    await tool(ctx, "create_document", path="report.md", content="# Report\nOriginal")
    if case == "edited_source":
        (project / "report.md").write_text("User changed this")
    elif case == "unknown_source":
        ctx.document_records.clear()
    elif case == "denied_read":
        ctx.allowed = lambda action, resource: action != "read"
    elif case in ("user_target", "edited_target"):
        if case == "edited_target":
            await tool(ctx, "export_document", source="report.md", path="report.docx")
        (project / "report.docx").write_bytes(b"USER WORK")
    before = (project / "report.docx").read_bytes() if (project / "report.docx").exists() else None
    r = await tool(ctx, "export_document", source="report.md", path="report.docx", overwrite=True)
    assert r.is_error
    assert ((project / "report.docx").read_bytes() if (project / "report.docx").exists() else None) == before


async def test_export_works_in_same_batch_and_after_workspace_reopen(home, project):
    model = FakeModel(reply(None,
        call("create_document", path="report.md", content="# Report\nBody"),
        call("export_document", source="report.md", path="report.docx")), reply("done"))
    ws = Workspace(load(home), connect=model.connect)
    log = ws.new_session(project)
    assert await ws.submit(log, "Create both formats") == "ok"
    assert len(model.requests) == 2 and (project / "report.docx").is_file()
    first_id = log.id
    log.close()
    ws.index.db.close()
    model2 = FakeModel(reply(None, call("export_document", source="report.md", path="reopened.docx")), reply("done"))
    ws = Workspace(load(home), connect=model2.connect)
    log = ws.open_session(first_id)
    assert await ws.submit(log, "Export again without rewriting") == "ok"
    assert len(model2.requests) == 2
    assert (project / "report.md").read_text() == "# Report\nBody"
    a = next(e.data["meta"]["artifact"] for e in reversed(log.path())
             if e.type == "tool_result" and e.data.get("name") == "export_document")
    assert a["derived_from"]["path"] == str(project / "report.md")


async def test_export_read_deny_is_enforced_through_runtime_gate(home, project):
    cfg = dataclasses.replace(load(home), rules=(policy.Rule("read", str(project / "report.md"), "deny"),))
    model = FakeModel(reply(None, call("create_document", path="report.md", content="# Private\nBody"),
                            call("export_document", source="report.md", path="report.docx")), reply("done"))
    ws = Workspace(cfg, connect=model.connect)
    log = ws.new_session(project)
    await ws.submit(log, "Try export")
    assert not (project / "report.docx").exists()
    result = next(e for e in log.path() if e.type == "tool_result" and e.data.get("name") == "export_document")
    assert result.data["is_error"] and "not allowed" in text_of(result.data["content"])
