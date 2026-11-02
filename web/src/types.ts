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
  branch?: string | null;
  year?: number | null;
  semester?: number | null;
  section?: string | null;
  residence?: string | null;
  mess?: string | null;
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
  leave_kind: "od" | "medical" | "family_emergency" | null;
  start_date: string | null;
  end_date: string | null;
  working_days: number | null;
  reason: string | null;
  projection: Projection | null;
  needs_exception: boolean | null;
  // Medical leave. Someone who may not open the certificate gets only
  // {attached: true} — no filename (app/certificates.public).
  certificate: Certificate | null;
  // A facility complaint's mirrored issue, when the tracker is configured.
  tracker: TrackerState | null;
  decided_by: string | null;
  decided_at: string | null;
}

export interface Certificate {
  attached?: boolean;
  filename?: string;
  content_type?: string;
  size?: number;
  uploaded_at?: string;
}

export interface TrackerState {
  issue?: number;
  url?: string | null;
  synced?: boolean;
  error?: string | null;
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
  retrieved?: Citation[];
  sources?: Citation[];
  index?: { state: string; detail?: string };
  // document_question: the filter Python built from who is asking
  filter?: { scope: string; doc_type?: string; branch?: string; semester?: number };
  // calendar_question: what was heard, the span it meant, what matched
  calendar?: {
    heard: Record<string, unknown>;
    range: string;
    mode: string;
    source: "calendar" | "seed";
    matched: number;
    year?: number;
  };
  holiday_calendar?: "calendar" | "seed" | "none";
  // mess_menu: the mess, day and meal heard, and what Python made of them
  mess?: {
    heard: Record<string, unknown>;
    resolved?: { mess: string; day: string; meal: string };
    source?: "saved" | "sample";
  };
  // request_od_leave, medical: the record that is waiting for its certificate
  attach_certificate?: string;
  [key: string]: unknown;
}

export interface ChatTurn {
  role: "user" | "assistant";
  content: string;
  trace?: Trace;
  pending?: boolean;
}

export interface Citation {
  source: string;
  section?: string | null;
  doc_id?: string | null; // "corpus" for the built-in regulations, DOC-… for uploads
  distance?: number;
}

export type DocType = "regulation" | "academic_calendar" | "syllabus" | "other";
export type DocStatus = "processing" | "indexed" | "needs_review" | "failed";

// Mirrors app/backends DOCUMENT_FIELDS.
export interface CampusDocument {
  id: string;
  title: string;
  filename: string;
  content_type: string | null;
  sha256: string;
  size: number;
  pages: number | null;
  doc_type: DocType;
  branch: string;
  semester: number | null;
  academic_year: string | null;
  status: DocStatus;
  error: string | null;
  chunks: number | null;
  uploaded_by: string;
  uploaded_at: string;
}

export type EventKind = "holiday" | "exam" | "semester_start" | "semester_end" | "other";

// Mirrors app/backends CALENDAR_EVENT_FIELDS.
export interface CalendarEvent {
  id: number | null;
  document_id: string | null;
  start_date: string | null;
  end_date: string | null;
  title: string;
  kind: EventKind;
  years: string; // "all" or "1,2"
  source_text?: string | null;
  page?: number | null;
  flags?: string[];
  include: boolean;
  confirmed: boolean;
}

export interface MessMenu {
  mess: string;
  official: boolean; // false: the sample, until the warden saves this mess's menu
  week: Record<string, Record<string, string[]>>; // day -> meal -> dishes
  timings: Record<string, string>;
  updated_by: string | null;
  updated_at: string | null;
}

export interface MessData {
  messes: string[];
  meals: string[];
  days: string[];
  mine: string | null;
  may_edit: boolean;
  menus: MessMenu[];
}
