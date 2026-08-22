import { useEffect, useRef } from "react";
import type { CSSProperties, RefObject } from "react";

import { NeonEdge, useNeonEdge } from "./neon-edge";

const CYAN = "34 211 238";
const VIOLET = "138 43 226";
const LIME = "215 255 100";
const MAGENTA = "255 47 208";

const STAGES = [
  {
    name: "Source",
    note: "git · GitLab · GitHub · paths",
    desc: "Reads the diff from wherever it lives, and reads nothing else.",
    edge: "border-cyan/50",
    ink: "text-cyan",
    dot: "bg-cyan",
    rgb: CYAN,
  },
  {
    name: "ChangeSet",
    note: "one IR for all four",
    desc: "Normalises all four sources into a single intermediate representation.",
    edge: "border-cyan/50",
    ink: "text-cyan",
    dot: "bg-cyan",
    rgb: CYAN,
  },
  {
    name: "Compressor",
    note: "chunk to fit the window",
    desc: "Chunks the change so a large diff still fits the model's context window.",
    edge: "border-violet/50",
    ink: "text-violet",
    dot: "bg-violet",
    rgb: VIOLET,
  },
  {
    name: "Static pass",
    note: "ruff · mypy · semgrep",
    desc: "Runs your linters with your config and keeps the results as evidence.",
    edge: "border-lime/50",
    ink: "text-lime",
    dot: "bg-lime",
    rgb: LIME,
  },
  {
    name: "LLM",
    note: "any LiteLLM model",
    desc: "Judges the change with the static results already in front of it.",
    edge: "border-violet/50",
    ink: "text-violet",
    dot: "bg-violet",
    rgb: VIOLET,
  },
  {
    name: "Validator",
    note: "anchors checked on disk",
    desc: "Re-checks every anchor against the file on disk before a finding is allowed out.",
    edge: "border-magenta/50",
    ink: "text-magenta",
    dot: "bg-magenta",
    rgb: MAGENTA,
  },
  {
    name: "Renderer",
    note: "terminal · md · JSON · forge",
    desc: "Writes the one validated result out in whichever form you asked for.",
    edge: "border-cyan/50",
    ink: "text-cyan",
    dot: "bg-cyan",
    rgb: CYAN,
  },
];

type Stage = (typeof STAGES)[number];

const RAIL_X = "left-[15px]";
const NODE_X = "left-[10px]";

const READING_LINE = 0.62;

const MOTION_QUERY = "(prefers-reduced-motion: reduce)";

function useRailProgress(host: RefObject<HTMLOListElement | null>) {
  useEffect(() => {
    const el = host.current;
    if (!el) return;

    const motion = window.matchMedia(MOTION_QUERY);
    let frame = 0;

    const update = () => {
      frame = 0;
      const box = el.getBoundingClientRect();
      if (box.height === 0) return;
      const reached = (window.innerHeight * READING_LINE - box.top) / box.height;
      el.style.setProperty("--rail", String(Math.min(1, Math.max(0, reached))));
    };

    const onScroll = () => {
      if (!frame) frame = requestAnimationFrame(update);
    };

    let teardown = () => {};
    const attach = () => {
      if (motion.matches) {
        el.style.setProperty("--rail", "1");
        teardown = () => {};
        return;
      }
      update();
      window.addEventListener("scroll", onScroll, { passive: true });
      window.addEventListener("resize", onScroll);
      teardown = () => {
        window.removeEventListener("scroll", onScroll);
        window.removeEventListener("resize", onScroll);
        if (frame) cancelAnimationFrame(frame);
      };
    };

    attach();
    const swap = () => {
      teardown();
      attach();
    };
    motion.addEventListener("change", swap);
    return () => {
      teardown();
      motion.removeEventListener("change", swap);
    };
  }, [host]);
}

export function Pipeline({
  orientation = "rail",
}: {
  orientation?: "rail" | "vertical";
}) {
  const host = useRef<HTMLOListElement>(null);
  useRailProgress(host);

  if (orientation === "vertical") {
    return (
      <ol>
        {STAGES.map((s, i) => (
          <li key={s.name} className="flex flex-row">
            <div className="mr-4 flex flex-col items-center">
              <div className={`mt-1.5 h-3 w-3 shrink-0 rounded-full border ${s.edge}`} />
              {i < STAGES.length - 1 ? <div className="w-px flex-1 bg-line" /> : null}
            </div>
            <div className={i < STAGES.length - 1 ? "min-w-0 flex-1 pb-6" : "min-w-0 flex-1"}>
              <p className={`font-mono text-sm font-semibold ${s.ink}`}>{s.name}</p>
              <p className="mt-1 font-sans text-sm leading-6 text-muted">{s.desc}</p>
              <p className="mt-1 font-mono text-xs leading-5 text-faint">{s.note}</p>
            </div>
          </li>
        ))}
      </ol>
    );
  }

  return (
    <ol
      ref={host}
      className="relative mx-auto grid w-full max-w-2xl auto-rows-fr grid-cols-1 gap-4"
    >
      <div
        aria-hidden
        className={`pointer-events-none absolute inset-y-8 w-0.5 bg-line ${RAIL_X}`}
      >
        <div
          className="absolute inset-x-0 top-0 bg-cyan [box-shadow:0_0_12px_rgba(34,211,238,0.65)]"
          style={{ height: "calc(var(--rail, 0) * 100%)" }}
        >
          <span className="absolute -bottom-1 left-1/2 h-2 w-2 -translate-x-1/2 rounded-full bg-cyan [box-shadow:0_0_14px_4px_rgba(34,211,238,0.55)]" />
        </div>
      </div>

      {STAGES.map((s, i) => (
        <li key={s.name} className="relative flex pl-12">
          <span
            aria-hidden
            className={`absolute top-8 z-10 h-3 w-3 -translate-y-1/2 rounded-full border border-line bg-bg ${NODE_X}`}
          >
            <span
              className={`absolute inset-0 rounded-full ${s.dot}`}
              style={{
                opacity: `calc((var(--rail, 0) - ${((i + 0.5) / STAGES.length).toFixed(3)}) * 40)`,
              }}
            />
          </span>
          <Chip stage={s} step={i + 1} />
        </li>
      ))}
    </ol>
  );
}

function Chip({ stage, step }: { stage: Stage; step: number }) {
  const ref = useNeonEdge<HTMLDivElement>();
  return (
    <div
      ref={ref}
      style={{ "--neon-c": stage.rgb } as CSSProperties}
      className={`relative isolate h-full w-full rounded-xl border bg-surface px-5 py-4 ${stage.edge}`}
    >
      <NeonEdge />
      <div className="flex flex-row items-baseline gap-3">
        <span className="font-mono text-[11px] tracking-[0.2em] text-faint">
          {String(step).padStart(2, "0")}
        </span>
        <p className={`font-mono text-sm font-semibold ${stage.ink}`}>{stage.name}</p>
      </div>
      <p className="mt-1.5 font-sans text-sm leading-6 text-muted">{stage.desc}</p>
      <p className="mt-1 font-mono text-[11px] leading-5 text-faint">{stage.note}</p>
    </div>
  );
}
