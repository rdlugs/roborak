# Security Policy

roborak reads private diffs, holds forge tokens and model API keys on disk, shells
out to external analysers, and sends code to third-party model providers. If you
find a way to break any of that, please tell us privately first.

## Supported versions

roborak is pre-1.0. Only the latest release on PyPI is supported: security fixes
land on `main` and go out in the next release. There is no backport branch, so an
older version is fixed by upgrading to the current one. `roborak --version` reports
which one you are on.

## Reporting a vulnerability

**Do not open a public issue for a security problem.**

Use GitHub's [Private Vulnerability Reporting][advisory] to open a draft advisory.
It is private to you and the maintainers until a fix ships.

[advisory]: https://github.com/rdlugs/roborak/security/advisories/new

Please include, as far as you have it:

- The roborak version and the Python version.
- The exact command you ran, and the affected component or file.
- Steps to reproduce, and what an attacker gains.
- A suggested fix, if you have one.

**Do not paste tokens or API keys into a report.** `roborak config show` redacts
them; raw config files, logs and terminal captures do not.

## What to expect

roborak is maintained by one person, so these are honest intentions rather than a
contractual SLA:

| Stage | Target |
|---|---|
| Acknowledgement | 3 business days |
| Initial assessment | 7 days |
| Fix for critical/high severity | 14 days, then coordinated disclosure |

If a report goes quiet for longer than that, a nudge on the advisory thread is
welcome.

## Scope

### In scope

- **Credential leakage.** LLM API keys or forge tokens escaping into logs,
  rendered reports, posted comments, prompts, or subprocess environments.
- **Escaping the static-analysis sandbox**, or getting roborak to execute
  untrusted analyser config or plugins. The static pass shells out to ruff, mypy,
  semgrep, eslint and phpstan; in CI it runs under Bubblewrap with a read-only
  filesystem and no network, and every static subprocess gets a
  credential-scrubbed environment in all modes.
- **Path traversal or arbitrary file read** via `--path`, custom rule files, or
  config discovery.
- **Injection through a diff or a prompt** that makes roborak post attacker-chosen
  content to a forge, or exfiltrate repository content beyond the diff under
  review.
- **Credentials written with unsafe permissions.** `roborak config init --global`
  is documented as creating its file mode 600.
- **Exploitable vulnerabilities in declared dependencies.**

### Out of scope

- Bugs in third-party LLM providers, or in LiteLLM itself — report those upstream.
- The fact that reviewing a diff sends that diff to the configured model provider.
  That is what the tool is for; choosing the provider, or pointing `api_base` at a
  local model, is the user's control.
- Anything that requires an already-compromised local machine, or an attacker who
  already holds the user's tokens.
- Local resource exhaustion — enormous diffs, runaway token spend — with no
  security impact.
- Social engineering, and automated-scanner output with no demonstrated impact.

## Disclosure

We prefer coordinated disclosure: we will work with you on a fix and agree on
timing before anything is published. Reporters are credited in the release notes
unless they would rather stay anonymous.
