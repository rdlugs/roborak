import type { PropsWithChildren, ReactNode } from "react";

function slug(children: ReactNode): string | undefined {
  return typeof children === "string"
    ? children
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, "-")
        .replace(/^-|-$/g, "")
    : undefined;
}

export function H1({ children }: PropsWithChildren) {
  return (
    <div className="mb-5">
      <h1 className="font-sans text-3xl font-bold leading-tight text-ink sm:text-4xl">
        {children}
      </h1>
      <div className="mt-4 h-px w-24 bg-cyan [box-shadow:0_0_12px_rgba(34,211,238,0.7)]" />
    </div>
  );
}

export function H2({ children }: PropsWithChildren) {
  return (
    <h2
      id={slug(children)}
      className="mb-3 mt-12 scroll-mt-24 font-sans text-xl font-semibold text-ink sm:text-2xl"
    >
      <span className="text-cyan">## </span>
      {children}
    </h2>
  );
}

export function H3({ children }: PropsWithChildren) {
  return (
    <h3 id={slug(children)} className="mb-2 mt-8 scroll-mt-24 font-sans text-base font-semibold text-ink">
      {children}
    </h3>
  );
}

export function Lead({ children }: PropsWithChildren) {
  return <p className="mb-8 font-sans text-lg leading-8 text-muted">{children}</p>;
}

export function P({ children }: PropsWithChildren) {
  return <p className="mb-4 font-sans text-[15px] leading-7 text-ink/90">{children}</p>;
}

export function Muted({ children }: PropsWithChildren) {
  return <p className="mb-4 font-sans text-sm leading-6 text-muted">{children}</p>;
}

export function Ul({ children }: PropsWithChildren) {
  return <ul className="mb-5 flex flex-col gap-2">{children}</ul>;
}

export function Li({ children }: PropsWithChildren) {
  return (
    <li className="flex flex-row gap-3">
      <span aria-hidden className="mt-[2px] font-mono text-sm text-cyan">
        ▸
      </span>
      <span className="min-w-0 flex-1 font-sans text-[15px] leading-7 text-ink/90">
        {children}
      </span>
    </li>
  );
}

export function Code({ children }: PropsWithChildren) {
  return (
    <code className="rounded border border-line bg-raised px-[5px] py-[1px] font-mono text-[13px] text-lime">
      {children}
    </code>
  );
}

export function Rule() {
  return <hr className="my-10 h-px border-0 bg-line" />;
}
