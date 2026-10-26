import { useEffect, useRef, useState } from "react";
import { chatStream } from "../api";
import type { ChatTurn, Trace as TraceT, User } from "../types";
import { Rich } from "./Rich";
import { Trace } from "./Trace";

const HISTORY_TURNS = 6; // matches app/graph.py — the classifier's sliding window

const PROMPTS: Record<string, string[]> = {
  student: [
    "What's the minimum attendance I need?",
    "What's my attendance right now?",
    "I need OD next Monday to Wednesday for a symposium",
    "The fan in my hostel room isn't working",
    "Any update on my requests?",
  ],
  hostel_warden: [
    "Show me Arjun's complaints",
    "Resolve CMP-1001, the electrician fixed it",
    "Approve Ravi's OD request",
    "What time do the hostel gates close?",
  ],
  faculty_advisor: [
    "Approve Ravi's OD request",
    "What's Arjun's attendance?",
    "Approve OD-2002",
    "Is OD counted as present?",
  ],
};

function Turn({ turn, onToggle, open }: { turn: ChatTurn; onToggle: () => void; open: boolean }) {
  if (turn.role === "user") {
    return (
      <div className="msg user">
        <div className="bubble">{turn.content}</div>
      </div>
    );
  }
  return (
    <div className="msg bot">
      <div className="avatar">C</div>
      <div className="bot-body">
        {turn.pending && !turn.content ? (
          <div className="bubble typing">
            <span /> <span /> <span />
            {turn.trace?.intent && <em className="heard-pill">heard: {turn.trace.intent}</em>}
          </div>
        ) : (
          <div className="bubble">
            <Rich text={turn.content} />
          </div>
        )}
        {turn.trace && (
          <>
            <button className="reason-btn" onClick={onToggle}>
              {open ? "Hide reasoning" : "Show reasoning"}
              {turn.trace.intent && <span className="chip small">{turn.trace.intent}</span>}
              {turn.trace.authz === "denied" && <span className="chip small deny">denied</span>}
            </button>
            {open && <Trace d={turn.trace} live={turn.pending} />}
          </>
        )}
      </div>
    </div>
  );
}

export function Chat({ user, onChanged }: { user: User; onChanged: () => void }) {
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [open, setOpen] = useState<Set<number>>(new Set());
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const end = useRef<HTMLDivElement>(null);

  // Braced on purpose: newer browsers return a Promise from scrollIntoView,
  // and an effect that returns anything but a function crashes on unmount.
  useEffect(() => {
    end.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [turns]);

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
    setOpen((s) => new Set(s).add(botIndex)); // the seam is worth watching arrive

    const patch = (p: Partial<ChatTurn>) =>
      setTurns((ts) => ts.map((t, i) => (i === botIndex ? { ...t, ...p } : t)));

    try {
      const final = await chatStream(message, history, (cls: TraceT) => patch({ trace: cls }));
      patch({ content: final.reply, trace: final.debug, pending: false });
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
    }
  }

  return (
    <div className="chat">
      <div className="chat-scroll">
        {turns.length === 0 && (
          <div className="empty">
            <h2>Hi {user.name.replace(/^Dr\.\s*/, "").split(" ")[0]} — what do you need?</h2>
            <p className="muted">
              Ask about the regulations, file a complaint, apply for OD or leave, or check on
              something you've already filed. Open "Show reasoning" on any answer to see what the
              model heard and what the code decided.
            </p>
            <div className="prompts">
              {(PROMPTS[user.role] ?? []).map((p) => (
                <button key={p} className="prompt" onClick={() => send(p)}>
                  {p}
                </button>
              ))}
            </div>
          </div>
        )}
        {turns.map((t, i) => (
          <Turn
            key={i}
            turn={t}
            open={open.has(i)}
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
          value={draft}
          placeholder="Message Compass…"
          rows={1}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              send(draft);
            }
          }}
        />
        <button className="send" disabled={busy || !draft.trim()} aria-label="Send">
          ↑
        </button>
      </form>
    </div>
  );
}
