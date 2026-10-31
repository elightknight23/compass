"""Uploaded documents, end to end and offline.

    extraction      PDF (text and tables), DOCX, Markdown, text; a scan is
                    refused with a reason, a disguised file is refused
    structure       the syllabus normaliser: semester > course > section, so
                    the chunk with the text books carries its course
    the calendar    every supported date shape; ranges; flags for what was
                    guessed; holiday vs exam vs other
    the API         who may upload, delete and confirm, over real HTTP —
                    and that a refused upload stored nothing
    the index       uploads carry branch/semester metadata, the filter
                    never crosses branches, a delete takes the chunks with
                    it, a full rebuild keeps uploads, the hash notices
    holidays        only CONFIRMED holidays change working days; per-year
                    holidays count for that year only; deleting the
                    calendar falls back to the placeholder file

Embeddings are local (ONNX MiniLM), so the index parts run offline once the
weights are cached. Fixtures are synthetic: scripts/make_fixtures.py.

    python scripts/test_documents.py
"""

from __future__ import annotations

import io
import json
import shutil
import tempfile
from datetime import date
from pathlib import Path

from _harness import check, finish, sandbox, section

import chromadb
from fastapi.testclient import TestClient

from app import config, dates, documents as D, library, rag
from app.main import app

FIX = Path(__file__).resolve().parent / "fixtures"
SYLLABUS_PDF = (FIX / "syllabus_cse.pdf").read_bytes()
CALENDAR_PDF = (FIX / "calendar_2026_27.pdf").read_bytes()
SCANNED_PDF = (FIX / "scanned.pdf").read_bytes()
CURRICULUM_PDF = (FIX / "curriculum_nit.pdf").read_bytes()


def blocks(rows: list[str], kind: str = "row") -> D.Extracted:
    return D.Extracted([D.Block(1, kind, r, cells=r.split(" | ") if kind == "row" else [])
                        for r in rows], 1)


def one(text: str, year: str | None = "2026-27", kind: str = "line") -> dict | None:
    got = D.parse_calendar(blocks([text], kind), year)
    return got[0] if got else None


def docx_bytes() -> bytes:
    import docx

    d = docx.Document()
    d.add_heading("Hostel Rules 2026", level=0)
    d.add_heading("Gate Timings", level=1)
    d.add_paragraph("The hostel gate closes at 9:00 PM on all days.")
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text, t.cell(0, 1).text = "Block", "Warden"
    t.cell(1, 0).text, t.cell(1, 1).text = "B", "Meena Krishnan"
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def main() -> int:
    # ------------------------------------------------------------------
    section("extraction")
    for name, data, expect in [
        ("a.pdf", SYLLABUS_PDF, ".pdf"), ("a.docx", docx_bytes(), ".docx"),
        ("a.md", b"# T\n\nx", ".md"), ("a.txt", b"plain", ".txt"),
    ]:
        check(f"sniff accepts a real {expect}", D.sniff(name, data) == expect)
    for name, data, kind, label in [
        ("a.exe", b"MZ..", "unsupported_type", "an unsupported extension"),
        ("a.pdf", b"PK\x03\x04 really a zip", "unsupported_type", "a .pdf that is not a PDF"),
        ("a.docx", b"%PDF-1.7", "unsupported_type", "a .docx that is not a Word file"),
        ("a.txt", b"\x00\x01binary", "unsupported_type", "a .txt full of binary"),
        ("a.pdf", b"", "empty", "an empty file"),
    ]:
        try:
            D.sniff(name, data)
            check(f"sniff refuses {label}", False)
        except D.DocumentError as e:
            check(f"sniff refuses {label} ({kind})", e.kind == kind, e.kind)

    try:
        D.extract(SCANNED_PDF, ".pdf")
        check("a scanned PDF is refused, not indexed as nothing", False)
    except D.DocumentError as e:
        check("a scanned PDF is refused, not indexed as nothing", e.kind == "scanned")
        check("  and the reason says what to upload instead", "OCR" in str(e) and "original" in str(e))
    try:
        D.extract(SYLLABUS_PDF, ".pdf", max_pages=0)
        check("a PDF over the page limit is refused", False)
    except D.DocumentError as e:
        check("a PDF over the page limit is refused", e.kind == "too_large")

    cal = D.extract(CALENDAR_PDF, ".pdf")
    check("a PDF table comes out as rows, cell by cell",
          any(b.kind == "row" and b.cells[:2] == ["5", "09.11.2026"] for b in cal.blocks),
          str([b.text for b in cal.blocks][:4]))
    dx = D.extract(docx_bytes(), ".docx")
    check("DOCX heading styles become headings",
          [(b.kind, b.text) for b in dx.blocks if b.kind == "heading"]
          == [("heading", "Hostel Rules 2026"), ("heading", "Gate Timings")])
    check("DOCX tables become rows", any(b.kind == "row" and "Meena Krishnan" in b.text for b in dx.blocks))
    md = D.extract(b"| Date | Event |\n|---|---|\n| 09.11.2026 | Deepavali |\n", ".md")
    check("a markdown table is read as rows, its |---| rule dropped",
          [b.kind for b in md.blocks] == ["row", "row"] and md.blocks[1].cells == ["09.11.2026", "Deepavali"])

    # ------------------------------------------------------------------
    section("syllabus structure")
    syl_md = D.to_markdown(D.extract(SYLLABUS_PDF, ".pdf"), "syllabus", "CSE Syllabus R2021")
    chunks = rag.structured_chunks(syl_md, "CSE Syllabus R2021")
    paths = [c.metadata["header_path"] for c in chunks]
    books = next((c for c in chunks if c.metadata["header_path"].endswith("CS3591 Computer Networks > Text Books")), None)
    check("the Text Books chunk is headed with its semester and course",
          books is not None and "Semester 5" in books.metadata["header_path"], str(paths))
    check("  and the books themselves are in it", books is not None and "Kurose" in books.text)
    check("  and the course name is in the embedded text, not just metadata",
          books is not None and books.text.startswith("[CSE Syllabus R2021 > Semester 5 > CS3591 Computer Networks"))
    check("units become their own sections",
          any(p.endswith("CS3591 Computer Networks > Unit II: Transport Layer") for p in paths))
    check("a course in the next semester is filed under it",
          any(p.endswith("Semester 6 > CCS356 Object Oriented Software Engineering > Text Books") for p in paths))
    check("'TEXT BOOK:' (singular) is the same section",
          any(p.endswith("CS3501 Compiler Design > Text Books") for p in paths))
    table_row = D.to_markdown(blocks(["1. CS3591 COMPUTER NETWORKS PCC 4 3 0 2 4"], "line"), "syllabus", "T")
    check("a curriculum-table line ('1. CS3591 ...') is not mistaken for a course header",
          "### CS3591" not in table_row)
    vtu = D.to_markdown(blocks(["21CS51 AUTOMATA THEORY AND COMPUTABILITY"], "line"), "syllabus", "T")
    check("VTU-style codes (21CS51) are course headers too", "### 21CS51 Automata Theory and Computability" in vtu)

    section("syllabus structure: the NIT layout (one course per page)")
    nit_md = D.to_markdown(D.extract(CURRICULUM_PDF, ".pdf"), "syllabus", "CSE Curriculum")
    doc = {"id": "DOC-1", "doc_type": "syllabus", "branch": "CSE", "semester": None, "title": "CSE Curriculum"}
    nit = {c.metadata["header_path"]: c for c in rag.document_chunks(doc, nit_md)}
    cn = nit.get("CSE Curriculum > Semester 5 > CS1305 Computer Networks > Text Books")
    check("'CS1305 3-0-0 (3)' + the title on the next line is a course; its Text Books are a section",
          cn is not None and "Peterson" in cn.text and "Tanenbaum" in cn.text, str(list(nit)))
    check("  'Learning Resources:' is not a section of its own (its books are)",
          not any(p.endswith("Learning Resources") for p in nit))
    check("  'Reference Books:' is the References section",
          "CSE Curriculum > Semester 5 > CS1305 Computer Networks > References" in nit)
    check("  and the chunk is semester 5", cn is not None and cn.metadata["semester"] == 5)
    check("code and L-T-P on separate lines ('CS1309' / '0-1-2 (2)') is a course too",
          "CSE Curriculum > Semester 5 > CS1309 Computer Networks Laboratory > Text Books" in nit)
    ai = nit.get("CSE Curriculum > Professional Electives > CS1325 Artificial Intelligence > Text Books")
    check("an elective is filed under its group, not under the last semester",
          ai is not None and "Norvig" in ai.text, str(list(nit)))
    check("  and is semester 0, which every semester's filter matches", ai is not None and ai.metadata["semester"] == 0)
    check("the CO-PO articulation matrix is dropped", "PSO-1" not in nit_md and "Slightly" not in nit_md)
    check("the running header and the page footers are dropped",
          "Department of Computer Science" not in nit_md and "P a ge" not in nit_md)
    check("  but section words that appear on every course page are kept",
          nit_md.count("#### Syllabus") == 3, str(nit_md.count("#### Syllabus")))
    check("the curriculum table stays as rows under its semester",
          "1 | CS1305 | Computer Networks | 3-0-0 | 3" in nit_md)
    minors = D.to_markdown(blocks(["CS1M01 2-0-2 (3)", "Data Structures", "Minor: Computer Science and Engineering",
                                   "CS1H01 3-0-0 (3)", "Advanced Compiler Design", "CS1495 0-0-0 (2)", "Minor Project"],
                                  "line"), "syllabus", "T")
    check("minor / honours codes (CS1M01, CS1H01) are course headers",
          "### CS1M01 Data Structures" in minors and "### CS1H01 Advanced Compiler Design" in minors, minors)
    check("  'Minor: ...' is a group, but the course 'Minor Project' is not",
          "## Minor: Computer Science and Engineering" in minors and "### CS1495 Minor Project" in minors)

    generic = D.to_markdown(blocks(["Some text with no headings at all."], "line"), "other", "Misc")
    check("a document with no headings still gets one to cite (its page)", "## Page 1" in generic)

    # ------------------------------------------------------------------
    section("the calendar parser")
    cases = [
        ("15.08.2026 Independence Day", ("2026-08-15", "2026-08-15")),
        ("15-08-2026 Independence Day", ("2026-08-15", "2026-08-15")),
        ("15/08/2026 Independence Day", ("2026-08-15", "2026-08-15")),
        ("15/08/26 Independence Day", ("2026-08-15", "2026-08-15")),
        ("15 Aug 2026 Independence Day", ("2026-08-15", "2026-08-15")),
        ("15th August 2026 Independence Day", ("2026-08-15", "2026-08-15")),
        ("August 15, 2026 Independence Day", ("2026-08-15", "2026-08-15")),
        ("21.10.2026 to 23.10.2026 Ayudha Pooja holidays", ("2026-10-21", "2026-10-23")),
        ("21.10.2026 - 23.10.2026 Ayudha Pooja holidays", ("2026-10-21", "2026-10-23")),
        ("21-23 Oct 2026 Ayudha Pooja holidays", ("2026-10-21", "2026-10-23")),
    ]
    for text, want in cases:
        e = one(text)
        got = (e["start_date"], e["end_date"]) if e else None
        check(f"reads {text.split(' ')[0]!r}-style dates", got == want, f"{text!r} -> {got}")

    e = one("Pongal holidays 14 January")
    check("a missing year comes from the academic year (Jan -> the second year)",
          e["start_date"] == "2027-01-14" and "inferred_year" in e["flags"])
    e = one("Reopening on 3rd August")
    check("  (Aug -> the first year), and is flagged for the reviewer",
          e["start_date"] == "2026-08-03" and "inferred_year" in e["flags"])
    e = one("12/25/2026 Christmas")
    check("an impossible day-first date is read month-first and flagged",
          e["start_date"] == "2026-12-25" and "month_first" in e["flags"])
    e = one("18.11.2026 Thursday Founders' Day holiday")
    check("a printed weekday that contradicts the date is flagged (18 Nov 2026 is a Wednesday)",
          "weekday_mismatch" in e["flags"])
    e = one("18.11.2026 Wednesday Founders' Day holiday")
    check("  and a matching one is not", "weekday_mismatch" not in e["flags"])
    check("31.02.2026 is not a date, and no event is invented", one("31.02.2026 Something") is None)
    e = one("Pongal | | Holidays (dates to be announced)", kind="row")
    check("a holiday row with no readable date is kept, unticked and flagged",
          e and e["start_date"] is None and "unparsed" in e["flags"] and e["include"] is False)

    kinds = {
        "Deepavali": "holiday", "Christmas holiday": "holiday", "Winter vacation": "holiday",
        "CAT I": "exam", "Model examinations": "exam", "End Semester Examinations": "exam",
        "Commencement of classes": "semester_start", "Last working day": "semester_end",
        "Last date for vacation leave application": "other",
        "Saturday working (Monday order)": "other",
    }
    for title, kind in kinds.items():
        check(f"kind: {title!r} -> {kind}", D.classify_kind(title) == kind, D.classify_kind(title))
    e = one("05.12.2026 | Saturday | Saturday working (Monday order)", kind="row")
    check("weekday words inside an event's own name survive (cells are read, not stripped)",
          e["title"] == "Saturday working (Monday order)", e["title"])
    e = one("CAT I from 28.09.2026 to 03.10.2026")
    check("the 'to' of a range goes with the dates; a dangling 'from' is trimmed",
          (e["title"], e["end_date"]) == ("CAT I", "2026-10-03"), str(e))
    check("other / undated rows start unticked; holidays and exams start ticked",
          one("05.12.2026 Fee payment last date")["include"] is False
          and one("09.11.2026 Deepavali")["include"] is True)
    check("academic_years parses '2026-27' and refuses nonsense",
          D.academic_years("2026-27") == (2026, 2027) and D.academic_years("2026-29") is None
          and D.academic_years("soon") is None)

    section("branch names")
    for raw, want in [("ECE", "ECE"), ("electronics and communication", "ECE"), ("CSE's", "CSE"),
                      ("Computer Science and Engineering", "CSE"), ("", "ALL"), ("all", "ALL"),
                      ("Bio-Tech", "BIOTECH"), (None, None)]:
        check(f"normalize_branch({raw!r}) -> {want!r}", D.normalize_branch(raw) == want)

    # ------------------------------------------------------------------
    sandbox("json")
    tmp = Path(tempfile.mkdtemp(prefix="compass-docs-"))
    store = chromadb.PersistentClient(path=str(tmp / "chroma"))
    saved_chroma = rag.chroma
    rag.chroma = lambda: store
    c = TestClient(app)

    def login(username: str, password: str) -> None:
        c.cookies.clear()
        assert c.post("/api/login", json={"username": username, "password": password}).status_code == 200

    def upload(name: str, data: bytes, **form) -> object:
        return c.post("/api/documents", data=form, files={"file": (name, data, "application/octet-stream")})

    def documents_on_file() -> list[dict]:
        p = config.DATA_DIR  # noqa: F841 — the sandbox store, via the backend:
        return library.backends.get_backend().list_documents()["data"]

    try:
        section("the API: who may upload, delete and confirm")
        c.cookies.clear()
        check("signed out: 401", upload("s.pdf", SYLLABUS_PDF, doc_type="syllabus").status_code == 401)
        for who, pw in [("ravi.kumar", "ravi-demo"), ("meena.krishnan", "warden-demo")]:
            login(who, pw)
            r = upload("s.pdf", SYLLABUS_PDF, doc_type="syllabus", branch="CSE")
            check(f"{who}: upload refused with 403", r.status_code == 403 and r.json()["outcome"] == "not_authorized")
        check("  and nothing was stored — not a registry row, not a file",
              documents_on_file() == [] and not any(config.UPLOADS_DIR.glob("*")) if config.UPLOADS_DIR.exists() else documents_on_file() == [])

        login("lakshmi.narayanan", "advisor-demo")
        r = upload("s.pdf", SYLLABUS_PDF, doc_type="syllabus", branch="Computer Science", title="CSE Syllabus R2021")
        check("the advisor's upload is accepted: 202", r.status_code == 202, r.text[:200])
        doc = c.get("/api/documents/DOC-3001").json()["data"]
        check("  processed in the background to 'indexed'",
              doc["status"] == "indexed" and doc["chunks"] > 3, str(doc)[:200])
        check("  with the branch normalised ('Computer Science' -> CSE)", doc["branch"] == "CSE")
        check("  and the original kept for citations",
              c.get("/api/documents/DOC-3001/file").content == SYLLABUS_PDF)
        r = upload("copy.pdf", SYLLABUS_PDF, doc_type="syllabus", branch="CSE")
        check("the same file twice is 409 duplicate, naming the first",
              r.status_code == 409 and r.json()["document"]["id"] == "DOC-3001")
        check("a scan is accepted, then fails with the reason on the document",
              upload("scan.pdf", SCANNED_PDF, doc_type="other").status_code == 202
              and c.get("/api/documents/DOC-3002").json()["data"]["status"] == "failed"
              and "OCR" in c.get("/api/documents/DOC-3002").json()["data"]["error"])
        check("an unsupported file is 415", upload("x.exe", b"MZ", doc_type="other").status_code == 415)
        check("an unknown document type is 400", upload("a.txt", b"x", doc_type="poster").status_code == 400)
        check("a semester out of range is 400",
              upload("a.txt", b"x", doc_type="syllabus", semester="12").status_code == 400)
        r = upload("c.pdf", CALENDAR_PDF, doc_type="academic_calendar")
        check("a calendar without its academic year is 400, with the reason",
              r.status_code == 400 and "academic year" in r.json()["detail"])
        saved_limit = config.MAX_UPLOAD_MB
        config.MAX_UPLOAD_MB = 0
        check("over the size limit is 413", upload("b.txt", b"xx", doc_type="other").status_code == 413)
        config.MAX_UPLOAD_MB = saved_limit

        ece = (D.to_markdown(D.extract(SYLLABUS_PDF, ".pdf"), "syllabus", "x")
               .replace("CS3591 Computer Networks", "EC3501 Wireless Communication")
               .replace("Kurose", "Rappaport"))
        r = upload("ece.md", ece.encode(), doc_type="syllabus", branch="ECE", title="ECE Syllabus R2021")
        check("an ECE syllabus (markdown) is indexed too",
              c.get(f"/api/documents/{r.json()['data']['id']}").json()["data"]["status"] == "indexed")

        login("priya.sharma", "priya-demo")
        listed = [d["id"] for d in c.get("/api/documents").json()["documents"]]
        check("a student lists only searchable documents (not the failed scan)",
              "DOC-3002" not in listed and "DOC-3001" in listed, str(listed))
        check("  and a failed one is 404 to them, not 'forbidden'",
              c.get("/api/documents/DOC-3002").status_code == 404)
        check("a student cannot delete: 403", c.delete("/api/documents/DOC-3001").status_code == 403)
        check("a student cannot rebuild the index: 403", c.post("/api/admin/reindex").status_code == 403)

        # --------------------------------------------------------------
        section("the index")
        check("the index is fresh after uploads", rag.index_status()["state"] == "fresh")
        cse = rag.retrieve("recommended text book for computer networks", k=4,
                           where=rag.build_filter({"role": "student", "branch": "CSE", "semester": 5}, "syllabus")[0])
        check("a CSE student's syllabus search returns only CSE (or ALL) chunks",
              cse and all(h["metadata"]["branch"] in ("CSE", "ALL") for h in cse))
        check("  and finds the Computer Networks text books",
              any("CS3591 Computer Networks > Text Books" in h["metadata"]["header_path"] for h in cse))
        ec = rag.retrieve("recommended text book", k=8,
                          where=rag.build_filter({"role": "student", "branch": "ECE"}, "syllabus")[0])
        check("an ECE student's never returns a CSE chunk",
              ec and all(h["metadata"]["branch"] in ("ECE", "ALL") for h in ec))
        sem6 = rag.retrieve("text books", k=8, where=rag.build_filter(
            {"role": "student", "branch": "CSE", "semester": 6}, "syllabus", my_courses=True)[0])
        check("'my subjects' narrows to the student's semester (chunk-level, from the heading)",
              sem6 and all(h["metadata"]["semester"] in (6, 0) for h in sem6), str([h["metadata"]["semester"] for h in sem6]))
        none = rag.retrieve("text books", where=rag.build_filter({"role": "student", "branch": "MECH"}, "syllabus")[0])
        check("a branch with no syllabus finds nothing — absence, not another branch's books", none == [])
        regs = rag.retrieve("minimum attendance", where=rag.build_filter(None, "regulations")[0])
        check("the built-in regulations carry metadata too",
              regs and all(h["metadata"]["doc_type"] == "regulation" and h["metadata"]["doc_id"] == "corpus" for h in regs))

        login("lakshmi.narayanan", "advisor-demo")
        before = store.get_collection(rag.STRUCT_COLLECTION).count()
        r = c.post("/api/admin/reindex").json()
        check("a full rebuild keeps every indexed upload",
              r["uploads"] == 2 and store.get_collection(rag.STRUCT_COLLECTION).count() == before
              and rag.index_status()["state"] == "fresh", str(r)[:160])
        rag.upload_text_path("DOC-3001").write_text(rag.upload_text_path("DOC-3001").read_text() + "\nedited\n")
        check("changing an upload's indexed text makes the index stale", rag.index_status()["state"] == "stale")
        c.post("/api/admin/reindex")
        r = c.delete("/api/documents/DOC-3001")
        check("the advisor deletes a document", r.status_code == 200)
        check("  its chunks leave the index",
              store.get_collection(rag.STRUCT_COLLECTION).get(where={"doc_id": "DOC-3001"})["ids"] == [])
        check("  its files leave the disk",
              not (config.UPLOADS_DIR / "DOC-3001.pdf").exists() and not rag.upload_text_path("DOC-3001").exists())
        check("  and the index is still fresh", rag.index_status()["state"] == "fresh")
        check("deleting it again is 404", c.delete("/api/documents/DOC-3001").status_code == 404)

        # --------------------------------------------------------------
        section("the calendar: parsed, reviewed, then in force")
        mon, fri = date(2026, 11, 16), date(2026, 11, 20)
        check("before any calendar, the placeholder file is used",
              dates.holiday_source() == "seed" and dates.working_days(mon, fri, dates.college_holidays(3)) == 4)
        r = upload("cal.pdf", CALENDAR_PDF, doc_type="academic_calendar", academic_year="2026-27",
                   title="Academic Calendar 2026-27")
        cal_id = r.json()["data"]["id"]
        check("a calendar lands as needs_review",
              c.get(f"/api/documents/{cal_id}").json()["data"]["status"] == "needs_review")
        check("  and nothing in it is in force yet", dates.holiday_source() == "seed")
        draft = c.get(f"/api/documents/{cal_id}/events").json()["data"]["events"]
        check("the draft holds every dated row, unconfirmed",
              len(draft) >= 9 and not any(e["confirmed"] for e in draft))
        login("meena.krishnan", "warden-demo")
        check("the warden cannot review the calendar: 403",
              c.get(f"/api/documents/{cal_id}/events").status_code == 403
              and c.put(f"/api/documents/{cal_id}/events", json={"events": draft}).status_code == 403)
        login("lakshmi.narayanan", "advisor-demo")
        check("undated rows are listed last, for every backend", draft[-1]["start_date"] is None)
        bad = [dict(e) for e in draft]
        bad[0]["end_date"] = "2026-01-01"
        r = c.put(f"/api/documents/{cal_id}/events", json={"events": bad})
        check("a row that ends before it starts is refused, naming the row",
              r.status_code == 400 and "Row 1" in r.json()["detail"], r.text[:160])
        bad = [dict(e) for e in draft]
        bad[-1]["end_date"] = "2027-01-15"
        check("an end date with no start date is refused",
              c.put(f"/api/documents/{cal_id}/events", json={"events": bad}).status_code == 400)
        bad = [dict(e) for e in draft]
        undated = next(i for i, e in enumerate(bad) if e["start_date"] is None)
        bad[undated]["include"] = True
        check("ticking a row with no date is refused",
              c.put(f"/api/documents/{cal_id}/events", json={"events": bad}).status_code == 400)
        check("a syllabus has no calendar to review: 400",
              c.get("/api/documents/DOC-3003/events").status_code in (400, 404))

        reviewed = [dict(e) for e in draft]
        for e in reviewed:
            if e["title"].startswith("Founders"):
                e["years"] = "1"  # a first-years-only holiday, for the scoping check
        r = c.put(f"/api/documents/{cal_id}/events", json={"events": reviewed})
        check("confirming the reviewed rows puts them in force",
              r.status_code == 200 and r.json()["data"]["holidays"] >= 4, r.text[:200])
        check("  the document is now indexed", c.get(f"/api/documents/{cal_id}").json()["data"]["status"] == "indexed")
        check("  and the calendar, not the placeholder, is the source",
              dates.holiday_source() == "calendar")
        check("a first-years-only holiday counts for a first year (Mon-Fri 16-20 Nov: 4 days)",
              dates.working_days(mon, fri, dates.college_holidays(1)) == 4)
        check("  and not for a third year (5 days) — or an unknown year",
              dates.working_days(mon, fri, dates.college_holidays(3)) == 5
              and dates.working_days(mon, fri, dates.college_holidays(None)) == 5)
        check("a multi-day holiday counts every day (Ayudha Pooja, Wed-Fri 21-23 Oct)",
              dates.working_days(date(2026, 10, 19), date(2026, 10, 23), dates.college_holidays(3)) == 2)
        check("unticked rows stay out of the arithmetic (Saturday working is not a holiday)",
              date(2026, 12, 5) not in dates.college_holidays(3))
        check("the seed's holidays are gone once a calendar is confirmed (New Year was only in the seed)",
              date(2027, 1, 1) not in dates.college_holidays(3))

        login("ravi.kumar", "ravi-demo")
        got = c.get("/api/calendar", params={"start": "2026-11-01", "end": "2026-11-30"}).json()
        check("GET /api/calendar: a third year's November, from the College's calendar",
              got["source"] == "calendar" and [e["title"] for e in got["events"]] == ["Deepavali holiday"],
              str(got)[:200])

        login("lakshmi.narayanan", "advisor-demo")
        c.delete(f"/api/documents/{cal_id}")
        check("deleting the calendar falls back to the placeholder file",
              dates.holiday_source() == "seed" and date(2026, 11, 18) in dates.college_holidays(3))

        section("an interrupted upload is not stuck")
        r = library.backends.get_backend().create_document(
            title="half", filename="h.txt", content_type="text/plain", sha256="00" * 32, size=1,
            doc_type="other", branch="ALL", status="processing", uploaded_by="U102",
            uploaded_at="2026-10-23T00:00:00+00:00")
        check("recover_interrupted marks 'processing' leftovers failed",
              library.recover_interrupted() == 1
              and library.backends.get_backend().get_document(r["data"]["id"])["data"]["status"] == "failed")
        check("  and a failed one can be deleted", c.delete(f"/api/documents/{r['data']['id']}").status_code == 200)
    finally:
        rag.chroma = saved_chroma
        shutil.rmtree(tmp, ignore_errors=True)

    return finish("documents")


if __name__ == "__main__":
    raise SystemExit(main())
