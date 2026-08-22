import { CodeBlock } from "./code-block";
import { NeonEdge, useNeonEdge } from "./neon-edge";
import { Table } from "./table";

export type Flag = {
  name: string;
  arg?: string;
  help: string;
};

export type FlagGroup = { title: string; flags: Flag[] };

export function Cmd({
  name,
  synopsis,
  summary,
  groups,
  example,
}: {
  name: string;
  synopsis: string;
  summary: string;
  groups: FlagGroup[];
  example?: string;
}) {
  const ref = useNeonEdge<HTMLDivElement>();
  return (
    <section className="mb-12">
      <div
        ref={ref}
        id={name.replace(/\s+/g, "-")}
        className="relative isolate mb-4 scroll-mt-24 rounded-xl border border-line bg-surface px-5 py-4"
      >
        <NeonEdge />
        <h2 className="font-mono text-lg font-semibold text-cyan">{name}</h2>
        <p className="mt-2 font-mono text-[13px] text-muted">{synopsis}</p>
        <p className="mt-3 font-sans text-[15px] leading-7 text-ink/90">{summary}</p>
      </div>

      {groups.map((group) => (
        <div key={group.title} className="mb-4">
          <p className="mb-2 font-mono text-xs uppercase tracking-[0.18em] text-faint">
            {group.title}
          </p>
          <Table
            minWidth={620}
            columns={[
              { key: "flag", header: "Flag", mono: true, width: 2 },
              { key: "help", header: "What it does", width: 3 },
            ]}
            rows={group.flags.map((f) => ({
              flag: f.arg ? `${f.name} <${f.arg}>` : f.name,
              help: f.help,
            }))}
          />
        </div>
      ))}

      {example ? <CodeBlock shell label="example" code={example} /> : null}
    </section>
  );
}
