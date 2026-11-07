"""Reading PDFs and office files (documents.py, the read tool) and laying out Markdown (render.py)."""

from pathlib import Path

import pytest

from nano_workbuddy import documents, render
from nano_workbuddy.tools import BUILTIN, Registry, ToolCtx, execute
from nano_workbuddy.types import text_of

TOOLS = Registry(BUILTIN)

MD = """# 调研简报

第一段**加粗**，有[链接](https://example.com)。
软换行接中文。

## 对比

| 项目 | 语言 |
|---|---|
| pi | TypeScript |

1. 第一
2. 第二
   - 子项

> 引用

```
code()
```
"""


async def read(root: Path, tmp: Path, **args):
    c = ToolCtx(root=root, session_id="s", spill_dir=tmp / "spill", store=tmp / "store")
    r = await execute(TOOLS.get("read"), {"type": "tool_call", "id": "c", "name": "read", "args": args}, c)
    return r, text_of(r.content)


def test_markdown_blocks_and_cjk_soft_breaks():
    blocks = render.parse(MD)
    assert [b.kind for b in blocks] == ["heading", "para", "heading", "table", "item", "item", "item", "quote", "code"]
    para = blocks[1]
    assert render.plain(para.runs) == "第一段加粗，有链接。软换行接中文。"  # no space between CJK lines
    assert [r.bold for r in para.runs if r.text == "加粗"] == [True]
    assert [r.href for r in para.runs if r.text == "链接"] == ["https://example.com"]
    assert (blocks[4].number, blocks[5].number, blocks[6].number, blocks[6].level) == (1, 2, None, 1)
    assert render.plain(blocks[3].rows[1][1]) == "TypeScript"
    assert render.plain(render.parse("one\ntwo")[0].runs) == "one two"  # but a space between words


@pytest.mark.parametrize("fmt", ["docx", "pdf"])
async def test_rendered_documents_read_back(project, tmp_path, fmt):
    f = project / f"brief.{fmt}"
    (render.to_docx if fmt == "docx" else render.to_pdf)(render.parse(MD), f, "调研简报")
    doc = documents.load(f)
    body = doc.text()
    assert "调研简报" in body and "TypeScript" in body and "第二" in body and "code()" in body
    if fmt == "docx":
        assert "| pi | TypeScript |" in body and "# 调研简报" in body.splitlines()  # title preserved
        assert "\n  - 子项" in body  # nested list level kept
    else:
        assert doc.describe() == "PDF, 1 page" and body.startswith("--- page 1 ---")
    r, out = await read(project, tmp_path, path=f"brief.{fmt}")
    assert not r.is_error and "调研简报" in out


async def test_pdf_pages_and_scanned_pages(project, tmp_path):
    from pypdf import PdfReader, PdfWriter

    render.to_pdf(render.parse("\n\n".join(f"# Part {i}\n\n" + "text " * 900 for i in range(1, 4))),
                  project / "long.pdf")
    pages = len(PdfReader(project / "long.pdf").pages)
    assert pages >= 3
    r, out = await read(project, tmp_path, path="long.pdf", pages="2")
    assert f"PDF, {pages} pages, showing pages 2-2" in out and "--- page 2 ---" in out and "--- page 3 ---" not in out
    r, out = await read(project, tmp_path, path="long.pdf", pages=f"{pages + 5}")
    assert r.is_error
    w = PdfWriter()
    w.add_blank_page(200, 200)
    w.write(project / "scan.pdf")
    _, out = await read(project, tmp_path, path="scan.pdf")
    assert "no text layer on page(s) 1" in out  # an empty page is reported, not silently blank


def test_pptx_and_xlsx(project):
    from openpyxl import Workbook
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[1])
    s.shapes.title.text = "季度回顾"
    s.placeholders[1].text_frame.text = "收入增长"
    s.notes_slide.notes_text_frame.text = "强调第二点"
    shapes = prs.slides.add_slide(prs.slide_layouts[5]).shapes
    table = shapes.add_table(2, 2, Inches(1), Inches(2), Inches(4), Inches(1))
    table.table.cell(0, 0).text, table.table.cell(1, 1).text = "指标", "42"
    prs.save(project / "deck.pptx")
    doc = documents.load(project / "deck.pptx")
    assert doc.describe() == "PowerPoint, 2 slides"
    first = doc.units[0]
    assert "## 季度回顾" in first and "- 收入增长" in first and "Speaker notes: 强调第二点" in first
    assert "| 指标 |" in doc.units[1] and "42" in doc.units[1]

    wb = Workbook()
    wb.active.title = "预算"
    wb.active.append(["项目", "金额"])
    wb.active.append(["A", 10])
    wb.active["B3"] = "=SUM(B2:B2)"
    wb.save(project / "budget.xlsx")
    doc = documents.load(project / "budget.xlsx")
    assert doc.names == ["预算"] and "2: A | 10" in doc.units[0]
    assert "=SUM(B2:B2) (not calculated)" in doc.units[0]  # openpyxl never computes formulas
    assert any("no saved result" in n for n in doc.notes)


def test_conversions_are_cached_by_content(project, tmp_path):
    render.to_docx(render.parse("# A"), project / "a.docx")
    cache = tmp_path / "cache"
    documents.load(project / "a.docx", cache)
    [entry] = cache.iterdir()
    entry.write_text(entry.read_text().replace("# A", "# FROM CACHE"))
    assert "FROM CACHE" in documents.load(project / "a.docx", cache).text()


async def test_broken_document_is_a_clear_error(project, tmp_path):
    (project / "bad.docx").write_bytes(b"not a zip")
    r, out = await read(project, tmp_path, path="bad.docx")
    assert r.is_error and "cannot read bad.docx as Word" in out


async def test_images_need_a_vision_model(project, tmp_path):
    (project / "x.png").write_bytes(b"\x89PNG fake")
    c = ToolCtx(root=project, session_id="s", spill_dir=tmp_path, vision=False)
    r = await execute(TOOLS.get("read"), {"type": "tool_call", "id": "c", "name": "read", "args": {"path": "x.png"}}, c)
    assert [b["type"] for b in r.content] == ["text"] and "cannot view images" in text_of(r.content)
