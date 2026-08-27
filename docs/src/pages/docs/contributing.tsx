import { PageHead } from "@/components/page-head";
import { Callout } from "@/components/callout";
import { CodeBlock } from "@/components/code-block";
import { Code, H1, H2, Lead, Li, P, Ul } from "@/components/prose";
import { Table } from "@/components/table";
import { REPO_URL } from "@/components/nav-data";
import { A } from "@/components/ui";

const INVARIANTS = [
  [
    "Line numbers are new-file coordinates everywhere.",
    "Only publishers translate them into a forge's position payload. tests/test_local_git.py checks computed numbers against the real files on disk, so an off-by-one cannot agree with itself and pass do not “fix” it by adjusting the expectation.",
  ],
  [
    "core/buckets.py is the single place that decides where a finding goes.",
    "The terminal, the markdown report, the summary comment and the publisher must never disagree about inline vs. summary.",
  ],
  [
    "One renderer builds one document.",
    "What --markdown writes, what a pipe returns and what --post publishes are byte for byte identical, and a test asserts it. A second rendering path is the refactor this repo is most likely to regress on.",
  ],
  [
    "The walkthrough runs on a copy of the changeset.",
    "Compression mutates; a shrunken diff would invalidate every line number already anchored against it.",
  ],
  [
    "A failed overview is logged, never fatal.",
    "A review without a walkthrough is still a review and must still exit clean.",
  ],
  [
    "stdout is the product, stderr is the chrome.",
    "--json, --agent, --prompt-only and piped output write only the payload to stdout; spinners, errors and the closing prompt always go to stderr.",
  ],
  [
    "Never prompt when not on a terminal.",
    "Pipes, scripts and CI jobs are never asked whether to publish.",
  ],
  [
    "roborak never approves or requests changes.",
    "GitHub reviews are always posted as COMMENT.",
  ],
  [
    "The static pass is untrusted by default in CI.",
    "It runs under Bubblewrap with a read-only filesystem and no network, and is skipped rather than run directly when Bubblewrap is unavailable.",
  ],
];

export default function Contributing() {
  return (
    <>
      <PageHead title="Contributing" description="Set up the repo, run the checks CI runs, and the invariants to read before changing the pipeline." />
      <H1>Contributing</H1>
      <Lead>
        The short version: <Code>uv sync --dev</Code>, run the checks below before you
        push, and read the invariants before you change anything in the middle of the pipeline.
      </Lead>

      <H2>Getting set up</H2>
      <CodeBlock
        shell
        code={[
          "uv sync --dev                  # runtime and dev tools",
          "uv run roborak --help          # smoke-test the CLI",
        ].join("\n")}
      />
      <P>
        Python 3.12 is the floor. Everything is driven through <Code>uv run</Code>, so there is no
        virtualenv to activate by hand.
      </P>
      <Callout kind="tip" title="You probably do not need a model key">
        <P>
          <Code>uv run roborak review --no-llm</Code> exercises the whole pipeline on static
          analysis alone, and the test suite never calls a real model.
        </P>
      </Callout>

      <H2>The checks</H2>
      <P>These are exactly what CI runs.</P>
      <CodeBlock
        shell
        code={[
          "uv run ruff check src tests evals",
          "uv run ruff format src tests evals",
          "uv run mypy src",
          "uv run pytest --cov=src/roborak --cov-fail-under=90",
          "uv build",
        ].join("\n")}
      />
      <P>
        Coverage is gated at 90%, <Code>mypy</Code> runs with <Code>disallow_untyped_defs</Code>,
        and <Code>ruff format --check</Code> is enforced so format before you commit rather than
        after the red build.
      </P>

      <H2>Where things live</H2>
      <Table
        minWidth={640}
        columns={[
          { key: "pkg", header: "Package", mono: true, width: 2 },
          { key: "what", header: "What belongs there", width: 4 },
        ]}
        rows={[
          { pkg: "sources/", what: "Local git, GitLab, GitHub and raw paths → ChangeSet" },
          {
            pkg: "core/",
            what: "The IR (models.py), config, severity, and finding routing (buckets.py)",
          },
          {
            pkg: "static/",
            what: "Running ruff, mypy, semgrep, eslint, phpstan and normalising their output",
          },
          { pkg: "llm/", what: "Prompt construction, LiteLLM calls, chunking, response parsing" },
          {
            pkg: "render/",
            what: "The one document: terminal, markdown, JSON, agent and summary forms",
          },
          {
            pkg: "publish/",
            what: "Translating new-file coordinates into each forge's position payload",
          },
          { pkg: "cli/", what: "Typer commands; thin, with the work delegated downward" },
          { pkg: "evals/", what: "Live reviewer-quality evaluation, deliberately outside PR CI" },
        ]}
      />
      <P>
        Adding a new source means writing a <Code>ChangeSet</Code> producer and nothing else. If a
        change to a source needs a matching change in the renderer, that is a sign the IR lost
        something it should have carried.
      </P>

      <H2>Invariants worth knowing before you break one</H2>
      <P>These are the parts where a plausible-looking change quietly corrupts output.</P>
      <Ul>
        {INVARIANTS.map(([claim, why]) => (
          <Li key={claim}>
            <Code>{claim}</Code> {why}
          </Li>
        ))}
      </Ul>

      <H2>Opening a pull request</H2>
      <Ul>
        <Li>
          Branch off <Code>main</Code>. Outside contributors work from a fork direct branch
          creation in the repository is restricted to admins.
        </Li>
        <Li>
          Keep the change focused; a refactor and a behaviour change in one PR are two reviews for
          whoever reads it.
        </Li>
        <Li>
          Commit messages: imperative mood, sentence case, no type prefix e.g.{" "}
          <Code>Add an interactive rk setup wizard for first-run configuration</Code>.
        </Li>
        <Li>
          Update <Code>README.md</Code> and this site when you change a flag, a config key or an
          exit code.
        </Li>
        <Li>
          CI must be green on every leg: three operating systems by three Python versions, all nine
          required. <Code>main</Code> takes no direct pushes and keeps a linear history, so PRs land
          squashed or rebased.
        </Li>
      </Ul>
      <Callout kind="tip" title="Dogfooding is encouraged">
        <P>
          <Code>uv run roborak review --base main</Code> on your own branch, before you ask anyone
          else to read it.
        </P>
      </Callout>

      <H2>Working on this website</H2>
      <P>
        The site lives in <Code>docs/</Code>: Vite, React Router and Tailwind, deployed to Cloudflare Pages.
        Node 20 or newer.
      </P>
      <CodeBlock
        shell
        code={[
          "cd docs",
          "npm ci",
          "npm run dev         # local dev server",
          "npm run typecheck",
          "npm run build       # bundle to docs/dist",
        ].join("\n")}
      />
      <P>
        Docs content is derived from <Code>README.md</Code>, <Code>CONTRIBUTING.md</Code>, the Typer{" "}
        <Code>help=</Code> strings and <Code>src/roborak/config_template.yaml</Code>. When you
        change one of those, change the page that quotes it a reference page that quietly
        disagrees with <Code>--help</Code> is worse than none.
      </P>

      <H2>The rest</H2>
      <Ul>
        <Li>
          <A href={`${REPO_URL}/blob/main/CONTRIBUTING.md`}>CONTRIBUTING.md</A> the full document,
          including the release procedure.
        </Li>
        <Li>
          <A href={`${REPO_URL}/blob/main/SECURITY.md`}>SECURITY.md</A> reporting a vulnerability.
        </Li>
        <Li>
          <A href={`${REPO_URL}/blob/main/CODE_OF_CONDUCT.md`}>CODE_OF_CONDUCT.md</A>.
        </Li>
      </Ul>
    </>
  );
}
