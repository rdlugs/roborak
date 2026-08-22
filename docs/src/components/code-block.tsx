import { useState } from "react";

import { NeonEdge, useNeonEdge } from "./neon-edge";

type Props = {
  code: string;
  label?: string;
  shell?: boolean;
};

export function CodeBlock({ code, label, shell }: Props) {
  const [copied, setCopied] = useState(false);
  const ref = useNeonEdge<HTMLDivElement>();

  async function copy() {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch {}
  }

  return (
    <div
      ref={ref}
      className="relative isolate mb-5 overflow-hidden rounded-xl border border-line bg-[#070911]"
    >
      <NeonEdge />
      <div className="flex flex-row items-center justify-between border-b border-line bg-surface px-4 py-2">
        <span className="font-mono text-xs text-faint">{label ?? (shell ? "shell" : "text")}</span>
        <button
          type="button"
          onClick={copy}
          aria-label={copied ? "Copied" : "Copy to clipboard"}
          className="rounded-md border border-line px-2 py-1 transition-colors duration-150 hover:border-cyan focus-visible:outline-none focus-visible:[outline:2px_solid_#22D3EE] focus-visible:[outline-offset:2px]"
        >
          <span className={`font-sans text-xs ${copied ? "text-lime" : "text-muted"}`}>
            {copied ? "copied" : "copy"}
          </span>
        </button>
      </div>

      <div className="overflow-x-auto px-4 py-3">
        <pre className="w-max font-mono text-[13px] leading-6">
          {code.split("\n").map((line, i) => (
            <ShellLine key={i} line={line} shell={shell} />
          ))}
        </pre>
      </div>
    </div>
  );
}

function ShellLine({ line, shell }: { line: string; shell?: boolean }) {
  const isComment = line.trimStart().startsWith("#");
  return (
    <div>
      {shell && !isComment && line.length > 0 ? <span className="text-faint">$ </span> : null}
      <span className={isComment ? "text-faint" : "text-ink/90"}>{line || " "}</span>
    </div>
  );
}
