
import { NeonEdge, useNeonEdge } from "./neon-edge";

type Line = { n: number; code: string; hit?: boolean };

const CONTEXT: Line[] = [
  { n: 40, code: "def issue_token(user: User) -> str:" },
  { n: 41, code: '    payload = {"sub": user.id, "role": user.role}' },
  { n: 42, code: '    return jwt.encode(payload, SECRET, algorithm="none")', hit: true },
];

const FIX: Line[] = [
  { n: 42, code: '-   return jwt.encode(payload, SECRET, algorithm="none")' },
  { n: 42, code: '+   return jwt.encode(payload, SECRET, algorithm="HS256")' },
];

export function TerminalHero() {
  const ref = useNeonEdge<HTMLDivElement>();
  return (
    <div
      ref={ref}
      className="relative isolate w-full overflow-hidden rounded-2xl border border-line bg-[#070911] [box-shadow:0_0_60px_rgba(34,211,238,0.10),0_24px_60px_rgba(0,0,0,0.6)]"
    >
      <NeonEdge />
      <TitleBar />

      <div className="overflow-x-auto">
        <div className="min-w-[560px] px-5 py-4">
          <p className="mb-4 font-mono text-[13px] leading-6">
            <span className="text-lime">$ </span>
            <span className="text-ink">rk review --uncommitted</span>
          </p>

          <SectionRule label="🛠️ Actionable comments (1)" />

          <p className="mb-3 font-mono text-[13px] text-cyan">src/api/session.py</p>

          <div className="mb-3 flex flex-row flex-wrap items-center gap-x-2 gap-y-1">
            <Badge tone="text-critical">🔴 Critical</Badge>
            <Dot />
            <Badge tone="text-critical">🔒 Security</Badge>
            <Dot />
            <Badge tone="text-lime">⚡ Quick win</Badge>
            <Dot />
            <Badge tone="text-muted">src/api/session.py:42</Badge>
          </div>

          <p className="mb-3 font-mono text-[13px] leading-6 text-ink/90">
            Tokens are signed with the <span className="text-magenta">none</span> algorithm, so any
            client can forge one.
          </p>

          <div className="mb-3 rounded-lg border border-line bg-surface/60 py-2">
            {CONTEXT.map((l) => (
              <CodeLine key={l.n} line={l} />
            ))}
          </div>

          <p className="mb-1 font-mono text-xs uppercase tracking-widest text-faint">
            Committable suggestion
          </p>
          <div className="mb-4 rounded-lg border border-line bg-surface/60 py-2">
            {FIX.map((l, i) => (
              <CodeLine key={i} line={l} diff />
            ))}
          </div>

          <p className="font-mono text-[13px] text-muted">
            <span className="text-critical">1 critical</span>
            <span className="text-faint"> · </span>
            <span className="text-minor">2 minor</span>
            <span className="text-faint"> · 6 filtered as low confidence · exit 1</span>
          </p>
        </div>
      </div>
    </div>
  );
}

function TitleBar() {
  return (
    <div className="flex flex-row items-center gap-2 border-b border-line bg-surface px-4 py-3">
      <div className="h-3 w-3 rounded-full [background-color:#FF5F57]" />
      <div className="h-3 w-3 rounded-full [background-color:#FEBC2E]" />
      <div className="h-3 w-3 rounded-full [background-color:#28C840]" />
      <span className="ml-3 font-mono text-xs text-faint">roborak - review</span>
    </div>
  );
}

function SectionRule({ label }: { label: string }) {
  return (
    <div className="mb-3 flex flex-row items-center gap-3">
      <span className="font-mono text-[13px] font-semibold text-ink">{label}</span>
      <div className="h-px flex-1 bg-line" />
    </div>
  );
}

function Badge({ children, tone }: { children: string; tone: string }) {
  return <span className={`font-mono text-xs ${tone}`}>{children}</span>;
}

function Dot() {
  return <span className="font-mono text-xs text-faint">·</span>;
}

function CodeLine({ line, diff }: { line: Line; diff?: boolean }) {
  const added = diff && line.code.startsWith("+");
  const removed = diff && line.code.startsWith("-");

  return (
    <div
      className={`flex flex-row px-3 ${
        line.hit ? "[background-color:rgba(255,47,208,0.10)]" : ""
      } ${added ? "[background-color:rgba(215,255,100,0.08)]" : ""} ${
        removed ? "[background-color:rgba(255,47,208,0.08)]" : ""
      }`}
    >
      <span
        className={`w-9 shrink-0 select-none font-mono text-[13px] leading-6 ${
          line.hit ? "text-critical" : "text-faint"
        }`}
      >
        {line.n}
      </span>
      <span
        className={`mr-2 shrink-0 font-mono text-[13px] leading-6 ${
          line.hit ? "text-critical" : "text-faint"
        }`}
      >
        {line.hit ? "●" : "│"}
      </span>
      <span
        className={`whitespace-pre font-mono text-[13px] leading-6 ${
          added ? "text-lime" : removed ? "text-magenta" : "text-ink/85"
        }`}
      >
        {line.code}
      </span>
    </div>
  );
}
