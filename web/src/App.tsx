import { useEffect, useState } from "react";
import { api } from "./api";
import { Chat } from "./components/Chat";
import { Login } from "./components/Login";
import { AttendanceCard, MyRecords } from "./components/Records";
import { StaffQueue } from "./components/StaffQueue";
import { initials, ROLE_NAMES } from "./format";
import type { User } from "./types";

type View = "chat" | "records" | "queue";

const VIEW_TITLE: Record<View, string> = {
  chat: "Ask Compass",
  records: "My requests",
  queue: "Staff queue",
};

export default function App() {
  const [user, setUser] = useState<User | null | undefined>(undefined);
  const [view, setView] = useState<View>("chat");
  // Bumped whenever chat changes a record, so the lists beside it refresh.
  const [refreshKey, setRefreshKey] = useState(0);
  const [chatKey, setChatKey] = useState(0);

  useEffect(() => {
    api
      .me()
      .then((r) => setUser(r.user))
      .catch(() => setUser(null)); // 401, or the API is down: either way, sign in
  }, []);

  if (user === undefined) return <div className="boot">Compass</div>;
  if (user === null) return <Login onLogin={(u) => { setUser(u); setView("chat"); }} />;

  const isStaff = user.capabilities.length > 0;
  const isStudent = user.role === "student";
  const views: View[] = ["chat", ...(isStudent ? (["records"] as View[]) : []), ...(isStaff ? (["queue"] as View[]) : [])];

  async function logout() {
    await api.logout().catch(() => undefined);
    setUser(null);
    setChatKey((k) => k + 1);
  }

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <div className="mark">C</div>
          <div>
            <div className="name">Compass</div>
            <span className="sub">Campus service desk</span>
          </div>
        </div>
        <nav>
          {views.map((v) => (
            <button key={v} className={`nav ${view === v ? "on" : ""}`} onClick={() => setView(v)}>
              {VIEW_TITLE[v]}
            </button>
          ))}
          <button className="nav subtle" onClick={() => { setChatKey((k) => k + 1); setView("chat"); }}>
            + New conversation
          </button>
        </nav>
        {isStudent && view !== "records" && (
          <div className="side-att">
            <AttendanceCard refreshKey={refreshKey} />
          </div>
        )}
        <div className="grow" />
        <div className="me">
          <div className="avatar-lg">{initials(user.name)}</div>
          <div className="me-text">
            <strong>{user.name}</strong>
            <span className="muted small">
              {ROLE_NAMES[user.role] ?? user.role}
              {user.section ? ` · ${user.section}` : ""}
            </span>
          </div>
          <button className="link" onClick={logout}>
            Sign out
          </button>
        </div>
      </aside>

      <main className="main">
        <header className="topbar">
          <h1>{VIEW_TITLE[view]}</h1>
          {isStaff && (
            <span className="muted small">
              may act on: {user.capabilities.map((c) => (c === "od_request" ? "leave" : "complaints")).join(", ")}
            </span>
          )}
        </header>
        <div className="view">
          {/* Chat stays mounted across tabs so a conversation survives a look at the queue. */}
          <div hidden={view !== "chat"} className="view-fill">
            <Chat key={chatKey} user={user} onChanged={() => setRefreshKey((k) => k + 1)} />
          </div>
          {view === "records" && (
            <div className="scroll-pad">
              <AttendanceCard refreshKey={refreshKey} />
              <h2 className="h2">Complaints and leave requests</h2>
              <MyRecords refreshKey={refreshKey} />
            </div>
          )}
          {view === "queue" && (
            <div className="scroll-pad">
              <StaffQueue user={user} refreshKey={refreshKey} />
            </div>
          )}
        </div>
      </main>
    </div>
  );
}
