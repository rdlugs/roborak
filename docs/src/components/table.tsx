import type { ReactNode } from "react";

import { NeonEdge, useNeonEdge } from "./neon-edge";

export type Column = {
  key: string;
  header: string;
  width?: number;
  mono?: boolean;
};

export type Row = Record<string, ReactNode>;

export function Table({
  columns,
  rows,
  minWidth = 620,
}: {
  columns: Column[];
  rows: Row[];
  minWidth?: number;
}) {
  const total = columns.reduce((sum, c) => sum + (c.width ?? 1), 0);
  const ref = useNeonEdge<HTMLDivElement>();

  return (
    <div ref={ref} className="relative isolate mb-6 overflow-hidden rounded-xl border border-line">
      <NeonEdge />
      <div className="overflow-x-auto">
        <table className="w-full table-fixed border-collapse" style={{ minWidth }}>
          <colgroup>
            {columns.map((c) => (
              <col key={c.key} style={{ width: `${((c.width ?? 1) / total) * 100}%` }} />
            ))}
          </colgroup>

          <thead>
            <tr className="border-b border-line bg-surface">
              {columns.map((c) => (
                <th
                  key={c.key}
                  scope="col"
                  className="px-4 py-3 text-left align-top font-sans text-xs font-normal uppercase tracking-widest text-faint"
                >
                  {c.header}
                </th>
              ))}
            </tr>
          </thead>

          <tbody>
            {rows.map((row, i) => (
              <tr
                key={i}
                className={`${i % 2 ? "bg-surface/40" : ""} ${
                  i === rows.length - 1 ? "" : "border-b border-line"
                }`}
              >
                {columns.map((c) => (
                  <td
                    key={c.key}
                    className={`px-4 py-3 align-top ${
                      c.mono
                        ? "font-mono text-[13px] leading-6 text-lime"
                        : "font-sans text-[14px] leading-6 text-ink/90"
                    }`}
                  >
                    {row[c.key]}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
