import { useState } from "react";
import { Link, Outlet, useLocation } from "react-router-dom";

import { neighbours } from "@/components/nav-data";
import { NeonEdge, useNeonEdge } from "@/components/neon-edge";
import { Sidebar } from "@/components/sidebar";
import { SiteFooter } from "@/components/site-footer";
import { SiteNav } from "@/components/site-nav";
import { Divider, Shell } from "@/components/ui";

export function DocsLayout() {
  const { pathname } = useLocation();
  const [drawerOpen, setDrawerOpen] = useState(false);
  const { prev, next } = neighbours(pathname);

  return (
    <div className="flex min-h-screen flex-col bg-bg">
      <SiteNav />

      <div className="border-b border-line lg:hidden">
        <Shell className="flex-col py-3">
          <button
            type="button"
            onClick={() => setDrawerOpen((o) => !o)}
            aria-expanded={drawerOpen}
            aria-label="Toggle documentation menu"
            className="flex flex-row items-center gap-2 self-start rounded-md border border-line px-3 py-2 focus-visible:outline-none focus-visible:[outline:2px_solid_#22D3EE] focus-visible:[outline-offset:2px]"
          >
            <span className="font-mono text-xs text-cyan">{drawerOpen ? "▾" : "▸"}</span>
            <span className="font-sans text-xs text-muted">Documentation</span>
          </button>

          {drawerOpen ? (
            <div className="mt-5">
              <Sidebar onNavigate={() => setDrawerOpen(false)} />
            </div>
          ) : null}
        </Shell>
      </div>

      <Shell className="flex-row gap-12 py-12">
        <div className="hidden w-56 shrink-0 lg:block">
          <div className="sticky top-24">
            <Sidebar />
          </div>
        </div>

        <div className="min-w-0 flex-1">
          <div className="w-full max-w-prose">
            <Outlet />

            <Divider className="mt-16" />
            <div className="mt-6 flex flex-row flex-wrap justify-between gap-4">
              {prev ? <PageLink page={prev} direction="prev" /> : <div />}
              {next ? <PageLink page={next} direction="next" /> : <div />}
            </div>
          </div>
        </div>
      </Shell>

      <SiteFooter />
    </div>
  );
}

function PageLink({
  page,
  direction,
}: {
  page: { href: string; title: string };
  direction: "prev" | "next";
}) {
  const ref = useNeonEdge<HTMLAnchorElement>();
  return (
    <Link
      ref={ref}
      to={page.href}
      className={`relative isolate rounded-lg border border-line px-4 py-3 transition-colors duration-150 hover:border-cyan focus-visible:outline-none focus-visible:[outline:2px_solid_#22D3EE] focus-visible:[outline-offset:3px] ${
        direction === "next" ? "text-right" : ""
      }`}
    >
      <NeonEdge />
      <span className="block font-mono text-xs text-faint">
        {direction === "prev" ? "← Previous" : "Next →"}
      </span>
      <span className="block font-mono text-sm text-ink">{page.title}</span>
    </Link>
  );
}
