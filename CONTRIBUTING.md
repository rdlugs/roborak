# Contributing to roborak

Thanks for taking the time. This file is also what roborak reads as repository
context when reviewing changes here, so it doubles as the conventions document —
keep it accurate.

## Getting set up

```bash
uv sync --all-extras --dev     # runtime, tree-sitter AST extra, and dev tools
uv run roborak --help          # smoke-test the CLI
```

Python 3.12 is the floor; CI runs 3.12 and 3.13. Everything is driven through
`uv run`, so there is no virtualenv to activate by hand.

For an end-to-end run you need a model credential — `ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`, `GEMINI_API_KEY`, or anything else LiteLLM speaks. Most work
does not: `uv run roborak review --no-llm` exercises the whole pipeline on static
analysis alone, and the test suite never calls a real model.

## The checks

Run these before pushing; they are exactly what CI runs.

```bash
uv run ruff check src tests evals
uv run ruff format src tests evals
uv run mypy src
uv run pytest --cov=src/roborak --cov-fail-under=90
uv build
```

Coverage is gated at 90%, `mypy` runs with `disallow_untyped_defs`, and
`ruff format --check` is enforced — so format before you commit rather than after
the red build.

## Where things live

The pipeline is one direction, and each stage only knows the stage before it:

```
Source → ChangeSet → Compressor → Static pass → LLM → Validator → Renderer
```

| Package | What belongs there |
|---|---|
| `src/roborak/sources/` | Local git, GitLab, GitHub and raw paths → `ChangeSet` |
| `src/roborak/core/` | The IR (`models.py`), config, severity, and finding routing (`buckets.py`) |
| `src/roborak/static/` | Running ruff, mypy, semgrep, eslint, phpstan and normalising their output |
| `src/roborak/llm/` | Prompt construction, LiteLLM calls, chunking, response parsing |
| `src/roborak/render/` | The one document: terminal, markdown, JSON, agent and summary forms |
| `src/roborak/publish/` | Translating new-file coordinates into each forge's position payload |
| `src/roborak/cli/` | Typer commands; thin, with the work delegated downward |
| `evals/` | Live reviewer-quality evaluation, deliberately outside PR CI |

Adding a new source means writing a `ChangeSet` producer and nothing else. If a
change to a source needs a matching change in the renderer, that is a sign the IR
lost something it should have carried.

## Invariants worth knowing before you break one

These are the parts where a plausible-looking change quietly corrupts output.

- **Line numbers are new-file coordinates everywhere.** Only publishers translate
  them into a forge's position payload. `tests/test_local_git.py` checks computed
  numbers against the real files on disk, so an off-by-one cannot agree with
  itself and pass — do not "fix" it by adjusting the expectation.
- **`roborak.core.buckets` is the single place that decides where a finding goes.**
  The terminal, the markdown report, the summary comment and the publisher must
  never disagree about inline vs. summary.
- **One renderer builds one document.** What `--markdown` writes, what a pipe
  returns and what `--post` publishes are byte for byte identical, and a test
  asserts it. A second rendering path is the refactor this repo is most likely to
  regress on.
- **The walkthrough runs on a copy of the changeset.** Compression mutates; a
  shrunken diff would invalidate every line number already anchored against it.
- **A failed overview is logged, never fatal.** A review without a walkthrough is
  still a review and must still exit clean.
- **stdout is the product, stderr is the chrome.** `--json`, `--agent`,
  `--prompt-only` and piped output write only the payload to stdout; spinners,
  errors and the closing prompt always go to stderr.
- **Never prompt when not on a terminal.** Pipes, scripts and CI jobs are never
  asked whether to publish.
- **roborak never approves or requests changes.** GitHub reviews are always
  posted as `COMMENT`.
- **The static pass is untrusted by default in CI.** It runs under Bubblewrap with
  a read-only filesystem and no network, and is skipped rather than run directly
  when Bubblewrap is unavailable. Every static subprocess gets a
  credential-scrubbed environment in all modes.

## Style

- Ruff with `E, F, I, UP, B, SIM, RUF, BLE`, 100-column lines. Bare `except` is an
  error, not a lint suggestion.
- Full type annotations on every function; `mypy` will not accept less.
- Comments explain *why*, not *what*. Match the density of the surrounding file —
  the existing code is comparatively sparse and specific.
- Pydantic models for anything crossing a boundary (config, IR, LLM responses).
- User-facing strings go through Rich; do not `print()`.

## Tests

`pytest`, one `tests/test_<area>.py` per area, fixtures in `tests/conftest.py`.
No test may reach the network or call a real model — stub at the LiteLLM boundary.

New behaviour needs a test that would fail without it. For anything touching line
anchoring, prefer a test that builds a real diff against real files over one that
asserts on a hand-written fixture, for the reason above.

## Evals

```bash
uv run python evals/run.py
```

30 labeled defect and clean-control cases in `evals/cases.yaml`, with recall,
false-positive, anchoring and parse-success gates. This costs real model calls and
is nondeterministic, which is why it is kept out of PR CI and run nightly. Run it
when you change prompts, the validator, or the parser; a change that improves
recall while quietly raising false positives is the failure mode it exists to
catch.

## Pull requests

1. Branch off `main`.
2. Keep the change focused; a refactor and a behaviour change in one PR are two
   reviews for whoever reads it.
3. Commit messages: imperative mood, sentence case, no type prefix — e.g. `Add an
   interactive rk setup wizard for first-run configuration`.
4. Run the checks above, and update `README.md` when you change a flag, a config
   key, or an exit code.
5. Open the PR against `main`. CI must be green on both Python versions.

Dogfooding is encouraged: `uv run roborak review --base main` on your own branch
before you ask anyone else to read it.

## Reporting bugs

Open an issue with the command you ran, the roborak version, the Python version,
and what you expected instead. Add `--full` output when the problem is in the
report itself. If it involves a forge, say which one and whether the instance is
self-hosted.

Please do not paste tokens or API keys into an issue — `roborak config show`
redacts them, raw config files do not.

## License

By contributing you agree that your contributions are licensed under the MIT
License, as in [LICENSE.md](LICENSE.md).
