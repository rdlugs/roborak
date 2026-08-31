import { PageHead } from "@/components/page-head";
import { Callout } from "@/components/callout";
import { CodeBlock } from "@/components/code-block";
import { Code, H1, H2, Lead, Li, P, Ul } from "@/components/prose";
import { Table } from "@/components/table";
import { A } from "@/components/ui";

export default function StaticAnalysis() {
  return (
    <>
      <PageHead title="Static analysis" description="How roborak runs your linters as evidence, and the sandbox it puts around repository tooling in CI." />
      <H1>Static analysis</H1>
      <Lead>
        roborak runs your linters and hands the model what they found. The interesting part is not
        that it runs them it is the trust boundary it puts around doing so.
      </Lead>

      <H2>What it runs</H2>
      <P>
        Whichever of ruff, mypy, semgrep, eslint, phpstan, actionlint, hadolint and checkov the
        repository actually has, using <em>the project&apos;s own config</em> the rules a team
        already agreed to, not a set roborak imposes. With <Code>tools: null</Code> it autodetects
        what is on <Code>PATH</Code>.
      </P>
      <Ul>
        <Li>
          Findings on lines the change did not touch are dropped, so a linted file&apos;s
          pre-existing debt never lands on the author of an unrelated diff.
        </Li>
        <Li>
          What survives is fed to the model as evidence to confirm or explain, rather than reported
          raw. <Code>feed_to_llm: false</Code> reports it directly instead.
        </Li>
      </Ul>
      <CodeBlock shell code={"rk review --no-llm    # static analysis only; makes no model calls"} />

      <H2>Scanners that would reach the network</H2>
      <P>
        One adapter is deliberately excluded from autodetection. <Code>osv-scanner</Code> queries a
        vulnerability service, and the static pass promises it installs nothing and fetches
        nothing so it runs only when a project names it explicitly:
      </P>
      <CodeBlock code={"static:\n  tools: [\"ruff\", \"osv-scanner\"]   # naming it is the opt-in"} />
      <P>
        A scanner that applied to the change and was not installed is named in the review&apos;s
        supply-chain section, so a clean report cannot be mistaken for a checked one.
      </P>

      <H2>Why this needs a trust boundary</H2>
      <Callout kind="warn" title="Static analysers execute repository code">
        <P>
          They load repository binaries, plugins and configuration. Running one against a checkout
          is running that checkout&apos;s code which is fine for your own branch, and not fine for
          an untrusted contribution in CI.
        </P>
      </Callout>

      <H2>Execution modes</H2>
      <Table
        minWidth={640}
        columns={[
          { key: "mode", header: "static.execution", mono: true, width: 2 },
          { key: "what", header: "What happens", width: 5 },
        ]}
        rows={[
          {
            mode: "auto",
            what: "The default. Runs directly for local work, where the checkout is yours. In CI it runs through Bubblewrap with a read-only filesystem and no network and if Bubblewrap is unavailable, the static pass is skipped rather than running untrusted code directly.",
          },
          {
            mode: "trusted",
            what: "Explicitly allows direct execution in CI. The same as passing --trust-static. For a checkout you control.",
          },
          { mode: "off", what: "Disables static tools entirely. The same as --no-static." },
        ]}
      />
      <P>
        Every static subprocess receives a credential-scrubbed environment in all three modes.
      </P>

      <H2>CI ignores the working tree&apos;s config</H2>
      <P>
        In CI, roborak does not read <Code>.roborak.yaml</Code> from the working tree a branch
        could otherwise redirect an API key or opt itself into trusted execution simply by editing a
        file.
      </P>
      <P>Put CI settings somewhere the branch cannot reach:</P>
      <Ul>
        <Li>
          <Code>ROBORAK_*</Code> environment variables, set on the job.
        </Li>
        <Li>
          The user config at <Code>~/.config/roborak/config.yaml</Code>.
        </Li>
        <Li>
          A trusted, base-controlled file passed with <Code>--config</Code>.
        </Li>
      </Ul>
      <P>
        The rest of the configuration surface is on <A href="/docs/configuration">Configuration</A>.
      </P>
    </>
  );
}
