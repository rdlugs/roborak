import { Link, useLocation } from "react-router-dom";

import { DOC_SECTIONS } from "./nav-data";

export function Sidebar({ onNavigate }: { onNavigate?: () => void }) {
  const { pathname } = useLocation();

  return (
    <nav aria-label="Documentation" className="flex flex-col gap-8">
      {DOC_SECTIONS.map((section) => (
        <div key={section.title}>
          <p className="mb-3 font-sans text-xs uppercase tracking-[0.18em] text-faint">
            {section.title}
          </p>
          <div className="flex flex-col gap-0.5">
            {section.pages.map((page) => {
              const active = pathname === page.href;
              return (
                <Link
                  key={page.href}
                  to={page.href}
                  onClick={onNavigate}
                  aria-current={active ? "page" : undefined}
                  className={`border-l-2 py-1.5 pl-3 font-sans text-sm transition-colors duration-150 focus-visible:outline-none focus-visible:[outline:2px_solid_#22D3EE] focus-visible:[outline-offset:2px] ${
                    active
                      ? "border-l-cyan text-cyan [text-shadow:0_0_14px_rgba(34,211,238,0.5)]"
                      : "border-l-line text-muted hover:border-l-line-bright hover:text-ink"
                  }`}
                >
                  {page.title}
                </Link>
              );
            })}
          </div>
        </div>
      ))}
    </nav>
  );
}
