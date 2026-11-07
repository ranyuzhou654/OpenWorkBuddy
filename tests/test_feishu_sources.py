"""Feishu JSON snapshots and readable excerpts share the ordinary source evidence lifecycle."""
import hashlib
import json

import pytest
from conftest import call

from nano_workbuddy import artifacts
from nano_workbuddy.connectors.feishu import feishu
from nano_workbuddy.tools import ToolCtx, execute
from nano_workbuddy.types import text_of


class Connector:
    def __init__(self, response):
        self.response = response

    async def risk(self, domain, command):
        return "read"

    async def run(self, argv, root):
        return 0, json.dumps(self.response, ensure_ascii=False)


def context(root, response):
    return ToolCtx(root=root, session_id="s", spill_dir=root / "spill", store=root / "files",
                   feishu=Connector(response))


async def fetch(ctx, doc="https://example.feishu.cn/docx/d1", **flags):
    argv = ["docs", "+fetch", "--doc", doc]
    for k, v in flags.items():
        argv.extend(["--" + k.replace("_", "-"), str(v)])
    return await execute(feishu, call("feishu", command=argv), ctx)


async def test_nested_document_preserves_response_comments_and_excerpt_offsets(tmp_path):
    response = {"ok": True, "identity": "user", "data": {"document": {
        "document_id": "d1", "revision_id": 7, "title": "Project notes", "content": "# Notes\nFACT",
        "reference_map": {"comments": {"c1": {"data": "Review this"}}}}}}
    ctx = context(tmp_path, response)
    r = await fetch(ctx)
    source = r.meta["sources"][0]
    assert not r.is_error and source["state"] == "extracted" and source["kind"] == "page"
    assert source["title"] == "Project notes" and "revision 7" in source["locator"]
    raw = json.dumps(response, ensure_ascii=False).encode()
    assert source["sha256"] == hashlib.sha256(raw).hexdigest()
    assert artifacts.blob(ctx.store, source["snapshot"]).read_bytes() == raw
    excerpt = artifacts.blob(ctx.store, source["text_ref"]).read_text()
    read = r.meta["source_reads"][0]
    assert text_of(r.content)[read["output_start"]:] == excerpt
    assert len(excerpt) == read["source_length"] and "Review this" in excerpt
    assert "inspections" not in source  # retrieval is not model exposure or factual validation


async def test_changed_revision_gets_new_number_and_keeps_old_snapshot(tmp_path):
    response = {"ok": True, "data": {"document": {"document_id": "d1", "revision_id": 7, "content": "FIRST"}}}
    ctx = context(tmp_path, response)
    first = (await fetch(ctx, scope="section", start_block_id="block1")).meta["sources"][0]
    response["data"]["document"].update(revision_id=8, content="SECOND")
    second = (await fetch(ctx, scope="section", start_block_id="block1")).meta["sources"][0]
    assert first["n"] == 1 and second["n"] == 2 and first["snapshot"] != second["snapshot"]
    assert "scope section" in second["locator"] and "start-block-id=block1" in second["locator"]
    saved = json.loads(artifacts.blob(ctx.store, first["snapshot"]).read_text())
    assert saved["data"]["document"]["content"] == "FIRST"


@pytest.mark.parametrize("empty", [True, False])
async def test_missing_body_or_auth_error_never_proves_inspection(tmp_path, empty):
    ctx = context(tmp_path, {"ok": True, "data": {"document": {"revision_id": 1}}} if empty else
                  {"ok": False, "error": {"message": "unauthorized"}})
    r = await fetch(ctx)
    assert not r.meta.get("source_reads")
    if empty:
        assert r.meta["sources"][0]["state"] == "fetched" and not r.meta["sources"][0].get("text_ref")
    else:
        assert r.is_error and not ctx.sources


async def test_legacy_flat_response_and_bare_token_remain_remote_sources(tmp_path):
    ctx = context(tmp_path, {"ok": True, "data": {"title": "Old schema", "content": "BODY"}})
    r = await fetch(ctx, doc="d1")
    source = r.meta["sources"][0]
    assert source["title"] == "Old schema" and source["url"] == "feishu:doc/d1"
    assert source["kind"] == "page" and "BODY" in text_of(r.content)
