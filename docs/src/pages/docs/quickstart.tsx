import { PageHead } from "@/components/page-head";
import { Callout } from "@/components/callout";
import { CodeBlock } from "@/components/code-block";
import { Code, H1, H2, H3, Lead, Li, P, Ul } from "@/components/prose";
import { Table } from "@/components/table";
import { A } from "@/components/ui";

export default function Quickstart() {
  return (
    <>
      <PageHead title="Quickstart" description="Review a local diff, a GitLab merge request or a GitHub pull request, and read the exit codes." />
      <H1>Quickstart</H1>
      <Lead>
        One command covers the common case. The flags below narrow what is reviewed, where the
        result goes, and how loudly it fails.
      </Lead>

      <H2>Reviewing a local diff</H2>
      <CodeBlock
        shell
        code={[
          "roborak review                       # everything that differs from the base branch",
          "roborak review --base main           # compare against a specific ref",
          "roborak review --uncommitted         # staged and unstaged edits only",
          "roborak review --committed --base main",
          "roborak review --include-untracked",
        ].join("\n")}
      />

      <H3>A directory without git</H3>
      <CodeBlock
        shell
        code={[
          "roborak review -C /path/to/codebase      # every file, reviewed whole",
          "roborak review -C /path/to/codebase/src  # or just one subtree",
        ].join("\n")}
      />
      <P>
        A directory that is not a git repository has no baseline to diff against, so roborak
        reviews every eligible file whole instead of refusing. The walk never descends into
        dependency, build-output, cache or VCS directories — <Code>node_modules</Code>,{" "}
        <Code>vendor</Code>, <Code>dist</Code>, <Code>build</Code>, <Code>target</Code>,{" "}
        <Code>__pycache__</Code>, <Code>.venv</Code>, <Code>.git</Code> and friends — and it
        honours the configured <Code>ignore_paths</Code>. Binary files and anything over 512 KiB
        are reported as omissions rather than reviewed, and static analysis still runs. The flags
        that name a diff (<Code>--base</Code>, <Code>--committed</Code>, <Code>--uncommitted</Code>,{" "}
        <Code>--include-untracked</Code>) have nothing to compare here and are refused.
      </P>

      <H3>Turning halves of the pipeline off</H3>
      <CodeBlock
        shell
        code={[
          "roborak review --no-llm              # static analysis only; no API key needed",
          "roborak review --no-static           # model only, skip the linters",
          "roborak review --no-verify           # don't run the project's own test commands",
          "roborak review --no-walkthrough      # skip the overview; one model call, not two",
        ].join("\n")}
      />
      <P>
        <Code>--no-llm</Code> is the cheapest way to see roborak working: it runs your linters, maps
        their output onto the diff and renders it through the same report as a full review.
      </P>

      <H2>GitLab and GitHub</H2>
      <CodeBlock
        shell
        code={[
          "roborak review --mr 298              # a GitLab merge request",
          "roborak review --mr https://gitlab.com/acme/web/-/merge_requests/298",
          "roborak review --pr 42               # a GitHub pull request",
          "roborak review --mr 298 --post       # publish inline threads + a summary",
          "roborak review --mr 298 --post --repost   # re-post findings already sent",
          "roborak review --mr 298 --no-discussions  # ignore existing MR discussion",
          "roborak review --mr 298 --no-post    # review it, never ask about publishing",
        ].join("\n")}
      />
      <P>
        Publishing needs a forge token. See <A href="/docs/tokens">Tokens</A> for which variables
        are read and in what order.
      </P>

      <H2>Judging a diff against its issue</H2>
      <P>
        With <Code>--issue</Code>, roborak reads what was asked for and reports the requirements the
        change does not meet, as <Code>requirement_gap</Code> findings in their own section.
      </P>
      <CodeBlock
        shell
        code={[
          "roborak review --issue 42            # review whatever MR/PR implements issue 42",
          "roborak review --issue https://gitlab.com/acme/web/-/issues/42",
          "roborak review --mr 298 --issue 42   # review MR 298, judged against issue 42",
          "roborak review --issue 42 --base main     # local diff, judged against issue 42",
        ].join("\n")}
      />

      <H2>Output and filtering</H2>
      <CodeBlock
        shell
        code={[
          "roborak review --json                # full result as JSON",
          "roborak review --agent               # JSON for another agent to act on",
          "roborak review --prompt-only         # findings as fix instructions",
          "roborak review --markdown report.md  # walkthrough-style markdown report",
          "roborak review --full                # add the agent prompts and the review info",
          "roborak review --panels              # one finding to a panel, not the report",
          "roborak review -m openai/gpt-5       # any LiteLLM model string",
          "roborak review -s major              # only major and critical",
          "roborak review --fail-on critical    # non-zero exit for CI",
          "roborak review > review.md           # piped: raw markdown, chrome on stderr",
        ].join("\n")}
      />
      <Ul>
        <Li>
          Attached to a terminal, roborak prints the report. Redirected, it writes the raw markdown
          to stdout and keeps its progress chrome on stderr, so a pipe never picks up spinner noise.
        </Li>
        <Li>
          Every mode renders the same document, so the terminal, the file and the MR comment cannot
          disagree about what the review said.
        </Li>
      </Ul>

      <H2>Exit codes</H2>
      <Table
        minWidth={520}
        columns={[
          { key: "code", header: "Exit code", mono: true, width: 1 },
          { key: "meaning", header: "Meaning", width: 4 },
        ]}
        rows={[
          { code: "0", meaning: "Review completed." },
          { code: "1", meaning: "Findings at or above --fail-on." },
          {
            code: "2",
            meaning:
              "Operational error or partial review failed chunks, unavailable forge patches, or a requested publish that did not complete.",
          },
        ]}
      />
      <Callout kind="note" title="Wiring it into CI">
        <P>
          <Code>--fail-on</Code> is what turns a review into a gate. Exit 2 is deliberately distinct
          from exit 1: a job that cannot tell &quot;found problems&quot; from &quot;could not
          finish&quot; will eventually pass a review that never ran.
        </P>
      </Callout>

      <H2>The other commands</H2>
      <P>
        Each takes the same <Code>--mr</Code> / <Code>--pr</Code> / <Code>--issue</Code> /{" "}
        <Code>--base</Code> targeting as <Code>review</Code>.
      </P>
      <CodeBlock
        shell
        code={[
          'roborak describe                     # title, overview, per-file table, mermaid flow',
          "roborak improve                      # suggestions only, every one committable",
          'roborak ask "why is this locked?"    # a question answered from the diff',
        ].join("\n")}
      />
      <P>
        The full flag list for every command lives in <A href="/docs/commands">Commands</A>.
      </P>
    </>
  );
}
