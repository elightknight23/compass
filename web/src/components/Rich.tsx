import { Fragment, type ReactNode } from "react";

// A very small markdown subset: **bold**, *italic*, paragraphs and "- " list
// lines — built as React elements, never as HTML.
//
// That constraint is why this exists instead of a markdown library. A reply
// can echo model output and retrieved regulation text, and the corpus holds
// a deliberately planted injection. Bold arriving as literal ** is cosmetic;
// a document getting to choose its own markup would not be.
const INLINE = /\*\*([^*]+)\*\*|\*([^*\n]+)\*/g;

function inline(text: string): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let m: RegExpExecArray | null;
  INLINE.lastIndex = 0;
  while ((m = INLINE.exec(text)) !== null) {
    if (m.index > last) out.push(text.slice(last, m.index));
    out.push(m[1] ? <strong key={m.index}>{m[1]}</strong> : <em key={m.index}>{m[2]}</em>);
    last = INLINE.lastIndex;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

// Models write bullets as "* " (often "*   ") as readily as "- ". Read as
// emphasis, that leading star pairs with the next one and italicises half
// a line, leaving stray stars behind; so every bullet becomes "- " first.
const BULLET = /^\s*[*•]\s+|^\s+-\s+/;

export function Rich({ text }: { text: string }) {
  const paras = (text || "").split(/\n{2,}/).map((p) => p.trim()).filter(Boolean);
  return (
    <>
      {paras.map((para, i) => {
        const lines = para.split("\n").map((l) => l.replace(BULLET, "- "));
        const items = lines.filter((l) => l.startsWith("- "));
        if (items.length && items.length === lines.length - (lines[0].startsWith("- ") ? 0 : 1)) {
          const lead = lines[0].startsWith("- ") ? null : lines[0];
          return (
            <Fragment key={i}>
              {lead && <p className="para">{inline(lead)}</p>}
              <ul className="rich-list">
                {items.map((l, j) => (
                  <li key={j}>{inline(l.slice(2))}</li>
                ))}
              </ul>
            </Fragment>
          );
        }
        return (
          <p className="para" key={i}>
            {lines.map((l, j) => (
              <Fragment key={j}>
                {j > 0 && <br />}
                {inline(l)}
              </Fragment>
            ))}
          </p>
        );
      })}
    </>
  );
}
