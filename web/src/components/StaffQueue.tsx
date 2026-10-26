import { useCallback, useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api";
import { isOpen, pct, rulePct } from "../format";
import type { CampusRecord, Projection, User } from "../types";
import { RecordCard } from "./Records";

// Buttons per record type. A courtesy, not the mechanism: every click goes
// to POST /api/records/{id}/action, which calls handlers.apply_staff_action,
// which re-checks the role itself. Hiding a button authorizes nothing.
const ACTIONS: Record<string, { verb: string; label: string; tone?: string }[]> = {
  od_request: [
    { verb: "approve", label: "Approve", tone: "primary" },
    { verb: "reject", label: "Reject" },
    { verb: "cancel", label: "Cancel" },
  ],
  facility_complaint: [
    { verb: "resolve", label: "Mark resolved", tone: "primary" },
    { verb: "cancel", label: "Cancel" },
  ],
};

interface Pending {
  record: CampusRecord;
  projection: Projection | null;
  student?: User;
}

function ExceptionModal({
  p,
  onConfirm,
  onClose,
}: {
  p: Pending;
  onConfirm: () => void;
  onClose: () => void;
}) {
  const proj = p.projection;
  return (
    <div className="modal-back" onClick={onClose}>
      <div className="modal" role="dialog" aria-modal="true" onClick={(e) => e.stopPropagation()}>
        <h3>Approve as an attendance exception?</h3>
        <p>
          <strong>{p.record.id}</strong> would take {p.student?.name ?? "the student"} from{" "}
          <strong>{pct(proj?.current_pct)}</strong> to <strong>{pct(proj?.projected_pct)}</strong>, below
          the {rulePct(proj?.threshold)} requirement.
        </p>
        <p className="muted">
          The system flagged this; it did not refuse it. Approving records that you granted an
          exception, against your account. It does not condone an end-of-semester shortage — that is a
          separate decision.
        </p>
        <div className="modal-actions">
          <button className="btn" onClick={onClose}>
            Back
          </button>
          <button className="btn primary" onClick={onConfirm}>
            Approve as exception
          </button>
        </div>
      </div>
    </div>
  );
}

export function StaffQueue({ user, refreshKey }: { user: User; refreshKey: number }) {
  const [data, setData] = useState<Awaited<ReturnType<typeof api.queue>> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ tone: string; text: string } | null>(null);
  const [filter, setFilter] = useState<"open" | "all">("open");
  const [busy, setBusy] = useState<string | null>(null);
  const [pending, setPending] = useState<Pending | null>(null);

  const load = useCallback(() => {
    api.queue().then(setData).catch((e) => setError(e.message));
  }, []);
  useEffect(() => {
    load();
  }, [load, refreshKey]);

  const rows = useMemo(
    () => (data?.records ?? []).filter((r) => filter === "all" || isOpen(r)),
    [data, filter],
  );
  const openCount = (data?.records ?? []).filter(isOpen).length;

  async function act(r: CampusRecord, verb: string, grantException = false) {
    setBusy(r.id + verb);
    setNotice(null);
    try {
      const res = await api.act(r.id, verb, grantException);
      setNotice({
        tone: "ok",
        text: `${r.id} is now ${res.record.status}${res.exception_granted ? " (attendance exception granted)" : ""}.`,
      });
      setPending(null);
      load();
    } catch (e) {
      const err = e as ApiError;
      if (err.body?.outcome === "needs_exception_ack") {
        // The server is the one that knows the record is flagged — the
        // button can be stale. Ask, then resend with the exception.
        setPending({
          record: r,
          projection: (err.body.projection as Projection) ?? r.projection,
          student: data?.users[r.user_id],
        });
      } else {
        setNotice({ tone: err.status === 403 ? "deny" : "warn", text: `${err.status}: ${err.message}` });
        load();
      }
    } finally {
      setBusy(null);
    }
  }

  if (error) return <div className="banner deny">{error}</div>;
  if (!data) return <div className="muted">Loading the queue…</div>;

  const scopeName = data.scope
    .map((s) => (s === "od_request" ? "OD and leave requests" : "facility complaints"))
    .join(" and ");

  return (
    <div className="queue">
      <div className="queue-head">
        <div>
          <div className="muted small">
            Showing {scopeName} — the records {user.name} can act on. Other records are readable from
            chat, but they are someone else's inbox.
          </div>
        </div>
        <div className="seg">
          <button className={filter === "open" ? "on" : ""} onClick={() => setFilter("open")}>
            Needs action <span className="count">{openCount}</span>
          </button>
          <button className={filter === "all" ? "on" : ""} onClick={() => setFilter("all")}>
            All
          </button>
        </div>
      </div>
      {data.errors.length > 0 && <div className="banner warn">Couldn't load everything: {data.errors.join("; ")}</div>}
      {notice && <div className={`banner ${notice.tone}`}>{notice.text}</div>}
      {!rows.length && <div className="muted">Nothing waiting. </div>}
      <div className="stack">
        {rows.map((r) => (
          <RecordCard
            key={r.id}
            r={r}
            owner={data.users[r.user_id]}
            users={data.users}
            actions={
              isOpen(r) &&
              (ACTIONS[r.type] ?? []).map((a) => (
                <button
                  key={a.verb}
                  className={`btn small ${a.tone ?? ""}`}
                  disabled={busy !== null}
                  onClick={() =>
                    a.verb === "approve" && r.needs_exception
                      ? setPending({ record: r, projection: r.projection, student: data.users[r.user_id] })
                      : act(r, a.verb)
                  }
                >
                  {busy === r.id + a.verb ? "…" : a.label}
                </button>
              ))
            }
          />
        ))}
      </div>
      {pending && (
        <ExceptionModal
          p={pending}
          onClose={() => setPending(null)}
          onConfirm={() => act(pending.record, "approve", true)}
        />
      )}
    </div>
  );
}
