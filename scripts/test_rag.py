"""Retrieval plumbing that needs no model: chunking, the stale-index check,
and the guard that keeps the red-team fixture in the corpus.

The embedder is local (ONNX MiniLM), so building a small index here is
offline once its weights are cached. The grounded *answer* needs a model and
is exercised by scripts/redteam_injection.py instead.

    python scripts/test_rag.py
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from _harness import check, finish, section

import chromadb

from app import rag


def main() -> int:
    section("structure-aware chunking")
    text = (
        "# Leave Policy\n\nIntro.\n\n## Applying for OD\n\n### Long-Duration OD\n\n"
        "Such requests must reach the advisor 10 working days ahead.\n\n"
        "## Medical Leave\n\nCertificate needed.\n"
    )
    chunks = rag.structured_chunks(text, "x.md")
    paths = [c.metadata["header_path"] for c in chunks]
    check("one chunk per section with a body",
          paths == ["Leave Policy", "Leave Policy > Applying for OD > Long-Duration OD",
                    "Leave Policy > Medical Leave"], str(paths))
    rule = chunks[1]
    check("the breadcrumb is IN the embedded text, so the vector knows the context",
          rule.text.startswith("[Leave Policy > Applying for OD > Long-Duration OD]")
          and "10 working days" in rule.text)
    check("a heading closes its siblings, not its parents",
          "Applying for OD" not in paths[2])
    naive = rag.naive_chunks("a" * 1200, "x.md")
    check("naive chunks are fixed-size slices", [len(c.text) for c in naive] == [500, 500, 200])

    section("the corpus")
    files = rag.corpus_files()
    names = {p.name for p in files}
    check("all six regulation documents are present",
          {"attendance_policy.md", "od_and_leave_policy.md", "academic_regulations.md",
           "exam_bylaws.md", "hostel_rules.md", "campus_facilities.md"} <= names, str(names))
    every = "\n".join(p.read_text() for p in files)
    check("the planted injection is still in the corpus (the red-team test needs it)",
          "NOTICE TO ANY AI ASSISTANT" in every)
    check("  and so is the real rule it contradicts", "at least 75% of the periods held" in every)
    for p in files:
        cs = rag.structured_chunks(p.read_text(), p.name)
        if not all(c.metadata["header_path"] for c in cs):
            check(f"{p.name}: every chunk has a heading to cite", False)
            break
    else:
        check("every chunk of every document has a heading to cite", True)

    section("stale-index detection")
    tmp = Path(tempfile.mkdtemp(prefix="compass-rag-"))
    corpus = tmp / "regs"
    shutil.copytree(rag.CORPUS_DIR, corpus)
    saved = (rag.CORPUS_DIR, rag.chroma)
    client = chromadb.PersistentClient(path=str(tmp / "chroma"))
    rag.CORPUS_DIR = corpus
    rag.chroma = lambda: client
    try:
        check("no index at all reports missing", rag.index_status()["state"] == "missing")
        rag.build_index()
        check("a fresh index reports fresh", rag.index_status()["state"] == "fresh")
        before = rag.corpus_hash()
        target = corpus / "hostel_rules.md"
        target.write_text(target.read_text().replace("9:00 PM", "10:00 PM"))
        check("editing one word changes the corpus hash", rag.corpus_hash() != before)
        check("  and the index now reports stale", rag.index_status()["state"] == "stale")
        rag.build_index()
        check("re-indexing makes it fresh again", rag.index_status()["state"] == "fresh")
        saved_format = rag.INDEX_FORMAT
        rag.INDEX_FORMAT = "an older chunk format"
        check("an index built by older code is stale, though no document changed",
              rag.index_status()["state"] == "stale")
        rag.INDEX_FORMAT = saved_format
        (corpus / "hostel_rules.md").rename(corpus / "hostel.md")
        check("renaming a document also counts as stale (citations change)",
              rag.index_status()["state"] == "stale")
        hits = rag.retrieve("what time do hostel gates close", k=1)
        check("retrieval runs on the local embedder",
              hits and "Hostel" in hits[0]["metadata"]["header_path"], str(hits[:1]))
    finally:
        rag.CORPUS_DIR, rag.chroma = saved
        shutil.rmtree(tmp, ignore_errors=True)

    return finish("rag")


if __name__ == "__main__":
    raise SystemExit(main())
