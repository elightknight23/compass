import { useEffect, useState } from "react";
import { api } from "./api";
import { Chat } from "./components/Chat";
import { Documents, UpcomingCard } from "./components/Documents";
import { Icon, Mark } from "./components/Icon";
import { Login } from "./components/Login";
import { AttendanceCard, MyRecords } from "./components/Records";
import { StaffQueue } from "./components/StaffQueue";
import { day, initials, ROLE_NAMES } from "./format";
import type { CalendarEvent, User } from "./types";

type View = "chat" | "records" | "queue" | "documents";

const VIEWS: Record<View, { title: string; short: string; icon: string; blurb: string }> = {
  chat: { title: "Ask Compass", short: "Ask", icon: "chat", blurb: "Questions, requests and complaints, in your own words." },
  records: { title: "My requests", short: "Requests", icon: "requests", blurb: "Everything you've filed, and where it stands." },
  queue: { title: "Your queue", short: "Queue", icon: "queue", blurb: "Requests waiting on a decision from you." },
  documents: { title: "Documents", short: "Documents", icon: "documents", blurb: "The calendar, syllabi and regulations answers come from." },
};

/** The next dated thing on the calendar, as a tear-off date tile. Reloads
 * when the calendar in force changes, so a confirmed calendar shows at once. */
function NextDate({ refreshKey }: { refreshKey: number }) {
  const [next, setNext] = useState<CalendarEvent | null | undefined>(undefined);
  useEffect(() => {
    api
      .calendar()
      .then((r) => setNext(r.events.find((e) => e.kind !== "other") ?? null))
      .catch(() => setNext(null));
  }, [refreshKey]);
  if (!next?.start_date) return null;
  const d = new Date(next.start_date + "T00:00:00");
  return (
    <div className="next-date" title={`${day(next.start_date)} — ${next.title}`}>
      <div className={`tile ${next.kind}`}>
        <span className="tile-month">{d.toLocaleDateString("en-IN", { month: "short" })}</span>
        <span className="tile-day">{d.getDate()}</span>
      </div>
      <div className="next-text">
        <div className="eyebrow">Next on the calendar</div>
        <div className="next-title">{next.title}</div>
      </div>
    </div>
  );
}

export default function App() {
  const [user, setUser] = useState<User | null | undefined>(undefined);
  const [today, setToday] = useState<string>(new Date().toISOString().slice(0, 10));
  const [view, setView] = useState<View>("chat");
  // Bumped whenever chat changes a record, so the lists beside it refresh.
  const [refreshKey, setRefreshKey] = useState(0);
  const [chatKey, setChatKey] = useState(0);

  function loadMe() {
    api
      .me()
      .then((r) => {
        setUser(r.user);
        setToday(r.today);
      })
      .catch(() => setUser(null)); // 401, or the API is down: either way, sign in
  }
  useEffect(loadMe, []);

  if (user === undefined)
    return (
      <div className="boot">
        <Mark size={36} />
      </div>
    );
  if (user === null)
    return (
      <Login
        onLogin={(u) => {
          setUser(u);
          setView("chat");
          loadMe(); // for the server's date
        }}
      />
    );

  const isStaff = user.capabilities.length > 0;
  const isStudent = user.role === "student";
  const views: View[] = [
    "chat",
    ...(isStudent ? (["records"] as View[]) : []),
    ...(isStaff ? (["queue"] as View[]) : []),
    "documents",
  ];

  async function logout() {
    await api.logout().catch(() => undefined);
    setUser(null);
    setChatKey((k) => k + 1);
  }

  function newConversation() {
    setChatKey((k) => k + 1);
    setView("chat");
  }

  const meta = VIEWS[view];
  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <Mark size={28} />
          <span className="name">Compass</span>
        </div>

        <nav className="nav" aria-label="Sections">
          {views.map((v) => (
            <button
              key={v}
              className={`nav-item ${view === v ? "on" : ""}`}
              onClick={() => setView(v)}
              aria-current={view === v ? "page" : undefined}
            >
              <Icon name={VIEWS[v].icon} />
              <span className="nav-long">{VIEWS[v].title}</span>
              <span className="nav-short">{VIEWS[v].short}</span>
            </button>
          ))}
        </nav>

        <button className="new-chat" onClick={newConversation}>
          <Icon name="plus" size={16} /> New conversation
        </button>

        <div className="today">
          {isStudent && view !== "records" && <AttendanceCard refreshKey={refreshKey} compact />}
          <NextDate refreshKey={refreshKey} />
        </div>

        <div className="grow" />
        <div className="me">
          <div className="avatar">{initials(user.name)}</div>
          <div className="me-text">
            <strong>{user.name}</strong>
            <span className="muted small">
              {ROLE_NAMES[user.role] ?? user.role}
              {user.branch ? ` · ${user.branch}` : ""}
              {user.semester ? ` · sem ${user.semester}` : ""}
            </span>
          </div>
          <button className="icon-btn" onClick={logout} title="Sign out" aria-label="Sign out">
            <Icon name="out" size={17} />
          </button>
        </div>
      </aside>

      <main className="main">
        <header className="topbar">
          <div>
            <h1>{meta.title}</h1>
            <p className="muted">{meta.blurb}</p>
          </div>
          {view === "chat" && (
            <button className="btn small soft topbar-new" onClick={newConversation}>
              <Icon name="plus" size={15} /> New
            </button>
          )}
        </header>
        <div className="view">
          {/* Chat stays mounted across tabs so a conversation survives a look at the queue. */}
          <div hidden={view !== "chat"} className="view-fill">
            <Chat key={chatKey} user={user} today={today} onChanged={() => setRefreshKey((k) => k + 1)} />
          </div>
          {view === "records" && (
            <div className="scroll-pad">
              <div className="records-top">
                <AttendanceCard refreshKey={refreshKey} />
                <UpcomingCard />
              </div>
              <h2 className="h2">Complaints and leave</h2>
              <MyRecords refreshKey={refreshKey} />
            </div>
          )}
          {view === "queue" && (
            <div className="scroll-pad">
              <StaffQueue user={user} refreshKey={refreshKey} />
            </div>
          )}
          {view === "documents" && (
            <div className="scroll-pad wide">
              <Documents onChanged={() => setRefreshKey((k) => k + 1)} />
            </div>
          )}
        </div>
      </main>
    </div>
  );
}
