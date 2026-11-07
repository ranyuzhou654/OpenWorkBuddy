"""Bundled-runtime DOCX build from one reviewed Markdown body; no model or search request."""
import re
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

folder = Path('docs/research/2026-10-26-agent-reliability-brief')
text = (folder / 'research-brief.md').read_text()
doc = Document()
section = doc.sections[0]
section.top_margin = section.bottom_margin = Cm(2.2)
section.left_margin = section.right_margin = Cm(2.3)
for name in ('Normal', 'Title', 'Heading 1', 'Heading 2', 'Heading 3', 'List Bullet'):
    style = doc.styles[name]
    style.font.name = 'Arial'
    style.font.color.rgb = RGBColor(0, 0, 0)
    style._element.get_or_add_rPr().get_or_add_rFonts().set(qn('w:eastAsia'), 'Noto Sans CJK SC')
    style.font.size = Pt({'Title': 22, 'Heading 1': 14, 'Heading 2': 12}.get(name, 10.5))
    style.paragraph_format.space_after = Pt(6)
    style.paragraph_format.line_spacing = 1.15
    if name not in ('Normal', 'List Bullet'):
        style.paragraph_format.keep_with_next = True
    borders = style._element.xpath('./w:pPr/w:pBdr')
    for border in borders:
        border.getparent().remove(border)
references = False


def append_text(p, text):
    start = 0
    for match in re.finditer(r'\[([^\]]+)\]\((https?://[^\s)]+)\)', text):
        p.add_run(text[start:match.start()])
        link = OxmlElement('w:hyperlink')
        link.set(qn('r:id'), p.part.relate_to(match[2], RT.HYPERLINK, is_external=True))
        run = OxmlElement('w:r')
        properties = OxmlElement('w:rPr')
        size = OxmlElement('w:sz')
        size.set(qn('w:val'), '16' if references else '21')
        color = OxmlElement('w:color')
        color.set(qn('w:val'), '000000')
        underline = OxmlElement('w:u')
        underline.set(qn('w:val'), 'single')
        properties.extend([size, color, underline])
        node = OxmlElement('w:t')
        node.text = match[1]
        run.extend([properties, node])
        link.append(run)
        p._p.append(link)
        start = match.end()
    p.add_run(text[start:])


for block in re.split(r'\n\s*\n', text.strip()):
    if block.startswith('# '):
        doc.add_paragraph(block[2:], style='Title')
    elif block.startswith('## '):
        heading = block[3:]
        references = heading == '参考文献'
        doc.add_paragraph(heading, style='Heading 1')
    elif block.startswith('### '):
        doc.add_paragraph(block[4:], style='Heading 2')
    else:
        lines = block.splitlines() if block.startswith('- ') else [' '.join(block.splitlines())]
        for line in lines:
            # Keep web link titles clickable; the bibliography already prints the URL once.
            line = re.sub(r'\\([\\`*_\[\]<>])', r'\1', line)
            bullet = line.startswith('- ')
            p = doc.add_paragraph(style='List Bullet' if bullet else 'Normal')
            append_text(p, line[2:] if bullet else line)
            if references:
                p.paragraph_format.line_spacing = 1.05
                for run in p.runs:
                    run.font.size = Pt(8)
footer = section.footer.paragraphs[0]
footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
field = OxmlElement('w:fldSimple')
field.set(qn('w:instr'), 'PAGE')
footer._p.append(field)
doc.core_properties.title = 'OpenWorkBuddy Agent 可靠性与上下文治理研究简报'
doc.core_properties.author = 'OpenWorkBuddy draft with Codex review'
doc.save(folder / 'research-brief.docx')
print('Reviewed DOCX created from the shared Markdown body')
