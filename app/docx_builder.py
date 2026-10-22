"""Renders the agent's structured sections into a polished .docx (no LLM involved)."""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

from .schemas import Interpretation, Section

ACCENT = RGBColor(0x1F, 0x3A, 0x5F)
MUTED = RGBColor(0x59, 0x59, 0x59)
_BOLD = re.compile(r"\*\*(.+?)\*\*")


def _clean(text: str) -> str:
    """Strip markdown the model may still emit (#, list dashes, stray asterisks)."""
    text = re.sub(r"^\s*(#{1,6}\s+|[-*•]\s+)", "", text.strip())
    return text


def _add_rich_text(paragraph, text: str) -> None:
    """Render **bold** spans as real bold runs instead of literal asterisks."""
    text = _clean(text)
    pos = 0
    for m in _BOLD.finditer(text):
        if m.start() > pos:
            paragraph.add_run(text[pos:m.start()])
        paragraph.add_run(m.group(1)).bold = True
        pos = m.end()
    if pos < len(text):
        paragraph.add_run(text[pos:].replace("**", ""))


def _shade(cell, hex_fill: str) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_fill)
    tc_pr.append(shd)


def _add_page_number(paragraph) -> None:
    paragraph.add_run("Page ")
    run = paragraph.add_run()
    for tag, attr in (("w:fldChar", "begin"), ("w:instrText", None), ("w:fldChar", "end")):
        el = OxmlElement(tag)
        if attr:
            el.set(qn("w:fldCharType"), attr)
        else:
            el.set(qn("xml:space"), "preserve")
            el.text = "PAGE"
        run._r.append(el)


def _add_table(doc, headers: list[str], rows: list[list[str]]) -> None:
    table = doc.add_table(rows=1, cols=len(headers))
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    for cell, h in zip(table.rows[0].cells, headers):
        cell.text = ""
        run = cell.paragraphs[0].add_run(_clean(h))
        run.bold = True
        run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
        _shade(cell, "1F3A5F")
    for i, row in enumerate(rows):
        cells = table.add_row().cells
        for cell, value in zip(cells, row):
            cell.text = ""
            _add_rich_text(cell.paragraphs[0], str(value))
            if i % 2:
                _shade(cell, "EEF2F7")
    doc.add_paragraph()


def build_docx(path: Path, interp: Interpretation, sections: list[Section], request_id: str) -> Path:
    doc = Document()

    normal = doc.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(11)
    for level, size in ((1, 15), (2, 12)):
        style = doc.styles[f"Heading {level}"]
        style.font.name = "Calibri"
        style.font.size = Pt(size)
        style.font.color.rgb = ACCENT

    # Header / footer
    sec = doc.sections[0]
    header_p = sec.header.paragraphs[0]
    header_p.text = interp.title
    header_p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    header_p.runs[0].font.size = Pt(8)
    header_p.runs[0].font.color.rgb = MUTED
    footer_p = sec.footer.paragraphs[0]
    footer_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _add_page_number(footer_p)

    # Title block
    title = doc.add_paragraph()
    run = title.add_run(interp.title)
    run.bold, run.font.size, run.font.color.rgb = True, Pt(24), ACCENT
    subtitle = doc.add_paragraph()
    run = subtitle.add_run(interp.doc_type.replace("_", " ").title())
    run.font.size, run.font.color.rgb = Pt(13), MUTED

    meta = doc.add_table(rows=0, cols=2)
    meta.style = "Table Grid"
    for label, value in (("Prepared for", interp.audience), ("Date", date.today().strftime("%d %B %Y")),
                         ("Prepared by", "Autonomous Document Agent"), ("Reference", request_id)):
        cells = meta.add_row().cells
        cells[0].text = ""
        cells[0].paragraphs[0].add_run(label).bold = True
        _shade(cells[0], "EEF2F7")
        cells[1].text = value
    doc.add_paragraph()

    for section in sections:
        doc.add_heading(_clean(section.title), level=1)
        for block in section.blocks:
            if block.type == "paragraph" and block.text:
                _add_rich_text(doc.add_paragraph(), block.text)
            elif block.type in ("bullets", "numbered") and block.items:
                style = "List Bullet" if block.type == "bullets" else "List Number"
                for item in block.items:
                    _add_rich_text(doc.add_paragraph(style=style), item)
            elif block.type == "table" and block.headers and block.rows:
                _add_table(doc, block.headers, block.rows)

    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    return path
