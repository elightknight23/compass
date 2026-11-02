import { useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api";
import type { MessData, MessMenu } from "../types";
import { Icon } from "./Icon";

// The mess menu. Everyone reads every mess; the hostel warden edits. As
// everywhere else, hiding the editor from others is a courtesy — app/mess.py
// refuses anyone else's save itself.

const DAY_SHORT: Record<string, string> = {
  monday: "Mon", tuesday: "Tue", wednesday: "Wed", thursday: "Thu", friday: "Fri", saturday: "Sat", sunday: "Sun",
};

function weekdayOf(iso: string): string {
  const d = new Date(iso + "T00:00:00");
  return ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"][d.getDay()];
}

const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

function MealCard({ meal, items, timing }: { meal: string; items: string[]; timing?: string }) {
  return (
    <div className={`meal meal-${meal}`}>
      <div className="meal-head">
        <strong>{cap(meal)}</strong>
        {timing && <span className="muted small">{timing}</span>}
      </div>
      {items.length ? (
        <ul className="dishes">{items.map((d, i) => <li key={i}>{d}</li>)}</ul>
      ) : (
        <div className="muted small">Nothing listed.</div>
      )}
    </div>
  );
}

/** The warden's editor: one dish per line, a CSV import that fills it, and Save. */
function MenuEditor({ menu, data, onSaved, onCancel }: {
  menu: MessMenu; data: MessData; onSaved: (m: MessMenu) => void; onCancel: () => void;
}) {
  const toText = (m: MessMenu) =>
    Object.fromEntries(data.days.map((d) => [d, Object.fromEntries(data.meals.map((meal) => [meal, m.week[d][meal].join("\n")]))]));
  const [week, setWeek] = useState<Record<string, Record<string, string>>>(() => toText(menu));
  const [timings, setTimings] = useState<Record<string, string>>({ ...menu.timings });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  async function importCsv(file: File | undefined) {
    if (!file) return;
    setError(null);
    try {
      const res = await api.importMenu(menu.mess, file);
      setWeek(toText({ ...menu, week: res.week }));
      setNote(`Read ${file.name}. Check it below, then Save — nothing is saved yet.`);
    } catch (e) {
      setError((e as ApiError).message);
    }
  }

  async function save() {
    setBusy(true);
    setError(null);
    try {
      const res = await api.saveMenu(menu.mess, week, timings);
      onSaved(res.menu);
    } catch (e) {
      setError((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card menu-editor">
      <div className="review-head">
        <div>
          <strong>Edit {menu.mess}'s menu</strong>
          <div className="muted small">One dish per line. Students see it as soon as you save.</div>
        </div>
        <div className="editor-tools">
          <label className="btn small soft file-btn">
            <Icon name="clip" size={14} /> Import CSV
            <input type="file" accept=".csv,text/csv" aria-label="import a CSV menu"
                   onChange={(e) => { importCsv(e.target.files?.[0]); e.target.value = ""; }} />
          </label>
          <a className="btn small" href={api.menuCsvUrl(menu.mess)} download>Download CSV</a>
        </div>
      </div>
      {error && <div className="banner deny">{error}</div>}
      {note && <div className="banner ok">{note}</div>}
      <div className="timings">
        {data.meals.map((m) => (
          <label key={m}>{cap(m)} time
            <input value={timings[m] ?? ""} placeholder="e.g. 7:30 – 9:30" aria-label={`${m} time`}
                   onChange={(e) => setTimings((t) => ({ ...t, [m]: e.target.value }))} />
          </label>
        ))}
      </div>
      <div className="review-scroll">
        <table className="menu-edit-table">
          <thead><tr><th />{data.meals.map((m) => <th key={m}>{cap(m)}</th>)}</tr></thead>
          <tbody>
            {data.days.map((d) => (
              <tr key={d}>
                <th scope="row">{DAY_SHORT[d]}</th>
                {data.meals.map((m) => (
                  <td key={m}>
                    <textarea rows={4} value={week[d][m]} aria-label={`${d} ${m}`}
                              onChange={(e) => setWeek((w) => ({ ...w, [d]: { ...w[d], [m]: e.target.value } }))} />
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="row-end">
        <button className="btn" onClick={onCancel}>Cancel</button>
        <button className="btn primary" disabled={busy} onClick={save}>{busy ? "Saving…" : "Save menu"}</button>
      </div>
    </div>
  );
}

export function Mess({ today }: { today: string }) {
  const [data, setData] = useState<MessData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [mess, setMess] = useState<string | null>(null);
  const [day, setDay] = useState(() => weekdayOf(today));
  const [editing, setEditing] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    api
      .messMenus()
      .then((d) => {
        setData(d);
        setMess((m) => m ?? d.mine ?? d.messes[0]);
      })
      .catch((e) => setError(e.message));
  }, []);
  useEffect(() => setDay(weekdayOf(today)), [today]);

  const menu = useMemo(() => data?.menus.find((m) => m.mess === mess) ?? null, [data, mess]);
  const todayName = weekdayOf(today);

  if (error) return <div className="banner deny">{error}</div>;
  if (!data || !menu) return <div className="muted">Loading the menu…</div>;

  function saved(m: MessMenu) {
    setData((d) => d && { ...d, menus: d.menus.map((x) => (x.mess === m.mess ? m : x)) });
    setEditing(false);
    setNotice(`${m.mess}'s menu is saved. Students see it now, and chat answers from it.`);
  }

  return (
    <div className="mess">
      <div className="mess-top">
        <div className="seg" role="tablist" aria-label="Mess">
          {data.messes.map((m) => (
            <button key={m} role="tab" aria-selected={m === mess} className={m === mess ? "on" : ""}
                    onClick={() => { setMess(m); setEditing(false); setNotice(null); }}>
              {m}{m === data.mine && <span className="home-dot" title="your mess" />}
            </button>
          ))}
        </div>
        {data.may_edit && !editing && (
          <button className="btn small soft" onClick={() => setEditing(true)}>Edit {menu.mess}'s menu</button>
        )}
      </div>
      <p className="muted small mess-note">
        {data.mine ? (
          mess === data.mine ? "Your mess." : `Your mess is ${data.mine}.`
        ) : data.may_edit ? (
          "You keep these menus. Students and chat read them as soon as you save."
        ) : (
          "You don't have a home mess on file — pick any to see its menu."
        )}
        {!menu.official && <> <span className="chip small warn">sample menu</span> The official menu hasn't been entered yet.</>}
      </p>
      {notice && <div className="banner ok">{notice}</div>}

      {editing ? (
        <MenuEditor key={menu.mess} menu={menu} data={data} onSaved={saved} onCancel={() => setEditing(false)} />
      ) : (
        <>
          <div className="day-chips" role="tablist" aria-label="Day">
            {data.days.map((d) => (
              <button key={d} role="tab" aria-selected={d === day} className={`day-chip ${d === day ? "on" : ""}`}
                      onClick={() => setDay(d)}>
                {DAY_SHORT[d]}{d === todayName && <span className="today-mark">today</span>}
              </button>
            ))}
          </div>
          <div className="meals">
            {data.meals.map((m) => (
              <MealCard key={m} meal={m} items={menu.week[day][m]} timing={menu.timings[m]} />
            ))}
          </div>

          <details className="week">
            <summary>The whole week at {menu.mess}</summary>
            <div className="review-scroll">
              <table className="week-table">
                <thead><tr><th />{data.meals.map((m) => <th key={m}>{cap(m)}</th>)}</tr></thead>
                <tbody>
                  {data.days.map((d) => (
                    <tr key={d} className={d === todayName ? "today" : ""}>
                      <th scope="row">{DAY_SHORT[d]}</th>
                      {data.meals.map((m) => <td key={m}>{menu.week[d][m].join(", ") || "—"}</td>)}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </details>
        </>
      )}
    </div>
  );
}
