## What this changes

<!-- What the change does, and why. If it fixes an issue, say `Closes #123`. -->

## How it was verified

<!--
Which tests you added or ran, and anything you exercised by hand.
`uv run roborak review --base main` on this branch is encouraged before asking
anyone else to read it.
-->

## Checks

These are exactly what CI runs. Run them before pushing rather than after the red build.

- [ ] `uv run ruff check src tests evals`
- [ ] `uv run ruff format src tests evals`
- [ ] `uv run mypy src`
- [ ] `uv run pytest --cov=src/roborak --cov-fail-under=90`
- [ ] `uv build`

## Before asking for a review

- [ ] The change is focused — a refactor and a behaviour change are two reviews.
- [ ] Commit messages are imperative, sentence case, no type prefix.
- [ ] New behaviour has a test that would fail without it.
- [ ] `README.md` is updated if a flag, a config key, or an exit code changed.
- [ ] `CHANGELOG.md` has an entry under `## [Unreleased]`, if this is user-visible.
- [ ] `version` in `pyproject.toml` is untouched — releases are the maintainer's step.

<!--
Touching any of these? Say so above, and say what you checked.

- Line numbers are new-file coordinates everywhere; only publishers translate them.
- `roborak.core.buckets` alone decides inline vs. summary.
- One renderer builds one document — `--markdown`, a pipe and `--post` stay identical.
- stdout is the product, stderr is the chrome.
- Never prompt when not on a terminal.

If you changed prompts, the validator or the parser, run `uv run python evals/run.py`
and paste the gate numbers — a recall win that quietly raises false positives is
what it exists to catch.
-->
