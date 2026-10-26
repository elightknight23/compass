import { useState } from "react";
import { api } from "../api";
import type { User } from "../types";

// Published demo accounts (see scripts/seed_credentials.py). Listed here so
// the demo can switch personas in two clicks; the passwords are seeded and
// public on purpose, and that is named as a limitation in the README.
const DEMO = [
  { username: "ravi.kumar", password: "ravi-demo", who: "Ravi Kumar", role: "Student · 76.2%" },
  { username: "arjun.menon", password: "arjun-demo", who: "Arjun Menon", role: "Student · below 75%" },
  { username: "priya.sharma", password: "priya-demo", who: "Priya Sharma", role: "Student" },
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
      <div className="login card">
        <div className="brand big">
          <div className="mark">C</div>
          <div>
            <div className="name">Compass</div>
            <span className="sub">Campus service desk</span>
          </div>
        </div>
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
          <div className="muted small">Demo accounts</div>
          {DEMO.map((d) => (
            <button key={d.username} className="demo-acct" disabled={busy} onClick={() => submit(d.username, d.password)}>
              <strong>{d.who}</strong>
              <span className="muted">{d.role}</span>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
