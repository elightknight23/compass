import { useEffect, useRef, useState, type ReactNode } from "react";
import { api, ApiError, certificateUrl } from "../api";
import { isOpen, LEAVE_NAMES, pct, rulePct, span, statusTone } from "../format";
import type { Attendance, CampusRecord, RecordEvent, User } from "../types";
import { Icon } from "./Icon";

export function StatusChip({ status }: { status: string }) {
  return <span className={`chip ${statusTone(status)}`}>{status}</span>;
}

/** The projection snapshot stored on a leave request — what the advisor sees. */
export function ProjectionLine({ r }: { r: CampusRecord }) {
  const p = r.projection;
  if (!p) return <span className="muted">attendance not on file</span>;
  return (
    <span className={`proj ${p.below_threshold ? "below" : ""}`}>
      {pct(p.current_pct)} → <strong>{pct(p.projected_pct)}</strong>
      <span className="muted"> · {p.condoned ? "OD counts as present" : "not counted as present"} · needs {rulePct(p.threshold)}</span>
    </span>
  );
}

/**
 * Attach a medical certificate. Only the student's own, undecided medical
 * leave offers this; the server (app/certificates.py) checks it again.
 */
export function CertificateUpload({
  recordId,
  onAttached,
  compact = false,
}: {
  recordId: string;
  onAttached?: (r: CampusRecord) => void;
  compact?: boolean;
}) {
  const input = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function pick(file: File | undefined) {
    if (!file) return;
    setBusy(true);
    setError(null);
    try {
      const res = await api.attachCertificate(recordId, file);
      setDone(res.record.certificate?.filename ?? file.name);
      onAttached?.(res.record);
    } catch (e) {
      setError((e as ApiError).message);
    } finally {
      setBusy(false);
      if (input.current) input.current.value = "";
    }
  }

  return (
    <div className={`cert-upload ${compact ? "compact" : ""}`}>
      <input
        ref={input}
        type="file"
        accept=".pdf,.jpg,.jpeg,.png,application/pdf,image/jpeg,image/png"
        hidden
        onChange={(e) => pick(e.target.files?.[0])}
      />
      {done ? (
        <span className="cert-done">
          <Icon name="check" size={16} /> Attached <strong>{done}</strong>
          <button className="link" onClick={() => input.current?.click()} disabled={busy}>
            replace
          </button>
        </span>
      ) : (
        <button className="btn small soft" onClick={() => input.current?.click()} disabled={busy}>
          <Icon name="clip" size={15} /> {busy ? "Attaching…" : "Attach medical certificate"}
        </button>
      )}
      {!compact && !done && <span className="muted small">PDF, JPG or PNG · seen only by you and your advisor</span>}
      {error && <span className="cert-error">{error}</span>}
    </div>
  );
}

function CertificateLine({ r, mine, onChanged }: { r: CampusRecord; mine: boolean; onChanged?: () => void }) {
  if (r.leave_kind !== "medical") return null;
  const c = r.certificate;
  if (c?.filename) {
    return (
      <div className="cert-line">
        <Icon name="clip" size={15} />
        <a href={certificateUrl(r.id)} target="_blank" rel="noreferrer">
          {c.filename}
        </a>
        {mine && isOpen(r) && <CertificateUpload recordId={r.id} compact onAttached={onChanged} />}
      </div>
    );
  }
  if (c?.attached) return <div className="cert-line muted"><Icon name="clip" size={15} /> certificate attached</div>;
  if (mine && isOpen(r)) return <CertificateUpload recordId={r.id} onAttached={onChanged} />;
  if (isOpen(r)) return <div className="cert-line"><span className="chip small warn">no certificate yet</span></div>;
  return null;
}

function TrackerLine({ r }: { r: CampusRecord }) {
  const t = r.tracker;
  if (!t) return null;
  if (t.url && t.issue)
    return (
      <a className="tracker" href={t.url} target="_blank" rel="noreferrer" title={t.synced ? "in step" : t.error ?? "sync pending"}>
        <Icon name="link" size={13} /> issue #{t.issue}
        {!t.synced && <span className="muted"> · updating</span>}
      </a>
    );
  return <span className="tracker muted" title={t.error ?? undefined}>tracker sync pending</span>;
}

function History({ id, users }: { id: string; users?: Record<string, User> }) {
  const [events, setEvents] = useState<RecordEvent[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api.record(id).then((r) => setEvents(r.events)).catch((e) => setError(e.message));
  }, [id]);
  if (error) return <div className="muted small">{error}</div>;
  if (!events) return <div className="muted small">Loading history…</div>;
  if (!events.length) return <div className="muted small">No history recorded.</div>;
  return (
    <ol className="history">
      {events.map((e) => (
        <li key={e.id}>
          <span className="mono">{e.at?.slice(0, 16).replace("T", " ")}</span>{" "}
          <strong>{e.verb}</strong> by {users?.[e.actor_id]?.name ?? e.actor_id}
          {e.from_status !== e.to_status && e.to_status && (
            <span className="muted"> · {e.from_status ?? "new"} → {e.to_status}</span>
          )}
          {e.note && <span className="muted"> · {e.note}</span>}
        </li>
      ))}
    </ol>
  );
}

export function RecordCard({
  r,
  owner,
  users,
  actions,
  mine = false,
  onChanged,
}: {
  r: CampusRecord;
  owner?: User;
  users?: Record<string, User>;
  actions?: ReactNode;
  mine?: boolean;
  onChanged?: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const isLeave = r.type === "od_request";
  const flagged = r.needs_exception && isOpen(r);
  return (
    <article className={`card record tone-${statusTone(r.status)} ${flagged ? "flagged" : ""}`}>
      <div className="record-head">
        <span className="kind">{isLeave ? LEAVE_NAMES[r.leave_kind ?? ""] ?? "Leave" : r.category}</span>
        {!isLeave && r.priority && <span className={`chip small ${r.priority === "high" ? "deny" : "muted"}`}>{r.priority}</span>}
        {flagged && <span className="chip small warn">needs exception</span>}
        <span className="grow" />
        <StatusChip status={r.status} />
      </div>
      {owner && (
        <div className="owner">
          {owner.name}
          <span className="muted"> · {owner.section ?? owner.department}</span>
        </div>
      )}
      <div className="record-body">
        {isLeave ? (
          <>
            <div className="when">
              {span(r)}
              <span className="muted"> · {r.working_days} working day{r.working_days === 1 ? "" : "s"}</span>
            </div>
            {r.reason && <div className="what">{r.reason}</div>}
            <ProjectionLine r={r} />
            <CertificateLine r={r} mine={mine} onChanged={onChanged} />
          </>
        ) : (
          <div className="what">{r.notes}</div>
        )}
      </div>
      <div className="record-foot">
        <span className="mono rid">{r.id}</span>
        <span className="muted small">
          filed {r.created_at}
          {r.decided_by && ` · decided by ${users?.[r.decided_by]?.name ?? r.decided_by} on ${r.decided_at}`}
        </span>
        <TrackerLine r={r} />
        <span className="grow" />
        <button className="link" onClick={() => setExpanded((x) => !x)}>
          {expanded ? "hide history" : "history"}
        </button>
        {actions && <span className="record-actions">{actions}</span>}
      </div>
      {expanded && <History id={r.id} users={users} />}
    </article>
  );
}

/** Attendance as a ruler: the fill is where you are, the notch is the rule. */
export function AttendanceCard({ refreshKey, compact = false }: { refreshKey: number; compact?: boolean }) {
  const [a, setA] = useState<Attendance | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api.attendance().then(setA).catch((e) => setError(e.message));
  }, [refreshKey]);
  if (error) return null;
  if (!a) return <div className={`att ${compact ? "compact" : "card"}`}><span className="muted small">Loading attendance…</span></div>;

  const value = a.current_pct ?? 0;
  const below = a.below_threshold;
  const note = below ? (
    <span className="chip deny">below {rulePct(a.threshold)}</span>
  ) : a.days_of_margin === 0 ? (
    <span className="chip warn">right at the line</span>
  ) : (
    <span className="chip ok">
      {a.days_of_margin} day{a.days_of_margin === 1 ? "" : "s"} of margin
    </span>
  );
  return (
    <div className={`att ${compact ? "compact" : "card"} ${below ? "below" : ""}`}>
      <div className="att-top">
        <div>
          <div className="eyebrow">Attendance</div>
          <div className="att-big">{pct(a.current_pct)}</div>
        </div>
        {note}
      </div>
      <div className="ruler" role="img" aria-label={`Attendance ${pct(a.current_pct)}, requirement ${rulePct(a.threshold)}`}>
        <i style={{ width: `${Math.min(100, value * 100)}%` }} />
        <b style={{ left: `${a.threshold * 100}%` }} title={`requirement ${rulePct(a.threshold)}`} />
      </div>
      <div className="muted small">
        {a.attended} of {a.held} periods{a.as_of && ` · as of ${a.as_of}`}
      </div>
      {!compact && (
        <div className="muted small">
          OD counts as present; medical and family-emergency leave don't. Margin is the days you could
          miss before dropping below {rulePct(a.threshold)}.
        </div>
      )}
    </div>
  );
}

export function MyRecords({ refreshKey }: { refreshKey: number }) {
  const [rows, setRows] = useState<CampusRecord[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    api.myRecords().then((r) => setRows(r.records)).catch((e) => setError(e.message));
  }, [refreshKey, tick]);

  if (error) return <div className="banner deny">{error}</div>;
  if (!rows) return <div className="muted">Loading…</div>;
  if (!rows.length)
    return <div className="empty-note">Nothing filed yet. Ask in the chat to file a complaint or apply for leave.</div>;
  return (
    <div className="stack">
      {rows.map((r) => (
        <RecordCard key={r.id} r={r} mine onChanged={() => setTick((t) => t + 1)} />
      ))}
    </div>
  );
}
