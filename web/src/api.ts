import type {
  Attendance, CalendarEvent, CampusDocument, CampusRecord, MessData, MessMenu, RecordEvent, Trace, User,
} from "./types";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public body: Record<string, unknown> = {},
  ) {
    super(message);
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  // A FormData body sets its own multipart Content-Type, boundary included.
  const json = !(init.body instanceof FormData);
  const res = await fetch(path, {
    credentials: "same-origin",
    ...init,
    headers: { ...(json ? { "Content-Type": "application/json" } : {}), ...(init.headers || {}) },
  });
  const text = await res.text();
  const body = text ? JSON.parse(text) : {};
  if (!res.ok) {
    throw new ApiError(res.status, String(body.detail ?? res.statusText), body);
  }
  return body as T;
}

export const api = {
  me: () => request<{ user: User; today: string }>("/api/me"),

  messMenus: () => request<MessData>("/api/mess"),
  saveMenu: (mess: string, week: Record<string, Record<string, string>>, timings: Record<string, string>) =>
    request<{ outcome: string; menu: MessMenu }>(`/api/mess/${encodeURIComponent(mess)}`, {
      method: "PUT",
      body: JSON.stringify({ week, timings }),
    }),
  importMenu: (mess: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<{ outcome: string; week: MessMenu["week"] }>(
      `/api/mess/${encodeURIComponent(mess)}/import`, { method: "POST", body: form },
    );
  },
  menuCsvUrl: (mess: string) => `/api/mess/${encodeURIComponent(mess)}.csv`,
  login: (username: string, password: string) =>
    request<{ user: User }>("/api/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    }),
  logout: () => request<{ ok: boolean }>("/api/logout", { method: "POST" }),

  myRecords: () => request<{ records: CampusRecord[]; backend: string }>("/api/records"),
  record: (id: string) =>
    request<{ record: CampusRecord; owner: User | null; events: RecordEvent[] }>(
      `/api/records/${encodeURIComponent(id)}`,
    ),
  attendance: () => request<Attendance>("/api/attendance/me"),

  queue: () =>
    request<{
      records: CampusRecord[];
      users: Record<string, User>;
      backend: string;
      scope: string[];
      errors: string[];
    }>("/api/queue"),
  attachCertificate: (id: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<{ outcome: string; record: CampusRecord }>(
      `/api/records/${encodeURIComponent(id)}/certificate`, { method: "POST", body: form },
    );
  },
  act: (id: string, action: string, grantException = false) =>
    request<{ record: CampusRecord; exception_granted: boolean }>(
      `/api/records/${encodeURIComponent(id)}/action`,
      { method: "POST", body: JSON.stringify({ action, grant_exception: grantException }) },
    ),

  documents: () => request<{ documents: CampusDocument[]; may_manage: boolean }>("/api/documents"),
  document: (id: string) =>
    request<{ data: CampusDocument }>(`/api/documents/${encodeURIComponent(id)}`),
  upload: (form: FormData) =>
    request<{ outcome: string; data: CampusDocument }>("/api/documents", { method: "POST", body: form }),
  deleteDocument: (id: string) =>
    request<{ data: CampusDocument }>(`/api/documents/${encodeURIComponent(id)}`, { method: "DELETE" }),
  calendarDraft: (id: string) =>
    request<{ data: { document: CampusDocument; events: CalendarEvent[] } }>(
      `/api/documents/${encodeURIComponent(id)}/events`,
    ),
  confirmCalendar: (id: string, events: CalendarEvent[]) =>
    request<{ data: { events: CalendarEvent[]; confirmed: number; holidays: number } }>(
      `/api/documents/${encodeURIComponent(id)}/events`,
      { method: "PUT", body: JSON.stringify({ events }) },
    ),
  calendar: (start?: string, end?: string) => {
    const q = new URLSearchParams();
    if (start) q.set("start", start);
    if (end) q.set("end", end);
    return request<{ source: "calendar" | "seed"; events: CalendarEvent[] }>(`/api/calendar?${q}`);
  },
  reindex: () =>
    request<{ structured: number; uploads: number; status: { state: string } }>(
      "/api/admin/reindex", { method: "POST" },
    ),
};

export const documentFileUrl = (id: string) => `/api/documents/${encodeURIComponent(id)}/file`;
export const certificateUrl = (id: string) => `/api/records/${encodeURIComponent(id)}/certificate`;

/**
 * One chat turn over Server-Sent Events.
 *
 * The server emits `classification` as soon as the classifier returns, then
 * `reply` with the answer and the full trace. EventSource can't POST, so the
 * stream is read off fetch's body and split on blank lines by hand.
 */
export async function chatStream(
  message: string,
  history: { role: string; content: string }[],
  onClassification: (t: Trace) => void,
): Promise<{ reply: string; debug: Trace }> {
  const res = await fetch("/api/chat", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify({ message, history }),
  });
  if (!res.ok || !res.body) {
    const body = await res.json().catch(() => ({}));
    throw new ApiError(res.status, String(body.detail ?? res.statusText), body);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let final: { reply: string; debug: Trace } | null = null;

  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut: number;
    while ((cut = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      let event = "message";
      let data = "";
      for (const line of block.split("\n")) {
        if (line.startsWith("event: ")) event = line.slice(7);
        else if (line.startsWith("data: ")) data += line.slice(6);
      }
      if (!data) continue;
      const payload = JSON.parse(data);
      if (event === "classification") onClassification(payload as Trace);
      else if (event === "reply") final = payload;
    }
  }
  if (!final) throw new ApiError(502, "The reply stream ended early.");
  return final;
}
