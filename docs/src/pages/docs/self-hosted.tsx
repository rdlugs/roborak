import { PageHead } from "@/components/page-head";
import { Callout } from "@/components/callout";
import { CodeBlock } from "@/components/code-block";
import { Code, H1, H2, Lead, Li, P, Ul } from "@/components/prose";
import { A } from "@/components/ui";

export default function SelfHosted() {
  return (
    <>
      <PageHead title="Self-hosted forges" description="Point roborak at your own GitLab or GitHub Enterprise with forge.hosts." />
      <H1>Self-hosted forges</H1>
      <Lead>
        Point roborak at your own GitLab or GitHub Enterprise with{" "}
        <Code>forge.hosts</Code>. Most of the time you will not need to: the checkout&apos;s git
        remote already says where the code lives.
      </Lead>

      <H2>Setting a host</H2>
      <CodeBlock
        label=".roborak.yaml"
        code={[
          "forge:",
          "  hosts:",
          "    gitlab: gitlab.acme.com",
          "    github: http://gh.local:8080",
        ].join("\n")}
      />
      <Ul>
        <Li>
          <Code>https</Code> is assumed. Give a scheme only for a plain-http instance.
        </Li>
        <Li>
          URL paths are not supported. For a one-off, pass a full URL to <Code>--mr</Code> or{" "}
          <Code>--pr</Code> instead.
        </Li>
      </Ul>

      <H2>The remote wins</H2>
      <Callout kind="note" title="hosts is a fallback, not an override">
        <P>
          <Code>forge.hosts</Code> is used only when the repository&apos;s git remote does not
          already answer the question. A domain set here never overrides a checkout whose remote
          points somewhere else so one user-wide config can name your company instance without
          breaking the day you clone something from gitlab.com.
        </P>
      </Callout>

      <H2>A one-off, without configuring anything</H2>
      <CodeBlock
        shell
        code={[
          "rk review --mr https://gitlab.acme.com/team/web/-/merge_requests/298",
          "rk review --pr https://gh.local:8080/team/web/pull/42",
        ].join("\n")}
      />
      <P>
        A full URL carries its own host, so nothing needs to be configured for it to work.
      </P>

      <H2>Credentials</H2>
      <P>
        Self-hosted instances use the same token resolution as the public ones see{" "}
        <A href="/docs/tokens">Tokens</A>. Inside a self-hosted GitLab&apos;s own CI,{" "}
        <Code>CI_JOB_TOKEN</Code> is picked up automatically.
      </P>
    </>
  );
}
