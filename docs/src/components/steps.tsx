import type { PropsWithChildren } from "react";

export function Steps({ children }: PropsWithChildren) {
  return <ol className="mb-6 flex flex-col">{children}</ol>;
}

export function Step({
  n,
  title,
  last,
  children,
}: PropsWithChildren<{ n: number; title: string; last?: boolean }>) {
  return (
    <li className="flex flex-row">
      <div className="mr-5 flex flex-col items-center">
        <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full border border-cyan/50 bg-surface [box-shadow:0_0_16px_rgba(34,211,238,0.25)]">
          <span className="font-sans text-sm text-cyan">{n}</span>
        </div>
        {last ? null : <div className="w-px flex-1 bg-line" />}
      </div>
      <div className={`flex min-w-0 flex-1 flex-col ${last ? "pb-0" : "pb-8"}`}>
        <p className="mb-2 font-sans text-base font-semibold text-ink">{title}</p>
        {children}
      </div>
    </li>
  );
}
