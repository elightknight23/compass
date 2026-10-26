import { useEffect, useState, type ReactNode } from "react";
import { api } from "../api";
import { isOpen, LEAVE_NAMES, pct, rulePct, span, statusTone } from "../format";
import type { Attendance, CampusRecord, RecordEvent, User } from "../types";

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
      <span className="muted"> · {p.condoned ? "OD counts as present" : "not condoned"} · needs {rulePct(p.threshold)}</span>
    </span>
  );
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
}: {
  r: CampusRecord;
  owner?: User;
  users?: Record<string, User>;
  actions?: ReactNode;
}) {
  const [expanded, setExpanded] = useState(false);
  const isLeave = r.type === "od_request";
  return (
    <div className={`card record ${r.needs_exception && isOpen(r) ? "flagged" : ""}`}>
      <div className="record-head">
        <span className="mono rid">{r.id}</span>
        <span className="chip small">{isLeave ? LEAVE_NAMES[r.leave_kind ?? ""] ?? "Leave" : r.category}</span>
        {!isLeave && r.priority && <span className={`chip small ${r.priority === "high" ? "deny" : ""}`}>{r.priority}</span>}
        {r.needs_exception && isOpen(r) && <span className="chip small warn">needs exception</span>}
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
            <div>
              <strong>{span(r)}</strong>
              <span className="muted"> · {r.working_days} working day{r.working_days === 1 ? "" : "s"}</span>
            </div>
            <div className="muted">{r.reason}</div>
            <ProjectionLine r={r} />
          </>
        ) : (
          <div>{r.notes}</div>
        )}
      </div>
      <div className="record-foot">
        <span className="muted small">
          filed {r.created_at}
          {r.decided_by && ` · decided by ${users?.[r.decided_by]?.name ?? r.decided_by} on ${r.decided_at}`}
        </span>
        <span className="grow" />
        <button className="link" onClick={() => setExpanded((x) => !x)}>
          {expanded ? "hide history" : "history"}
        </button>
        {actions}
      </div>
      {expanded && <History id={r.id} users={users} />}
    </div>
  );
}

export function AttendanceCard({ refreshKey }: { refreshKey: number }) {
  const [a, setA] = useState<Attendance | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api.attendance().then(setA).catch((e) => setError(e.message));
  }, [refreshKey]);
  if (error) return null;
  if (!a) return <div className="card att">Loading attendance…</div>;

  const value = a.current_pct ?? 0;
  const below = a.below_threshold;
  return (
    <div className={`card att ${below ? "below" : ""}`}>
      <div className="att-top">
        <div>
          <div className="att-label">Attendance</div>
          <div className="att-big">{pct(a.current_pct)}</div>
          <div className="muted small">
            {a.attended} of {a.held} periods{a.as_of && ` · as of ${a.as_of}`}
          </div>
        </div>
        <div className="att-note">
          {below ? (
            <span className="chip deny">below {rulePct(a.threshold)}</span>
          ) : a.days_of_margin === 0 ? (
            <span className="chip warn">right at the line</span>
          ) : (
            <span className="chip ok">
              {a.days_of_margin} day{a.days_of_margin === 1 ? "" : "s"} of margin
            </span>
          )}
        </div>
      </div>
      <div className="meter" role="img" aria-label={`Attendance ${pct(a.current_pct)}, requirement ${rulePct(a.threshold)}`}>
        <i style={{ width: `${Math.min(100, value * 100)}%` }} />
        <b style={{ left: `${a.threshold * 100}%` }} title={`requirement ${rulePct(a.threshold)}`} />
      </div>
      <div className="muted small">
        OD counts as present; medical and casual leave don't. Margin = days of non-OD absence
        before falling below {rulePct(a.threshold)}.
      </div>
    </div>
  );
}

export function MyRecords({ refreshKey }: { refreshKey: number }) {
  const [rows, setRows] = useState<CampusRecord[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api.myRecords().then((r) => setRows(r.records)).catch((e) => setError(e.message));
  }, [refreshKey]);

  if (error) return <div className="banner deny">{error}</div>;
  if (!rows) return <div className="muted">Loading…</div>;
  if (!rows.length) return <div className="muted">Nothing filed yet. Ask in the chat to file a complaint or apply for leave.</div>;
  return (
    <div className="stack">
      {rows.map((r) => (
        <RecordCard key={r.id} r={r} />
      ))}
    </div>
  );
}
