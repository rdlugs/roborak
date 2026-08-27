import { Callout } from "@/components/callout";
import { CodeBlock } from "@/components/code-block";
import { Code, H1, H2, Lead, Li, P, Ul } from "@/components/prose";
import { PageHead } from "@/components/page-head";
import { Pipeline } from "@/components/pipeline";
import { A } from "@/components/ui";

export default function HowItWorks() {
  return (
    <>
      <PageHead title="How it works" description="The roborak pipeline one stage at a time: ChangeSet, line anchoring, the validator, routing and publishing." />
      <H1>How it works</H1>
      <Lead>
        One directional pipeline. Each stage knows only the stage before it which is what lets the
        terminal, the markdown file, the JSON and the merge request comment be the same review
        rather than four descriptions of one.
      </Lead>

      <div className="mb-10">
        <Pipeline orientation="vertical" />
      </div>

      <H2>ChangeSet is the universal IR</H2>
      <P>
        Local git, GitLab, GitHub and raw paths all normalise into one structure, so nothing
        downstream knows or cares where the code came from. Four sources, one set of behaviours.
      </P>

      <H2>Line anchoring</H2>
      <P>
        This is the correctness-critical part. Findings are always in new-file coordinates;{" "}
        <Code>Hunk.line_map</Code> records each line&apos;s position within the diff, and only the
        publishers translate that into a forge&apos;s position payload.
      </P>
      <Callout kind="note" title="Checked against the real files">
        <P>
          <Code>tests/test_local_git.py</Code> checks the computed numbers against the files on
          disk, so an off-by-one cannot agree with itself and pass.
        </P>
      </Callout>

      <H2>The validator refuses things</H2>
      <P>
        It drops findings that do not point at a changed line, snapping near misses onto the nearest
        one, then filters by confidence and severity and collapses duplicates.
      </P>
      <P>Most of roborak&apos;s usefulness is in what it refuses to say.</P>

      <H2>The static pass, as evidence</H2>
      <P>
        roborak runs whichever of ruff, mypy, semgrep, eslint and phpstan the repository actually
        has, using <em>the project&apos;s own config</em> the rules a team already agreed to.
      </P>
      <Ul>
        <Li>
          Findings on lines the change did not touch are dropped, so a linted file&apos;s
          pre-existing debt never lands on the author.
        </Li>
        <Li>
          What survives is fed to the model as evidence to confirm or explain, rather than reported
          raw.
        </Li>
      </Ul>
      <P>
        The trust boundary around running repository tooling has its own page:{" "}
        <A href="/docs/static-analysis">Static analysis</A>.
      </P>

      <H2>Every finding is routed, not just printed</H2>
      <Ul>
        <Li>
          A finding that points at a changed line and is worth interrupting for goes inline on the
          diff, where the author is already looking.
        </Li>
        <Li>A nitpick is folded into the summary, so the small stuff cannot drown the review.</Li>
        <Li>
          One that cannot be anchored is <em>reported</em> in the summary under a warning banner
          rather than discarded a finding nobody reading the merge request learns about may as
          well not exist.
        </Li>
      </Ul>
      <P>
        <Code>roborak.core.buckets</Code> is the one place that decides, so the terminal, the
        markdown report, the summary comment and the publisher cannot disagree about where a finding
        belongs.
      </P>

      <H2>Publishing</H2>
      <P>
        New-file coordinates become each forge&apos;s position payload here, and only here. GitLab
        needs all three of <Code>base_sha</Code> / <Code>start_sha</Code> / <Code>head_sha</Code>{" "}
        from the MR&apos;s own <Code>diff_refs</Code>. GitHub takes one review containing every
        comment, always as <Code>COMMENT</Code> roborak never approves or requests changes on your
        behalf. A rejected comment never costs you the rest of the review.
      </P>

      <H2>Incremental review</H2>
      <P>
        Each finding is fingerprinted independently of its line number, so re-running on a new push
        posts only what is genuinely new instead of repeating itself. State lives in{" "}
        <Code>.roborak/state.json</Code>; <Code>--repost</Code> overrides it.
      </P>
      <P>
        A <Code>&lt;!-- roborak:v1:… --&gt;</Code> marker also records each finding&apos;s identity
        in the published comment itself, so a review carries a record of itself that does not depend
        on local state.
      </P>

      <H2>Existing discussion is context, not instruction</H2>
      <P>
        Forge reviews include bounded unresolved human comments by default, while dropping system
        notes, bots, stale positions and roborak&apos;s own output.{" "}
        <Code>--no-discussions</Code> disables it.
      </P>

      <H2>Deciding to publish comes after reading the review</H2>
      <P>
        <Code>--post</Code> has to be chosen before the model has said anything, so an interactive
        run ends by asking instead post it, save it as markdown, or neither showing first how
        many inline comments are new and how many an earlier run already sent. A local diff has
        nowhere to post, so only saving is offered.
      </P>
      <P>
        The question is asked only on a terminal: a pipe, a script and a CI job are never prompted.{" "}
        <Code>--no-post</Code> or <Code>output.confirm_post</Code> turns it off for good.
      </P>

      <H2>The overview is a second pass</H2>
      <P>
        <Code>review</Code> asks for a walkthrough after it has the findings, which is what fills
        the summary comment and the markdown report&apos;s file table. It runs on a copy of the
        changeset, because compression mutates and shrinking the diff the findings were anchored
        against would corrupt every line number already reported.
      </P>
      <P>
        A failed overview is logged, never fatal: a review without one is still a review, and must
        still exit clean. <Code>--no-walkthrough</Code> skips it and halves the cost of a run.
      </P>

      <H2>An overview is written once per shape of a change</H2>
      <P>
        The published summary carries a digest of its changed files and hunk headers. Re-posting to
        the same merge request compares that digest against the diff in front of it: unmoved means
        the story is unmoved, so the model is not asked again and the existing comment stays put.
        When it has moved, the overview is rewritten and that same comment is edited in place rather
        than a second one appended.
      </P>

      <H2>One document, read before it is published</H2>
      <P>
        One renderer builds it, so what <Code>--markdown</Code> writes, what a pipe gives back and
        what <Code>--post</Code> publishes are byte for byte the same thing asserted by a test,
        because it is the invariant a refactor would quietly break.
      </P>
      <P>
        It means the comment repeats the findings that also went out as inline threads, which is the
        deliberate half of the trade: a comment that omitted them would be a fourth document nobody
        had read before it was published.
      </P>

      <H2>How it reaches you depends on who is reading</H2>
      <CodeBlock
        shell
        code={[
          "rk review > review.md    # piped: the raw markdown, exactly the publishable file",
          "rk review                # at a terminal: rendered, in colour, with context",
        ].join("\n")}
      />
      <P>
        Everything roborak says <em>about</em> a run spinners, errors, the closing question goes
        to stderr either way.
      </P>
      <P>
        A terminal cannot fold a section, so the rendered form leaves out what is written for a
        machine: the agent prompts and the review-info tree. What a reader must not lose an
        omitted file, a skipped file, an error goes in a one-line footer instead.{" "}
        <Code>--full</Code> restores them. What it never drops is the review itself: every finding,
        badge, body and fix is in both forms, which is what <Code>tests/test_render.py</Code>{" "}
        asserts.
      </P>

      <H2>Large diffs are reviewed in several passes</H2>
      <P>
        Not truncated. The chunker splits by directory so related files stay together, each pass
        inherits the parent&apos;s metadata, and one failed pass never discards the others.
        Compression which <em>does</em> drop things is the last resort, and always reports what
        it skipped.
      </P>

      <H2>Issue context</H2>
      <P>
        <Code>--issue 42</Code> turns &quot;is this code good?&quot; into &quot;does this code do
        what was asked?&quot;. It fetches the issue&apos;s title, body, labels and discussion, puts
        them in the prompt, and when no other target was named reviews the merge or pull request
        linked to it, so <Code>--issue 42</Code> alone is enough.
      </P>
      <P>
        Findings of kind <Code>requirement_gap</Code> name what the issue asked for that the diff
        does not do. A gap is the one finding with no honest line to point at, so it is exempt from
        line anchoring and is published in the summary comment rather than inline.
      </P>

      <H2>AST context</H2>
      <P>
        Via tree-sitter, which is installed by default. It names the function or class each hunk
        sits inside. A diff hunk is a window with arbitrary edges; a model that knows it is looking
        at the middle of{" "}
        <Code>run()</Code> stops guessing at the surrounding control flow, which is where many false
        positives come from.
      </P>
    </>
  );
}
