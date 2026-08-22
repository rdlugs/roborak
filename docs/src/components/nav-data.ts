export type DocPage = {
  href: string;
  title: string;
  blurb: string;
};

export type DocSection = {
  title: string;
  pages: DocPage[];
};

export const DOC_SECTIONS: DocSection[] = [
  {
    title: "Getting started",
    pages: [
      {
        href: "/docs/install",
        title: "Install",
        blurb: "uvx, uv tool, pipx, or from a checkout.",
      },
      {
        href: "/docs/quickstart",
        title: "Quickstart",
        blurb: "Review a local diff, an MR, or a PR.",
      },
    ],
  },
  {
    title: "Reference",
    pages: [
      {
        href: "/docs/commands",
        title: "Commands",
        blurb: "review, describe, improve, ask, rules, config, setup.",
      },
      {
        href: "/docs/configuration",
        title: "Configuration",
        blurb: "Every key in .roborak.yaml, and where it comes from.",
      },
      {
        href: "/docs/rules",
        title: "Custom rules",
        blurb: "Teach roborak the conventions only your team has.",
      },
      {
        href: "/docs/tokens",
        title: "Tokens",
        blurb: "Forge credentials and the order they are resolved in.",
      },
    ],
  },
  {
    title: "Going deeper",
    pages: [
      {
        href: "/docs/how-it-works",
        title: "How it works",
        blurb: "The pipeline, one stage at a time.",
      },
      {
        href: "/docs/static-analysis",
        title: "Static analysis",
        blurb: "Linters as evidence, and the trust boundary around them.",
      },
      {
        href: "/docs/self-hosted",
        title: "Self-hosted forges",
        blurb: "Point roborak at your own GitLab or GitHub Enterprise.",
      },
      {
        href: "/docs/contributing",
        title: "Contributing",
        blurb: "The checks, the invariants, and how a change lands.",
      },
    ],
  },
];

export const DOC_PAGES: DocPage[] = DOC_SECTIONS.flatMap((s) => s.pages);

export function neighbours(href: string): { prev?: DocPage; next?: DocPage } {
  const i = DOC_PAGES.findIndex((p) => p.href === href);
  if (i === -1) return {};
  return { prev: DOC_PAGES[i - 1], next: DOC_PAGES[i + 1] };
}

export const REPO_URL = "https://github.com/rdlugs/roborak";
export const PYPI_URL = "https://pypi.org/project/roborak/";
