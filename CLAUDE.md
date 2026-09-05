# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`roborak` (CLI aliases `roborak` and `rk`) is an AI code-review tool: it turns a local diff,
a GitLab MR, a GitHub PR or a raw directory into severity-graded, line-anchored findings, and
optionally publishes them back to the forge. Python 3.12+, packaged with `uv`, Typer CLI,
Pydantic models, LiteLLM for any model provider.

`AGENTS.md` points other agents here, and `CONTRIBUTING.md` is the human-facing companion.
roborak reads the first of `AGENTS.md`, `CLAUDE.md`, `.roborak/context.md`, `CONTRIBUTING.md`
it finds as repository context, so keep the three in step when conventions change.

## Commands

```bash
uv sync --dev                     # set up; there is no venv to activate, everything is `uv run`

# The checks. These are exactly what CI runs, on 3 OSes x Python 3.12/3.13/3.14.
uv run ruff check src tests evals .github/scripts
uv run ruff format src tests evals .github/scripts     # CI runs --check; format before committing
uv run mypy src
uv run pytest --cov=src/roborak --cov-fail-under=90    # coverage gate is 90%
uv build

uv run pytest tests/test_render.py                     # one file
uv run pytest tests/test_render.py::test_name          # one test
uv run pytest -k anchor                                # by name

uv run roborak review --no-llm     # exercise the whole pipeline with no API key (static only)
uv run roborak review --base main  # dogfood on your own branch before asking for review
uv run python evals/run.py         # 30 labeled cases; real model calls, out of PR CI, run nightly
```

Docs site (`docs/`, Vite + React Router + Tailwind, Node 20+):
`cd docs && npm ci && npm run dev | npm run typecheck | npm run build | npm run serve`.

## Architecture

One directional pipeline; each stage only knows the stage before it.

```text
Source → ChangeSet → Compressor → Static/supply/verify passes → LLM → Investigate → Validator →
Renderer → Publisher
```

- `sources/` — local git, GitLab, GitHub, raw paths, all producing the same `ChangeSet` IR
  (`core/models.py`). Adding a source means writing a `ChangeSet` producer and nothing else;
  if it needs a matching renderer change, the IR lost something it should have carried.
- `context/` — `compressor.py` (diff budget), `chunker.py` (contract-first multi-pass splitting
  of large diffs), `ast_context.py` (tree-sitter, inward from a hunk to its enclosing symbol),
  `impact.py` (outward to dependents — the blast radius), `forge_checkout.py` (a throwaway
  clone of a PR/MR head this machine does not have, so the blast radius has a tree to search).
- `static/` — adapters for ruff, mypy, semgrep, eslint, phpstan, actionlint, hadolint, checkov,
  osv-scanner, run with *the project's own* config; findings off changed lines are dropped, and
  what survives is fed to the model as evidence rather than reported raw.
- `supply/` — parses manifest/lockfile pairs (npm, Python, Go, Cargo, Composer) out of git into a
  bounded delta. Lockfiles deliberately never enter the prompt.
- `verify/` — runs the project's own configured test commands, proportionally to the change,
  reading the commands from the *base* revision so a change cannot define what verifies it.
- `investigate/` — the bounded evidence pass between candidate findings and validation:
  `tools.py` (the read-only execution boundary — path containment, `git grep` as argv, bounded
  results), `availability.py` (whether this checkout *is* the reviewed change), `runner.py` (the
  round loop and confirm/revise/drop). Read-only by construction; nothing here writes, executes a
  repository-chosen command, or reaches the network.
- `analysis/reviewer.py` — the orchestrator (`review`, `describe`, `improve`, `ask`, `walkthrough`);
  `analysis/validator.py` — drops unanchored findings, snaps near misses, filters by confidence
  and severity, collapses duplicates.
- `render/` — one result object, many forms: terminal, markdown, JSON, agent, prompt-only.
- `publish/` — the only place new-file coordinates become a forge position payload.
- `cli/` — thin Typer commands; `cli/shared.py` holds the shared `start`/`emit`/`finish` flow.
- `state/store.py` — `.roborak/state.json`, for incremental review fingerprints.

## Invariants — a plausible-looking change here quietly corrupts output

- **Line numbers are new-file coordinates everywhere.** Only publishers translate them.
  `tests/test_local_git.py` checks computed numbers against real files on disk — do not "fix" a
  failure there by adjusting the expectation.
- **`core/buckets.py` is the single place that decides inline vs. summary.** Terminal, markdown,
  summary comment and publisher must never disagree.
- **One renderer builds one document.** `--markdown`, a pipe and `--post` are byte for byte
  identical; a test asserts it. This is the refactor most likely to regress.
- **The walkthrough runs on a copy of the changeset** — compression mutates, and a shrunken diff
  would invalidate every already-anchored line number.
- **A failed overview is logged, never fatal.** A review without a walkthrough still exits clean.
- **stdout is the product, stderr is the chrome.** `--json`, `--agent`, `--prompt-only` and piped
  output put only the payload on stdout; spinners, errors and the closing prompt go to stderr.
- **Never prompt when not on a terminal** — pipes, scripts and CI are never asked about publishing.
- **roborak never approves or requests changes**; GitHub reviews always post as `COMMENT`.
- **The static pass is untrusted by default**: Bubblewrap, read-only FS, no network, skipped rather
  than run directly when `bwrap` is unavailable; static subprocesses always get a
  credential-scrubbed environment.
- **Blocking takes evidence**: a critical/major model finding without a trigger, failure path,
  violated contract or reproduction is demoted to a `minor` `verification_needed`. Self-reported
  confidence is not evidence. Static findings are exempt.
- **Investigation never settles a candidate by default.** A tool error, an exhausted budget, an
  unparseable reply or a provider failure leaves the finding exactly as it arrived — "we could not
  tell" must never be recorded as "we checked". `validator.is_unproven_blocker` is the single
  predicate deciding both what gets investigated and what gets demoted; two copies would drift.
- **A forge review never reads a checkout that is not the reviewed change.** Dynamic reads require
  a clean tree at the reviewed head SHA; otherwise roborak uses forge-supplied file content or
  reports the stage unavailable. `context/forge_checkout.py` may fetch its own tree when the local
  one lacks the head, and is held to the same bar: the fetch is verified against `head_sha`
  afterwards — a published head ref moves — and an unverified checkout is discarded, never
  searched and downgraded. It writes nothing to the user's repository and deletes itself either way.

## Config

Precedence: CLI flags > `ROBORAK_*` env vars > project `.roborak.yaml` > `~/.config/roborak/config.yaml`
> defaults. `src/roborak/config_template.yaml` is the annotated source of truth for every key and
default, mirrored by `core/config.py`. Note `review.severity_floor` (what is reported) is not
`review.block_on` (what fails the verdict); only `--fail-on` moves the exit code.

Exit codes: `0` completed, `1` findings at or above `--fail-on`, `2` operational or partial review.

## Conventions

- Ruff `E,F,I,UP,B,SIM,RUF,BLE`, 100 columns. Bare `except` is an error.
- Full annotations everywhere — `mypy` runs `disallow_untyped_defs`.
- Pydantic models for anything crossing a boundary (config, IR, LLM responses).
- User-facing strings go through Rich; never `print()`.
- Comments explain *why*; match the surrounding files, which are sparse and specific.
- Tests: one `tests/test_<area>.py` per area, fixtures in `conftest.py`. No test may reach the
  network or call a real model — stub at the LiteLLM boundary. For anything touching line anchoring,
  build a real diff against real files rather than asserting on a hand-written fixture.
- Commits: imperative mood, sentence case, no type prefix. Branch off `main`; `main` takes no direct
  pushes and keeps linear history.
- Update `README.md` and the `docs/` page that quotes it in the same PR as a flag, config key or
  exit code change. Do not bump `version` in a PR — releases are the maintainer's step.
