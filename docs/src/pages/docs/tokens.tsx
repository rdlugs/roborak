import { PageHead } from "@/components/page-head";
import { Callout } from "@/components/callout";
import { CodeBlock } from "@/components/code-block";
import { Code, H1, H2, Lead, Li, P, Ul } from "@/components/prose";
import { Table } from "@/components/table";
import { A } from "@/components/ui";

export default function Tokens() {
  return (
    <>
      <PageHead title="Tokens" description="Which forge credentials roborak reads, in what order, and what scope each operation needs." />
      <H1>Tokens</H1>
      <Lead>
        Reading a merge request or pull request needs a forge credential. roborak looks in several
        places, in a fixed order, and never asks for more scope than the operation needs.
      </Lead>

      <H2>GitLab</H2>
      <Table
        minWidth={620}
        columns={[
          { key: "src", header: "Source", mono: true, width: 3 },
          { key: "note", header: "Notes", width: 5 },
        ]}
        rows={[
          { src: "ROBORAK_GITLAB_TOKEN", note: "Checked first. Use it when GITLAB_TOKEN is already taken by something else." },
          { src: "GITLAB_TOKEN", note: "The conventional variable." },
          { src: "CI_JOB_TOKEN", note: "Provided automatically inside GitLab CI." },
          { src: "forge.tokens.gitlab", note: "From the config file. Loses to both ROBORAK_* and the plain variable." },
        ]}
      />

      <H2>GitHub</H2>
      <Table
        minWidth={620}
        columns={[
          { key: "src", header: "Source", mono: true, width: 3 },
          { key: "note", header: "Notes", width: 5 },
        ]}
        rows={[
          { src: "ROBORAK_GITHUB_TOKEN", note: "Checked first." },
          { src: "GITHUB_TOKEN", note: "The conventional variable, and what Actions injects." },
          { src: "gh auth token", note: "Falls back to the GitHub CLI's stored credential if you are logged in." },
          { src: "forge.tokens.github", note: "From the config file." },
        ]}
      />

      <CodeBlock
        shell
        code={[
          "export GITLAB_TOKEN=glpat-...",
          "export GITHUB_TOKEN=ghp_...",
          "# or, if you already use the GitHub CLI:",
          "gh auth login",
        ].join("\n")}
      />

      <Callout kind="warn" title="Tokens in the config file are secrets on disk">
        <P>
          <Code>forge.tokens</Code> is convenient for a checkout whose shell does not carry
          credentials, but it is a plaintext secret. Keep it in{" "}
          <Code>~/.config/roborak/config.yaml</Code>, or in a <Code>.roborak.yaml</Code> /{" "}
          <Code>.roborak.yml</Code> your
          repository ignores. <Code>roborak config show</Code> redacts them; git does not.
        </P>
      </Callout>

      <H2>What the token needs to do</H2>
      <Ul>
        <Li>
          <b>Reading</b> a merge request or pull request: read access to the repository and its
          discussions.
        </Li>
        <Li>
          <b>Publishing</b> with <Code>--post</Code>: permission to create discussions and notes on
          the merge/pull request. Nothing is pushed, and no branch is written.
        </Li>
        <Li>
          <b>Issue context</b> with <Code>--issue</Code>: read access to the issue tracker.
        </Li>
      </Ul>
      <P>
        Pointing roborak at a self-hosted instance is a separate setting see{" "}
        <A href="/docs/self-hosted">Self-hosted forges</A>.
      </P>
    </>
  );
}
