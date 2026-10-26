import type { CampusRecord } from "./types";

// Display helpers only. Every number they format was computed server-side
// by app/attendance.py — the browser never does the arithmetic either.

export const pct = (v: number | null | undefined) =>
  v === null || v === undefined ? "—" : `${(v * 100).toFixed(1)}%`;

export const rulePct = (v: number | null | undefined) =>
  v === null || v === undefined ? "—" : `${+(v * 100).toFixed(2)}%`;

export function day(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso + "T00:00:00");
  return d.toLocaleDateString("en-IN", { weekday: "short", day: "numeric", month: "short" });
}

export function span(r: CampusRecord): string {
  if (!r.start_date) return "";
  return r.start_date === r.end_date || !r.end_date
    ? day(r.start_date)
    : `${day(r.start_date)} – ${day(r.end_date)}`;
}

export const LEAVE_NAMES: Record<string, string> = {
  od: "OD",
  medical: "Medical leave",
  casual: "Casual leave",
};

export const ROLE_NAMES: Record<string, string> = {
  student: "Student",
  hostel_warden: "Hostel warden",
  faculty_advisor: "Faculty advisor",
};

export function title(r: CampusRecord): string {
  if (r.type === "od_request") return `${LEAVE_NAMES[r.leave_kind ?? ""] ?? "Leave"} · ${span(r)}`;
  return r.notes ?? "Facility complaint";
}

const OPEN = new Set(["open", "submitted"]);
export const isOpen = (r: CampusRecord) => OPEN.has(r.status);

export function statusTone(status: string): "ok" | "deny" | "warn" | "muted" {
  if (["approved", "resolved"].includes(status)) return "ok";
  if (["rejected"].includes(status)) return "deny";
  if (OPEN.has(status)) return "warn";
  return "muted"; // cancelled, withdrawn
}

export function initials(name: string): string {
  return name
    .replace(/^Dr\.\s*/, "")
    .split(/\s+/)
    .map((p) => p[0])
    .slice(0, 2)
    .join("")
    .toUpperCase();
}
