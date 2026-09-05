import { PageHead } from "@/components/page-head";
import { Cmd } from "@/components/cmd";
import { Callout } from "@/components/callout";
import { CodeBlock } from "@/components/code-block";
import { Code, H1, H2, Lead, Li, P, Rule, Ul } from "@/components/prose";
import { Table } from "@/components/table";
import { A } from "@/components/ui";
import {
  ASK_GROUPS,
  DESCRIBE_GROUPS,
  GLOBAL_FLAGS,
  IMPROVE_GROUPS,
  REVIEW_GROUPS,
} from "@/content/commands";

export default function Commands() {
  return (
    <>
      <PageHead title="Commands" description="Full flag reference for review, describe, improve, ask, rules, config and setup." />
      <H1>Commands</H1>
      <Lead>
        Seven commands, one shared way of naming a change. <Code>roborak</Code> and{" "}
        <Code>rk</Code> are the same executable, and a bare invocation falls through to{" "}
        <Code>review</Code>.
      </Lead>

      <Table
        minWidth={560}
        columns={[
          { key: "cmd", header: "Command", mono: true, width: 2 },
          { key: "what", header: "What it does", width: 4 },
        ]}
        rows={[
          { cmd: "review", what: "Review changes and report findings." },
          {
            cmd: "describe",
            what: "Summarise a change: title, overview, per-file table, and a flow diagram.",
          },
          { cmd: "improve", what: "Propose concrete, committable improvements to the changed code." },
          { cmd: "ask", what: "Ask a question about the change, answered from the diff." },
          { cmd: "rules", what: "Inspect and test the project's review rules." },
          { cmd: "config", what: "Inspect and scaffold roborak's configuration." },
          { cmd: "setup", what: "Answer a few questions and write the config they imply." },
        ]}
      />

      <Cmd
        name="review"
        synopsis="roborak review [OPTIONS]"
        summary="Review changes and report findings. This is the default: running roborak with no command runs this one."
        groups={[...REVIEW_GROUPS, ...GLOBAL_FLAGS]}
        example={"rk review --mr 298 --issue 42 --severity major --post"}
      />

      <Rule />

      <Cmd
        name="describe"
        synopsis="roborak describe [OPTIONS]"
        summary="Summarise a change: title, overview, per-file table, and a mermaid flow diagram. No findings this is the half of a review that explains rather than judges."
        groups={DESCRIBE_GROUPS}
        example={"rk describe --pr 42 --markdown walkthrough.md"}
      />

      <Cmd
        name="improve"
        synopsis="roborak improve [OPTIONS]"
        summary="Propose concrete improvements to the changed code. Every suggestion is committable if it cannot be expressed as a diff, it does not appear."
        groups={IMPROVE_GROUPS}
        example={"rk improve --uncommitted --prompt-only"}
      />

      <Cmd
        name="ask"
        synopsis='roborak ask "your question" [OPTIONS]'
        summary="Ask a question about the change and get an answer grounded in the diff rather than in the model's memory of similar code."
        groups={ASK_GROUPS}
        example={'rk ask "why is this locked?" --mr 298'}
      />

      <Rule />

      <H2>rules</H2>
      <P>
        Project rules are markdown files under <Code>.roborak/rules/</Code>. See{" "}
        <A href="/docs/rules">Custom rules</A> for the file format.
      </P>
      <CodeBlock
        shell
        code={[
          "rk rules init                     # create the rules directory with a worked example",
          "rk rules list                     # every rule roborak will apply in this repository",
          "rk rules test <rule.md> [target]  # validate one rule, and check its scope",
        ].join("\n")}
      />
      <Ul>
        <Li>
          <Code>rules init</Code> and <Code>rules list</Code> take <Code>--dir, -C</Code>.
        </Li>
        <Li>
          <Code>rules test</Code> takes the rule file, and optionally a source file to check the
          rule&apos;s scope against.
        </Li>
      </Ul>

      <H2>config</H2>
      <CodeBlock
        shell
        code={[
          "rk config init                    # write a commented .roborak.yaml with every default",
          "rk config init --global           # …or ~/.config/roborak/.roborak.yaml, mode 600",
          "rk config init --force            # overwrite an existing file",
          "rk config show                    # the effective config, all layers merged",
        ].join("\n")}
      />
      <Callout kind="note">
        <P>
          <Code>config show</Code> redacts API keys. It is the fastest way to answer &quot;which
          layer set this?&quot; see <A href="/docs/configuration">Configuration</A> for the
          precedence chain it is resolving.
        </P>
      </Callout>

      <H2>setup</H2>
      <P>
        A guided first run: it asks for a model, a key and your forge tokens with arrow-key prompts,
        then writes the config those answers imply. Takes <Code>--dir, -C</Code> and{" "}
        <Code>--force</Code>.
      </P>
      <CodeBlock shell code={"rk setup"} />
      <P>
        Outside a TTY it falls back to reading answers from the environment rather than hanging on a
        prompt, and any file it writes with a key in it is created mode 600.
      </P>
    </>
  );
}
