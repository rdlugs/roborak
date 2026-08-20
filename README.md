# roborak

AI code review from the terminal — local diffs, GitLab MRs, and GitHub PRs.

Modelled on the process and output of [CodeRabbit](https://coderabbit.ai),
[Qodo Merge](https://github.com/qodo-ai/pr-agent), and [Kodus](https://github.com/kodustech/kodus-ai):
severity-graded, line-anchored findings with committable fix suggestions.

## Status

**All seven phases complete.** — local diffs, GitLab MRs, GitHub PRs, issue context,
static analysis, custom rules, posting and every output mode work end to end. See
[Roadmap](#roadmap) for what is not built yet.

## Install

```bash
uv sync                  # or: uv sync --all-extras, for tree-sitter AST context
export ANTHROPIC_API_KEY=...        # or OPENAI_API_KEY, GEMINI_API_KEY, …
```

Keys can also live in the config file, per provider, which is useful when a
checkout needs credentials the shell does not carry:

```yaml
llm:
  api_keys:
    anthropic: sk-ant-...
    openai: sk-...
  api_base: http://localhost:11434   # optional: proxy, Azure, or a local Ollama
```

A configured key wins over the provider's environment variable. These are real
secrets on disk, so keep them in `~/.config/roborak/config.yaml` or in a
`.roborak.yaml` your repo ignores — `roborak config show` redacts them, git does
not. `roborak config init --global` scaffolds that user-wide file and creates it
mode 600. Setting `api_base` alone is enough for endpoints that need no key.

## Use

```bash
uv run roborak review                       # everything that differs from the base branch
uv run roborak review --base main           # compare against a specific ref
uv run roborak review --uncommitted         # staged and unstaged edits only
uv run roborak review --committed --base main
uv run roborak review --include-untracked
uv run roborak review --no-llm              # static analysis only; no API key needed
uv run roborak review --no-static           # model only, skip the linters
uv run roborak review --no-walkthrough      # skip the overview; one model call, not two
uv run roborak review --panels              # rich panels with code context, not the report
uv run roborak review > review.md           # piped: the raw markdown, chrome on stderr

uv run roborak review --mr 298              # a GitLab merge request
uv run roborak review --mr https://gitlab.com/acme/web/-/merge_requests/298
uv run roborak review --pr 42               # a GitHub pull request
uv run roborak review --mr 298 --post       # publish inline threads + a summary
uv run roborak review --mr 298 --post --repost   # re-post findings already sent
uv run roborak review --mr 298 --no-post    # review it, never ask about publishing

uv run roborak review --issue 42            # review whatever MR/PR implements issue 42
uv run roborak review --issue https://gitlab.com/acme/web/-/issues/42
uv run roborak review --mr 298 --issue 42   # review MR 298, judged against issue 42
uv run roborak review --issue 42 --base main     # local diff, judged against issue 42

uv run roborak review --json                # full result as JSON
uv run roborak review --agent               # JSON for another agent to act on
uv run roborak review --prompt-only         # findings as fix instructions
uv run roborak review --markdown report.md  # walkthrough-style markdown report
uv run roborak review -m openai/gpt-5       # any LiteLLM model string
uv run roborak review -s major              # only major and critical
uv run roborak review --fail-on critical    # non-zero exit for CI
```

Exit codes: `0` clean, `1` findings at or above `--fail-on`, `2` error.

### Other commands

```bash
uv run roborak describe                     # title, overview, per-file table, mermaid flow
uv run roborak improve                      # suggestions only, every one committable
uv run roborak ask "why is this locked?"    # a question answered from the diff

uv run roborak rules init                   # scaffold .roborak/rules/ with an example
uv run roborak rules list                   # what roborak will apply here
uv run roborak rules test <rule.md> <file>  # validate a rule and check its scope
uv run roborak config init                  # write a commented .roborak.yaml
uv run roborak config init --global         # …or ~/.config/roborak/config.yaml, mode 600
uv run roborak config show                  # the effective config, all layers merged
```

Each accepts the same `--mr` / `--pr` / `--issue` / `--base` targeting as `review`.

### Tokens

`--mr` needs `GITLAB_TOKEN` (or `ROBORAK_GITLAB_TOKEN`, or CI's `CI_JOB_TOKEN`).
`--pr` needs `GITHUB_TOKEN`, or an existing `gh auth login` session, which roborak
will use automatically. `--issue` needs whichever of the two matches the issue's
forge, inferred from the URL or the git remote.

Like the LLM keys, these can live in the config file instead of the shell:

```yaml
forge:
  tokens:
    gitlab: glpat-...
    github: ghp_...
```

A configured token wins over `GITLAB_TOKEN` / `GITHUB_TOKEN` and over the `gh`
session, while `ROBORAK_GITLAB_TOKEN` / `ROBORAK_GITHUB_TOKEN` still win over the
file. They are secrets on disk, so the same advice applies: keep them in
`~/.config/roborak/config.yaml` (`roborak config init --global` creates it mode
600) or in a `.roborak.yaml` your repo ignores. `roborak config show` redacts them.

### Self-hosted instances

A bare `--mr 705` has to work out which server it means, which it normally reads
off the repository's git remote. Name the instance in the config for the cases
where that does not answer it — no remote, or a remote pointing at a mirror:

```yaml
forge:
  hosts:
    gitlab: gitlab.acme.com
    github: http://gh.local:8080   # https is assumed unless you say otherwise
```

The git remote still wins: a domain configured user-wide can never hijack a
checkout whose remote plainly says otherwise, and a full URL passed to
`--mr` / `--pr` / `--issue` beats both. `ROBORAK_GITLAB_HOST` /
`ROBORAK_GITHUB_HOST` set the same thing from the environment.

A configured host also teaches roborak *which* forge an unrecognisable domain is,
so `--issue 24` works in a repo whose remote is something like `git.acme.com`
rather than failing with "could not tell which forge".

## How it works

One directional pipeline; each stage only knows the stage before it.

```
Source → ChangeSet → Compressor → Static pass → LLM → Validator → Renderer
```

- **`ChangeSet`** is the universal IR. Local git, GitLab, GitHub and raw paths all
  normalise into it, so nothing downstream knows where the code came from.
- **Line anchoring** is the correctness-critical part. Findings are always in
  new-file coordinates; `Hunk.line_map` records each line's position within the
  diff, and only the publishers translate that into a forge's position payload.
  `tests/test_local_git.py` checks the computed numbers against the real files on
  disk, so an off-by-one cannot agree with itself and pass.
- **The validator** drops findings that do not point at a changed line, snapping
  near misses onto the nearest one, then filters by confidence and severity and
  collapses duplicates. Most of roborak's usefulness is in what it refuses to say.
- **The static pass** runs whichever of ruff, mypy, semgrep, eslint and phpstan
  the repo actually has, using *the project's own config* — the rules a team
  already agreed to. Findings on lines the change did not touch are dropped, so a
  linted file's pre-existing debt never lands on the author. What survives is fed
  to the model as evidence to confirm or explain, rather than reported raw.
- **Every finding is routed, not just printed.** A finding that points at a
  changed line and is worth interrupting for goes inline on the diff, where the
  author is already looking. A nitpick is folded into the summary instead, so the
  small stuff cannot drown the review. And one that cannot be anchored is
  *reported* in the summary under a warning banner rather than discarded — roborak
  used to count those as failures and show them only in the terminal, which meant
  nobody reading the merge request learned they existed. `roborak.core.buckets` is
  the one place that decides, so the terminal, the markdown report, the summary
  comment and the publisher cannot disagree about where a finding belongs.
- **Publishing** translates new-file coordinates into each forge's position
  payload, and only there. GitLab needs all three of `base_sha`/`start_sha`/
  `head_sha` from the MR's own `diff_refs`; GitHub takes one review containing every
  comment, always as `COMMENT` — roborak never approves or requests changes on your
  behalf. A rejected comment never costs you the rest of the review.
- **Incremental review** fingerprints each finding independently of its line
  number, so re-running on a new push posts only what is genuinely new instead of
  repeating itself. State lives in `.roborak/state.json`; `--repost` overrides it.
- **Deciding to publish comes after reading the review.** `--post` has to be
  chosen before the model has said anything, so an interactive run ends by asking
  instead — post it, save it as markdown, or neither — showing first how many
  inline comments are new and how many an earlier run already sent. A local diff
  has nowhere to post, so only saving is offered. The question is asked only on a
  terminal: a pipe, a script and a CI job are never prompted, and `--no-post` or
  `output.confirm_post` turns it off for good.
- **The overview is a second pass.** `review` asks for a walkthrough after it has
  the findings, which is what fills the summary comment and the markdown report's
  file table. It runs on a copy of the changeset, because compression mutates and
  shrinking the diff the findings were anchored against would corrupt every line
  number already reported. A failed overview is logged, never fatal: a review
  without one is still a review, and must still exit clean. `--no-walkthrough`
  skips it.
- **Output modes** share one result object, so the terminal report, the markdown
  file, the JSON payload and the forge comment can never disagree. `--json`,
  `--agent` and `--prompt-only` write to stdout alone, so they stay pipeable.
- **The report is shaped like CodeRabbit's.** Findings are grouped into
  collapsible sections by where they belong, badged with category, severity and
  the effort a fix will cost, and each one carries a 🤖 prompt a coding agent can
  act on — plus one collated block for the whole review. A `<!-- roborak:v1:… -->`
  marker records each finding's identity in the comment itself, so a published
  review carries a record of itself that does not depend on local state.
- **There is one document, and you read it before you publish it.** The report
  you see is what `--markdown` writes and what `--post` publishes as the comment
  — asserted by a test, because it is the invariant a refactor would quietly
  break. It means the comment repeats the findings that also went out as inline
  threads, which is the deliberate half of the trade: a comment that omitted them
  would be a fourth document nobody had read before it was published.
- **How it reaches you depends on who is reading.** At a terminal it is rendered:
  headings, tables, syntax-highlighted fixes. Redirected or piped it is the raw
  markdown, so `roborak review > review.md` gives back the publishable file.
  Rendering costs one small translation — `rich.Markdown` drops HTML silently,
  which would take every `<details>` section heading with it, so the collapsible
  sections are opened out into headings first. Same document, same order, same
  words; only the way a section folds changes. Everything roborak says *about* a
  run — spinners, errors, the closing question — goes to stderr either way.
  `--panels` brings back the old rich view, still the only one that shows each
  finding's code in context, read from your working tree.
- **Large diffs are reviewed in several passes**, not truncated. The chunker
  splits by directory so related files stay together, each pass inherits the
  parent's metadata, and one failed pass never discards the others. Compression —
  which *does* drop things — is the last resort, and always reports what it
  skipped.
- **Issue context** turns "is this code good?" into "does this code do what was
  asked?". `--issue 42` fetches the issue's title, body, labels and discussion and
  puts them in the prompt, and — when no other target was named — reviews the merge
  or pull request linked to it, so `--issue 42` alone is enough. Findings of kind
  `requirement_gap` name what the issue asked for that the diff does not do. A gap
  is the one finding with no honest line to point at, so it is exempt from line
  anchoring and is published in the summary comment rather than inline.
- **AST context** (optional, via tree-sitter) names the function or class each
  hunk sits inside. A diff hunk is a window with arbitrary edges; a model that
  knows it is looking at the middle of `run()` stops guessing at the surrounding
  control flow, which is where many false positives come from.

## Configuration

`.roborak.yaml` in the repo root. Precedence: CLI flags > `ROBORAK_*` env vars >
project config > `~/.config/roborak/config.yaml` > defaults. `config init` writes
the first, `config init --global` the second; both get the same commented template,
which ships inside the package rather than being read out of a source checkout.

```yaml
version: 1

llm:
  model: anthropic/claude-sonnet-5
  fallback_models: [openai/gpt-5]
  temperature: 0.2

review:
  categories: [security, bug, performance, logic]
  severity_floor: minor
  max_findings: 25
  committable_suggestions: true
  min_confidence: 0.5
  check_requirements: true   # with --issue, report requirements the change misses

static:
  enabled: true
  tools: null          # null = autodetect what is on PATH

output:
  walkthrough: true    # spend a second model call on the overview
  confirm_post: true   # offer to publish at the end of an interactive review
  panels: false        # rich panels instead of the report; not what gets posted

ignore_paths:
  - "**/*.lock"
  - "**/vendor/**"
  - "**/node_modules/**"

language_instructions:
  php: "Laravel 10 with the repository pattern; controllers stay thin."
```

### Custom rules

Standards a linter cannot express go in `.roborak/rules/*.md` as plain language —
the same idea as Kodus' Kody Rules:

```markdown
---
id: no-raw-sql
paths: ["app/**/*.php"]
severity: major
category: security
---
Never build SQL by string concatenation. Use the query builder or bound parameters.
```

Only rules matching the changed files enter the prompt, so token cost stays flat
as the rule set grows. Frontmatter is optional — a file containing one sentence is
a valid rule, named after the file.

roborak also reads `AGENTS.md`, `CLAUDE.md`, `.roborak/context.md`, or
`CONTRIBUTING.md` (first one found) so reviews match the repo's own conventions.

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| 1 | Local diff review, terminal output, config, LiteLLM | **done** |
| 2 | AST context via tree-sitter, multi-chunk merge for large diffs | **done** |
| 3 | Static analysis adapters (ruff, mypy, semgrep, eslint, phpstan) | **done** |
| 4 | GitLab MR and GitHub PR sources, posting inline threads, incremental review | **done** |
| 5 | Markdown walkthrough with mermaid, JSON/agent mode, `describe`/`improve`/`ask` | **done** |
| 6 | Custom rules (`.roborak/rules/*.md`), `config init`, `rules test` | **done** |
| 7 | Issue context and targeting (`--issue`), requirement-gap findings | **done** |

## Design notes

Three decisions carry most of the weight:

1. **`ChangeSet` is the only thing the pipeline knows.** Four sources normalise
   into it, so adding a fifth touches nothing downstream.
2. **Line numbers are new-file coordinates everywhere**, translated to forge
   position payloads only at the publisher. `tests/test_local_git.py` checks the
   computed numbers against files on disk, so an off-by-one cannot agree with
   itself and pass.
3. **The tool's value is mostly in what it refuses to say.** Findings outside the
   change are dropped, low-confidence ones filtered, duplicates collapsed,
   pre-existing lint debt suppressed, and already-posted comments skipped.

## Development

```bash
uv run pytest              # 434 tests
uv run ruff check src tests
uv run ruff format src tests
uv run mypy src/roborak
```
