# Changelog

All notable changes to roborak are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and roborak adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Pre-1.0, the CLI
flags, config schema and JSON output may change in a minor release.

The release workflow reads the section for the tag being published and uses it as
the GitHub Release body, so the `## [x.y.z] - date` heading format is load-bearing.

## [Unreleased]

## [0.2.0] - 2026-08-22

First public release, and the first version on PyPI. `uvx roborak review` now
works without a checkout.

### Added

- **Review, four sources, one pipeline.** Local git diffs, GitLab merge requests,
  GitHub pull requests and raw paths all normalise into one intermediate
  representation, so no two output modes can disagree about a change.
- **Line-anchored findings.** Every finding points at a line the change actually
  touched, verified against the files on disk rather than taken on the model's
  word, and graded by severity and category.
- **Static analysis as evidence.** ruff, mypy, semgrep, eslint and phpstan run
  with the repo's own config, and their results are fed to the model to confirm
  or explain — pre-existing lint debt is suppressed rather than reported.
- **Noise control.** Low-confidence findings are filtered, duplicates collapsed,
  and comments already posted are skipped so re-runs do not repeat themselves.
- **Posting.** Inline threads for what is worth interrupting for and a summary
  comment for the rest, incrementally, on both GitLab and GitHub.
- **Issue awareness.** `--issue 42` judges the diff against what was asked and
  reports the requirements it misses.
- **Custom rules**, inspectable and testable through `roborak rules`.
- **Other commands:** `describe` summarises a change, `improve` proposes
  committable fixes, `ask` answers questions grounded in the diff, `setup`
  scaffolds a config, and `config` inspects one with secrets redacted.
- **Output modes:** rich terminal, `--json`, `--agent`, `--markdown` and
  `--prompt-only`, with `--fail-on` to gate CI on severity.
- **Any LiteLLM model**, with keys from the environment or the config file.
- `--version` / `-V` on the CLI.

[Unreleased]: https://github.com/rdlugs/roborak/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/rdlugs/roborak/releases/tag/v0.2.0
