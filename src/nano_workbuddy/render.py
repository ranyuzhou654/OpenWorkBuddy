"""Markdown into Word and PDF files (M1 deliverables).

The model writes Markdown, which it is good at; this module lays it out. markdown-it-py parses it into a
short list of blocks (headings, paragraphs, list items, tables, code, quotes, rules) with styled inline
runs, and two small writers turn those into a .docx (python-docx) or a .pdf (reportlab). Both embed
nothing the user has to install: the PDF uses a CJK TrueType font found on the system (macOS ships
several), falling back to a standard CJK font that PDF viewers provide.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from markdown_it import MarkdownIt

CJK = re.compile(r"[\u2e80-\u9fff\uac00-\ud7af\uf900-\ufaff\uff00-\uffef]")
SOFT = "\0"  # placeholder for a soft line break until its neighbours are known


@dataclass
class Run:
    text: str
    bold: bool = False
    italic: bool = False
    code: bool = False
    href: str | None = None


@dataclass
class Block:
    kind: str  # heading, para, item, quote, code, table, hr
    runs: list[Run] = field(default_factory=list)
    level: int = 0  # heading level; list depth (0 = top)
    number: int | None = None  # ordered list item number
    text: str = ""  # code
    rows: list[list[list[Run]]] = field(default_factory=list)  # table: rows of cells of runs; row 0 is the header


def parse(md: str) -> list[Block]:
    tokens = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"]).parse(md)
    out: list[Block] = []
    lists: list[list[Any]] = []  # [ordered, counter] per open list
    heading, quote, fresh_item = 0, 0, False
    table: list[list[list[Run]]] | None = None
    for t in tokens:
        match t.type:
            case "heading_open":
                heading = int(t.tag[1])
            case "bullet_list_open":
                lists.append([False, 0])
            case "ordered_list_open":
                lists.append([True, int(t.attrs.get("start", 1) or 1) - 1])
            case "bullet_list_close" | "ordered_list_close":
                lists.pop()
            case "list_item_open":
                lists[-1][1] += 1
                fresh_item = True
            case "blockquote_open":
                quote += 1
            case "blockquote_close":
                quote -= 1
            case "table_open":
                table = []
            case "tr_open":
                table.append([])
            case "table_close":
                out.append(Block("table", rows=table))
                table = None
            case "fence" | "code_block":
                out.append(Block("code", text=t.content.rstrip("\n"), level=len(lists)))
            case "hr":
                out.append(Block("hr"))
            case "inline":
                runs = _runs(t.children or [])
                if table is not None:
                    table[-1].append(runs)
                elif heading:
                    out.append(Block("heading", runs, level=heading))
                    heading = 0
                elif lists:
                    ordered, n = lists[-1]
                    if fresh_item:
                        out.append(Block("item", runs, level=len(lists) - 1, number=n if ordered else None))
                    else:  # a further paragraph inside the same item
                        out.append(Block("para", runs, level=len(lists)))
                    fresh_item = False
                elif quote:
                    out.append(Block("quote", runs))
                else:
                    out.append(Block("para", runs))
    return out


def _runs(children: list[Any]) -> list[Run]:
    out: list[Run] = []
    bold = italic = False
    href: str | None = None
    for c in children:
        match c.type:
            case "text":
                out.append(Run(c.content, bold, italic, False, href))
            case "code_inline":
                out.append(Run(c.content, bold, italic, True, href))
            case "strong_open" | "strong_close":
                bold = c.type == "strong_open"
            case "em_open" | "em_close":
                italic = c.type == "em_open"
            case "link_open":
                href = str(c.attrs.get("href", "")) or None
            case "link_close":
                href = None
            case "softbreak":
                out.append(Run(SOFT, bold, italic, False, href))
            case "hardbreak":
                out.append(Run("\n", bold, italic, False, href))
            case "image":
                out.append(Run(f"[image: {c.content or c.attrs.get('src', '')}]", bold, italic))
            case "s_open" | "s_close":
                pass
    # a soft break is a space between words, but nothing between CJK characters
    for i, r in enumerate(out):
        if r.text == SOFT:
            before = out[i - 1].text[-1:] if i else ""
            after = out[i + 1].text[:1] if i + 1 < len(out) else ""
            r.text = "" if CJK.match(before) or CJK.match(after) else " "
    return [r for r in out if r.text]


def plain(runs: list[Run]) -> str:
    return "".join(r.text for r in runs)


# --- Word ---

EAST_ASIAN_FONT = "Microsoft YaHei"  # present on Windows and installed with Office on macOS; Word substitutes otherwise
MONO = "Menlo"


def to_docx(blocks: list[Block], path: Path, title: str = "") -> None:
    import docx
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor

    d = docx.Document()
    d.core_properties.title = title
    d.styles["Normal"].font.size = Pt(11)
    d.styles["Normal"].paragraph_format.space_after = Pt(4)
    for name in ("Normal", "Title", *(f"Heading {i}" for i in range(1, 7))):
        style = d.styles[name]
        style.element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), EAST_ASIAN_FONT)
        if name != "Normal":
            style.font.color.rgb = RGBColor(0, 0, 0)
            style.paragraph_format.keep_with_next = True
            for border in style.element.xpath("./w:pPr/w:pBdr"):
                border.getparent().remove(border)

    def fill(p: Any, runs: list[Run]) -> None:
        for r in runs:
            if r.href:
                _hyperlink(p, r.text, r.href)
                continue
            run = p.add_run(r.text)
            run.bold, run.italic = r.bold or None, r.italic or None
            if r.code:
                run.font.name = MONO
                run.font.color.rgb = RGBColor(0x8B, 0x3A, 0x1E)

    for index, b in enumerate(blocks):
        if b.kind == "heading":
            p = d.add_paragraph(style="Title") if index == 0 and b.level == 1 else d.add_heading(
                level=min(b.level, 6))
            fill(p, b.runs)
        elif b.kind == "para":
            p = d.add_paragraph()
            if b.level:
                p.paragraph_format.left_indent = Pt(18 * b.level)
            fill(p, b.runs)
        elif b.kind == "item":
            if b.number is None:
                p = d.add_paragraph(style="List Bullet" if b.level == 0 else f"List Bullet {min(b.level + 1, 3)}")
            else:  # numbers written out: Word's own numbering would continue across separate lists
                p = d.add_paragraph()
                p.paragraph_format.left_indent = Pt(18 * (b.level + 1))
                p.paragraph_format.first_line_indent = Pt(-18)
                p.add_run(f"{b.number}.\t")
            fill(p, b.runs)
        elif b.kind == "quote":
            fill(d.add_paragraph(style="Quote"), b.runs)
        elif b.kind == "code":
            p = d.add_paragraph()
            p.paragraph_format.left_indent = Pt(12 + 18 * b.level)
            run = p.add_run(b.text)
            run.font.name, run.font.size = MONO, Pt(9)
        elif b.kind == "table" and b.rows:
            width = max(len(r) for r in b.rows)
            t = d.add_table(rows=len(b.rows), cols=width, style="Table Grid")
            _borders(t)
            weights = []
            for j in range(width):
                texts = ["".join(r.text for r in row[j]) for row in b.rows if j < len(row)]
                length = max((len(s) + len(CJK.findall(s)) for s in texts), default=0)
                weights.append(max(8, min(50, length)))
            section = d.sections[-1]
            available = section.page_width - section.left_margin - section.right_margin
            for col, weight in zip(t.columns, weights, strict=True):
                col.width = int(available * weight / sum(weights))
                for cell in col.cells:
                    cell.width = col.width
            for i, row in enumerate(t.rows):
                props = row._tr.get_or_add_trPr()
                props.append(OxmlElement("w:cantSplit"))
                if i == 0:
                    props.append(OxmlElement("w:tblHeader"))
            for i, row in enumerate(b.rows):
                for j, cell in enumerate(row):
                    p = t.cell(i, j).paragraphs[0]
                    fill(p, [Run(r.text, r.bold or i == 0, r.italic, r.code, r.href) for r in cell])
            d.add_paragraph()
        elif b.kind == "hr":
            _rule(d.add_paragraph())
    d.save(str(path))


def _hyperlink(p: Any, text: str, url: str) -> None:
    """python-docx has no hyperlink API; this is the standard relationship + w:hyperlink element."""
    from docx.opc.constants import RELATIONSHIP_TYPE
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    rid = p.part.relate_to(url, RELATIONSHIP_TYPE.HYPERLINK, is_external=True)
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), rid)
    run = OxmlElement("w:r")
    props = OxmlElement("w:rPr")
    for tag, val in (("w:color", "1F5FAD"), ("w:u", "single")):
        el = OxmlElement(tag)
        el.set(qn("w:val"), val)
        props.append(el)
    run.append(props)
    t = OxmlElement("w:t")
    t.text = text
    t.set(qn("xml:space"), "preserve")
    run.append(t)
    link.append(run)
    p._p.append(link)


def _borders(t: Any) -> None:
    """Explicit cell borders: some viewers (macOS Quick Look, Pages) ignore the Table Grid style's own."""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        el = OxmlElement(f"w:{edge}")
        for k, v in (("w:val", "single"), ("w:sz", "4"), ("w:space", "0"), ("w:color", "BBBBBB")):
            el.set(qn(k), v)
        borders.append(el)
    t._tbl.tblPr.append(borders)


def _rule(p: Any) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    border = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    for k, v in (("w:val", "single"), ("w:sz", "6"), ("w:space", "1"), ("w:color", "999999")):
        bottom.set(qn(k), v)
    border.append(bottom)
    p._p.get_or_add_pPr().append(border)


# --- PDF ---

# (path, subfont index) of TrueType CJK fonts, regular then bold. reportlab cannot embed PostScript-outline
# (CFF) fonts, which rules out Hiragino and Noto CJK OTF.
FONT_CANDIDATES = [
    (("/System/Library/Fonts/STHeiti Light.ttc", 1), ("/System/Library/Fonts/STHeiti Medium.ttc", 1)),
    (("/System/Library/Fonts/Supplemental/Arial Unicode.ttf", 0), None),
    (("/Library/Fonts/Arial Unicode.ttf", 0), None),
    (("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc", 0), None),
    (("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", 0), None),
    (("C:/Windows/Fonts/msyh.ttc", 0), ("C:/Windows/Fonts/msyhbd.ttc", 0)),
]
_fonts: tuple[str, str] | None = None


def pdf_fonts() -> tuple[str, str]:
    """(regular, bold) font names registered with reportlab, found once per process."""
    global _fonts
    if _fonts:
        return _fonts
    from reportlab.lib.fonts import addMapping
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfbase.ttfonts import TTFont

    for regular, bold in FONT_CANDIDATES:
        if not Path(regular[0]).is_file():
            continue
        try:
            pdfmetrics.registerFont(TTFont("NWB-Sans", regular[0], subfontIndex=regular[1]))
            bold_name = "NWB-Sans"
            if bold and Path(bold[0]).is_file():
                pdfmetrics.registerFont(TTFont("NWB-Sans-Bold", bold[0], subfontIndex=bold[1]))
                bold_name = "NWB-Sans-Bold"
        except Exception:  # an unreadable or CFF font: try the next one
            continue
        _fonts = ("NWB-Sans", bold_name)
        break
    else:  # not embedded: viewers supply a Song-style CJK font
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        _fonts = ("STSong-Light", "STSong-Light")
    for bold_flag, italic_flag in ((0, 0), (0, 1), (1, 0), (1, 1)):  # no italic CJK face: italic maps to upright
        addMapping(_fonts[0], bold_flag, italic_flag, _fonts[1] if bold_flag else _fonts[0])
    return _fonts


def to_pdf(blocks: list[Block], path: Path, title: str = "") -> None:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import HRFlowable, Paragraph, Preformatted, SimpleDocTemplate, Spacer, Table, TableStyle

    regular, bold = pdf_fonts()
    base = ParagraphStyle("body", fontName=regular, fontSize=10.5, leading=16, wordWrap="CJK", spaceAfter=6)
    heads = {n: ParagraphStyle(f"h{n}", parent=base, fontName=bold, fontSize=size, leading=size * 1.35,
                               spaceBefore=10 if n > 1 else 4, spaceAfter=6, keepWithNext=True)
             for n, size in ((1, 20), (2, 16), (3, 13.5), (4, 12), (5, 11), (6, 10.5))}
    quote = ParagraphStyle("quote", parent=base, leftIndent=12, textColor=colors.HexColor("#555555"))
    code = ParagraphStyle("code", parent=base, fontName="Courier", fontSize=8.5, leading=11.5,
                          backColor=colors.HexColor("#F4F2EE"), borderPadding=4, leftIndent=4)
    cell = ParagraphStyle("cell", parent=base, fontSize=9, leading=13, spaceAfter=0)

    def markup(runs: list[Run]) -> str:
        out = []
        for r in runs:
            s = r.text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br/>")
            if r.code:
                s = f'<font face="{"Courier" if not CJK.search(r.text) else regular}" color="#8B3A1E">{s}</font>'
            if r.bold:
                s = f"<b>{s}</b>"
            if r.italic:
                s = f"<i>{s}</i>"
            if r.href:
                href = r.href.replace("&", "&amp;").replace(chr(34), "%22").replace("<", "%3C").replace(">", "%3E")
                s = f'<a href="{href}" color="#1F5FAD">{s}</a>'
            out.append(s)
        return "".join(out)

    width = A4[0] - 40 * mm
    story: list[Any] = []
    for b in blocks:
        if b.kind == "heading":
            story.append(Paragraph(markup(b.runs), heads[min(b.level, 6)]))
        elif b.kind == "para":
            story.append(Paragraph(markup(b.runs), ParagraphStyle("p", parent=base, leftIndent=14 * b.level)))
        elif b.kind == "item":
            indent = 14 * (b.level + 1)
            style = ParagraphStyle("li", parent=base, leftIndent=indent, bulletIndent=indent - 11, spaceAfter=3)
            story.append(Paragraph(markup(b.runs), style, bulletText=f"{b.number}." if b.number else "•"))
        elif b.kind == "quote":
            story.append(Paragraph(markup(b.runs), quote))
        elif b.kind == "code":
            story.append(Preformatted(b.text, code if not CJK.search(b.text)
                                      else ParagraphStyle("cjkcode", parent=code, fontName=regular)))
            story.append(Spacer(1, 6))
        elif b.kind == "table" and b.rows:
            cols = max(len(r) for r in b.rows)
            data = [[Paragraph(markup(c), ParagraphStyle("th", parent=cell, fontName=bold) if i == 0 else cell)
                     for c in row] + [""] * (cols - len(row)) for i, row in enumerate(b.rows)]
            t = Table(data, colWidths=[width / cols] * cols, repeatRows=1)
            t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#BBBBBB")),
                                   ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EFECE6")),
                                   ("VALIGN", (0, 0), (-1, -1), "TOP")]))
            story += [t, Spacer(1, 8)]
        elif b.kind == "hr":
            story.append(HRFlowable(width="100%", color=colors.HexColor("#BBBBBB"), spaceBefore=4, spaceAfter=8))

    def footer(canvas: Any, doc: Any) -> None:
        canvas.setFont(regular, 8)
        canvas.setFillColor(colors.HexColor("#888888"))
        canvas.drawCentredString(A4[0] / 2, 12 * mm, str(doc.page))

    doc = SimpleDocTemplate(str(path), pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=20 * mm,
                            bottomMargin=20 * mm, title=title)
    doc.build(story or [Spacer(1, 1)], onFirstPage=footer, onLaterPages=footer)
