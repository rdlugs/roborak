import { PageHead } from "@/components/page-head";
import { Callout } from "@/components/callout";
import { CodeBlock } from "@/components/code-block";
import { Code, H1, H2, Lead, Li, P, Ul } from "@/components/prose";
import { Table } from "@/components/table";
import { A } from "@/components/ui";

export default function Rules() {
  return (
    <>
      <PageHead title="Custom rules" description="Teach roborak the conventions only your team holds, as markdown files under .roborak/rules/." />
      <H1>Custom rules</H1>
      <Lead>
        Standards a linter cannot express, written in plain language. A rule is a markdown file
        under <Code>.roborak/rules/</Code>; the model reads the ones that match the files a change
        touched.
      </Lead>

      <H2>The file format</H2>
      <CodeBlock
        label=".roborak/rules/no-raw-sql.md"
        code={[
          "---",
          "id: no-raw-sql",
          'paths: ["app/**/*.php"]',
          "severity: major",
          "category: security",
          "---",
          "Never build SQL by string concatenation. Use the query builder or bound parameters.",
        ].join("\n")}
      />
      <P>
        Frontmatter is optional. A file containing one sentence is a valid rule, named after the
        file.
      </P>

      <Table
        minWidth={620}
        columns={[
          { key: "key", header: "Key", mono: true, width: 2 },
          { key: "what", header: "What it does", width: 5 },
        ]}
        rows={[
          { key: "id", what: "Stable identifier for the rule. Defaults to the filename." },
          {
            key: "paths",
            what: "Glob patterns. The rule only enters the prompt when a changed file matches one.",
          },
          { key: "severity", what: "critical, major, minor or info the grade a violation gets." },
          {
            key: "category",
            what: "security, bug, performance, logic, maintainability, testing, style or docs.",
          },
        ]}
      />

      <Callout kind="tip" title="Token cost stays flat">
        <P>
          Only rules matching the changed files enter the prompt, so a rule set can grow to hundreds
          of files without making every review more expensive.
        </P>
      </Callout>

      <H2>Working with them</H2>
      <CodeBlock
        shell
        code={[
          "rk rules init                       # scaffold .roborak/rules/ with a worked example",
          "rk rules list                       # every rule roborak will apply here",
          "rk rules test .roborak/rules/no-raw-sql.md app/Models/User.php",
        ].join("\n")}
      />
      <Ul>
        <Li>
          <Code>rules test</Code> validates the frontmatter and tells you whether the rule&apos;s{" "}
          <Code>paths</Code> actually match the file you name which is where most rules that
          &quot;do nothing&quot; turn out to be wrong.
        </Li>
        <Li>
          <Code>rules list</Code> resolves <Code>rules_dir</Code> the same way a review does, so it
          shows what will really be applied, not what is on disk somewhere.
        </Li>
      </Ul>

      <H2>Where a rule belongs</H2>
      <P>
        A rule is for a convention only your team holds the sort of thing a new reviewer would be
        told in their first week and a linter has no opinion about. If a linter <em>can</em> express
        it, write it there instead: roborak already runs your linters and feeds the results to the
        model as evidence. See <A href="/docs/static-analysis">Static analysis</A>.
      </P>
      <P>
        For conventions that describe the whole repository rather than a path, use{" "}
        <Code>AGENTS.md</Code>, <Code>CLAUDE.md</Code>, <Code>.roborak/context.md</Code> or{" "}
        <Code>CONTRIBUTING.md</Code> roborak reads the first it finds. See{" "}
        <A href="/docs/configuration">Configuration</A>.
      </P>
    </>
  );
}
