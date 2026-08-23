# Changelog

All notable changes to roborak are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and roborak adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Pre-1.0, the CLI
flags, config schema and JSON output may change in a minor release.

The release workflow reads the section for the tag being published and uses it as
the GitHub Release body, so the `## [x.y.z] - date` heading format is load-bearing.

## [Unreleased]

## [0.4.0] - 2026-08-23

### Added

- **A pre-merge check on every review.** `--fail-on` already decided whether a
  change should be blocked, but only ever said so through an exit code, so the
  verdict was invisible to anyone reading the review. Every review now ends with
  a pre-merge check block — the verdict, the severity floor it was judged
  against, and the finding counts that drove it — in the terminal report, in
  `--markdown` output and, because the summary comment *is* the report, on the
  merge request too, on every re-run. A clean review states an explicit pass
  rather than omitting the section. The floor is `--fail-on` when given and the
  new `review.block_on` (default `critical`) otherwise; only `--fail-on` moves
  the exit code, and the block says which one it is rather than implying CI is
  gated when it is not. Publishing with `--post` also sets a `roborak/review`
  commit status on the change's head commit — a GitHub commit status and the
  GitLab equivalent — so branch protection and approval rules can gate on the
  verdict; re-running replaces that status instead of stacking another. A token
  that may comment but not set a status is not fatal: the review still publishes
  and the skipped check is reported. `--no-check` (or `output.post_check: false`)
  publishes comments only. The status links straight to the summary comment it
  was posted with, falling back to the merge request page when the forge does not
  name the comment. The rendered verdict, the forge status and the exit code all
  come from one function, so the three cannot disagree.

- **Reviewing a directory that is not a git repository.** `roborak review -C
  <dir>` used to exit with `<dir> is not a git repository`, so an extracted
  archive, a generated tree, a vendor handoff or a Mercurial checkout could only
  be reviewed by running `git init` and fabricating a baseline first. There is no
  baseline to diff against in those cases, so the new `PathsSource` reviews every
  eligible file whole instead — the `paths` origin the IR has always declared.
  The walk prunes dependency, build-output, cache and VCS directories before
  descending into them and honours the configured `ignore_paths`; binary files,
  files over 512 KiB and anything past the 2000-file cap are reported as
  omissions rather than dropped in silence. Static analysis runs, since a
  directory is checked out even without git metadata, and every output mode
  (`--json`, `--agent`, `--prompt-only`, `--markdown`) works unchanged. The flags
  that name a diff — `--base`, `--committed`, `--uncommitted`,
  `--include-untracked` — have nothing to compare against there and are refused
  rather than quietly reinterpreted.
- **Blocking a merge now takes evidence, not confidence.** A `critical` or
  `major` model finding used to need nothing but prose and a number the model
  assigned to its own claim — and a missing number defaulted to `0.8`, comfortably
  above the threshold that filtered it. With the pre-merge check now gating
  branches, that made one plausible guess enough to fail a build. Findings carry
  an `evidence` label (`execution_path`, `reproduction`, `contract`,
  `static_tool`, `unverified`) and a one-sentence `evidence_note` saying what
  makes them true, the review prompt requires both for anything blocker-level,
  and a claim that arrives without them — or that names a label with nothing
  behind it — is demoted to a `minor` `verification_needed` rather than dropped:
  still reported, still anchored, no longer counted by the verdict. Static
  findings are exempt and labelled `static_tool`, because a tool ran. The
  evidence rides along in the terminal report, `--markdown`, the published
  summary comment, `--json` and `--agent`. In a published review it is a
  collapsible **Evidence** section under the finding's agent prompt: the summary
  line names the kind of evidence, and the sentence behind it — along with
  `evidence_files`, the other paths the evidence rests on, when the model names
  any — unfolds only for a reader arguing with the finding. The confidence stays
  out of the fold, on its own last line. The terminal, which cannot fold a
  section, keeps all of it on their own lines. Set
  `review.require_evidence: false` to turn the policy off.

### Changed

- **`--json` and `--agent` report `schema_version: 2`.** Both finding payloads
  gained `evidence`, and `evidence_note` and `evidence_files` where there are
  any. Additive only; no existing field moved or changed meaning.

- **The documentation site deploys only after a release is created.** Pull
  requests and ordinary pushes still build the site in CI, but the release
  workflow now uploads the released tag to Cloudflare Pages after publishing
  the package and GitHub Release.

## [0.3.1] - 2026-08-23

### Added

- **A documentation website.** `README.md` had grown past what one scroll can
  carry — around thirty `review` flags, a hundred-key config schema and an
  eighteen-bullet "How it works" all competing for the same page. The site in
  `docs/` splits that into a landing page and ten navigable documentation pages,
  built with Vite, React Router and Tailwind and bundled to static assets.
  Content is derived from `README.md`, `CONTRIBUTING.md`, the Typer `help=`
  strings and `src/roborak/config_template.yaml`, so a reference page and
  `--help` cannot drift apart unnoticed. Deployed to Cloudflare Pages; a
  `Website` job in CI type-checks the site, builds it, and checks the bundle
  carries its content.

### Fixed

- **`review --post` no longer falls silent on a clean review.** Reusing a
  published overview depended on `.roborak/state.json`, which never leaves the
  machine that wrote it. Any other checkout — a colleague's, or CI, which starts
  empty every run — found no local copy and switched the summary off; a review
  with no inline comments then had nothing left to post, and exited without a
  comment, a success line or an error. With findings it was quieter but still
  wrong: the inline threads went out while the summary kept the previous run's
  verdict. The published comment now carries the overview it renders, so any
  machine can read it back and the summary is always published. When no copy
  can be recovered — a comment predating the marker, or a payload that no longer
  parses — the overview is narrated again rather than edited off the comment
  that still carries it.

### Security

- **A carried overview cannot inflate without bound.** The overview travels
  compressed on a comment anyone with write access can edit, where a few
  kilobytes could otherwise stand for far more once expanded. A marker past the
  8 KiB the encoder already enforced is rejected before decoding, and the
  decompressor stops at 1 MiB. A payload wanting more room reads as absent, like
  any other unreadable marker.

## [0.3.0] - 2026-08-22

### Changed

- **An overview is written once per shape of a change.** `review --post` used to
  spend a model call narrating the change on every run and append the result as a
  new summary comment, so a re-reviewed merge request collected near-identical
  overviews and paid for each one. The published summary now carries a digest of
  its changed files and hunk headers: if the diff in front of roborak has the same
  shape, the model is not asked again and the existing comment is left alone. If
  it has moved, the overview is rewritten and that comment is edited in place
  rather than duplicated, with a line saying which commit it now describes.
  `--repost` still forces a fresh overview, `--no-walkthrough` still skips the
  pass entirely, and local reviews are unaffected.
  A published overview is only reused when the forge attests to who wrote it: the
  publishing account, or -- for a CI token that cannot name itself -- a bot
  account. A comment in which someone pasted the markers by hand is ignored.

- **`setup` asks with the arrow keys.** Where the config goes and which model to
  use are now lists you move through with ↑/↓ and Enter, instead of a number to
  type and a provider-prefixed string to remember. Every list ends with
  `Other (type it in)…`, so the curated model list stays a starting point rather
  than a ceiling — any LiteLLM model string is still accepted. Keys, tokens and
  self-hosted domains are unchanged: free text, and secrets still unechoed. The
  lists are styled to the palette the rest of the CLI uses — cyan for a path or
  the row you are on, green for a settled answer, dim for an aside — so setup no
  longer looks like a different program mid-run.
- **`setup` without a terminal no longer errors.** A piped or CI invocation gets
  the previous line-based prompts and can be answered from stdin; with nothing on
  stdin it writes nothing and exits 0 instead of failing with "needs a terminal".

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

[Unreleased]: https://github.com/rdlugs/roborak/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/rdlugs/roborak/releases/tag/v0.4.0
[0.3.1]: https://github.com/rdlugs/roborak/releases/tag/v0.3.1
[0.3.0]: https://github.com/rdlugs/roborak/releases/tag/v0.3.0
[0.2.0]: https://github.com/rdlugs/roborak/releases/tag/v0.2.0
