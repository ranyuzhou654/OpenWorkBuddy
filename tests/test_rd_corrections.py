"""R-D fixes: readable local references and export pagination, without weakening provenance checks."""

import pytest
from docx import Document
from pypdf import PdfReader

from nano_workbuddy import render
from nano_workbuddy.tools.docs import bibliography, citation_checks
from nano_workbuddy.tools.registry import ToolCtx


@pytest.mark.parametrize(("citation", "known", "unknown", "unread"), [
    ("[S1 p2]", [1], False, False),
    ("[S1, 第 2 页]", [1], False, False),
    ("[S1; S2]", [1, 2], False, True),
    ("[S1，第 2 页；S2]", [1, 2], False, True),
    ("[S1, 第 2 页；S99]", [1], True, False),
    ("[Section1] and S99", [], False, False),
    ("[S1oops]", [], False, False),
])
def test_qualified_citations_preserve_sources_and_validation(tmp_path, citation, known, unknown, unread):
    context = ToolCtx(root=tmp_path, session_id="s", spill_dir=tmp_path / "spill")
    for n in (1, 2):
        uri = (tmp_path / f"source-{n}.pdf").as_uri()
        context.sources[uri] = {"n": n, "url": uri, "title": f"Source {n}", "snapshot": str(n) * 64,
                               "state": "inspected" if n == 1 else "discovered"}
    _, sources = bibliography(f"Fact {citation}.", context)
    checks = citation_checks(citation, context)
    assert [s["n"] for s in sources] == known
    if not known and not unknown:
        assert checks == []
    else:
        assert checks[0]["ok"] is not unknown
        assert checks[1]["ok"] is not unread
        assert checks[2]["ok"] is True
        if unknown:
            assert checks[0]["detail"] == "unknown: S99"


@pytest.mark.parametrize("suffix", [".md", ".docx", ".pdf"])
@pytest.mark.parametrize("title", ["facts_[final]_data.txt", ""])
def test_local_references_render_once_with_snapshot_and_no_file_hyperlink(tmp_path, suffix, title):
    uri = (tmp_path / "facts_[final]_data.txt").as_uri()
    context = ToolCtx(root=tmp_path, session_id="s", spill_dir=tmp_path / "spill")
    context.sources[uri] = {"n": 1, "url": uri, "title": title, "sha256": "a" * 64,
                            "state": "inspected", "retrieved_at": "2026-10-26", "locator": "lines 1-4"}
    content, snapshots = bibliography("# Evidence\n\nA fact [S1].", context)
    blocks = render.parse(content)
    path = tmp_path / f"report{suffix}"
    if suffix == ".md":
        body = "\n".join(render.plain(b.runs) for b in blocks)
    elif suffix == ".docx":
        render.to_docx(blocks, path)
        doc = Document(path)
        body = "\n".join(p.text for p in doc.paragraphs)
        assert doc.paragraphs[0].style.name == "Title"
        assert str(doc.styles["Title"].font.color.rgb) == "000000"
        assert not any(rel.target_ref.startswith("file:") for rel in doc.part.rels.values())
    else:
        render.to_pdf(blocks, path)
        pdf = PdfReader(path)
        body = "".join(p.extract_text() for p in pdf.pages).replace("\n", "")
        assert not any(a.get_object().get("/A", {}).get("/URI", "").startswith("file:")
                       for p in pdf.pages for a in p.get("/Annots", []))
    assert body.count(uri) == 1
    assert "](" not in body
    assert "a" * 64 in body.replace("\n", "")
    assert "lines 1-4" in body
    assert snapshots[0]["url"] == uri and snapshots[0]["sha256"] == "a" * 64


@pytest.mark.parametrize("fillers", range(28, 36))
def test_pdf_heading_stays_with_its_body_near_page_boundary(tmp_path, fillers):
    path = tmp_path / "page-break.pdf"
    markdown = "\n\n".join(["Filler paragraph"] * fillers + ["## NEXT SECTION", "FOLLOWER body text"])
    render.to_pdf(render.parse(markdown), path)
    pages = [p.extract_text() for p in PdfReader(path).pages]
    if fillers >= 34:
        assert len(pages) > 1
    assert next(i for i, t in enumerate(pages) if "NEXT SECTION" in t) == next(
        i for i, t in enumerate(pages) if "FOLLOWER" in t)
