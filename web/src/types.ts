// Shapes returned by the FastAPI backend. Mirrors app/backends RECORD_FIELDS
// and main.public_user — kept loose where the server sends a free-form trace.

export type Role = "student" | "hostel_warden" | "faculty_advisor";
export type RecordType = "facility_complaint" | "od_request";

export interface User {
  id: string;
  username: string;
  name: string;
  role: Role;
  department?: string | null;
  year?: number | null;
  section?: string | null;
  residence?: string | null;
  // Derived server-side from auth.ROLE_CAPABILITIES. Used to decide which
  // buttons to draw — never as the authorization itself.
  capabilities: RecordType[];
}

export interface Projection {
  attended: number;
  held: number;
  working_days: number;
  periods_per_day: number;
  condoned: boolean;
  threshold: number;
  added_periods: number;
  added_present: number;
  current_pct: number | null;
  projected_pct: number | null;
  below_threshold: boolean;
  already_below: boolean;
}

export interface CampusRecord {
  id: string;
  user_id: string;
  type: RecordType;
  status: string;
  created_at: string;
  notes: string | null;
  category: string | null;
  priority: string | null;
  leave_kind: "od" | "medical" | "casual" | null;
  start_date: string | null;
  end_date: string | null;
  working_days: number | null;
  reason: string | null;
  projection: Projection | null;
  needs_exception: boolean | null;
  decided_by: string | null;
  decided_at: string | null;
}

export interface RecordEvent {
  id: number;
  record_id: string;
  actor_id: string;
  verb: string;
  from_status: string | null;
  to_status: string | null;
  note: string | null;
  at: string | null;
}

export interface Attendance {
  attended: number;
  held: number;
  as_of: string | null;
  current_pct: number | null;
  threshold: number;
  periods_per_day: number;
  below_threshold: boolean;
  days_of_margin: number | null;
}

// The reasoning trace is a flat dict that every graph node contributes to
// (app/state.py). Only the keys the panel reads are named here.
export interface Trace {
  intent?: string;
  confidence?: number;
  reasoning?: string;
  entities?: Record<string, unknown>;
  dates?: { heard: Record<string, unknown>; resolved: string };
  provider?: string;
  downgraded_from?: string;
  classifier_error?: string;
  authz?: string;
  role?: string;
  reason?: string;
  checked_against?: string;
  outcome?: string;
  error?: string;
  backend?: string;
  latency_ms?: number;
  classify_ms?: number;
  handled_by?: string;
  record_id?: string;
  working_days?: number;
  span?: string;
  holidays_skipped?: string[];
  attendance?: Projection;
  needs_exception?: boolean;
  retrieved?: { source: string; section?: string | null; distance?: number }[];
  sources?: { source: string; section?: string | null; distance?: number }[];
  index?: { state: string; detail?: string };
  [key: string]: unknown;
}

export interface ChatTurn {
  role: "user" | "assistant";
  content: string;
  trace?: Trace;
  pending?: boolean;
}
