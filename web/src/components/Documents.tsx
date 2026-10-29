import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import { api, ApiError, documentFileUrl } from "../api";
import { day } from "../format";
import type { CalendarEvent, CampusDocument, DocType, EventKind } from "../types";

// The College's documents. Everyone sees what their answers can come from;
// the faculty advisor also uploads, removes, and reviews calendars. As with
// the staff queue, hiding the controls is a courtesy — app/library.py
// re-checks the caller on every upload, delete and confirm.

const DOC_TYPE_NAMES: Record<DocType, string> = {
  academic_calendar: "Academic calendar",
  syllabus: "Syllabus",
  regulation: "Regulation",
  other: "Other",
};

const STATUS: Record<string, { label: string; tone: string }> = {
  processing: { label: "processing…", tone: "muted" },
  indexed: { label: "searchable", tone: "ok" },
  needs_review: { label: "needs review", tone: "warn" },
  failed: { label: "failed", tone: "deny" },
};

const KINDS: EventKind[] = ["holiday", "exam", "semester_start", "semester_end", "other"];
const KIND_NAMES: Record<EventKind, string> = {
  holiday: "Holiday",
  exam: "Exam",
  semester_start: "Semester start",
  semester_end: "Semester end",
  other: "Other",
};

// What each parser flag means, in the reviewer's words.
const FLAG_NAMES: Record<string, string> = {
  inferred_year: "year not printed — taken from the academic year",
  weekday_mismatch: "the weekday printed doesn't match this date",
  month_first: "read as month/day — check it",
  several_dates_in_row: "one of several dates in this row",
  no_title: "no event name found",
  unparsed: "no date could be read",
};

const BRANCHES = ["ALL", "CSE", "IT", "ECE", "EEE", "MECH", "CIVIL", "AIDS", "AIML"];

function sizeOf(bytes: number) {
  return bytes > 1024 * 1024 ? `${(bytes / 1024 / 1024).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

function UploadForm({ onUploaded }: { onUploaded: (d: CampusDocument) => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [docType, setDocType] = useState<DocType>("syllabus");
  const [title, setTitle] = useState("");
  const [branch, setBranch] = useState("ALL");
  const [semester, setSemester] = useState("");
  const [year, setYear] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [formKey, setFormKey] = useState(0);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!file) return;
    const form = new FormData();
    form.set("file", file);
    form.set("doc_type", docType);
    form.set("title", title);
    form.set("branch", branch);
    form.set("semester", semester);
    form.set("academic_year", year);
    setBusy(true);
    setError(null);
    try {
      const res = await api.upload(form);
      onUploaded(res.data);
      setFile(null);
      setTitle("");
      setFormKey((k) => k + 1);
    } catch (err) {
      setError((err as ApiError).message);
    } finally {
      setBusy(false);
    }
  }

  const calendar = docType === "academic_calendar";
  return (
    <form className="card upload" onSubmit={submit} key={formKey}>
      <div className="upload-grid">
        <label className="span2">
          File <span className="muted">· PDF, Word, Markdown or text · scans can't be read</span>
          <input type="file" accept=".pdf,.docx,.md,.txt" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
        </label>
        <label>
          Type
          <select value={docType} onChange={(e) => setDocType(e.target.value as DocType)}>
            {(Object.keys(DOC_TYPE_NAMES) as DocType[]).map((t) => (
              <option key={t} value={t}>{DOC_TYPE_NAMES[t]}</option>
            ))}
          </select>
        </label>
        <label>
          Title
          <input value={title} placeholder={file?.name.replace(/\.[^.]+$/, "") ?? "e.g. CSE Syllabus R2021"}
                 onChange={(e) => setTitle(e.target.value)} />
        </label>
        <label>
          Branch
          <input list="branches" value={branch} onChange={(e) => setBranch(e.target.value)} />
          <datalist id="branches">{BRANCHES.map((b) => <option key={b} value={b} />)}</datalist>
        </label>
        <label>
          Semester <span className="muted">· blank = all</span>
          <select value={semester} onChange={(e) => setSemester(e.target.value)}>
            <option value="">All</option>
            {Array.from({ length: 8 }, (_, i) => i + 1).map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
        </label>
        <label>
          Academic year {calendar ? <span className="muted">· required</span> : <span className="muted">· optional</span>}
          <input value={year} placeholder="2026-27" required={calendar} onChange={(e) => setYear(e.target.value)} />
        </label>
      </div>
      <div className="muted small">
        {docType === "syllabus"
          ? "A syllabus is searched only by students of its branch (ALL = every branch). One file may cover every semester: each course is filed under the SEMESTER heading above it."
          : calendar
            ? "Dates are read from the calendar by the system, not by the language model, and nothing counts until you review and confirm them."
            : "Searchable by everyone once processed."}
      </div>
      {error && <div className="banner deny">{error}</div>}
      <div className="row-end">
        <button className="btn primary" disabled={!file || busy}>{busy ? "Uploading…" : "Upload"}</button>
      </div>
    </form>
  );
}

function CalendarReview({ doc, onDone, onClose }: { doc: CampusDocument; onDone: (msg: string) => void; onClose: () => void }) {
  const [rows, setRows] = useState<CalendarEvent[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.calendarDraft(doc.id).then((r) => setRows(r.data.events)).catch((e) => setError(e.message));
  }, [doc.id]);

  const set = (i: number, patch: Partial<CalendarEvent>) =>
    setRows((rs) => rs && rs.map((r, j) => (j === i ? { ...r, ...patch } : r)));

  async function confirm() {
    if (!rows) return;
    setBusy(true);
    setError(null);
    try {
      const res = await api.confirmCalendar(doc.id, rows);
      onDone(`${doc.title}: ${res.data.confirmed} events in force, ${res.data.holidays} of them holidays. Leave requests now count working days against it.`);
    } catch (e) {
      setError((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  }

  const flagged = rows?.filter((r) => r.flags?.length).length ?? 0;
  return (
    <div className="card review">
      <div className="review-head">
        <div>
          <strong>Review {doc.title}</strong>
          <div className="muted small">
            Each row was read from the document by the parser. Tick what is right, fix what isn't — only ticked,
            dated rows are confirmed, and only confirmed holidays change working days.
            {flagged > 0 && <> <span className="chip small warn">{flagged} flagged</span> rows need a look.</>}
          </div>
        </div>
        <button className="link" onClick={onClose}>close</button>
      </div>
      {error && <div className="banner deny">{error}</div>}
      {!rows ? (
        <div className="muted">Loading the parsed dates…</div>
      ) : !rows.length ? (
        <div className="muted">No dated events were found in this calendar.</div>
      ) : (
        <div className="review-scroll">
          <table className="review-table">
            <thead>
              <tr><th>Use</th><th>From</th><th>To</th><th>Event</th><th>Kind</th><th>Years</th></tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={i} className={`${r.flags?.length ? "flag" : ""} ${r.include ? "" : "off"}`}>
                  <td><input type="checkbox" checked={r.include} aria-label={`use ${r.title}`}
                             onChange={(e) => set(i, { include: e.target.checked })} /></td>
                  <td><input type="date" value={r.start_date ?? ""}
                             onChange={(e) => set(i, { start_date: e.target.value || null })} /></td>
                  <td><input type="date" value={r.end_date ?? ""}
                             onChange={(e) => set(i, { end_date: e.target.value || null })} /></td>
                  <td>
                    <input className="wide" value={r.title} onChange={(e) => set(i, { title: e.target.value })} />
                    {r.source_text && <div className="src-text">from: “{r.source_text}”{r.page ? ` · p.${r.page}` : ""}</div>}
                    {r.flags?.map((f) => <span key={f} className="chip small warn">{FLAG_NAMES[f] ?? f}</span>)}
                  </td>
                  <td>
                    <select value={r.kind} onChange={(e) => set(i, { kind: e.target.value as EventKind })}>
                      {KINDS.map((k) => <option key={k} value={k}>{KIND_NAMES[k]}</option>)}
                    </select>
                  </td>
                  <td><input className="years" value={r.years} title='"all", or years like "1" or "1,2"'
                             onChange={(e) => set(i, { years: e.target.value })} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="row-end">
        <button className="btn" onClick={onClose}>Cancel</button>
        <button className="btn primary" disabled={!rows || busy} onClick={confirm}>
          {busy ? "Confirming…" : "Confirm calendar"}
        </button>
      </div>
    </div>
  );
}

export function Documents() {
  const [docs, setDocs] = useState<CampusDocument[] | null>(null);
  const [mayManage, setMayManage] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ tone: string; text: string } | null>(null);
  const [reviewing, setReviewing] = useState<CampusDocument | null>(null);
  const [armed, setArmed] = useState<string | null>(null); // two-step delete, no browser dialog
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(() => {
    api.documents()
      .then((r) => { setDocs(r.documents); setMayManage(r.may_manage); })
      .catch((e) => setError(e.message));
  }, []);
  useEffect(() => { load(); }, [load]);

  // Processing happens after the upload returns; poll until it settles.
  const processing = useMemo(() => (docs ?? []).some((d) => d.status === "processing"), [docs]);
  useEffect(() => {
    if (!processing) return;
    const t = setInterval(load, 1500);
    return () => clearInterval(t);
  }, [processing, load]);

  async function remove(d: CampusDocument) {
    setBusy(d.id);
    try {
      await api.deleteDocument(d.id);
      setNotice({ tone: "ok", text: `Removed ${d.id} ${d.title} — it no longer answers any question.` });
      if (reviewing?.id === d.id) setReviewing(null);
      load();
    } catch (e) {
      setNotice({ tone: "deny", text: (e as ApiError).message });
    } finally {
      setBusy(null);
      setArmed(null);
    }
  }

  async function reindex() {
    setBusy("reindex");
    try {
      const r = await api.reindex();
      setNotice({ tone: "ok", text: `Index rebuilt: ${r.structured} sections, including ${r.uploads} uploaded documents.` });
    } catch (e) {
      setNotice({ tone: "deny", text: (e as ApiError).message });
    } finally {
      setBusy(null);
    }
  }

  if (error) return <div className="banner deny">{error}</div>;
  if (!docs) return <div className="muted">Loading documents…</div>;

  return (
    <div className="docs">
      <p className="muted small lede">
        {mayManage
          ? "What you upload here is what students' answers come from: the regulations are built in; calendars and syllabi are yours to add. Students only ever search the syllabus for their own branch."
          : "The College's documents Compass answers from. Syllabus answers are filtered to your branch and semester."}
      </p>
      {mayManage && (
        <UploadForm onUploaded={(d) => {
          setNotice({ tone: "ok", text: `Uploaded ${d.id}. It is being processed — this takes a few seconds for a long syllabus.` });
          load();
        }} />
      )}
      {notice && <div className={`banner ${notice.tone}`}>{notice.text}</div>}
      {reviewing && (
        <CalendarReview
          doc={reviewing}
          onClose={() => setReviewing(null)}
          onDone={(msg) => { setReviewing(null); setNotice({ tone: "ok", text: msg }); load(); }}
        />
      )}
      <div className="docs-head">
        <h2 className="h2">Uploaded documents</h2>
        {mayManage && (
          <button className="btn small" disabled={busy !== null} onClick={reindex}>
            {busy === "reindex" ? "Rebuilding…" : "Rebuild index"}
          </button>
        )}
      </div>
      {!docs.length && (
        <div className="muted">
          Nothing uploaded yet{mayManage ? "" : " — your faculty advisor adds the calendar and syllabi"}. Questions are answered from the built-in regulations until then.
        </div>
      )}
      <div className="stack">
        {docs.map((d) => {
          const st = STATUS[d.status] ?? { label: d.status, tone: "muted" };
          return (
            <div key={d.id} className={`card doc ${d.status}`}>
              <div className="record-head">
                <span className="mono rid">{d.id}</span>
                <strong className="doc-title">{d.title}</strong>
                <span className="grow" />
                <span className={`chip ${st.tone}`}>{st.label}</span>
              </div>
              <div className="doc-meta muted small">
                {DOC_TYPE_NAMES[d.doc_type]} · {d.branch === "ALL" ? "all branches" : d.branch}
                {d.semester ? ` · semester ${d.semester}` : ""}
                {d.academic_year ? ` · ${d.academic_year}` : ""}
                {` · ${d.filename} (${sizeOf(d.size)}${d.pages ? `, ${d.pages} page${d.pages === 1 ? "" : "s"}` : ""})`}
                {d.chunks ? ` · ${d.chunks} section${d.chunks === 1 ? "" : "s"} indexed` : ""}
                {` · uploaded ${day(d.uploaded_at.slice(0, 10))}`}
              </div>
              {d.error && <div className={`banner ${d.status === "failed" ? "deny" : "warn"}`}>{d.error}</div>}
              <div className="record-foot">
                <a className="link" href={documentFileUrl(d.id)} target="_blank" rel="noreferrer">open file</a>
                <span className="grow" />
                {mayManage && d.doc_type === "academic_calendar" && (d.status === "needs_review" || d.status === "indexed") && (
                  <button className={`btn small ${d.status === "needs_review" ? "primary" : ""}`} onClick={() => setReviewing(d)}>
                    {d.status === "needs_review" ? "Review dates" : "Edit dates"}
                  </button>
                )}
                {mayManage && d.status !== "processing" && (
                  armed === d.id ? (
                    <>
                      <button className="btn small" onClick={() => setArmed(null)}>Keep</button>
                      <button className="btn small danger" disabled={busy !== null} onClick={() => remove(d)}>
                        {busy === d.id ? "Removing…" : "Remove for everyone"}
                      </button>
                    </>
                  ) : (
                    <button className="btn small" onClick={() => setArmed(d.id)}>Delete</button>
                  )
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

export function UpcomingCard() {
  const [data, setData] = useState<Awaited<ReturnType<typeof api.calendar>> | null>(null);
  useEffect(() => {
    api.calendar().then(setData).catch(() => setData(null));
  }, []);
  if (!data) return null;
  const rows = data.events.filter((e) => e.kind !== "other").slice(0, 6);
  return (
    <div className="card upcoming">
      <div className="att-label">Coming up</div>
      {!rows.length && <div className="muted small">Nothing on the calendar in the coming weeks.</div>}
      <ul className="upcoming-list">
        {rows.map((e, i) => (
          <li key={i}>
            <span className={`dot ${e.kind}`} />
            <span className="when">{day(e.start_date)}{e.end_date && e.end_date !== e.start_date ? ` – ${day(e.end_date)}` : ""}</span>
            <span className="what">{e.title}</span>
          </li>
        ))}
      </ul>
      <div className="muted small">
        {data.source === "seed" ? "Placeholder holidays — no academic calendar has been confirmed yet." : "From the College's confirmed academic calendar."}
      </div>
    </div>
  );
}
