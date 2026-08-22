import type { PropsWithChildren, ReactNode } from "react";
import { Link } from "react-router-dom";

import { NeonEdge, useNeonEdge } from "./neon-edge";

const NEW_TAB = { target: "_blank", rel: "noreferrer noopener" } as const;

export function ExtLink({
  href,
  children,
  className = "",
}: {
  href: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <a href={href} {...NEW_TAB} className={className}>
      {children}
    </a>
  );
}

export function NeonGrid({ className = "" }: { className?: string }) {
  return (
    <div
      aria-hidden
      className={`pointer-events-none absolute inset-0 opacity-[0.35] [background-image:linear-gradient(rgba(34,211,238,0.09)_1px,transparent_1px),linear-gradient(90deg,rgba(34,211,238,0.09)_1px,transparent_1px)] [background-size:56px_56px] [mask-image:radial-gradient(ellipse_70%_60%_at_50%_0%,#000_20%,transparent_75%)] ${className}`}
    />
  );
}

export function Card({
  children,
  className = "",
}: PropsWithChildren<{ className?: string }>) {
  const ref = useNeonEdge<HTMLDivElement>();
  return (
    <div
      ref={ref}
      className={`relative isolate flex flex-col rounded-2xl border border-line bg-surface p-6 transition-all duration-200 hover:border-line-bright hover:bg-raised ${className}`}
    >
      <NeonEdge />
      {children}
    </div>
  );
}

export function Pill({
  children,
  tone = "cyan",
}: PropsWithChildren<{ tone?: "cyan" | "violet" | "lime" | "magenta" }>) {
  const box = {
    cyan: "border-cyan/40 [background-color:rgba(34,211,238,0.08)]",
    violet: "border-violet/50 [background-color:rgba(138,43,226,0.14)]",
    lime: "border-lime/40 [background-color:rgba(215,255,100,0.08)]",
    magenta: "border-magenta/40 [background-color:rgba(255,47,208,0.08)]",
  } as const;
  const ink = {
    cyan: "text-cyan",
    violet: "text-violet",
    lime: "text-lime",
    magenta: "text-magenta",
  } as const;
  return (
    <span className={`self-start rounded-full border px-3 py-1 ${box[tone]}`}>
      <span className={`font-mono text-xs tracking-wide ${ink[tone]}`}>{children}</span>
    </span>
  );
}

export function Divider({ className = "" }: { className?: string }) {
  return <div className={`h-px bg-line ${className}`} />;
}

export function A({
  href,
  children,
  className = "",
  external,
}: {
  href: string;
  children: ReactNode;
  className?: string;
  external?: boolean;
}) {
  const isExternal = external ?? /^https?:/.test(href);
  const base =
    "text-cyan underline decoration-cyan/30 underline-offset-4 transition-colors duration-150 hover:decoration-cyan focus-visible:outline-none focus-visible:[outline:2px_solid_#22D3EE] focus-visible:[outline-offset:3px] rounded-sm";

  if (isExternal) {
    return (
      <ExtLink href={href} className={`${base} ${className}`}>
        {children}
      </ExtLink>
    );
  }
  return (
    <Link to={href} className={`${base} ${className}`}>
      {children}
    </Link>
  );
}

export function Shell({
  children,
  className = "",
}: PropsWithChildren<{ className?: string }>) {
  return (
    <div className={`mx-auto flex w-full max-w-shell px-5 sm:px-8 ${className}`}>
      {children}
    </div>
  );
}
