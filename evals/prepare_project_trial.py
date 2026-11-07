"""Freeze actual repository documents for R-D local replay. No model or network calls.

Use the bundled document runtime for this builder. Render both internal DOCX files with render_docx.py;
the review PDF is installed by finalize after render/visual inspection.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
from datetime import datetime
from html import escape
from pathlib import Path

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

ROOT = Path(__file__).resolve().parents[1]
SOURCES = (
    "docs/design/2026-10-24-personal-assistant-re-review.md",
    "docs/design/2026-10-25-resume-implementation-status.md",
    "docs/research/2026-10-25-resume-baseline/baseline.json",
    "docs/research/2026-10-26-rb-validation.json",
    "docs/research/2026-10-26-rc-validation.json",
)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def section(body, heading):
    start = body.index(heading)
    end = body.find("\n## ", start + len(heading))
    return body[start:end if end >= 0 else len(body)].strip()


def document(path, title, chunks):
    doc = Document()
    page = doc.sections[0]
    page.page_width, page.page_height = Inches(8.5), Inches(11)
    page.top_margin = page.bottom_margin = Inches(.75)
    page.left_margin = page.right_margin = Inches(.8)
    for name in ("Normal", "Title", "Heading 1", "Heading 2", "Heading 3"):
        style = doc.styles[name]
        style.font.name, style.font.size = "Arial", Pt(11)
        style.font.color.rgb = RGBColor(0, 0, 0)
        style.element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), "PingFang SC")
        style.paragraph_format.space_after = Pt(6)
    doc.styles["Title"].font.size = Pt(18)
    doc.styles["Heading 1"].font.size = Pt(13)
    doc.styles["Normal"].paragraph_format.line_spacing = 1.12
    doc.add_paragraph(title, style="Title")
    doc.add_paragraph("项目真实资料的原文节选，用于本地读取与简报试用。标题为整理标识，正文按冻结来源保留。")
    for source, body in chunks:
        p = doc.add_paragraph(source, style="Caption")
        p.runs[0].font.size = Pt(9)
        for line in body.splitlines():
            if not line.strip():
                continue
            if line.startswith("#"):
                doc.add_heading(line.lstrip("# ").strip(), level=min(len(line) - len(line.lstrip("#")), 3))
            else:
                doc.add_paragraph(line)
    # A visible page number, without any title rules or decorative panels.
    footer = doc.sections[0].footer.paragraphs[0]
    footer.alignment = 1
    field = OxmlElement("w:fldSimple")
    field.set(qn("w:instr"), "PAGE")
    footer._p.append(field)
    doc.save(path)


def prepare(dest):
    if dest.exists() and any(dest.iterdir()):
        raise ValueError("destination is not empty; use a new dataset path")
    (dest / "inputs").mkdir(parents=True)
    (dest / "sources").mkdir()
    (dest / "qa").mkdir()
    source_hashes = {}
    for rel in SOURCES:
        src = ROOT / rel
        target = dest / "sources" / src.name
        shutil.copyfile(src, target)
        source_hashes[rel] = {"sha256": sha(target), "snapshot": str(target.relative_to(dest))}
    review = (dest / "sources" / Path(SOURCES[0]).name).read_text()
    status = (dest / "sources" / Path(SOURCES[1]).name).read_text()
    selected = [section(review, heading) for heading in (
        "## 1. 本轮结论", "## 2. 实际审查范围与验证结果", "## 4. 本轮发现及证据")]
    # Keep only the R10 evidence from section 4, with its original heading and paragraphs.
    findings = selected[-1]
    r10 = findings[findings.index("### R10"):findings.index("### R11")]
    review_body = "\n\n".join([selected[0], selected[1], r10.strip()])
    status_body = "\n\n".join(section(status, h) for h in ("## 开发试跑发现的问题", "## 尚未闭环的二轮问题"))
    (dest / "qa" / "review-excerpt.md").write_text(review_body)
    (dest / "qa" / "current-status.md").write_text(status_body)
    document(dest / "qa" / "review-excerpt.docx", "OpenWorkBuddy 二轮复审原文节选",
             [(SOURCES[0], review_body)])
    document(dest / "inputs" / "current-status.docx", "OpenWorkBuddy 当前实施进度原文节选",
             [(SOURCES[1], status_body)])
    baseline = json.loads((dest / "sources" / Path(SOURCES[2]).name).read_text())
    rb = json.loads((dest / "sources" / Path(SOURCES[3]).name).read_text())
    rc = json.loads((dest / "sources" / Path(SOURCES[4]).name).read_text())
    with (dest / "inputs" / "validation-history.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["stage", "tests", "failures", "errors", "skipped", "seconds", "source"])
        writer.writeheader()
        for label, row, timing, source in (
            ("baseline", baseline["pytest"], baseline["pytest"]["junit_suite_duration_seconds"], SOURCES[2]),
            ("R-B", rb["pytest"], rb["pytest"]["time"], SOURCES[3]),
            ("R-C", rc["pytest"], rc["pytest"]["time"], SOURCES[4]),
        ):
            writer.writerow({"stage": label, **{k: row[k] for k in ("tests", "failures", "errors", "skipped")},
                             "seconds": timing, "source": source})
    for period, rec in (("period-one.json", rb), ("period-two.json", rc)):
        (dest / "inputs" / period).write_text(json.dumps(rec, ensure_ascii=False, indent=2) + "\n")
    manifest = {"id": "rd-project-replay-v1", "created_at": datetime.now().astimezone().isoformat(),
                "origin": "actual repository documents, format-converted excerpts and unedited JSON snapshots",
                "sources": source_hashes, "human_review": "pending", "real_user_trials_accepted": 0,
                "limits": {"model_calls_per_trial": 12, "seconds_per_trial": 180, "sdk_retries": 0},
                "provider": "opendesign", "model": "deepseek-v4.1-flash", "cost": None,
                "blocked_cases": {"U2": "no live search key or user question",
                                  "U3": "no enabled Feishu or selected source",
                                  "U4": "no user-designated write target or payload approval"},
                "visual_qa": "pending"}
    (dest / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")


def finalize(dest):
    manifest = json.loads((dest / "manifest.json").read_text())
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate

    pdfmetrics.registerFont(TTFont("ProjectCJK", "/System/Library/Fonts/STHeiti Light.ttc", subfontIndex=0))
    style = ParagraphStyle("source", fontName="ProjectCJK", fontSize=11, leading=17,
                           wordWrap="CJK", spaceAfter=6)
    lines = ["OpenWorkBuddy 二轮复审原文节选", SOURCES[0],
             *(dest / "qa" / "review-excerpt.md").read_text().splitlines()]
    SimpleDocTemplate(str(dest / "inputs" / "review-excerpt.pdf"), pagesize=letter,
                      leftMargin=54, rightMargin=54, topMargin=48, bottomMargin=48).build(
        [Paragraph(escape(line), style) for line in lines if line.strip()])
    manifest["inputs"] = {str(p.relative_to(dest)): {"sha256": sha(p), "bytes": p.stat().st_size}
                          for p in sorted((dest / "inputs").iterdir()) if p.is_file()}
    manifest["visual_qa"] = "pending: inspect final input PDF and DOCX render separately"
    (dest / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("mode", choices=("prepare", "finalize"))
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    (prepare if args.mode == "prepare" else finalize)(args.destination.resolve())
