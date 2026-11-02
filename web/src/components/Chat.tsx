import { useEffect, useRef, useState } from "react";
import { chatStream } from "../api";
import { firstName, longDay } from "../format";
import type { ChatTurn, Trace as TraceT, User } from "../types";
import { CertificateUpload } from "./Records";
import { Icon, Mark } from "./Icon";
import { Rich } from "./Rich";
import { Trace } from "./Trace";

const HISTORY_TURNS = 6; // matches app/graph.py — the classifier's sliding window

// Starting points, grouped by what the person came to do. Each group is a
// different kind of answer: read from documents, worked out by code, or a
// record created or changed.
type Group = { title: string; hint: string; prompts: string[] };
const GROUPS: Record<string, Group[]> = {
  student: [
    { title: "Look something up", hint: "answered from the College's documents",
      prompts: ["What's the minimum attendance I need?", "What's the textbook for Computer Networks?"] },
    { title: "Dates and meals", hint: "worked out in code",
      prompts: ["When is the next holiday?", "Any exams in November?", "What's for dinner tonight?"] },
    { title: "Apply or report", hint: "creates a request",
      prompts: ["I need OD next Monday to Wednesday for a symposium",
                "I was sick yesterday, apply medical leave",
                "The fan in my hostel room isn't working"] },
    { title: "Follow up", hint: "your own records",
      prompts: ["What's my attendance right now?", "Any update on my requests?"] },
  ],
  hostel_warden: [
    { title: "Complaints", hint: "your inbox",
      prompts: ["Show me Arjun's complaints", "Resolve CMP-1001, the electrician fixed it"] },
    { title: "Try the boundary", hint: "staff, but not for leave",
      prompts: ["Approve Ravi's OD request"] },
    { title: "Look something up", hint: "the regulations and the mess",
      prompts: ["What time do the hostel gates close?", "What's for lunch at IFC B tomorrow?"] },
  ],
  faculty_advisor: [
    { title: "Leave", hint: "your inbox",
      prompts: ["Approve Ravi's OD request", "Approve OD-2002", "What's Arjun's attendance?"] },
    { title: "Look something up", hint: "documents and the calendar",
      prompts: ["Is OD counted as present?", "Any holidays in November?"] },
  ],
};

function Turn({
  turn,
  onToggle,
  open,
  onChanged,
}: {
  turn: ChatTurn;
  onToggle: () => void;
  open: boolean;
  onChanged: () => void;
}) {
  if (turn.role === "user") {
    return (
      <div className="msg user">
        <div className="bubble">{turn.content}</div>
      </div>
    );
  }
  const attach = !turn.pending ? turn.trace?.attach_certificate : undefined;
  return (
    <div className="msg bot">
      <div className="bot-mark">
        <Mark size={22} />
      </div>
      <div className="bot-body">
        {turn.pending && !turn.content ? (
          <div className="note typing">
            <span /> <span /> <span />
            {turn.trace?.intent && <em className="heard-pill">heard: {turn.trace.intent}</em>}
          </div>
        ) : (
          <div className="note">
            <Rich text={turn.content} />
          </div>
        )}
        {attach && <CertificateUpload recordId={attach} onAttached={onChanged} />}
        {turn.trace && (
          <>
            <button className="reason-btn" onClick={onToggle} aria-expanded={open}>
              <span className="reason-label">{open ? "Hide the working" : "Show the working"}</span>
              {turn.trace.intent && <span className="chip small muted">{turn.trace.intent}</span>}
              {turn.trace.authz === "denied" && <span className="chip small deny">refused</span>}
            </button>
            {open && <Trace d={turn.trace} live={turn.pending} />}
          </>
        )}
      </div>
    </div>
  );
}

export function Chat({ user, today, onChanged }: { user: User; today: string; onChanged: () => void }) {
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [open, setOpen] = useState<Set<number>>(new Set());
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const end = useRef<HTMLDivElement>(null);
  const box = useRef<HTMLTextAreaElement>(null);

  // Braced on purpose: newer browsers return a Promise from scrollIntoView,
  // and an effect that returns anything but a function crashes on unmount.
  // An empty conversation stays at the top: on a phone the starters are
  // taller than the screen, and scrolling would hide the greeting.
  useEffect(() => {
    if (turns.length) end.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [turns]);

  // The composer grows with what's typed, up to a point.
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
  }, [draft]);

  async function send(text: string) {
    const message = text.trim();
    if (!message || busy) return;
    setDraft("");
    setBusy(true);

    const history = turns
      .filter((t) => !t.pending)
      .slice(-HISTORY_TURNS)
      .map((t) => ({ role: t.role, content: t.content }));
    const botIndex = turns.length + 1;
    setTurns((ts) => [...ts, { role: "user", content: message }, { role: "assistant", content: "", pending: true }]);

    const patch = (p: Partial<ChatTurn>) =>
      setTurns((ts) => ts.map((t, i) => (i === botIndex ? { ...t, ...p } : t)));

    try {
      const final = await chatStream(message, history, (cls: TraceT) => patch({ trace: cls }));
      patch({ content: final.reply, trace: final.debug, pending: false });
      // The two moments the working is the point — dates resolved in code,
      // and a refusal decided by an if statement — open it unasked.
      if (final.debug.dates || final.debug.authz === "denied") setOpen((s) => new Set(s).add(botIndex));
      if (final.debug.outcome && ["created", "updated", "cancelled"].includes(final.debug.outcome)) {
        onChanged(); // a record moved — refresh the lists beside the chat
      }
    } catch (e) {
      patch({
        content: `I couldn't get an answer just now (${(e as Error).message}). Nothing was changed.`,
        pending: false,
      });
    } finally {
      setBusy(false);
      box.current?.focus();
    }
  }

  const groups = GROUPS[user.role] ?? [];
  return (
    <div className="chat">
      <div className="chat-scroll">
        {turns.length === 0 && (
          <div className="welcome">
            <div className="dateline">{longDay(today)}</div>
            <h2>
              Hello, {firstName(user.name)}. <span className="soft">What do you need today?</span>
            </h2>
            <p className="muted lede">
              Ask in your own words. Every answer has its working underneath — what the model heard,
              and what the code decided.
            </p>
            <div className="starter-grid">
              {groups.map((g) => (
                <section key={g.title} className="starter">
                  <h3>{g.title}</h3>
                  <div className="starter-hint">{g.hint}</div>
                  {g.prompts.map((p) => (
                    <button key={p} className="prompt" onClick={() => send(p)} disabled={busy}>
                      {p}
                    </button>
                  ))}
                </section>
              ))}
            </div>
          </div>
        )}
        <div className="thread">
          {turns.map((t, i) => (
            <Turn
              key={i}
              turn={t}
              open={open.has(i)}
              onChanged={onChanged}
              onToggle={() =>
                setOpen((s) => {
                  const n = new Set(s);
                  if (n.has(i)) n.delete(i);
                  else n.add(i);
                  return n;
                })
              }
            />
          ))}
        </div>
        <div ref={end} />
      </div>
      <form
        className="composer"
        onSubmit={(e) => {
          e.preventDefault();
          send(draft);
        }}
      >
        <textarea
          ref={box}
          value={draft}
          placeholder="Write to Compass…"
          rows={1}
          aria-label="Message"
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              send(draft);
            }
          }}
        />
        <button className="send" disabled={busy || !draft.trim()} aria-label="Send">
          <Icon name="send" size={18} />
        </button>
      </form>
      <div className="composer-note">Enter to send · Shift+Enter for a new line</div>
    </div>
  );
}
