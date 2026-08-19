# roborak

AI code review from the terminal — local diffs, GitLab MRs, and GitHub PRs.

Modelled on the process and output of [CodeRabbit](https://coderabbit.ai),
[Qodo Merge](https://github.com/qodo-ai/pr-agent), and [Kodus](https://github.com/kodustech/kodus-ai):
severity-graded, line-anchored findings with committable fix suggestions.

## Status

**Phases 1 and 3 of 6 complete** — the local-diff review path and static analysis work end to end. See
[Roadmap](#roadmap) for what is not built yet.

## Install

```bash
uv sync
export ANTHROPIC_API_KEY=...        # or OPENAI_API_KEY, GEMINI_API_KEY, …
```

## Use

```bash
uv run roborak review                       # everything that differs from the base branch
uv run roborak review --base main           # compare against a specific ref
uv run roborak review --uncommitted         # staged and unstaged edits only
uv run roborak review --committed --base main
uv run roborak review --include-untracked
uv run roborak review --no-llm              # static analysis only; no API key needed
uv run roborak review --no-static           # model only, skip the linters
uv run roborak review -m openai/gpt-5       # any LiteLLM model string
uv run roborak review -s major              # only major and critical
uv run roborak review --fail-on critical    # non-zero exit for CI
```

Exit codes: `0` clean, `1` findings at or above `--fail-on`, `2` error.

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
- **The compressor** degrades predictably when a diff will not fit the context
  window — ignored files, then deleted-file bodies, then surplus hunk context, then
  whole files — and always reports what it skipped.

## Configuration

`.roborak.yaml` in the repo root. Precedence: CLI flags > `ROBORAK_*` env vars >
project config > `~/.config/roborak/config.yaml` > defaults.

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

static:
  enabled: true
  tools: null          # null = autodetect what is on PATH

ignore_paths:
  - "**/*.lock"
  - "**/vendor/**"
  - "**/node_modules/**"

language_instructions:
  php: "Laravel 10 with the repository pattern; controllers stay thin."
```

roborak also reads `AGENTS.md`, `CLAUDE.md`, `.roborak/context.md`, or
`CONTRIBUTING.md` (first one found) so reviews match the repo's own conventions.

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| 1 | Local diff review, terminal output, config, LiteLLM | **done** |
| 2 | AST context via tree-sitter, multi-chunk merge for large diffs | todo |
| 3 | Static analysis adapters (ruff, mypy, semgrep, eslint, phpstan) | **done** |
| 4 | GitLab MR and GitHub PR sources, posting inline threads, incremental review | todo |
| 5 | Markdown walkthrough with mermaid, JSON/agent mode, `describe`/`improve`/`ask` | todo |
| 6 | Custom rules (`.roborak/rules/*.md`), `config init`, `rules test` | todo |

## Development

```bash
uv run pytest              # 154 tests
uv run ruff check src tests
uv run ruff format src tests
uv run mypy src/roborak
```
