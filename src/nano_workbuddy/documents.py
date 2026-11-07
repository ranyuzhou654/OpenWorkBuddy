"""Text out of PDFs and office documents, for the model to read (M1).

Each format becomes plain text with its structure kept as Markdown (headings, lists, tables) and split
into the units a reader cites: pages of a PDF, slides of a presentation, sheets of a workbook (a Word
document is one unit: it has no stable pages). Conversion is cached by content hash, so a document read
again, or attached to another session, is parsed once.

Light, pure-Python readers only: pypdf, python-docx, python-pptx, openpyxl. A PDF page without a text
layer (a scan) is reported as such instead of silently coming back empty; there is no OCR.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

KINDS = {".pdf": "PDF", ".docx": "Word", ".pptx": "PowerPoint", ".xlsx": "Excel", ".xlsm": "Excel"}
UNITS = {"PDF": "page", "PowerPoint": "slide", "Excel": "sheet"}
MAX_SHEET_ROWS = 5_000
CACHE_VERSION = 1


class DocumentError(Exception):
    """The file cannot be read as the document it claims to be."""


@dataclass
class Doc:
    kind: str  # PDF, Word, PowerPoint, Excel
    units: list[str]  # text of each page / slide / sheet; Word: one item
    names: list[str] = field(default_factory=list)  # sheet names (Excel)
    notes: list[str] = field(default_factory=list)  # what the reader should know: scanned pages, formulas, ...
    title: str = ""

    @property
    def unit(self) -> str | None:
        return UNITS.get(self.kind)

    def describe(self) -> str:
        """One line: "PDF, 12 pages" / "Word document"."""
        if not self.unit:
            return f"{self.kind} document"
        return f"{self.kind}, {len(self.units)} {self.unit}{'s' if len(self.units) != 1 else ''}"

    def text(self, first: int = 1, last: int | None = None) -> str:
        """Units first..last (1-based, inclusive), each under a marker the model can cite."""
        if not self.unit:
            return self.units[0] if self.units else ""
        last = min(last or len(self.units), len(self.units))
        out = []
        for n in range(first, last + 1):
            label = f"sheet {n}: {self.names[n - 1]}" if self.kind == "Excel" else f"{self.unit} {n}"
            out.append(f"--- {label} ---\n{self.units[n - 1].strip() or '(no text)'}")
        return "\n\n".join(out)


def is_document(p: Path) -> bool:
    return p.suffix.lower() in KINDS


def load(p: Path, cache: Path | None = None) -> Doc:
    """Read a document, from the cache when this exact content was read before."""
    return load_bytes(p.read_bytes(), p.suffix.lower(), cache, name=p.name)


def load_bytes(raw: bytes, suffix: str, cache: Path | None = None, *, name: str = "") -> Doc:
    """Parse the exact bytes hashed for the cache/source, even if the original path changes meanwhile."""
    digest = hashlib.sha256(raw).hexdigest()
    f = cache / f"{digest}.json" if cache else None
    if f and f.is_file():
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if data.pop("v", None) == CACHE_VERSION:
                return Doc(**data)
        except (ValueError, TypeError):
            pass  # unreadable cache entry: parse again
    fd, tmp = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(raw)
        try:
            doc = parse(Path(tmp), KINDS[suffix])
        except DocumentError as exc:
            raise DocumentError(str(exc).replace(Path(tmp).name, name or "snapshot")) from exc
    finally:
        os.unlink(tmp)
    if f:
        f.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=f.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump({"v": CACHE_VERSION, **asdict(doc)}, out, ensure_ascii=False)
        os.replace(tmp, f)
    return doc


def parse(p: Path, kind: str) -> Doc:
    try:
        return {"PDF": _pdf, "Word": _docx, "PowerPoint": _pptx, "Excel": _xlsx}[kind](p)
    except DocumentError:
        raise
    except Exception as e:  # a damaged or mislabelled file: say so, with the library's reason
        raise DocumentError(f"cannot read {p.name} as {kind}: {type(e).__name__}: {e}") from e


# --- PDF ---


def _pdf(p: Path) -> Doc:
    from pypdf import PdfReader

    reader = PdfReader(p)
    if reader.is_encrypted and not reader.decrypt(""):
        raise DocumentError(f"{p.name} is password-protected")
    pages = [(page.extract_text() or "").strip() for page in reader.pages]
    empty = [str(n) for n, t in enumerate(pages, 1) if not t]
    notes = []
    if empty:
        which = ", ".join(empty[:20]) + (" ..." if len(empty) > 20 else "")
        notes.append(f"no text layer on page(s) {which}: probably scanned images; their text cannot be read")
    title = str((reader.metadata or {}).get("/Title") or "")
    return Doc("PDF", pages, notes=notes, title=title)


# --- Word ---


def _docx(p: Path) -> Doc:
    import docx
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    d = docx.Document(str(p))
    out: list[str] = []
    for child in d.element.body.iterchildren():  # paragraphs and tables in document order
        if child.tag == qn("w:p"):
            if line := _paragraph(Paragraph(child, d)):
                out.append(line)
        elif child.tag == qn("w:tbl"):
            out.append(_md_table([[c.text for c in row.cells] for row in Table(child, d).rows]))
    return Doc("Word", ["\n\n".join(out)], title=d.core_properties.title or "")


def _paragraph(para: Any) -> str:
    text = para.text.strip()
    if not text:
        return ""
    style = (para.style.name if para.style is not None else "") or ""
    if style == "Title":
        return f"# {text}"
    if style.startswith("Heading ") and style[8:].isdigit():
        return f"{'#' * min(6, int(style[8:]) + 1)} {text}"  # Title takes #, so Heading 1 is ##
    depth = "  " * (int(style[-1]) - 1) if style[-1:] in "23456789" and style.startswith("List ") else ""
    if style.startswith("List Number"):
        return f"{depth}1. {text}"
    if style.startswith(("List Bullet", "List Paragraph")) or para._p.pPr is not None and para._p.pPr.numPr is not None:
        return f"{depth}- {text}"
    if style in ("Quote", "Intense Quote"):
        return f"> {text}"
    return text


def _md_table(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    width = max(len(r) for r in rows)

    def line(r: list[str]) -> str:
        cells = [" ".join(str(c).split()).replace("|", "\\|") for c in r] + [""] * (width - len(r))
        return "| " + " | ".join(cells) + " |"

    return "\n".join([line(rows[0]), "|" + " --- |" * width, *(line(r) for r in rows[1:])])


# --- PowerPoint ---


def _pptx(p: Path) -> Doc:
    from pptx import Presentation

    prs = Presentation(str(p))
    slides = []
    pictures = 0
    for slide in prs.slides:
        lines: list[str] = []
        title = slide.shapes.title
        if title is not None and title.text_frame.text.strip():
            lines.append(f"## {title.text_frame.text.strip()}")
        for shape in slide.shapes:
            if shape is not title:
                pictures += _shape(shape, lines)
        if slide.has_notes_slide and (notes := slide.notes_slide.notes_text_frame.text.strip()):
            lines.append(f"Speaker notes: {notes}")
        slides.append("\n".join(lines))
    notes = [f"{pictures} picture(s) on the slides are not described"] if pictures else []
    return Doc("PowerPoint", slides, notes=notes, title=prs.core_properties.title or "")


def _shape(shape: Any, lines: list[str]) -> int:
    """Text of one shape into lines; returns how many pictures it holds."""
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
        return sum(_shape(s, lines) for s in shape.shapes)
    if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
        return 1
    if shape.has_table:
        lines.append(_md_table([[c.text for c in row.cells] for row in shape.table.rows]))
    elif shape.has_text_frame:
        for para in shape.text_frame.paragraphs:
            if t := "".join(r.text for r in para.runs).strip():
                lines.append(f"{'  ' * para.level}- {t}")
    return 0


# --- Excel ---


def _xlsx(p: Path) -> Doc:
    from openpyxl import load_workbook
    from openpyxl.utils import get_column_letter

    formulas = load_workbook(p, read_only=True, data_only=False)
    values = load_workbook(p, read_only=True, data_only=True)
    sheets, names, notes = [], [], []
    uncached = 0
    for ws in formulas.worksheets:
        cached = values[ws.title].iter_rows()
        rows: list[str] = []
        width = 0
        for n, (row, vrow) in enumerate(zip(ws.iter_rows(), cached, strict=False), 1):
            if n > MAX_SHEET_ROWS:
                notes.append(f"sheet {ws.title}: only the first {MAX_SHEET_ROWS} rows are shown")
                break
            cells = []
            for c, v in zip(row, vrow, strict=False):
                if isinstance(c.value, str) and c.value.startswith("="):
                    uncached += v.value is None
                    cells.append(f"{c.value} → {v.value}" if v.value is not None else f"{c.value} (not calculated)")
                else:
                    cells.append("" if c.value is None else str(c.value))
            while cells and not cells[-1]:
                cells.pop()
            if cells:
                width = max(width, len(cells))
                rows.append(f"{n}: " + " | ".join(" ".join(x.split()) for x in cells))
        header = "columns: " + " | ".join(get_column_letter(i) for i in range(1, width + 1)) if width else "(empty)"
        sheets.append("\n".join([header, *rows]))
        names.append(ws.title)
    if uncached:
        notes.append(f"{uncached} formula cell(s) have no saved result (the file was never recalculated); "
                     "their values are unknown")
    return Doc("Excel", sheets, names=names, notes=notes)
