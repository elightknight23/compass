"""Reading uploaded documents — extraction, structure, and calendar dates.

Everything here is pure: bytes in, text and events out. No backend, no
index, no model. That is what lets the parsing be tested exhaustively
offline, and it is why the calendar can feed the working-day arithmetic
without breaking I5: a date reaches the system because a regex matched it
in the document and a person confirmed it, never because a model read it.

Three stages:

1. **Extract** (`extract`) — PDF through pdfplumber, page by page, keeping
   tables as tables: a calendar is a table, and flattening it to prose
   separates every date from its event. DOCX through python-docx, keeping
   heading styles. Markdown and text as they are. A PDF with no text layer
   (a scan) is refused with a reason rather than indexed as nothing.

2. **Normalise** (`to_markdown`) — turn the extracted blocks into headed
   markdown so `rag.structured_chunks` can cut it at real boundaries. The
   syllabus normaliser knows the shape of a university syllabus (semester,
   course code and title, units, text books), so the chunk that lists the
   text books is headed "... > CS3591 Computer Networks > Text Books" and
   the course travels with its books.

3. **Parse the calendar** (`parse_calendar`) — find dated events in table
   rows and lines. Dates are day-first (the Indian convention), years are
   inferred from the academic year when a row leaves them out, and every
   guess is flagged for the reviewer: an inferred year, a weekday printed
   in the row that does not match the parsed date (which is how a swapped
   day and month gets caught), a row that looks like a holiday but has no
   date that could be read.

The patterns are written against Indian-university layouts: NIT-style
course pages ("CS1305 3-0-0 (3)" with the title on the next line,
"Learning Resources: Text Books:", elective / minor / honours groups),
Anna University-style codes such as CS3591 with "UNIT I" and "TEXT BOOKS",
VTU-style 21CS51, and dd.mm.yyyy dates. A college with a different layout
needs these patterns adjusted, and the tests in scripts/test_documents.py
say which shapes are supported.
"""

from __future__ import annotations

import calendar as _calendar
import io
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------
SUPPORTED = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".md": "text/markdown",
    ".txt": "text/plain",
}


class DocumentError(ValueError):
    """A file that cannot become searchable text. `kind` is the outcome the
    API maps to a status code; the message is safe to show the uploader."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass
class Block:
    """One unit of extracted content, in reading order.

    kind "heading" carries a level (1-4); "row" is a table row, cells
    joined with " | "; "day" is one square of a month-grid calendar, cells
    [weekday and date, what is written in it]; "line" is everything else.
    """

    page: int
    kind: str
    text: str
    level: int = 0
    cells: list[str] = field(default_factory=list)


@dataclass
class Extracted:
    blocks: list[Block]
    pages: int | None


# ---------------------------------------------------------------------------
# 1. Extraction
# ---------------------------------------------------------------------------
def sniff(filename: str, data: bytes) -> str:
    """The file's extension, once its bytes agree with it. Raises
    DocumentError for anything unsupported or disguised."""
    name = (filename or "").lower()
    ext = next((e for e in SUPPORTED if name.endswith(e)), None)
    if ext is None:
        raise DocumentError(
            "unsupported_type",
            "Only PDF, Word (.docx), Markdown and plain-text files can be uploaded.",
        )
    if not data:
        raise DocumentError("empty", "That file is empty.")
    if ext == ".pdf" and not data.startswith(b"%PDF"):
        raise DocumentError("unsupported_type", "That file is named .pdf but is not a PDF.")
    if ext == ".docx" and not data.startswith(b"PK"):
        raise DocumentError("unsupported_type", "That file is named .docx but is not a Word document.")
    if ext in (".md", ".txt") and b"\x00" in data[:4096]:
        raise DocumentError("unsupported_type", "That file is named as text but contains binary data.")
    return ext


def extract(data: bytes, ext: str, max_pages: int = 400) -> Extracted:
    if ext == ".pdf":
        return _extract_pdf(data, max_pages)
    if ext == ".docx":
        return _extract_docx(data)
    return _extract_text(data, markdown=ext == ".md")


def _cell(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _extract_pdf(data: bytes, max_pages: int) -> Extracted:
    import pdfplumber

    try:
        pdf = pdfplumber.open(io.BytesIO(data))
    except Exception as e:  # noqa: BLE001 — pdfminer raises a zoo of types
        name = type(e).__name__
        if "Password" in name or "Encrypt" in name:
            raise DocumentError("unreadable", "That PDF is password-protected.") from e
        raise DocumentError("unreadable", f"That PDF could not be opened ({name}).") from e

    blocks: list[Block] = []
    with pdf:
        n = len(pdf.pages)
        if n > max_pages:
            raise DocumentError(
                "too_large", f"That PDF has {n} pages; the limit is {max_pages}."
            )
        text_chars = 0
        for number, page in enumerate(pdf.pages, start=1):
            try:
                tables = page.find_tables()
            except Exception:  # noqa: BLE001 — a malformed page keeps its text
                tables = []
            # A month grid is read square by square, from where the words
            # sit; the small tables pdfplumber also finds inside it go.
            grids = {id(t): days for t in tables if (days := _grid_days(page, t, number))}
            grid_boxes = [t.bbox for t in tables if id(t) in grids]
            tables = [t for t in tables if id(t) in grids or not any(
                _inside(t.bbox, g) for g in grid_boxes)]
            boxes = [t.bbox for t in tables]

            def outside(obj, boxes=boxes) -> bool:
                x = (obj["x0"] + obj["x1"]) / 2
                y = (obj["top"] + obj["bottom"]) / 2
                return not any(b[0] <= x <= b[2] and b[1] <= y <= b[3] for b in boxes)

            # Tables and the text around them, merged back into reading
            # order by their vertical position on the page.
            placed: list[tuple[float, Block]] = []
            body = page.filter(outside) if boxes else page
            for line in body.extract_text_lines() or []:
                text = _cell(line.get("text"))
                if text:
                    text_chars += len(text)
                    placed.append((line["top"], Block(number, "line", text)))
            for t in tables:
                if id(t) in grids:
                    for i, day in enumerate(grids[id(t)]):
                        text_chars += len(day.text)
                        placed.append((t.bbox[1] + i * 0.01, day))
                    continue
                for i, row in enumerate(t.extract() or []):
                    cells = [_cell(c) for c in row]
                    if any(cells):
                        text_chars += sum(len(c) for c in cells)
                        placed.append((t.bbox[1] + i * 0.01,
                                       Block(number, "row", " | ".join(c for c in cells if c), cells=cells)))
            placed.sort(key=lambda p: p[0])
            blocks.extend(b for _, b in placed)

    if n and text_chars < 20 * n:
        raise DocumentError(
            "scanned",
            "That PDF has almost no selectable text — it looks like a scan or a "
            "photo. Compass can't read scanned documents (no OCR); please upload "
            "the original digital PDF or a Word file.",
        )
    return Extracted(blocks, n)


def _inside(inner: tuple, outer: tuple) -> bool:
    return (inner[0] >= outer[0] - 1 and inner[1] >= outer[1] - 1
            and inner[2] <= outer[2] + 1 and inner[3] <= outer[3] + 1) and inner != outer


_DAY_HEADS = {name.lower(): i for i, name in enumerate(_calendar.day_name)}
_DAY_HEADS.update({name.lower(): i for i, name in enumerate(_calendar.day_abbr)})
_WEEK_NO_RE = re.compile(r"^\(?\s*week\s*\d+\s*\)?$", re.I)
_ONLY_WEEKDAY_RE = re.compile(r"^\(\s*([A-Za-z]+)\s*\)$")


def _grid_days(page, table, number: int) -> list[Block] | None:
    """A month-grid calendar (a SUN..SAT header, a square per day), as one
    block per day that has something written in it.

    Rows of such a grid don't line up as table rows — a label wraps onto
    lines of its own, and squares are merged — so the words are placed by
    position instead: each word belongs to the weekday column it sits
    under, and to the date above it in that column. The column also says
    which weekday the date is, which the parser uses as a check.
    None if this table is not a grid.
    """
    x0, top, x1, bottom = bbox = table.bbox
    box = (max(x0 - 1, 0), max(top - 1, 0), min(x1 + 1, page.width), min(bottom + 1, page.height))
    try:
        words = page.within_bbox(box).extract_words(use_text_flow=False)
    except Exception:  # noqa: BLE001 — a malformed table is read as rows instead
        return None
    heads = sorted((w for w in words if w["text"].lower().rstrip(".") in _DAY_HEADS),
                   key=lambda w: w["top"])
    if not heads:
        return None
    header = [w for w in heads if abs(w["top"] - heads[0]["top"]) < 3]
    if len({_DAY_HEADS[w["text"].lower().rstrip(".")] for w in header}) < 5:
        return None
    # A column is the header's whole cell, not where its text sits: a
    # left-aligned "MON" is nearer the Sunday square's right-hand words
    # than its own.
    cells = [c for row in table.rows for c in row.cells if c]
    columns = []
    for w in sorted(header, key=lambda w: w["x0"]):
        mid = (w["x0"] + w["x1"]) / 2
        cell = min((c for c in cells if c[0] <= mid <= c[2] and c[1] <= w["top"] <= c[3]),
                   key=lambda c: c[2] - c[0], default=(w["x0"], 0, w["x1"], 0))
        columns.append((cell[0], cell[2], _DAY_HEADS[w["text"].lower().rstrip(".")]))
    below = max(w["bottom"] for w in header)

    def column_of(mid: float) -> int:
        inside = [i for i, (a, b, _) in enumerate(columns) if a <= mid <= b]
        return inside[0] if inside else min(
            range(len(columns)), key=lambda i: min(abs(columns[i][0] - mid), abs(columns[i][1] - mid)))

    lines: dict[int, list[list[dict]]] = {}
    for w in sorted((w for w in words if w["top"] > below), key=lambda w: (w["top"], w["x0"])):
        col = column_of((w["x0"] + w["x1"]) / 2)
        col_lines = lines.setdefault(col, [])
        if col_lines and abs(col_lines[-1][0]["top"] - w["top"]) < 2.5:
            col_lines[-1].append(w)
        else:
            col_lines.append([w])

    days: list[tuple[float, float, Block]] = []
    for col, col_lines in lines.items():
        weekday = _calendar.day_name[columns[col][2]]
        current: list | None = None
        for line in col_lines:
            text = " ".join(w["text"] for w in line)
            if re.fullmatch(rf"({MONTH_ALT})\.?\s*\d{{1,2}}", text, re.I):
                current = [line[0]["top"], columns[col][0], f"{weekday}, {text}", []]
                days.append(current)
            elif current is not None and not _WEEK_NO_RE.match(text):
                current[3].append(text)
    blocks = []
    for top_, x, when, label_lines in sorted(days, key=lambda d: (d[0], d[1])):
        # "Mid-" at a line end and "Semester" on the next are one word.
        label = re.sub(r"(\w)- (\w)", r"\1-\2", " ".join(label_lines)).strip()
        m = _ONLY_WEEKDAY_RE.match(label)
        if m and m.group(1).lower() in _DAY_HEADS:
            label = f"Follows {m.group(1).title()}'s timetable"  # a compensatory working day
        if label:
            blocks.append(Block(number, "day", f"{when} | {label}", cells=[when, label]))
    return blocks if days else None


def _extract_docx(data: bytes) -> Extracted:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as e:  # noqa: BLE001
        raise DocumentError("unreadable", f"That Word file could not be opened ({type(e).__name__}).") from e

    blocks: list[Block] = []
    body = document.element.body
    for child in body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            p = Paragraph(child, document)
            text = _cell(p.text)
            if not text:
                continue
            style = (p.style.name or "") if p.style is not None else ""
            m = re.match(r"Heading (\d)", style)
            if style == "Title":
                blocks.append(Block(1, "heading", text, level=1))
            elif m:
                blocks.append(Block(1, "heading", text, level=min(int(m.group(1)) + 1, 4)))
            else:
                blocks.append(Block(1, "line", text))
        elif tag == "tbl":
            for row in Table(child, document).rows:
                cells = [_cell(c.text) for c in row.cells]
                # merged cells repeat their text across the span
                deduped = [c for i, c in enumerate(cells) if c and (i == 0 or c != cells[i - 1])]
                if deduped:
                    blocks.append(Block(1, "row", " | ".join(deduped), cells=cells))
    if not blocks:
        raise DocumentError("empty", "That Word file has no text in it.")
    return Extracted(blocks, None)


def _extract_text(data: bytes, markdown: bool) -> Extracted:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    blocks = []
    for line in text.splitlines():
        m = re.match(r"^(#{1,4})\s+(.+)", line) if markdown else None
        if m:
            blocks.append(Block(1, "heading", m.group(2).strip(), level=len(m.group(1))))
        elif re.fullmatch(r"\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*", line):
            continue  # a markdown table's |---|---| rule
        elif line.count("|") >= 2 or (line.count("|") == 1 and not line.strip().startswith("|")):
            # A table row, in a markdown table or a pasted one: read it by
            # cell, as a PDF table is, so the calendar parser sees columns.
            cells = [_cell(c) for c in line.strip().strip("|").split("|")]
            blocks.append(Block(1, "row", " | ".join(c for c in cells if c), cells=cells))
        elif line.strip():
            blocks.append(Block(1, "line", line.rstrip()))
        else:
            blocks.append(Block(1, "line", ""))
    if not any(b.text.strip() for b in blocks):
        raise DocumentError("empty", "That file has no text in it.")
    return Extracted(blocks, None)


# ---------------------------------------------------------------------------
# 2. Normalisation to headed markdown
# ---------------------------------------------------------------------------
ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8,
         "IX": 9, "X": 10}

SEMESTER_RE = re.compile(r"^\s*SEMESTER\s*[-:–]?\s*(VIII|VII|VI|IV|IX|V|III|II|I|X|\d{1,2})\b\s*$", re.I)
SEMESTER_ALT_RE = re.compile(r"^\s*(VIII|VII|VI|IV|IX|V|III|II|I|X|\d{1,2})\s*(?:st|nd|rd|th)?\s+SEMESTER\b\s*$", re.I)
# CS1M01 / CS1H01 (NIT minors, honours), CS1305 / EC 1263 / CS3591 /
# GE3451A (NIT, Anna University), or 21CS51 / 21CSL57 (VTU).
COURSE_CODE = r"(?:[A-Z]{2,4}\d[A-Z]\d{2}|[A-Z]{2,4}\s?\d{3,5}[A-Z]?|\d{2}[A-Z]{2,4}\d{2,3}[A-Z]?)"
COURSE_RE = re.compile(
    rf"^\s*({COURSE_CODE})\s*[-–:]?\s+([A-Z][A-Za-z0-9&,/()'.\- ]{{2,}}?)"
    r"(?:\s+L\s*T\s*P\s*C)?(?:\s+\d+(?:\.\d)?){0,4}\s*$"
)
# NIT layout: "CS1305 3-0-0 (3)" — or the code and the L-T-P (credits) on
# two lines — with the course title on the line after.
_LTP = r"\d{1,2}\s*-\s*\d{1,2}\s*-\s*\d{1,2}\s*\(\s*\d{1,2}(?:\.\d{1,2})?\s*\)"
COURSE_LTP_RE = re.compile(rf"^\s*({COURSE_CODE})\s+{_LTP}\s*$")
CODE_ONLY_RE = re.compile(rf"^\s*({COURSE_CODE})\s*$")
LTP_ONLY_RE = re.compile(rf"^\s*{_LTP}\s*$")
# Groups of courses that belong to no one semester: a chunk under one is
# semester 0, which every semester's filter matches.
GROUP_RE = re.compile(
    r"^\s*(professional\s+electives?(?:\s+courses)?|open\s+electives?(?:\s+courses)?"
    r"|(?:minor|honou?rs)\s*(?:degree\b[^.]*|:[^.]*)"
    r"|(?:basic\s+science|engineering\s+science|humanities[a-z ,&]*|program\s+core)\s+courses"
    r"|the\s+overall\s+credit\s+structure)\s*:?\s*$", re.I,
)
ARTICULATION_RE = re.compile(r"^\s*(?:course\s+)?articulation\s+matrix\b", re.I)
MATRIX_LEGEND_RE = re.compile(r"^\s*1\s*-\s*slightly\b", re.I)
LEARNING_RESOURCES_RE = re.compile(r"^\s*learning\s+resources?\s*:?\s*$", re.I)
UNIT_RE = re.compile(r"^\s*UNIT\s*[-–:]?\s*(V|IV|III|II|I|\d)\b\s*[-–:.]?\s*(.*?)\s*(?:\d{1,2})?\s*$", re.I)
SECTION_NAMES = [
    (re.compile(r"^\s*TEXT\s*-?\s*BOOKS?\b\s*:?\s*(.*)$", re.I), "Text Books"),
    (re.compile(r"^\s*(?:REFERENCE\s*BOOKS?|REFERENCES?)\b\s*:?\s*(.*)$", re.I), "References"),
    (re.compile(r"^\s*COURSE\s+OBJECTIVES?\b\s*:?\s*(.*)$", re.I), "Course Objectives"),
    (re.compile(r"^\s*COURSE\s+OUTCOMES?\b\s*:?\s*(.*)$", re.I), "Course Outcomes"),
    (re.compile(r"^\s*(?:LIST\s+OF\s+EXPERIMENTS|PRACTICAL\s+EXERCISES)\b\s*:?\s*(.*)$", re.I), "Practicals"),
    (re.compile(r"^\s*(?:WEB\s*REFERENCES?|ONLINE\s+RESOURCES?|E-?RESOURCES?)\b\s*:?\s*(.*)$", re.I), "Online Resources"),
    (re.compile(r"^\s*SYLLABUS\s*:\s*(.*)$", re.I), "Syllabus"),
]
# Page furniture: "62 | P a ge", "Page 3 of 40", a bare number.
PAGE_NUMBER_RE = re.compile(
    r"^\s*(?:\d{1,4}\s*\|\s*p\s*a\s*g\s*e|page\s*\d{1,4}(?:\s*of\s*\d{1,4})?|[-–]?\s*\d{1,4}\s*[-–]?)\s*$", re.I,
)
MONTH_HEADING_RE = re.compile(
    r"^\s*(JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER)"
    r"\s*[-–,]?\s*(\d{4})?\s*$", re.I,
)


def semester_number(token: str) -> int | None:
    token = token.strip().upper()
    if token.isdigit():
        n = int(token)
        return n if 1 <= n <= 10 else None
    return ROMAN.get(token)


def _title_case(text: str) -> str:
    """'COMPUTER NETWORKS' -> 'Computer Networks'; mixed case is left alone."""
    if text.upper() != text:
        return text
    small = {"and", "of", "in", "for", "to", "the", "a", "an", "on", "with"}
    words = text.lower().split()
    return " ".join(w if (i and w in small) else w[:1].upper() + w[1:] for i, w in enumerate(words))


def _is_caps_heading(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return (
        4 <= len(text) <= 80
        and len(letters) >= 4
        and sum(c.isupper() for c in letters) / len(letters) > 0.85
        and not text.rstrip().endswith((".", ","))
        and "|" not in text
    )


def strip_furniture(blocks: list[Block], pages: int | None) -> list[Block]:
    """Drop running headers, footers and page numbers.

    A line counts as a running header or footer when it is the first or
    last line on at least half the pages. Position matters: "Syllabus:"
    is on most pages of a curriculum too, but never as the page's edge.
    """
    edges: dict[int, list[int]] = {}
    for i, b in enumerate(blocks):
        if b.kind == "line":
            edges.setdefault(b.page, []).append(i)
    edge_ids = {i for ids in edges.values() for i in (ids[:2] + ids[-2:])}
    seen: dict[str, set[int]] = {}
    for i in edge_ids:
        seen.setdefault(blocks[i].text.strip(), set()).add(blocks[i].page)
    n = pages or len(edges)
    running = {t for t, on in seen.items() if n >= 4 and len(on) >= max(3, n / 2)}
    return [
        b for i, b in enumerate(blocks)
        if not (b.kind == "line" and (PAGE_NUMBER_RE.match(b.text)
                                      or (i in edge_ids and b.text.strip() in running)))
    ]


def to_markdown(extracted: Extracted, doc_type: str, title: str) -> str:
    """Headed markdown for the chunker. The document title is the top
    heading, so it heads every chunk's breadcrumb (and so every citation)."""
    blocks = strip_furniture(extracted.blocks, extracted.pages)
    if doc_type == "syllabus":
        body = _syllabus(blocks)
    elif doc_type == "academic_calendar":
        body = _calendar_md(blocks)
    else:
        body = _generic(blocks)
    return f"# {title}\n\n" + "\n".join(body).strip() + "\n"


def _render(b: Block) -> str:
    return b.text


def _syllabus(blocks: list[Block]) -> list[str]:
    """Semester (or elective group) -> course -> unit/section, as ## / ### / ####."""
    out: list[str] = []
    in_course = False
    pending: tuple[str, str] | None = None  # (code, "ltp" | "title")
    in_matrix = False
    for b in blocks:
        text = b.text.strip()
        if b.kind == "heading":
            out.append(f"\n{'#' * max(2, min(b.level, 4))} {text}\n")
            pending, in_matrix = None, False
            continue
        if in_matrix:
            # The CO-PO articulation matrix is a grid of 1-3s: noise to a
            # reader and to retrieval. It ends at its legend or at prose.
            if b.kind == "row" or MATRIX_LEGEND_RE.match(text):
                continue
            in_matrix = False
        if b.kind == "line" and pending:
            code, stage = pending
            pending = None
            if stage == "ltp" and LTP_ONLY_RE.match(text):
                pending = (code, "title")
                continue
            if stage == "title" and 2 <= len(text) <= 120 and not any(
                p.match(text) for p, _ in SECTION_NAMES
            ):
                out.append(f"\n### {code} {_title_case(text)}\n")
                in_course = True
                continue
            out.append(code)  # not a course header after all
        if b.kind == "line":
            m = SEMESTER_RE.match(text) or SEMESTER_ALT_RE.match(text)
            if m and semester_number(m.group(1)):
                out.append(f"\n## Semester {semester_number(m.group(1))}\n")
                in_course = False
                continue
            if GROUP_RE.match(text):
                out.append(f"\n## {_title_case(text.rstrip(' :'))}\n")
                in_course = False
                continue
            m = COURSE_LTP_RE.match(text) or CODE_ONLY_RE.match(text)
            if m:
                pending = (re.sub(r"\s+", "", m.group(1)), "title" if COURSE_LTP_RE.match(text) else "ltp")
                continue
            m = COURSE_RE.match(text)
            # A line starting "1. CS3591 ..." is a row of the curriculum
            # table, not the course's own header; VTU codes start with digits.
            if m and (not text[:1].isdigit() or re.match(r"^\d{2}[A-Z]", text)):
                code = re.sub(r"\s+", "", m.group(1))
                out.append(f"\n### {code} {_title_case(m.group(2).strip())}\n")
                in_course = True
                continue
            if in_course:
                if ARTICULATION_RE.match(text):
                    in_matrix = True
                    continue
                if LEARNING_RESOURCES_RE.match(text):
                    continue  # its Text Books / References follow as sections
                m = UNIT_RE.match(text)
                if m:
                    name = _title_case(m.group(2).strip(" :-–")) if m.group(2) else ""
                    out.append(f"\n#### Unit {m.group(1).upper()}{': ' + name if name else ''}\n")
                    continue
                matched = False
                for pattern, heading in SECTION_NAMES:
                    m = pattern.match(text)
                    if m:
                        out.append(f"\n#### {heading}\n")
                        if m.group(1).strip():
                            out.append(m.group(1).strip())
                        matched = True
                        break
                if matched:
                    continue
        out.append(_render(b))
    if pending:
        out.append(pending[0])
    return out


def _calendar_md(blocks: list[Block]) -> list[str]:
    out: list[str] = []
    last_page = None
    seen_heading_on_page = False
    for b in blocks:
        if b.page != last_page:
            last_page, seen_heading_on_page = b.page, False
        text = b.text.strip()
        if b.kind == "heading":
            out.append(f"\n{'#' * max(2, min(b.level, 4))} {text}\n")
            seen_heading_on_page = True
            continue
        m = MONTH_HEADING_RE.match(text) if b.kind == "line" else None
        if m:
            out.append(f"\n## {_title_case(m.group(1))}{' ' + m.group(2) if m.group(2) else ''}\n")
            seen_heading_on_page = True
            continue
        if b.kind == "line" and _is_caps_heading(text):
            out.append(f"\n## {_title_case(text)}\n")
            seen_heading_on_page = True
            continue
        if not seen_heading_on_page and text:
            out.append(f"\n## Page {b.page}\n")
            seen_heading_on_page = True
        out.append(_render(b))
    return out


NUMBERED_RE = re.compile(r"^\s*(\d{1,2}(?:\.\d{1,2}){0,2})\.?\s+([A-Z][^.]{2,80})$")


def _generic(blocks: list[Block]) -> list[str]:
    """Regulations and anything else: real headings where the file has them,
    ALL-CAPS lines and numbered clause titles where it does not, and a
    page heading as the last resort so every chunk still has a place to
    cite."""
    out: list[str] = []
    last_page = None
    has_heading_on_page = False
    for b in blocks:
        if b.page != last_page:
            last_page, has_heading_on_page = b.page, False
        text = b.text.strip()
        if b.kind == "heading":
            out.append(f"\n{'#' * max(2, min(b.level, 4))} {text}\n")
            has_heading_on_page = True
            continue
        if b.kind == "line" and _is_caps_heading(text):
            out.append(f"\n## {_title_case(text)}\n")
            has_heading_on_page = True
            continue
        m = NUMBERED_RE.match(text) if b.kind == "line" else None
        if m and len(text) <= 80:
            depth = m.group(1).count(".")
            out.append(f"\n{'#' * (2 + min(depth, 2))} {m.group(1)} {m.group(2).strip()}\n")
            has_heading_on_page = True
            continue
        if not has_heading_on_page and text:
            out.append(f"\n## Page {b.page}\n")
            has_heading_on_page = True
        out.append(_render(b))
    return out


# ---------------------------------------------------------------------------
# 3. The calendar: dated events, parsed in Python, flagged for a person
# ---------------------------------------------------------------------------
MONTHS = {m.lower(): i for i, m in enumerate(_calendar.month_name) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(_calendar.month_abbr) if m})
MONTHS["sept"] = 9
MONTH_ALT = "|".join(sorted(MONTHS, key=len, reverse=True))
WEEKDAYS = {name.lower(): i for i, name in enumerate(_calendar.day_name)}
WEEKDAYS.update({name.lower(): i for i, name in enumerate(_calendar.day_abbr)})
WEEKDAY_RE = re.compile(r"\b(" + "|".join(sorted(WEEKDAYS, key=len, reverse=True)) + r")\b", re.I)

# Order matters: ranges before single dates, so "21-23 Oct" is not read as
# the single date 21 Oct.
NUMERIC_RE = re.compile(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4}|\d{2})\b")
DAY_MONTH_RANGE_RE = re.compile(
    rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s*(?:-|–|to|&|and)\s*(\d{{1,2}})(?:st|nd|rd|th)?\s+({MONTH_ALT})\.?,?\s*(\d{{4}})?\b",
    re.I,
)
DAY_MONTH_RE = re.compile(
    rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s*(?:of\s+)?({MONTH_ALT})\.?,?(?:\s*(\d{{4}}))?\b", re.I
)
MONTH_DAY_RANGE_RE = re.compile(
    rf"\b({MONTH_ALT})\.?\s*(\d{{1,2}})(?:st|nd|rd|th)?\s*(?:-|–|to|&|and)\s*(\d{{1,2}})(?:st|nd|rd|th)?\b,?(?:\s*(\d{{4}}))?",
    re.I,
)
# \s* rather than \s+: "October15, 2026" is printed that way.
MONTH_DAY_RE = re.compile(
    rf"\b({MONTH_ALT})\.?\s*(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s*(\d{{4}}))?", re.I
)
RANGE_JOINER_RE = re.compile(r"^\s*(?:-|–|—|to|till|until|upto|up to|through)\s*$", re.I)

HOLIDAY_WORDS = (
    "holiday", "vacation", "pongal", "deepavali", "diwali", "christmas", "new year",
    "republic day", "independence day", "gandhi jayanti", "gandhi jayanthi", "ayudha",
    "vijaya dasami", "vijayadasami", "saraswathi pooja", "saraswati puja", "ramzan",
    "ramadan", "bakrid", "eid", "muharram", "good friday", "milad", "mahaveer",
    "mahavir", "tamil new year", "may day", "ambedkar", "onam", "holi", "dussehra",
    "dasara", "vinayagar", "ganesh chaturthi", "vinayaka", "krishna jayanthi",
    "janmashtami", "thiruvalluvar", "uzhavar", "thaipusam", "shivaratri", "sivarathri",
    "telugu new year", "ugadi", "labour day", "bhogi", "guru nanak",
)
EXAM_WORDS = ("exam", "examination", "test", "cat ", "cat-", "assessment", "model ",
              "end semester", "end-semester", "practical", "internal", "viva")
# Paperwork about an exam is not the exam.
NOT_AN_EXAM = ("grades", "results", "verification", "submission")
START_WORDS = ("commencement", "reopening", "re-opening", "classes begin", "class begins",
               "classes commence", "commence", "semester begins", "start of")
END_WORDS = ("last working day", "end of semester", "semester ends", "last instruction day",
             "closing day", "last day of class")
# A row that merely says when a form is due is not a holiday even though it
# mentions leave or vacation; these phrases veto the holiday reading.
NOT_A_HOLIDAY = ("application", "apply", "form", "submission", "fee", "working day",
                 "compensatory", "in lieu", "timetable", "for faculty", "for staff")


def classify_kind(title: str) -> str:
    t = f" {title.lower()} "
    if any(w in t for w in END_WORDS):
        return "semester_end"
    if any(w in t for w in START_WORDS):
        return "semester_start"
    if any(w in t for w in HOLIDAY_WORDS) and not any(w in t for w in NOT_A_HOLIDAY):
        return "holiday"
    if any(w in t for w in EXAM_WORDS) and not any(w in t for w in NOT_AN_EXAM):
        return "exam"
    return "other"


def academic_years(academic_year: str | None) -> tuple[int, int] | None:
    """'2026-27' -> (2026, 2027). None if it does not look like one."""
    m = re.match(r"^\s*(\d{4})\s*[-–/]\s*(\d{2}|\d{4})\s*$", academic_year or "")
    if not m:
        return None
    first = int(m.group(1))
    second = int(m.group(2)) if len(m.group(2)) == 4 else (first // 100) * 100 + int(m.group(2))
    return (first, second) if second == first + 1 else None


def _year_for(month: int, years: tuple[int, int] | None, fallback: int | None) -> int | None:
    if years:
        return years[0] if month >= 6 else years[1]  # Jun-Dec: first year; Jan-May: second
    return fallback


@dataclass
class _Hit:
    start: date
    end: date
    span: tuple[int, int]  # character span in the text
    inferred_year: bool = False
    swapped: bool = False


def _safe_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def find_dates(text: str, years: tuple[int, int] | None, fallback_year: int | None) -> tuple[list[_Hit], bool]:
    """Every date or date range in one row of text.

    Returns (hits, saw_bad_date). A numeric date whose second part is above
    12 cannot be day-first, so it is read month-first and marked swapped —
    that is the one case where the reading is changed, and the reviewer is
    told.
    """
    hits: list[_Hit] = []
    taken: list[tuple[int, int]] = []
    bad = False

    def free(a: int, b: int) -> bool:
        return all(b <= x or a >= y for x, y in taken)

    for pattern, month_group, first, last in ((DAY_MONTH_RANGE_RE, 3, 1, 2),
                                               (MONTH_DAY_RANGE_RE, 1, 2, 3)):
        for m in pattern.finditer(text):
            if not free(*m.span()):
                continue
            month = MONTHS[m.group(month_group).lower().rstrip(".")]
            year = int(m.group(4)) if m.group(4) else _year_for(month, years, fallback_year)
            if year is None:
                bad = True
                continue
            a, b = _safe_date(year, month, int(m.group(first))), _safe_date(year, month, int(m.group(last)))
            if a and b and a <= b:
                hits.append(_Hit(a, b, m.span(), inferred_year=not m.group(4)))
                taken.append(m.span())
            else:
                bad = True

    for m in NUMERIC_RE.finditer(text):
        if not free(*m.span()):
            continue
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        y = y + 2000 if y < 100 else y
        swapped = False
        if mo > 12 and d <= 12:
            d, mo, swapped = mo, d, True
        parsed = _safe_date(y, mo, d)
        if parsed is None:
            bad = True
            continue
        hits.append(_Hit(parsed, parsed, m.span(), swapped=swapped))
        taken.append(m.span())

    for pattern, day_group, month_group, year_group in (
        (DAY_MONTH_RE, 1, 2, 3),
        (MONTH_DAY_RE, 2, 1, 3),
    ):
        for m in pattern.finditer(text):
            if not free(*m.span()):
                continue
            month = MONTHS[m.group(month_group).lower().rstrip(".")]
            year = int(m.group(year_group)) if m.group(year_group) else _year_for(month, years, fallback_year)
            if year is None:
                bad = True
                continue
            parsed = _safe_date(year, month, int(m.group(day_group)))
            if parsed is None:
                bad = True
                continue
            hits.append(_Hit(parsed, parsed, m.span(), inferred_year=not m.group(year_group)))
            taken.append(m.span())

    hits.sort(key=lambda h: h.span[0])
    return hits, bad


def _remove_spans(text: str, spans: list[tuple[int, int]]) -> str:
    for start, end in sorted(spans, reverse=True):
        text = text[:start] + " " + text[end:]
    return text


_WD = WEEKDAY_RE.pattern[2:-2]  # the alternation, without its \b( )\b
_META_CELL_RE = re.compile(
    rf"^[\s\W\d]*(?:(?:{_WD})\b[\s\W]*|(?:to|till|until|upto|from|and)\b[\s\W]*)*$", re.I
)


def _tidy(text: str) -> str:
    text = re.sub(rf"\(\s*(?:{_WD})(?:\s*(?:-|–|to|&)\s*(?:{_WD}))?\s*\)", " ", text, flags=re.I)  # "(Monday)" after a date
    text = re.sub(r"\(\s*\)", " ", re.sub(r"\s+", " ", text))
    # A preposition left dangling where a date was: "CAT I from", "holiday on".
    text = re.sub(r"\s+(?:from|on|is|at|by|dated|of|w\.e\.f\.?)\s*$", "", text.strip(" -–—:,;.&|("), flags=re.I)
    return text.strip(" -–—:,;.&|(")


def _title_for(b: Block, text: str, removed: list[tuple[int, int]],
               years: tuple[int, int] | None, fallback: int | None) -> str:
    """What the event is called, without its dates.

    A table row is read cell by cell: a cell that is only a serial number,
    a weekday or a date is bookkeeping, and every other cell is part of the
    name. Words are never stripped from inside a cell, so "Saturday working
    (Monday order)" keeps its weekdays, because there they ARE the event.
    """
    if b.kind in ("row", "day") and b.cells:
        kept = []
        for cell in b.cells:
            hits, _ = find_dates(cell, years, fallback)
            rest = _remove_spans(cell, [h.span for h in hits])
            if rest.strip() and not _META_CELL_RE.match(rest):
                kept.append(_tidy(rest))
        return " — ".join(k for k in kept if k)
    rest = _remove_spans(text, removed)
    rest = re.sub(rf"\(\s*(?:{_WD})(?:\s*(?:-|–|to|&)\s*(?:{_WD}))?\s*\)", " ", rest, flags=re.I)
    rest = re.sub(r"^\s*\d{1,3}\s*[.)]\s+", " ", rest)  # a leading "3." serial number
    rest = re.sub(rf"^[\s\W]*(?:{_WD})\b", " ", rest, flags=re.I)
    rest = re.sub(rf"\b(?:{_WD})[\s\W]*$", " ", rest, flags=re.I)
    return _tidy(rest)


def parse_calendar(extracted: Extracted, academic_year: str | None) -> list[dict]:
    """Draft events from a calendar, for a person to review.

    Every event comes back unconfirmed. The reviewer sees each one beside
    the row it came from and any flags; nothing here reaches the
    working-day arithmetic until they confirm it.
    """
    years = academic_years(academic_year)
    # Without an academic year, a missing year is borrowed from the most
    # common year the document prints in full — and flagged.
    full_years = [int(y) for b in extracted.blocks for y in re.findall(r"\b(20\d{2})\b", b.text)]
    fallback = max(set(full_years), key=full_years.count) if full_years else None

    events: list[dict] = []
    seen: set[tuple] = set()
    for b in extracted.blocks:
        if b.kind == "heading":
            continue
        text = b.text.strip()
        if not text:
            continue
        hits, bad = find_dates(text, years, fallback)
        if not hits:
            # A table row that names a holiday but gives no readable date
            # ("Pongal | to be announced") is kept for the reviewer; a loose
            # line needs some sign it meant to carry a date, or every
            # sentence about vacations would become an event.
            if classify_kind(text) == "holiday" and (b.kind == "row" or bad or re.search(r"\d", text)):
                events.append(_event(None, None, _title_for(b, text, [], years, fallback) or text,
                                     text, b.page, ["unparsed"], include=False))
            continue

        # Two dates joined by "to"/"-" (or in adjacent table columns) are one
        # range; anything else is one event per date.
        groups: list[list[_Hit]] = []
        i = 0
        while i < len(hits):
            h = hits[i]
            if i + 1 < len(hits) and h.start == h.end:
                nxt = hits[i + 1]
                between = text[h.span[1]: nxt.span[0]]
                joined = RANGE_JOINER_RE.match(between) or (b.kind == "row" and between.strip() == "|")
                if joined and h.inferred_year and not nxt.inferred_year:
                    # "August 31 - September 1, 2026": the year printed at the
                    # end belongs to the start too.
                    year = nxt.start.year - (h.start.month > nxt.start.month)
                    if (moved := _safe_date(year, h.start.month, h.start.day)):
                        h = _Hit(moved, moved, h.span)
                if joined and nxt.start >= h.end and (nxt.start - h.start).days <= 62:
                    groups.append([h, nxt])
                    i += 2
                    continue
            groups.append([h])
            i += 1

        # Remove each group's whole span, so the "to" between the two ends
        # of a range goes with them (and a "to" anywhere else stays).
        removed = [(g[0].span[0], g[-1].span[1]) for g in groups]
        title = _title_for(b, text, removed, years, fallback) or "(untitled)"
        weekdays = [WEEKDAYS[w.lower()] for w in WEEKDAY_RE.findall(text)]
        for g in groups:
            start, end = g[0].start, g[-1].end
            flags = []
            if any(h.inferred_year for h in g):
                flags.append("inferred_year")
            if any(h.swapped for h in g):
                flags.append("month_first")
            if len(groups) > 1:
                flags.append("several_dates_in_row")
            if title == "(untitled)":
                flags.append("no_title")
            # A weekday printed in the row is a free cross-check on the date.
            if weekdays and len(groups) == 1:
                expected = [start.weekday()] + ([end.weekday()] if end != start else [])
                if weekdays[: len(expected)] != expected and weekdays[0] != start.weekday():
                    flags.append("weekday_mismatch")
                elif "inferred_year" in flags:
                    flags.remove("inferred_year")  # the right weekday vouches for the year
            key = (start, end, title.lower())
            if key in seen:
                continue
            seen.add(key)
            events.append(_event(start, end, title, text, b.page, flags) | {"_grid": b.kind == "day"})
    events = _fold_grid(events)
    events.sort(key=lambda e: (e["start_date"] or "9999", e["title"]))
    return events


def _fold_grid(events: list[dict]) -> list[dict]:
    """A month grid repeats, square by square, what a calendar usually also
    lists: "Mid-Sem. Exams" on seven days beside "Mid-Semester Examinations,
    September 3-11". Runs of the same label on consecutive days (weekends
    between them allowed) become one event, and a run that a listed event of
    the same kind already covers is dropped. What is left is what only the
    grid says — usually the holidays.
    """
    listed = [e for e in events if not e.get("_grid")]
    runs: list[dict] = []
    for e in sorted((e for e in events if e.get("_grid")), key=lambda e: (e["title"].lower(), e["start_date"])):
        prev = runs[-1] if runs else None
        if prev and prev["title"].lower() == e["title"].lower():
            last, nxt = date.fromisoformat(prev["end_date"]), date.fromisoformat(e["start_date"])
            gap = [last + timedelta(days=k) for k in range(1, (nxt - last).days)]
            if nxt > last and all(d.weekday() >= 5 for d in gap):
                prev["end_date"] = e["end_date"]
                prev["source_text"] = f"{prev['source_text']} / {e['source_text']}"[:500]
                continue
        runs.append(e)

    def covered(g: dict) -> bool:
        return any(o["kind"] == g["kind"] and o["start_date"] and o["start_date"] <= g["start_date"]
                   and g["end_date"] <= o["end_date"] for o in listed)

    kept = [g for g in runs if not covered(g)]
    return [{k: v for k, v in e.items() if k != "_grid"} for e in listed + kept]


def _event(start: date | None, end: date | None, title: str, source: str, page: int,
           flags: list[str], include: bool | None = None) -> dict:
    kind = classify_kind(title)
    return {
        "start_date": start.isoformat() if start else None,
        "end_date": end.isoformat() if end else None,
        "title": title[:200],
        "kind": kind,
        "years": "all",
        "source_text": source[:500],
        "page": page,
        "flags": flags,
        # Undated rows and plain "other" rows start unticked: they cannot
        # be placed on a calendar, or are rarely what a student asks about.
        "include": (start is not None and kind != "other") if include is None else include,
        "confirmed": False,
    }


# ---------------------------------------------------------------------------
# Branches — one spelling, wherever a branch comes from
# ---------------------------------------------------------------------------
# An upload form, a user profile and a sentence ("ECE's syllabus", "for
# computer science") must all land on the same code, or the filter silently
# matches nothing. Unknown names fall through as their own upper-cased
# alphanumerics, so a college's own codes still work.
BRANCH_ALIASES = {
    "CSE": ("cse", "cs", "computer science", "computer science and engineering", "comp sci"),
    "IT": ("it", "information technology"),
    "ECE": ("ece", "ec", "electronics", "electronics and communication",
            "electronics and communication engineering"),
    "EEE": ("eee", "ee", "electrical", "electrical and electronics",
            "electrical and electronics engineering"),
    "MECH": ("mech", "me", "mechanical", "mechanical engineering"),
    "CIVIL": ("civil", "ce", "civil engineering"),
    "AIDS": ("aids", "ai&ds", "ai and ds", "ai & ds", "artificial intelligence and data science"),
    "AIML": ("aiml", "ai&ml", "ai and ml", "artificial intelligence and machine learning"),
    "CSBS": ("csbs", "computer science and business systems"),
}
_ALIAS_TO_CODE = {a: code for code, names in BRANCH_ALIASES.items() for a in names}


def normalize_branch(value: str | None) -> str | None:
    """'Electronics and Communication' -> 'ECE'; 'all'/'' -> 'ALL'; None -> None."""
    if value is None:
        return None
    raw = re.sub(r"\s+", " ", value.strip().lower().replace("'s", ""))
    if raw in ("", "all", "any", "every", "all branches", "common"):
        return "ALL"
    if raw in _ALIAS_TO_CODE:
        return _ALIAS_TO_CODE[raw]
    code = re.sub(r"[^A-Z0-9]", "", raw.upper())
    return code[:16] or None
