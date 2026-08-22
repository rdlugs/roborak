import { Link } from "react-router-dom";

import { CodeBlock } from "@/components/code-block";
import { HeroAura } from "@/components/hero-aura";
import { REPO_URL } from "@/components/nav-data";
import { NeonEdge, useNeonEdge } from "@/components/neon-edge";
import { PageHead } from "@/components/page-head";
import { Pipeline } from "@/components/pipeline";
import { SiteFooter } from "@/components/site-footer";
import { SiteNav } from "@/components/site-nav";
import { TerminalHero } from "@/components/terminal";
import { A, Card, ExtLink, NeonGrid, Pill, Shell } from "@/components/ui";

const FEATURES = [
  {
    icon: "🎯",
    title: "Anchored, not approximate",
    body: "Every finding points at a line the change actually touched, verified against the files on disk, not the model's word for it.",
  },
  {
    icon: "🧹",
    title: "Refuses more than it says",
    body: "Low-confidence findings filtered, duplicates collapsed, pre-existing lint debt suppressed, already-posted comments skipped.",
  },
  {
    icon: "🔌",
    title: "Four sources, one pipeline",
    body: "Local git, GitLab MRs, GitHub PRs and raw paths all normalise into one IR, so output modes can never disagree.",
  },
  {
    icon: "🛠",
    title: "Static analysis as evidence",
    body: "Runs ruff, mypy, semgrep, eslint and phpstan with your config, and feeds the results to the model to confirm or explain.",
  },
  {
    icon: "💬",
    title: "Publishes where you're looking",
    body: "Inline threads for what's worth interrupting for, a summary comment for the rest, incremental so re-runs don't repeat themselves.",
  },
  {
    icon: "📋",
    title: "Issue-aware",
    body: "--issue 42 judges the diff against what was actually asked, and reports the requirements it misses.",
  },
];

const REFUSALS = [
  ["Low confidence", "Below the confidence floor, the finding never reaches you."],
  ["Duplicates", "The same problem in five places collapses into one thread."],
  ["Pre-existing debt", "Lint your repo already fails is not this diff's fault."],
  ["Already posted", "A re-run on an updated MR adds what changed, not what it said last time."],
];

export default function Landing() {
  return (
    <div className="min-h-screen bg-bg">
      <PageHead
        title="roborak"
        description="AI code review that earns the interrupt severity-graded, line-anchored findings with committable fix suggestions, across local diffs, GitLab MRs and GitHub PRs."
      />
      <SiteNav />

      <section className="relative overflow-hidden">
        <NeonGrid />
        <HeroAura />

        <Shell className="relative z-10 flex-col gap-14 pb-20 pt-16 lg:flex-row lg:items-center lg:gap-16 lg:pt-24">
          <div className="w-full lg:flex-1">
            <Pill tone="cyan">v0.3.0</Pill>

            <h1 className="mt-6 font-mono text-4xl font-bold leading-[1.1] text-ink sm:text-5xl lg:text-[3.4rem]">
              AI code review
              <br />
              <span className="text-cyan [text-shadow:0_0_28px_rgba(34,211,238,0.55)]">
                that earns the interrupt
              </span>
            </h1>

            <p className="mt-6 max-w-prose font-sans text-lg leading-8 text-muted">
              Low-confidence findings filtered, duplicates collapsed, pre-existing lint debt
              suppressed. What's left is severity-graded, pinned to a real line, and comes with
              a fix you can commit across local diffs, GitLab MRs and GitHub PRs.
            </p>

            <div className="mt-8 max-w-md">
              <CodeBlock shell label="install" code={"uvx roborak review"} />
            </div>

            <div className="flex flex-row flex-wrap items-center gap-3">
              <Link
                to="/docs/quickstart"
                className="rounded-lg border border-cyan bg-cyan px-5 py-3 font-sans text-sm font-semibold text-bg transition-all duration-200 hover:[box-shadow:0_0_28px_rgba(34,211,238,0.6)] focus-visible:outline-none focus-visible:[outline:2px_solid_#22D3EE] focus-visible:[outline-offset:3px]"
              >
                Get started
              </Link>
              <ExtLink
                href={REPO_URL}
                className="rounded-lg border border-line px-5 py-3 font-sans text-sm text-ink transition-colors duration-200 hover:border-line-bright hover:bg-surface focus-visible:outline-none focus-visible:[outline:2px_solid_#22D3EE] focus-visible:[outline-offset:3px]"
              >
                View on GitHub
              </ExtLink>
            </div>

            <p className="mt-6 font-mono text-xs text-faint">
              Python 3.12+ · MIT · any LiteLLM model · GitLab and GitHub
            </p>
          </div>

          <div className="w-full min-w-0 lg:flex-1">
            <TerminalHero />
          </div>
        </Shell>
      </section>

      <Shell className="flex-col py-20">
        <SectionHeading kicker="What it does" title="Review output you can act on" />

        <div className="mt-10 grid auto-rows-fr grid-cols-1 gap-5 sm:grid-cols-2 lg:grid-cols-3">
          {FEATURES.map((f) => (
            <Card key={f.title} className="h-full">
              <span aria-hidden className="text-2xl">
                {f.icon}
              </span>
              <p className="mt-3 font-mono text-base font-semibold text-ink">{f.title}</p>
              <p className="mt-2 font-sans text-sm leading-6 text-muted">{f.body}</p>
            </Card>
          ))}
        </div>
      </Shell>

      <section className="relative overflow-hidden border-y border-line bg-surface/30">
        <Shell className="flex-col py-20">
          <SectionHeading
            kicker="How it works"
            title="One direction, one stage at a time"
            titleClassName="font-sans"
            body="Each stage knows only the one before it. That is why the terminal, the markdown report, the JSON and the forge comment can never disagree about what the review said."
          />
          <div className="mt-10">
            <Pipeline />
          </div>
          <div className="mt-8">
            <A href="/docs/how-it-works" className="font-sans text-sm">
              Read the full pipeline →
            </A>
          </div>
        </Shell>
      </section>

      <Shell className="flex-col py-20">
        <div className="flex flex-col gap-12 lg:flex-row lg:gap-16">
          <div className="w-full lg:flex-1">
            <SectionHeading
              kicker="Signal"
              title="It refuses more than it says"
              titleClassName="font-sans"
              body="A review tool earns attention by spending it carefully. Four filters run before anything reaches your screen or your MR."
            />
          </div>

          <div className="flex w-full flex-col gap-3 lg:flex-1">
            {REFUSALS.map(([title, body]) => (
              <RefusalRow key={title} title={title} body={body} />
            ))}
          </div>
        </div>
      </Shell>

      <section className="border-y border-line bg-surface/30">
        <Shell className="flex-row flex-wrap items-center justify-center gap-x-10 gap-y-4 py-10">
          {["GitLab MRs", "GitHub PRs", "Local diffs", "Self-hosted forges", "Any LiteLLM model"].map(
            (label) => (
              <span key={label} className="font-mono text-sm text-muted">
                {label}
              </span>
            ),
          )}
        </Shell>
      </section>

      <Shell className="flex-col items-center py-24">
        <h2 className="text-center font-sans text-3xl font-bold text-ink sm:text-4xl">
          Review the diff before{" "}
          <span className="text-cyan [text-shadow:0_0_24px_rgba(34,211,238,0.5)]">someone else</span>{" "}
          has to
        </h2>
        <p className="mt-4 max-w-prose text-center font-sans text-base leading-7 text-muted">
          No account, no service, no repository access to grant. It runs on your machine, against
          your diff, with the model you already pay for.
        </p>
        <div className="mt-8 w-full max-w-md">
          <CodeBlock shell label="try it" code={"uvx roborak review --uncommitted"} />
        </div>
        <Link
          to="/docs/install"
          className="rounded-lg border border-cyan bg-cyan px-6 py-3 font-sans text-sm font-semibold text-bg transition-all duration-200 hover:[box-shadow:0_0_28px_rgba(34,211,238,0.6)] focus-visible:outline-none focus-visible:[outline:2px_solid_#22D3EE] focus-visible:[outline-offset:3px]"
        >
          Install roborak
        </Link>
      </Shell>

      <SiteFooter />
    </div>
  );
}

function RefusalRow({ title, body }: { title: string; body: string }) {
  const ref = useNeonEdge<HTMLDivElement>();
  return (
    <div
      ref={ref}
      className="relative isolate flex flex-row gap-4 rounded-xl border border-line bg-surface px-5 py-4"
    >
      <NeonEdge />
      <span aria-hidden className="font-mono text-sm text-magenta">
        ✕
      </span>
      <div className="min-w-0 flex-1">
        <p className="font-mono text-sm font-semibold text-ink">{title}</p>
        <p className="mt-1 font-sans text-sm leading-6 text-muted">{body}</p>
      </div>
    </div>
  );
}

function SectionHeading({
  kicker,
  title,
  body,
  titleClassName = "font-mono",
}: {
  kicker: string;
  title: string;
  body?: string;
  titleClassName?: string;
}) {
  return (
    <div>
      <p className="font-mono text-xs uppercase tracking-[0.2em] text-cyan">{kicker}</p>
      <h2 className={`mt-3 text-2xl font-bold text-ink sm:text-3xl ${titleClassName}`}>{title}</h2>
      {body ? (
        <p className="mt-4 max-w-prose font-sans text-base leading-7 text-muted">{body}</p>
      ) : null}
    </div>
  );
}
