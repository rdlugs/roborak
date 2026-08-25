<div align="center">

<img src="https://raw.githubusercontent.com/rdlugs/roborak/main/assets/roborak_256.png" alt="roborak" width="128" height="128">

# roborak

**AI code review from the terminal** — local diffs, GitLab MRs, and GitHub PRs.

Severity-graded, line-anchored findings with committable fix suggestions.

[![PyPI](https://img.shields.io/pypi/v/roborak?logo=pypi&logoColor=white)](https://pypi.org/project/roborak/)
[![Python](https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![CI](https://github.com/rdlugs/roborak/actions/workflows/ci.yml/badge.svg)](https://github.com/rdlugs/roborak/actions/workflows/ci.yml)
[![Ruff](https://img.shields.io/badge/lint-ruff-D7FF64?logo=ruff&logoColor=black)](https://docs.astral.sh/ruff/)
[![Providers](https://img.shields.io/badge/LLM-any%20LiteLLM%20model-8A2BE2)](https://docs.litellm.ai/docs/providers)
[![Forges](https://img.shields.io/badge/forges-GitLab%20%7C%20GitHub-FC6D26?logo=gitlab&logoColor=white)](#tokens)
[![Docs](https://img.shields.io/badge/docs-roborak.pages.dev-22D3EE)](https://roborak.pages.dev)
[![License](https://img.shields.io/badge/license-MIT-blue)](https://github.com/rdlugs/roborak/blob/main/LICENSE.md)

[Documentation](https://roborak.pages.dev) · [Install](#install) · [Usage](#use) · [How it works](#how-it-works) · [Configuration](#configuration) · [Security](https://github.com/rdlugs/roborak/blob/main/SECURITY.md) · [License](https://github.com/rdlugs/roborak/blob/main/LICENSE.md)

</div>

---

## What it does

|  | |
|---|---|
| 🎯 **Anchored, not approximate** | Every finding points at a line the change actually touched — verified against the files on disk, not the model's word for it. |
| 🧹 **Refuses more than it says** | Low-confidence findings filtered, duplicates collapsed, pre-existing lint debt suppressed, already-posted comments skipped. |
| 🔌 **Four sources, one pipeline** | Local git, GitLab MRs, GitHub PRs and raw paths all normalise into one IR, so output modes can never disagree. |
| 🛠 **Static analysis as evidence** | Runs ruff, mypy, semgrep, eslint and phpstan with *your* config, and feeds the results to the model to confirm or explain. |
| 💬 **Publishes where you're looking** | Inline threads for what's worth interrupting for, a summary comment for the rest, incremental so re-runs don't repeat themselves. |
| 🧭 **Maps the blast radius** | Traces changed symbols, routes, events, config keys and env vars out to the unchanged code that depends on them, and says plainly when it could not look. |
| 📋 **Issue-aware** | `--issue 42` judges the diff against what was actually asked, and reports the requirements it misses. |

**Status: feature complete.** Local diffs, GitLab MRs, GitHub PRs, issue context,
static analysis, custom rules, posting and every output mode work end to end.

## Install

```bash
uvx roborak review       # try it without installing anything
```

```bash
uv tool install roborak             # or: pipx install roborak
export ANTHROPIC_API_KEY=...        # or OPENAI_API_KEY, GEMINI_API_KEY, …
roborak review           # review everything that differs from the base branch
```

`rk` is a shorter alias for the same command. That is the whole quick start.
Everything below is optional.

<details>
<summary><b>From a checkout instead</b></summary>

<br>

```bash
uv sync                  # or: uv sync --all-extras, for tree-sitter AST context
uv run roborak review
```

The `ast` extra pulls in tree-sitter for AST context; `uvx roborak[ast]` and
`uv tool install "roborak[ast]"` get the same thing from the released package.

</details>

<details>
<summary><b>Keys in the config file instead of the shell</b></summary>

<br>

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

</details>

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
uv run roborak review --full                # add the agent prompts and the review info
uv run roborak review --panels              # one finding to a panel, not the report
uv run roborak review > review.md           # piped: the raw markdown, chrome on stderr
```

**Directories without git**

Point `-C` at a directory that is not a git repository and roborak reviews every
file in it whole, rather than refusing for want of a baseline — useful for
extracted archives, generated trees, vendor handoffs and other version-control
systems.

```bash
uv run roborak review -C /path/to/codebase   # every file, reviewed whole
uv run roborak review -C /path/to/codebase/src   # or just one subtree
```

The walk never descends into dependency, build-output, cache or VCS directories
(`node_modules`, `vendor`, `dist`, `build`, `target`, `__pycache__`, `.venv`,
`.git` and friends), and it also honours the configured `ignore_paths`. Binary
files and anything over 512 KiB are reported as omissions rather than reviewed.
Static analysis still runs; the flags that name a diff (`--base`, `--committed`,
`--uncommitted`, `--include-untracked`) do not apply and are refused.

**Forges**

```bash
uv run roborak review --mr 298              # a GitLab merge request
uv run roborak review --mr https://gitlab.com/acme/web/-/merge_requests/298
uv run roborak review --pr 42               # a GitHub pull request
uv run roborak review --mr 298 --post       # publish inline threads + a summary
uv run roborak review --mr 298 --post --repost   # re-post findings already sent
uv run roborak review --mr 298 --no-discussions # ignore existing MR discussion
uv run roborak review --mr 298 --no-post    # review it, never ask about publishing
```

**Issue context**

```bash
uv run roborak review --issue 42            # review whatever MR/PR implements issue 42
uv run roborak review --issue https://gitlab.com/acme/web/-/issues/42
uv run roborak review --mr 298 --issue 42   # review MR 298, judged against issue 42
uv run roborak review --issue 42 --base main     # local diff, judged against issue 42
```

**Output and filtering**

```bash
uv run roborak review --json                # full result as JSON
uv run roborak review --agent               # JSON for another agent to act on
uv run roborak review --prompt-only         # findings as fix instructions
uv run roborak review --markdown report.md  # walkthrough-style markdown report
uv run roborak review -m openai/gpt-5       # any LiteLLM model string
uv run roborak review -s major              # only major and critical
uv run roborak review --fail-on critical    # non-zero exit for CI
uv run roborak review --mr 298 --post --no-check   # comments, but no commit status
```

| Exit code | Meaning |
|---|---|
| `0` | Review completed |
| `1` | Findings at or above `--fail-on` |
| `2` | Operational error or partial review — failed chunks, unavailable forge patches, or a requested publish that did not complete |

### The pre-merge check

Every review ends with a pre-merge check: the verdict, the severity floor it was judged
against, and the finding counts that drove it. It is the last section of the report, so it
shows in the terminal, in `--markdown` output, and — because the summary comment *is* the
report — on the merge request too, on every re-run.

The floor is `--fail-on` when you pass it, and `review.block_on` (default `critical`)
otherwise. Only `--fail-on` moves the exit code; without it the block says so rather than
implying CI is gated when it is not.

```yaml
review:
  block_on: major     # the floor the verdict is judged against
output:
  post_check: true    # post it to the forge as a commit status
```

`review.block_on` is not `review.severity_floor`: the floor decides what is *reported* at
all, `block_on` decides what *blocks*.

**Blocking takes evidence.** A `critical` or `major` model finding has to say what makes it
true — the trigger and the failure path, a violated contract, or a reproduction — not just how
confident it feels. One that cannot is demoted to a `minor` `verification_needed`: still
reported, still anchored, no longer counted by the verdict. A self-assigned `confidence: 0.95`
is the model grading its own homework, and it is not grounds to fail a build. Static-analyser
findings are exempt, because a tool ran. Set `review.require_evidence: false` to turn the
policy off.

**As a forge status.** A review posted with `--post` also sets a commit status on the
change's head commit, named `roborak/review`, so branch protection and approval rules can
gate on it. Re-running a review replaces that status rather than stacking another. Pass
`--no-check` (or set `output.post_check: false`) to publish comments only.

| | Token scope | Where to require it |
|---|---|---|
| GitHub | `statuses:write`, or the classic `repo` scope | Settings → Branches → branch protection rule → *Require status checks to pass* → add `roborak/review` |
| GitLab | `api` | Settings → Merge requests → *Pipelines must succeed* (the status joins the MR's head pipeline) |

A token that may comment but not set a status is not an error: the review still publishes
and roborak reports the skipped check.

### Other commands

```bash
uv run roborak describe                     # title, overview, per-file table, mermaid flow
uv run roborak improve                      # suggestions only, every one committable
uv run roborak ask "why is this locked?"    # a question answered from the diff

uv run roborak rules init                   # scaffold .roborak/rules/ with an example
uv run roborak rules list                   # what roborak will apply here
uv run roborak rules test <rule.md> <file>  # validate a rule and check its scope
uv run roborak setup                        # guided first run: model, key, forge tokens
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
- **Existing review discussion is context, not instruction.** Forge reviews include
  bounded unresolved human comments by default, while dropping system notes, bots,
  stale positions and roborak's own output. `--no-discussions` disables it.
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
- **An overview is written once per shape of a change.** The published summary
  carries a digest of its changed files and hunk headers. Re-posting to the same
  merge request compares that digest against the diff in front of it: unmoved
  means the story is unmoved, so the model is not asked again and the existing
  comment stays put. When it has moved, the overview is rewritten and that same
  comment is edited in place rather than a second one appended. `--repost`
  forces a fresh overview, as it does for inline findings.
- **Output modes** share one result object, so the terminal report, the markdown
  file, the JSON payload and the forge comment can never disagree. `--json`,
  `--agent` and `--prompt-only` write to stdout alone, so they stay pipeable.
- **The report is built for skimming.** Findings are grouped into
  collapsible sections by where they belong, badged with category, severity and
  the effort a fix will cost, and each one carries a 🤖 prompt a coding agent can
  act on — plus one collated block for the whole review. A `<!-- roborak:v1:… -->`
  marker records each finding's identity in the comment itself, so a published
  review carries a record of itself that does not depend on local state.
- **There is one document, and you read it before you publish it.** One renderer
  builds it, so what `--markdown` writes, what a pipe gives back and what `--post`
  publishes are byte for byte the same thing — asserted by a test, because it is
  the invariant a refactor would quietly break. It means the comment repeats the
  findings that also went out as inline threads, which is the deliberate half of
  the trade: a comment that omitted them would be a fourth document nobody had
  read before it was published.
- **How it reaches you depends on who is reading.** Redirected or piped it is the
  raw markdown, so `roborak review > review.md` gives back exactly the publishable
  file. At a terminal it is rendered — headings, tables, severity in colour, the
  flagged lines shown in context from your working tree, syntax-highlighted fixes.
  Everything roborak says *about* a run — spinners, errors, the closing question —
  goes to stderr either way.
- **A terminal cannot fold a section, so it leaves them out instead.** A report is
  built to be skimmed by opening what you want, and every `<details>` opened at
  once is the opposite of that: on a twenty-finding review the per-finding agent
  prompts alone outweigh the findings. The rendered form drops the sections
  written for a machine — the agent prompts, the review-info tree — and puts what
  a reader must not lose (an omitted file, a skipped file, an error) in a one-line
  footer instead. `--full` restores them. What it never drops is the review: every
  finding, every badge, every body and every fix is in both forms, which is what
  `tests/test_render.py` asserts. `--panels` is the older view, one finding to a
  bordered panel.
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

`roborak setup` is the guided path to the same files: it asks for a model, a
credential for it, and optional forge tokens, then writes *only* those keys — a
sparse file, so every other default stays live across upgrades. It chmods 600
whatever it writes that holds secrets, wherever it wrote it. `config init` remains
the manual path, and the full annotated file to edit.

In a terminal the closed questions — where the file goes, which model — are
arrow-key lists rather than strings to type. Every list ends with
`Other (type it in)…`, because the model list can only ever be a starting point:
roborak takes any LiteLLM model string. Keys, tokens and a self-hosted domain
stay free text, and keys are never echoed. Run it without a terminal — piped, or
in CI — and the same questions come back as plain line prompts reading stdin, so
`printf '1\n\nsk-ant-…\n\n\n' | rk setup` works; with nothing on stdin it
writes nothing and exits 0 rather than waiting.

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
  require_evidence: true     # a critical/major model finding must show its evidence
  check_requirements: true   # with --issue, report requirements the change misses
  include_discussions: true  # use relevant unresolved MR/PR comments as context

static:
  enabled: true
  execution: auto     # local direct; CI sandboxed, or skipped if unavailable
  tools: null          # null = autodetect what is on PATH

impact:
  enabled: true           # trace changed symbols out to their consumers
  max_nodes: 12           # boundaries traced per review
  max_consumers_per_node: 5
  max_files_scanned: 2000 # ceiling on the no-git fallback walk
  max_snippet_lines: 6
  token_budget: 1500      # prompt tokens the consumer snippets may occupy
  timeout_seconds: 10

output:
  walkthrough: true    # spend a second model call on the overview
  confirm_post: true   # offer to publish at the end of an interactive review
  panels: false        # one finding to a panel instead of the report
  full: false          # show the agent prompts and review info the terminal hides

ignore_paths:
  - "**/*.lock"
  - "**/vendor/**"
  - "**/node_modules/**"

language_instructions:
  php: "Laravel 10 with the repository pattern; controllers stay thin."
```

### Custom rules

Standards a linter cannot express go in `.roborak/rules/*.md` as plain language:

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
When a base revision is available, conventions are read from that revision so a
change cannot rewrite the instructions used to review itself.

### Static-analysis trust

> [!IMPORTANT]
> Static analyzers load repository binaries, plugins, and configuration, which can
> execute code. Outside CI, `static.execution: auto` treats the checkout as trusted.

In CI it runs through Bubblewrap with a read-only filesystem and no network; when
Bubblewrap is unavailable, the static pass is skipped rather than running
untrusted code directly. `--trust-static` (or `static.execution: trusted`) is the
explicit override for a checkout you control. Every static subprocess receives a
credential-scrubbed environment in all modes.

CI also ignores `.roborak.yaml` from the working tree, since it could redirect an
API key or opt into trusted execution. Put CI settings in environment variables,
the user config, or pass a trusted base-controlled file with `--config`.

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
uv run pytest              # 502 tests
uv run ruff check src tests
uv run ruff format src tests
uv run mypy src/roborak
```

Live reviewer-quality evaluation is intentionally separate from deterministic PR
CI. `uv run python evals/run.py` exercises 30 labeled defect and clean-control
cases, writes token and quality metrics, and enforces the nightly recall,
false-positive, anchoring, and parse-success gates.

Conventions, invariants and the PR checklist are in
[CONTRIBUTING.md](https://github.com/rdlugs/roborak/blob/main/CONTRIBUTING.md).

## Documentation

Full documentation lives at **[roborak.pages.dev](https://roborak.pages.dev)** —
install, every command and flag, configuration reference, custom rules and CI
recipes.

## License

MIT — see [LICENSE.md](https://github.com/rdlugs/roborak/blob/main/LICENSE.md).

<div align="center">
<br>
<sub>Built with <a href="https://docs.litellm.ai/">LiteLLM</a>, <a href="https://typer.tiangolo.com/">Typer</a> and <a href="https://rich.readthedocs.io/">Rich</a>.</sub>
</div>
