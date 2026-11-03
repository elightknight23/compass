import { useState } from "react";
import { api } from "../api";
import { initials } from "../format";
import type { User } from "../types";
import { Mark } from "./Icon";

// Published demo accounts (see scripts/seed_credentials.py). Listed here so
// the demo can switch personas in two clicks; the passwords are seeded and
// public on purpose, and that is named as a limitation in the README.
const DEMO = [
  { username: "ravi.kumar", password: "ravi-demo", who: "Ravi Kumar", role: "Student · CSE sem 5 · 76.2%" },
  { username: "arjun.menon", password: "arjun-demo", who: "Arjun Menon", role: "Student · below 75%" },
  { username: "priya.sharma", password: "priya-demo", who: "Priya Sharma", role: "Student · ECE sem 3" },
  { username: "meena.krishnan", password: "warden-demo", who: "Meena Krishnan", role: "Hostel warden" },
  { username: "lakshmi.narayanan", password: "advisor-demo", who: "Dr. Lakshmi Narayanan", role: "Faculty advisor" },
];

export function Login({ onLogin, message }: { onLogin: (u: User) => void; message?: string }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(message ?? null);
  const [busy, setBusy] = useState(false);

  async function submit(u = username, p = password) {
    setBusy(true);
    setError(null);
    try {
      onLogin((await api.login(u, p)).user);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login-wrap">
      <section className="login-cover" aria-hidden="true">
        <div className="cover-mark">
          <Mark size={46} />
          <span>Compass</span>
        </div>
        <div className="cover-copy">
          <p className="cover-big">
            One desk for the things
            <br />
            college makes you queue for.
          </p>
          <ul className="cover-list">
            <li>Leave and OD, worked out against your attendance</li>
            <li>Hostel, lab and transport complaints, tracked to the end</li>
            <li>The syllabus and the calendar, answered with the page they came from</li>
            <li>What IFC A to D are serving, today or any day</li>
          </ul>
        </div>
        <div className="cover-foot">Campus service desk</div>
      </section>

      <section className="login-panel">
        <div className="login">
          <h1>Sign in</h1>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              submit();
            }}
          >
            <label>
              Username
              <input value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" autoFocus />
            </label>
            <label>
              Password
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="current-password"
              />
            </label>
            {error && <div className="banner deny">{error}</div>}
            <button className="btn primary wide" disabled={busy || !username || !password}>
              {busy ? "Signing in…" : "Sign in"}
            </button>
          </form>
          <div className="demo">
            <div className="eyebrow">Or step in as</div>
            {DEMO.map((d) => (
              <button key={d.username} className="demo-acct" disabled={busy} onClick={() => submit(d.username, d.password)}>
                <span className="avatar">{initials(d.who)}</span>
                <span className="demo-text">
                  <strong>{d.who}</strong>
                  <span className="muted small">{d.role}</span>
                </span>
              </button>
            ))}
          </div>
        </div>
      </section>
    </div>
  );
}
