import type { PropsWithChildren } from "react";

type Kind = "note" | "tip" | "warn";

const STYLES: Record<Kind, { edge: string; ink: string; label: string }> = {
  note: { edge: "border-l-cyan", ink: "text-cyan", label: "Note" },
  tip: { edge: "border-l-lime", ink: "text-lime", label: "Tip" },
  warn: { edge: "border-l-major", ink: "text-major", label: "Careful" },
};

export function Callout({
  kind = "note",
  title,
  children,
}: PropsWithChildren<{ kind?: Kind; title?: string }>) {
  const s = STYLES[kind];
  return (
    <aside className={`mb-5 rounded-r-lg border-l-2 bg-surface px-5 py-4 ${s.edge}`}>
      <p className={`mb-1 font-sans text-xs uppercase tracking-widest ${s.ink}`}>
        {title ?? s.label}
      </p>
      <div>{children}</div>
    </aside>
  );
}
