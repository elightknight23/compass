import type { ReactNode } from "react";
import { pct, rulePct } from "../format";
import type { Trace as TraceT } from "../types";

// The reasoning panel. The backend returns one flat trace; this lays it out
// in the order the request actually moved: what the model understood, the
// seam between what it HEARD and what Python COMPUTED, the permission
// verdict, the attendance arithmetic, the sources, and the result.
//
// The seam is deliberate product, not debug output: it is where you can
// see that the model never did the calendar or the percentage.

const TONE: Record<string, "ok" | "deny" | "warn"> = {
  created: "ok", updated: "ok", found: "ok", answered: "ok", cancelled: "ok",
  not_authorized: "deny",
  terminal_state: "warn", needs_exception_ack: "warn", needs_target: "warn",
  needs_dates: "warn", needs_leave_kind: "warn", needs_category: "warn",
  needs_action: "warn", needs_change: "warn", not_found: "warn",
  write_failed: "warn", read_failed: "warn", past_dates: "warn", bad_dates: "warn",
  model_unavailable: "warn", index_missing: "warn",
};

function heardPhrase(h: Record<string, unknown>): string {
  const parts: string[] = [];
  if (h.explicit_start) parts.push(String(h.explicit_start) + (h.explicit_end ? ` → ${h.explicit_end}` : ""));
  if (typeof h.day_offset === "number")
    parts.push(h.day_offset === 0 ? "today" : h.day_offset === 1 ? "tomorrow" : `+${h.day_offset} days`);
  if (h.anchor_weekday) parts.push(String(h.anchor_weekday));
  if (typeof h.week_offset === "number")
    parts.push(h.week_offset === 0 ? "this week" : `+${h.week_offset} week${h.week_offset === 1 ? "" : "s"}`);
  if (h.end_weekday) parts.push(`through ${h.end_weekday}`);
  if (h.duration_weekdays) parts.push(`${h.duration_weekdays} working day${h.duration_weekdays === 1 ? "" : "s"}`);
  return parts.join(" · ") || "—";
}

function Row({ k, children }: { k: string; children: ReactNode }) {
  return (
    <div className="trow">
      <span className="tkey">{k}</span>
      <span className="tval">{children}</span>
    </div>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="tsec">
      <div className="ttitle">{title}</div>
      {children}
    </div>
  );
}

const Chip = ({ tone, children }: { tone?: string; children: ReactNode }) => (
  <span className={`chip ${tone ?? ""}`}>{children}</span>
);

export function Trace({ d, live = false }: { d: TraceT; live?: boolean }) {
  const cites = d.retrieved ?? d.sources;
  const att = d.attendance;
  const entities = Object.entries(d.entities ?? {}).filter(
    ([k]) => !["start_date", "end_date"].includes(k),
  );

  return (
    <div className="trace">
      <Section title="Understood · the one model call">
        {d.intent && <Row k="intent"><Chip>{d.intent}</Chip></Row>}
        {typeof d.confidence === "number" && (
          <Row k="confidence">
            <span className="conf">
              <span className="conf-bar"><i style={{ width: `${Math.round(d.confidence * 100)}%` }} /></span>
              <span className="mono">{d.confidence.toFixed(2)}</span>
            </span>
          </Row>
        )}
        {d.reasoning && <Row k="because"><span className="quote">“{d.reasoning}”</span></Row>}
        {entities.length > 0 && (
          <Row k="heard">
            <span className="mono">{entities.map(([k, v]) => `${k}=${JSON.stringify(v)}`).join("  ")}</span>
          </Row>
        )}
        {d.downgraded_from && <Row k="downgraded"><Chip tone="warn">from {d.downgraded_from} · low confidence</Chip></Row>}
        {d.classifier_error && <Row k="model"><span className="mono">{d.classifier_error}</span></Row>}
        {d.provider && <Row k="provider"><span className="mono">{d.provider}</span></Row>}
      </Section>

      {d.dates && (
        <Section title="Dates · model heard → Python resolved">
          <Row k="heard"><span className="seam mono">{heardPhrase(d.dates.heard ?? {})}</span></Row>
          <Row k="resolved"><span className="mono">{d.dates.resolved}</span></Row>
          {typeof d.working_days === "number" && (
            <Row k="working days">
              <span className="mono">{d.working_days}</span>
              {d.holidays_skipped?.length ? (
                <span className="muted"> · skipped {d.holidays_skipped.join(", ")}</span>
              ) : null}
            </Row>
          )}
        </Section>
      )}

      {att && (
        <Section title="Attendance · computed in attendance.py">
          <Row k="now"><span className="mono">{att.attended}/{att.held} = {pct(att.current_pct)}</span></Row>
          <Row k="adds">
            <span className="mono">
              +{att.added_periods} held, +{att.added_present} present
            </span>
            <span className="muted"> · {att.condoned ? "condoned (OD)" : "not condoned"}</span>
          </Row>
          <Row k="projected">
            <Chip tone={att.below_threshold ? "deny" : "ok"}>
              {pct(att.projected_pct)} {att.below_threshold ? "<" : "≥"} {rulePct(att.threshold)}
            </Chip>
          </Row>
          {d.needs_exception && <Row k="routing"><Chip tone="warn">flagged for the advisor's exception</Chip></Row>}
        </Section>
      )}

      {d.authz && (
        <Section title="Authorization · an if statement, not a prompt">
          <Row k="verdict">
            <Chip tone={d.authz === "allowed" ? "ok" : "deny"}>
              {d.authz === "allowed" ? "✓ allowed" : "✕ denied"}
            </Chip>
          </Row>
          {d.role && <Row k="role">{d.role}</Row>}
          {d.reason && <Row k="why">{d.reason}</Row>}
          {d.checked_against && <Row k="source"><span className="mono">{d.checked_against}</span></Row>}
        </Section>
      )}

      {Array.isArray(cites) && cites.length > 0 && (
        <Section title="Retrieved from the regulations">
          {cites.map((c, i) => (
            <div className="cite" key={i}>
              <span className="src">{c.source}</span>
              <span className="sec">{(c.section ?? "").split(" > ").slice(1).join(" › ") || c.section}</span>
              {typeof c.distance === "number" && <span className="d">{c.distance.toFixed(3)}</span>}
            </div>
          ))}
          {d.index && d.index.state !== "fresh" && (
            <Row k="index"><Chip tone="warn">{d.index.state}: {d.index.detail}</Chip></Row>
          )}
        </Section>
      )}

      {(d.outcome || d.error) && (
        <Section title="Result">
          {d.outcome && <Row k="outcome"><Chip tone={TONE[d.outcome]}>{d.outcome}</Chip></Row>}
          {d.record_id && <Row k="record"><span className="mono">{d.record_id}</span></Row>}
          {d.error && <Row k="error"><span className="quote">{String(d.error)}</span></Row>}
        </Section>
      )}

      {live && <div className="tfoot">classified · working on it…</div>}
      {!live && (
        <div className="tfoot">
          {[
            typeof d.latency_ms === "number" && `${(d.latency_ms / 1000).toFixed(1)}s total`,
            typeof d.classify_ms === "number" && `${(d.classify_ms / 1000).toFixed(1)}s classify`,
            d.backend && `${d.backend} backend`,
            d.handled_by,
          ]
            .filter(Boolean)
            .join(" · ")}
        </div>
      )}

      <details className="traw">
        <summary>raw trace</summary>
        <pre>{JSON.stringify(d, null, 2)}</pre>
      </details>
    </div>
  );
}
