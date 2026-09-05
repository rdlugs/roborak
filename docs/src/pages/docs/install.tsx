import { PageHead } from "@/components/page-head";
import { Callout } from "@/components/callout";
import { CodeBlock } from "@/components/code-block";
import { Code, H1, H2, Lead, Li, P, Ul } from "@/components/prose";
import { Step, Steps } from "@/components/steps";
import { A } from "@/components/ui";

export default function Install() {
  return (
    <>
      <PageHead title="Install" description="Install roborak with uvx, uv tool, pipx or from a checkout, and point it at a model." />
      <H1>Install</H1>
      <Lead>
        roborak is a Python package with two entry points <Code>roborak</Code> and the shorter{" "}
        <Code>rk</Code>. It needs Python 3.12 or newer and an API key for whichever model you
        already use.
      </Lead>

      <H2>Try it without installing</H2>
      <P>
        <Code>uvx</Code> fetches roborak into a throwaway environment and runs it. Nothing is left
        behind on your machine.
      </P>
      <CodeBlock shell code={"uvx roborak review"} />

      <H2>Install it properly</H2>
      <Steps>
        <Step n={1} title="Put the CLI on your PATH">
          <CodeBlock
            shell
            code={"uv tool install roborak\n# or\npipx install roborak"}
          />
        </Step>
        <Step n={2} title="Give it a model to talk to">
          <P>
            roborak routes through <A href="https://docs.litellm.ai/docs/providers">LiteLLM</A>, so
            any provider it supports works. Export the key that provider expects:
          </P>
          <CodeBlock
            shell
            code={
              "export ANTHROPIC_API_KEY=...\n# or OPENAI_API_KEY, GEMINI_API_KEY, and so on"
            }
          />
        </Step>
        <Step n={3} title="Review something" last>
          <CodeBlock shell code={"roborak review"} />
          <P>
            That reviews everything differing from your base branch. It is the whole quick start
            everything past this page is optional.
          </P>
        </Step>
      </Steps>

      <Callout kind="tip" title="Guided setup">
        <P>
          <Code>roborak setup</Code> walks through the model, the key and your forge tokens with
          arrow-key prompts, and writes the result to a config file for you.
        </P>
      </Callout>

      <H2>From a checkout</H2>
      <P>Working on roborak itself, or running an unreleased revision:</P>
      <CodeBlock shell code={"uv sync\nuv run roborak review"} />

      <H2>Keys in the config file</H2>
      <P>
        Keys can live in the config file instead of the shell, per provider useful when a checkout
        needs credentials your shell does not carry.
      </P>
      <CodeBlock
        label=".roborak.yaml"
        code={
          "llm:\n  api_keys:\n    anthropic: sk-ant-...\n    openai: sk-...\n  api_base: http://localhost:11434   # optional: proxy, Azure, or a local Ollama"
        }
      />
      <Ul>
        <Li>A configured key wins over the provider&apos;s environment variable.</Li>
        <Li>
          Setting <Code>api_base</Code> alone is enough for endpoints that need no key.
        </Li>
        <Li>
          <Code>roborak config init --global</Code> scaffolds{" "}
          <Code>~/.config/roborak/config.yaml</Code> and creates it mode 600.
        </Li>
      </Ul>

      <Callout kind="warn" title="These are real secrets on disk">
        <P>
          Keep them in <Code>~/.config/roborak/config.yaml</Code>, or in a{" "}
          <Code>.roborak.yaml</Code> / <Code>.roborak.yml</Code> your repository ignores.{" "}
          <Code>roborak config show</Code> redacts them; git does not.
        </P>
      </Callout>
    </>
  );
}
