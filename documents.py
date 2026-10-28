"""Read text out of PDF, DOCX, and PPTX uploads for the chat window.

Pictures and scanned pages have no text layer, so they are skipped.
"""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree

TEXT_LIMIT = 12_000
FILE_LIMIT = 8_000_000
MEMBER_LIMIT = 5_000_000
MAX_PDF_PAGES = 40

W_P = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"
W_T = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"
W_TAB = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}tab"
W_BR = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}br"
A_P = "{http://schemas.openxmlformats.org/drawingml/2006/main}p"
A_T = "{http://schemas.openxmlformats.org/drawingml/2006/main}t"

ALLOWED = {".pdf", ".docx", ".pptx"}


def display_name(filename: str) -> str:
    name = Path(filename.replace("\\", "/")).name.strip()
    name = re.sub(r"[\x00-\x1f]", "", name)
    if not name or name in {".", ".."}:
        raise ValueError("That file needs a name ending in .pdf, .docx, or .pptx.")
    return name[:120]


def extract_document(filename: str, data: bytes) -> dict:
    if len(data) > FILE_LIMIT:
        raise ValueError("That file is larger than 8 MB.")
    if not data:
        raise ValueError("That file is empty.")
    name = display_name(filename)
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED:
        raise ValueError("VLe reads PDF, DOCX, and PPTX files.")
    if suffix == ".pdf":
        text = extract_pdf(data)
    elif suffix == ".docx":
        text = extract_docx(data)
    else:
        text = extract_pptx(data)
    text = text.replace("\x00", "").strip()
    if not text:
        raise ValueError(
            "No readable text was found in that file. "
            "Scanned pages and pictures are skipped."
        )
    truncated = len(text) > TEXT_LIMIT
    if truncated:
        text = text[:TEXT_LIMIT].rstrip()
    return {"name": name, "text": text, "truncated": truncated}


def extract_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError as err:
        raise ValueError("PDF reading is not installed. Run pip install -r requirements.txt.") from err
    try:
        reader = PdfReader(io.BytesIO(data))
    except (PdfReadError, OSError, ValueError) as err:
        raise ValueError("VLe could not open that PDF.") from err
    if reader.is_encrypted:
        try:
            opened = reader.decrypt("")
        except Exception as err:
            raise ValueError("That PDF is password-protected.") from err
        if not opened:
            raise ValueError("That PDF is password-protected.")
    pages: list[str] = []
    total = min(len(reader.pages), MAX_PDF_PAGES)
    for index in range(total):
        try:
            raw = reader.pages[index].extract_text() or ""
        except Exception:
            raw = ""
        raw = raw.strip()
        if raw:
            pages.append(f"Page {index + 1}\n{raw}")
    if len(reader.pages) > MAX_PDF_PAGES:
        pages.append(f"(Stopped after {MAX_PDF_PAGES} pages.)")
    return "\n\n".join(pages)


def extract_docx(data: bytes) -> str:
    members = read_office_members(
        data,
        ["word/document.xml", "word/footnotes.xml", "word/endnotes.xml"],
    )
    document = members.get("word/document.xml")
    if document is None:
        raise ValueError("That Word file has no document text.")
    chunks = [paragraphs_docx(document)]
    notes = paragraphs_docx(members.get("word/footnotes.xml", b""))
    if notes:
        chunks.append("Footnotes\n" + notes)
    endnotes = paragraphs_docx(members.get("word/endnotes.xml", b""))
    if endnotes:
        chunks.append("Endnotes\n" + endnotes)
    return "\n\n".join(part for part in chunks if part)


def extract_pptx(data: bytes) -> str:
    with open_office_zip(data) as package:
        slides = sorted(
            (
                name
                for name in package.namelist()
                if name.startswith("ppt/slides/slide") and name.endswith(".xml")
            ),
            key=slide_number,
        )
        notes = {
            slide_number(name): name
            for name in package.namelist()
            if name.startswith("ppt/notesSlides/notesSlide") and name.endswith(".xml")
        }
        if not slides:
            raise ValueError("That PowerPoint file has no slides.")
        blocks: list[str] = []
        for name in slides:
            number = slide_number(name)
            lines = paragraphs_pptx(read_member(package, name))
            note_name = notes.get(number)
            note_lines = paragraphs_pptx(read_member(package, note_name)) if note_name else ""
            if not lines and not note_lines:
                continue
            block = [f"Slide {number}"]
            if lines:
                block.append(lines)
            if note_lines:
                block.append("Notes\n" + note_lines)
            blocks.append("\n".join(block))
    return "\n\n".join(blocks)


def paragraphs_docx(xml: bytes) -> str:
    if not xml:
        return ""
    root = parse_xml(xml)
    lines: list[str] = []
    for paragraph in root.iter(W_P):
        pieces: list[str] = []
        for node in paragraph.iter():
            if node.tag == W_T and node.text:
                pieces.append(node.text)
            elif node.tag == W_TAB:
                pieces.append("\t")
            elif node.tag == W_BR:
                pieces.append("\n")
        line = "".join(pieces).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def paragraphs_pptx(xml: bytes) -> str:
    if not xml:
        return ""
    root = parse_xml(xml)
    lines: list[str] = []
    for paragraph in root.iter(A_P):
        line = "".join(node.text or "" for node in paragraph.iter(A_T)).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def parse_xml(xml: bytes) -> ElementTree.Element:
    if len(xml) > MEMBER_LIMIT:
        raise ValueError("That file is too large to read.")
    try:
        return ElementTree.fromstring(xml)
    except ElementTree.ParseError as err:
        raise ValueError("VLe could not read the text in that file.") from err


def slide_number(name: str) -> int:
    match = re.search(r"(\d+)\.xml$", name)
    return int(match.group(1)) if match else 0


def open_office_zip(data: bytes) -> zipfile.ZipFile:
    try:
        package = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as err:
        raise ValueError("VLe could not open that file.") from err
    return package


def read_member(package: zipfile.ZipFile, name: str) -> bytes:
    try:
        info = package.getinfo(name)
    except KeyError:
        return b""
    if info.file_size > MEMBER_LIMIT:
        raise ValueError("That file is too large to read.")
    return package.read(name)


def read_office_members(data: bytes, names: list[str]) -> dict[str, bytes]:
    with open_office_zip(data) as package:
        return {name: read_member(package, name) for name in names}
