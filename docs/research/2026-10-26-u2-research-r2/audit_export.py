"""Offline audit and separate layout-only re-export; never writes into the real trial."""
import asyncio
import json
import re
import shutil
from pathlib import Path

from nano_workbuddy import artifacts, render
from nano_workbuddy.eval.runner import archive_source, code_version, write_json
from nano_workbuddy.eval.schema import file_hash
from nano_workbuddy.log import SessionLog
from nano_workbuddy.tools import BUILTIN, Registry, ToolCtx, execute

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
RUN = REPO / "eval-runs/rd-research-2026-10-26-r2"
TRIAL = RUN / "U2-2"


def manifest(root):
    return {str(p.relative_to(root)): file_hash(p) for p in sorted(root.rglob("*")) if p.is_file()}


async def main():
    before = manifest(RUN)
    original = HERE / "original"
    original.mkdir()
    for p in TRIAL.glob("*"):
        if p.is_file():
            shutil.copyfile(p, original / p.name)
    for name in ("freeze.json", "runner.py", "source.zip"):
        shutil.copyfile(RUN / name, original / name)
    for name in ("sessions", "usage", "context", "spill", "files"):
        if (TRIAL / "home" / name).exists():
            shutil.copytree(TRIAL / "home" / name, original / name)
    shutil.copyfile(TRIAL / "workspace/project-context.md", original / "project-context.md")
    log = SessionLog.open(next((TRIAL / "home/sessions").glob("*/*.jsonl")))
    sources, versions = artifacts.session_records(log)
    records = artifacts.document_records(log)
    md = records[str(TRIAL / "workspace/research-brief.md")]
    write_json(HERE / "sources.json", md["sources"])
    write_json(HERE / "raw-artifacts.json", list(records.values()))
    version = code_version()
    write_json(HERE / "layout-fix-freeze.json", version)
    archive_source(version, HERE / "layout-fix-source.zip")
    ctx = ToolCtx(root=TRIAL / "workspace", session_id=log.id, spill_dir=HERE / "offline-spill",
                  store=HERE / "offline-store", sources=sources, versions=versions, document_records=records,
                  allowed=lambda action, path: action == "read" and path == md["path"])
    result = await execute(Registry(BUILTIN).get("export_document"), {
        "type": "tool_call", "id": "offline-layout-export", "name": "export_document", "args": {
            "source": md["path"], "path": str(HERE / "assisted/research-brief.docx")}}, ctx)
    assert not result.is_error, result.content
    write_json(HERE / "assisted-export.json", result.meta["artifact"])
    blocks = render.parse((HERE / "research-brief.md").read_text())
    visible = []
    for b in blocks:
        if b.kind == "table":
            visible.extend("".join(r.text for r in cell) for row in b.rows for cell in row)
        elif b.kind == "code":
            visible.append(b.text)
        else:
            visible.append((f"{b.number}." if b.kind == "item" and b.number is not None else "")
                           + "".join(r.text for r in b.runs))
    write_json(HERE / "expected-visible.json", {"text": re.sub(r"\s+", "", "".join(visible))})
    retained = []
    for s in md["sources"]:
        for digest in {s.get("snapshot"), s.get("text_ref"),
                       *(x["excerpt_ref"] for x in s.get("inspections", []))} - {None}:
            assert file_hash(TRIAL / "home/files/blobs" / digest) == digest
            retained.append(digest)
    assert manifest(RUN) == before
    write_json(HERE / "offline-audit.json", {"raw_trial_unchanged": True,
        "raw_manifest": before, "retained_blob_hashes_verified": sorted(set(retained)),
        "content_revision": False, "layout_reexport": True, "additional_model_requests": 0,
        "additional_tavily_requests": 0})
    print(json.dumps({"raw_trial_unchanged": True, "verified_blobs": len(set(retained)),
                      "offline_output": result.meta["artifact"]["path"]}))


if __name__ == "__main__":
    asyncio.run(main())
