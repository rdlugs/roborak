import { Link } from "react-router-dom";

import { PageHead } from "@/components/page-head";
import { SiteNav } from "@/components/site-nav";
import { Shell } from "@/components/ui";

export default function NotFound() {
  return (
    <>
      <PageHead title="Not found" description="No page at this address." />
      <div className="min-h-screen bg-bg">
        <SiteNav />
        <Shell className="flex-col items-center py-32">
          <p className="font-mono text-6xl font-bold text-magenta [text-shadow:0_0_32px_rgba(255,47,208,0.5)]">
            404
          </p>
          <p className="mt-6 font-sans text-base text-muted">
            No page here. It may have moved, or never existed.
          </p>
          <Link
            to="/docs/install"
            className="mt-8 rounded-lg border border-cyan px-5 py-3 font-sans text-sm text-cyan transition-all duration-200 hover:[box-shadow:0_0_24px_rgba(34,211,238,0.45)] focus-visible:outline-none focus-visible:[outline:2px_solid_#22D3EE] focus-visible:[outline-offset:3px]"
          >
            Go to the docs
          </Link>
        </Shell>
      </div>
    </>
  );
}
