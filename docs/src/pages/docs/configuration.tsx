import { PageHead } from "@/components/page-head";
import { Callout } from "@/components/callout";
import { CodeBlock } from "@/components/code-block";
import { Code, H1, H2, H3, Lead, Li, P, Ul } from "@/components/prose";
import { Table } from "@/components/table";
import { A } from "@/components/ui";

const PRECEDENCE = [
  ["1", "CLI flags", "--model, --severity, --config, and the rest."],
  ["2", "ROBORAK_* environment variables", "Set per shell or per CI job."],
  ["3", "Project .roborak.yaml", "The repository root. Ignored in CI see the warning below."],
  ["4", "~/.config/roborak/config.yaml", "User-wide. Where secrets belong."],
  ["5", "Built-in defaults", "Everything you did not set."],
];

export default function Configuration() {
  return (
    <>
      <PageHead title="Configuration" description="Every key in .roborak.yaml, the precedence chain that resolves them, and where secrets belong." />
      <H1>Configuration</H1>
      <Lead>
        Every key is optional. <Code>.roborak.yaml</Code> in the repository root holds project
        settings; <Code>~/.config/roborak/config.yaml</Code> holds the ones that follow you between
        checkouts.
      </Lead>

      <H2>Precedence</H2>
      <P>Highest wins. A layer only supplies the keys it actually names.</P>
      <Table
        minWidth={620}
        columns={[
          { key: "n", header: "", mono: true, width: 1 },
          { key: "layer", header: "Layer", mono: true, width: 4 },
          { key: "note", header: "Notes", width: 5 },
        ]}
        rows={PRECEDENCE.map(([n, layer, note]) => ({ n, layer, note }))}
      />
      <P>
        <Code>roborak config show</Code> prints the result after every layer has merged, with
        secrets redacted. It is the fastest way to answer &quot;where did this value come from?&quot;
      </P>

      <H2>Writing the file</H2>
      <Ul>
        <Li>
          <Code>roborak config init</Code> writes a fully commented <Code>.roborak.yaml</Code> with
          every option at its default. This is the manual path, and the annotated file to edit.
        </Li>
        <Li>
          <Code>roborak config init --global</Code> writes{" "}
          <Code>~/.config/roborak/config.yaml</Code> instead, mode 600.
        </Li>
        <Li>
          <Code>roborak setup</Code> is the guided path: it asks for a model, a credential and
          optional forge tokens, then writes <em>only</em> those keys a sparse file, so every
          other default stays live across upgrades.
        </Li>
      </Ul>
      <P>
        In a terminal, <Code>setup</Code>&apos;s closed questions are arrow-key lists ending in{" "}
        <Code>Other (type it in)…</Code>, because the model list can only be a starting point:
        roborak takes any LiteLLM model string. Without a terminal the same questions come back as
        line prompts reading stdin, so{" "}
        <Code>printf &apos;1\n\nsk-ant-…\n\n\n&apos; | rk setup</Code> works. With nothing on stdin
        it writes nothing and exits 0 rather than waiting.
      </P>

      <H2>The keys</H2>

      <H3>llm</H3>
      <CodeBlock
        label=".roborak.yaml"
        code={[
          "llm:",
          "  model: anthropic/claude-sonnet-5",
          "  fallback_models: []       # tried in order if the primary model fails",
          "  temperature: 0.2",
          "  max_tokens: 8000",
          "  context_budget: null      # prompt token ceiling; null derives it from the model",
          "  api_keys: {}              # per-provider, overriding the provider's env var",
          "  api_base: null            # proxy, Azure deployment, or a local Ollama",
        ].join("\n")}
      />
      <P>
        Setting <Code>api_base</Code> skips the missing-key check, which is what makes a local
        endpoint work with no credential at all.
      </P>

      <H3>forge</H3>
      <CodeBlock
        label=".roborak.yaml"
        code={[
          "forge:",
          "  tokens: {}                       # gitlab: glpat-… / github: ghp_…",
          "  hosts: {}                        # self-hosted instances; see Self-hosted forges",
          "  max_recovered_file_bytes: 1048576",
        ].join("\n")}
      />
      <P>
        <Code>max_recovered_file_bytes</Code> caps how much of a forge-truncated file roborak will
        fetch when reconstructing a patch the API would not give it whole.
      </P>

      <H3>review</H3>
      <CodeBlock
        label=".roborak.yaml"
        code={[
          "review:",
          "  categories: [security, bug, performance, logic]",
          "  severity_floor: minor         # findings below this are not reported",
          "  max_findings: 25",
          "  committable_suggestions: true # emit replacement code you can commit as-is",
          "  min_confidence: 0.5           # drop findings the model was not sure about",
          "  require_evidence: true        # a critical/major model finding must show its evidence",
          "  full_file: false              # allow findings on untouched lines",
          "  check_requirements: true      # with --issue, report requirements the change misses",
          "  include_discussions: true     # unresolved MR/PR comments as bounded context",
        ].join("\n")}
      />
      <Callout kind="note" title="What require_evidence buys you">
        <P>
          A <Code>critical</Code> or <Code>major</Code> model finding has to say what makes it true
          — the trigger and the failure path, a violated contract, or a reproduction. One that
          cannot is demoted to a <Code>minor</Code> <Code>verification_needed</Code>: still
          reported, still anchored, no longer counted by the pre-merge verdict. A self-assigned{" "}
          <Code>confidence</Code> is the model grading its own homework, and it is not grounds to
          fail a build. Static-analyser findings are exempt, because a tool ran.
        </P>
      </Callout>
      <Callout kind="note" title="Why full_file defaults to off">
        <P>
          Untouched code is not what the author asked about, and reviewing it is the single largest
          source of noise. Turn it on deliberately, for an audit rather than a review.
        </P>
      </Callout>
      <P>
        The categories are the eight roborak knows: <Code>security</Code>, <Code>bug</Code>,{" "}
        <Code>performance</Code>, <Code>logic</Code>, <Code>maintainability</Code>,{" "}
        <Code>testing</Code>, <Code>style</Code>, <Code>docs</Code>. Severities run{" "}
        <Code>critical</Code>, <Code>major</Code>, <Code>minor</Code>, <Code>info</Code>.
      </P>

      <H3>static</H3>
      <CodeBlock
        label=".roborak.yaml"
        code={[
          "static:",
          "  enabled: true",
          "  execution: auto      # auto | trusted | off see Static analysis",
          "  tools: null          # null autodetects whatever is on PATH",
          "  timeout_seconds: 90",
          "  feed_to_llm: true    # pass findings to the model as evidence, not raw output",
        ].join("\n")}
      />
      <P>
        <A href="/docs/static-analysis">Static analysis</A> covers what each{" "}
        <Code>execution</Code> mode actually does, and why the default is not simply
        &quot;run it&quot;.
      </P>

      <H3>verification</H3>
      <CodeBlock
        label=".roborak.yaml"
        code={[
          "verification:",
          "  enabled: true          # no command configured = nothing runs, and no claim is made",
          "  execution: auto        # auto | trusted | off, as for static",
          "  commands:              # argv arrays, never shell strings",
          '    - paths: ["src/roborak/context/**"]',
          '      command: ["uv", "run", "pytest", "tests/test_context.py"]',
          '    - paths: ["src/roborak/core/**"]',
          '      command: ["uv", "run", "pytest", "tests/test_pipeline.py"]',
          "  broaden_paths:         # a change touching one of these runs fallback instead",
          '    - "src/roborak/core/models.py"',
          '    - "pyproject.toml"',
          '  fallback: ["uv", "run", "pytest"]',
          "  timeout_seconds: 300",
          "  max_commands: 4        # ceiling on how many commands one review runs",
          "  max_output_lines: 40   # per command; a citation, not a build log",
          "  feed_to_llm: true",
        ].join("\n")}
      />
      <P>
        Verification runs the project&apos;s own tests so a review about changed behaviour has an
        execution record behind it. Selection is proportional: the commands whose{" "}
        <Code>paths</Code> match the changed files, deduplicated and capped. A change touching a{" "}
        <Code>broaden_paths</Code> entry — a shared contract, a schema, the build configuration —
        runs <Code>fallback</Code> <em>instead of</em> the targeted set, because a full suite
        already contains every subset of itself. A change matching nothing falls back to the broad
        check, and with no <Code>fallback</Code> configured it is simply not verified, which the
        report says in words rather than by omission.
      </P>
      <Callout kind="note" title="Commands come from the base revision">
        <P>
          Verification runs argv the repository authored, so the section is read from the revision
          being merged into and never from the working tree: reviewing a branch somebody else wrote
          must not run what that branch says to run. The consequence is worth knowing — a change
          that <em>adds</em> a verification command is not verified by it until it lands.{" "}
          <Code>--config</Code> is the deliberate override, and your own user config and{" "}
          <Code>ROBORAK_*</Code> environment are always yours.
        </P>
      </Callout>
      <P>
        Otherwise verification obeys the same execution model as{" "}
        <A href="/docs/static-analysis">static analysis</A>: direct locally, sandboxed in CI,
        skipped in CI when Bubblewrap is unavailable, and <Code>--trust-verify</Code> as the
        explicit opt-in. Nothing is installed and nothing is fetched, forge diffs that are not
        checked out are never verified, and every command gets a credential-scrubbed environment.
        A failed suite prints beside the pre-merge verdict without moving it: that verdict counts
        findings.
      </P>

      <H3>impact</H3>
      <CodeBlock
        label=".roborak.yaml"
        code={[
          "impact:",
          "  enabled: true             # trace changed symbols out to their consumers",
          "  max_nodes: 12             # boundaries traced per review",
          "  max_consumers_per_node: 5",
          "  max_files_scanned: 2000   # ceiling on the fallback walk",
          "  max_snippet_lines: 6",
          "  token_budget: 1500        # prompt tokens the consumer snippets may occupy",
          "  timeout_seconds: 10",
        ].join("\n")}
      />
      <P>
        Before the review runs, roborak works outward from the functions, classes, exported
        constants, routes, event names, configuration keys, environment variables and schema
        fields the change touched, and finds the unchanged code that depends on them. The
        consumers it finds are shown to the model as evidence about the changed lines, never as
        review surface: a finding anchored to a consumer is discarded, so a contract break is
        reported against the line that caused it.
      </P>
      <P>
        Every number is a ceiling, and a search that hits one says so rather than reporting an
        empty result. That distinction is the point — <Code>contained</Code> is only ever claimed
        for a symbol a parser identified and a search that completed, while{" "}
        <Code>no references found</Code> means the name did not match anywhere, which an alias, a
        re-export or a runtime lookup would also produce. A review of a directory with no git
        repository reports <Code>not applicable</Code>: every file is already under review, so
        there is no unchanged consumer to find.
      </P>

      <H3>supply_chain</H3>
      <CodeBlock
        label=".roborak.yaml"
        code={[
          "supply_chain:",
          "  enabled: true         # dependency delta + CI/container/IaC checklists",
          "  max_changes: 40       # dependency movements carried into the report",
          "  max_assets: 20        # manifest, lock and infrastructure files parsed",
          "  timeout_seconds: 10",
          "  feed_to_llm: true",
          "  token_budget: 1200    # prompt tokens the dependency section may occupy",
        ].join("\n")}
      />
      <P>
        <Code>ignore_paths</Code> excludes every lockfile from a review, and it should: a lockfile
        is generated data, and sending one to a model spends thousands of tokens asking it to do a
        diff badly. But that exclusion also hides an unexpected transitive package, a swapped
        registry, a checksum that quietly disappeared, or a manifest edit that never reached the
        lock. So the lockfile stays out of the prompt and a parser reads it instead.
      </P>
      <Table
        minWidth={640}
        columns={[
          { key: "eco", header: "Ecosystem", width: 2 },
          { key: "manifests", header: "Manifests", mono: true, width: 3 },
          { key: "locks", header: "Lockfiles", mono: true, width: 4 },
        ]}
        rows={[
          {
            eco: "npm / yarn / pnpm",
            manifests: "package.json",
            locks: "package-lock.json, npm-shrinkwrap.json, yarn.lock, pnpm-lock.yaml",
          },
          {
            eco: "Python",
            manifests: "pyproject.toml, requirements*.txt",
            locks: "uv.lock, poetry.lock, pdm.lock",
          },
          { eco: "Go", manifests: "go.mod", locks: "go.sum" },
          { eco: "Rust", manifests: "Cargo.toml", locks: "Cargo.lock" },
          { eco: "PHP", manifests: "composer.json", locks: "composer.lock" },
        ]}
      />
      <P>
        Anything else <Code>Gemfile.lock</Code>, <Code>Pipfile.lock</Code>,{" "}
        <Code>gradle.lockfile</Code>, <Code>mix.lock</Code> is named in the report as unanalysed
        rather than passed over in silence. Both sides are read whole from git and compared, so a
        reformat that moves every line produces no changes at all, and the old side is read at the{" "}
        <em>merge base</em>, so a dependency somebody else bumped on the base branch is not
        reported as part of this change. A forge diff that is not checked out has no readable
        sides and says <Code>unavailable</Code> instead of guessing.
      </P>
      <P>
        Changes to CI workflows, Dockerfiles, compose files and Terraform additionally turn on
        review checklists written for those trust boundaries workflow permissions and secret
        exposure, container privilege and mutable base images, IAM broadening and public access.
        Each is gated on the boundary actually being touched, so a Terraform-only change never
        pays for the npm checklist and an ordinary code change pays for none of them.
      </P>
      <P>
        Everything here stays offline. <Code>actionlint</Code>, <Code>hadolint</Code> and{" "}
        <Code>checkov</Code> run when the repository already has them; <Code>osv-scanner</Code> is
        supported but never autodetected, because it queries a vulnerability service see{" "}
        <A href="/docs/static-analysis">static analysis</A>. Nothing is ever installed.
      </P>

      <H3>output</H3>
      <CodeBlock
        label=".roborak.yaml"
        code={[
          "output:",
          "  walkthrough: true    # a second model call for the overview --post publishes",
          "  confirm_post: true   # offer to publish at the end of an interactive review",
          "  panels: false        # one finding to a panel instead of the report",
          "  full: false          # show the agent prompts and review info the terminal hides",
        ].join("\n")}
      />
      <P>
        Turning <Code>walkthrough</Code> off halves the cost of a run. Re-posting to a merge request
        whose files and hunks have not moved reuses the overview already published there rather than
        paying for it twice.
      </P>

      <H3>Paths, rules and language guidance</H3>
      <CodeBlock
        label=".roborak.yaml"
        code={[
          "ignore_paths:",
          '  - "**/*.lock"',
          '  - "**/*.min.js"',
          '  - "**/vendor/**"',
          '  - "**/node_modules/**"',
          '  - "**/dist/**"',
          "",
          "rules_dir: .roborak/rules",
          "",
          "language_instructions:",
          '  php: "Laravel 10 with the repository pattern; controllers stay thin."',
        ].join("\n")}
      />

      <H2>Repository conventions</H2>
      <P>
        Beyond the config file, roborak reads <Code>AGENTS.md</Code>, <Code>CLAUDE.md</Code>,{" "}
        <Code>.roborak/context.md</Code> or <Code>CONTRIBUTING.md</Code> the first one it finds
        so reviews match the conventions the repository already writes down.
      </P>
      <Callout kind="note" title="Read from the base revision">
        <P>
          When a base revision is available, those conventions are read from that revision, so a
          change cannot rewrite the instructions used to review itself.
        </P>
      </Callout>
    </>
  );
}
