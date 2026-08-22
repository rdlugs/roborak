import { Link } from "react-router-dom";

import { Logo } from "./logo";
import { PYPI_URL, REPO_URL } from "./nav-data";
import { ExtLink, Shell } from "./ui";

const LINK =
  "font-mono text-sm text-muted transition-colors duration-150 hover:text-ink focus-visible:outline-none focus-visible:[outline:2px_solid_#22D3EE] focus-visible:[outline-offset:3px] rounded-sm";

export function SiteNav() {
  return (
    <header className="sticky top-0 z-50 border-b border-line [backdrop-filter:blur(12px)] [background-color:rgba(5,6,10,0.82)]">
      <Shell className="h-16 flex-row items-center justify-between">
        <Link to="/">
          <Logo />
        </Link>

        <nav className="flex flex-row items-center gap-6">
          <Link to="/docs/install" className={LINK}>
            Docs
          </Link>
          <ExtLink href={REPO_URL} className={`hidden sm:inline ${LINK}`}>
            GitHub
          </ExtLink>
          <ExtLink href={PYPI_URL} className={`hidden sm:inline ${LINK}`}>
            PyPI
          </ExtLink>
        </nav>
      </Shell>
    </header>
  );
}
