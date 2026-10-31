// A handful of line icons, drawn here rather than pulled from a library:
// one stroke weight, round caps, currentColor — they take the ink of
// whatever they sit in.
const PATHS: Record<string, string> = {
  chat: "M4 5.5h16v10H9l-5 4v-14z",
  requests: "M7 4h10v16H7zM10 8h4M10 12h4M10 16h2",
  queue: "M4 13l2.5-7h11l2.5 7v6H4zM4 13h5l1 2h4l1-2h5",
  documents: "M6 3.5h8l4 4v13H6zM14 3.5v4h4M9 12h6M9 16h6",
  plus: "M12 5v14M5 12h14",
  out: "M14 4h5v16h-5M10 8l-4 4 4 4M6 12h10",
  clip: "M15.5 7.5l-6.4 6.4a2 2 0 002.8 2.8l6.7-6.7a4 4 0 00-5.6-5.6L6.3 11.1a6 6 0 008.5 8.5L19 15.4",
  link: "M14 5h5v5M19 5l-8 8M18 14v5H5V6h5",
  check: "M5 12.5l4.5 4.5L19 7.5",
  calendar: "M4.5 6h15v14h-15zM4.5 10h15M9 3.5v4M15 3.5v4",
  send: "M5 12h13M13 6l6 6-6 6",
};

export function Icon({ name, size = 18 }: { name: keyof typeof PATHS | string; size?: number }) {
  return (
    <svg
      className="icon"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d={PATHS[name] ?? ""} />
    </svg>
  );
}

/** The mark: a compass rose, four points and a ring, in ink. */
export function Mark({ size = 30 }: { size?: number }) {
  return (
    <svg className="mark" width={size} height={size} viewBox="0 0 32 32" aria-hidden="true">
      <circle cx="16" cy="16" r="13" fill="none" stroke="currentColor" strokeWidth="1.3" opacity="0.45" />
      <path d="M16 4.5l2.6 11.5L16 27.5l-2.6-11.5z" fill="currentColor" />
      <path d="M4.5 16l11.5-2.2L27.5 16 16 18.2z" fill="currentColor" opacity="0.4" />
      <circle cx="16" cy="16" r="1.6" style={{ fill: "var(--paper)" }} />
    </svg>
  );
}
