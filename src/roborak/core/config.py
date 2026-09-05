"""Layered configuration.

Precedence, highest first: CLI flags, environment (``ROBORAK_*``), the project's
``.roborak.yaml`` / ``.roborak.yml``, the user's ``~/.config/roborak/.roborak.yaml``, then defaults.
The shape is a section per stage -- review, static analysis, verification, blast
radius, the model, forge credentials, output -- plus path ignores and a rules
directory.

One section is deliberately outside that layering. ``verification`` names
executables to run, so ``load_verification`` reads it from the base revision
rather than the working tree; everything else here is layered normally, because
the worst a hostile config can otherwise do is ask for a different model.
"""

from __future__ import annotations

import logging
import os
import subprocess
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from roborak.core.severity import Category, Severity
from roborak.sandbox import in_ci

log = logging.getLogger(__name__)

PROJECT_CONFIG_NAMES = (".roborak.yaml", ".roborak.yml")
"""Discovery order; the first name is the canonical generated default.
Only the first existing file is loaded, so the two files are never merged.
"""
USER_CONFIG_PATH = Path.home() / ".config" / "roborak" / PROJECT_CONFIG_NAMES[0]

DEFAULT_IGNORE_PATHS = [
    "**/*.lock",
    "**/*.min.js",
    "**/*.min.css",
    "**/*.map",
    "**/vendor/**",
    "**/node_modules/**",
    "**/dist/**",
    "**/build/**",
    "**/__pycache__/**",
    "**/*.snap",
    "**/package-lock.json",
    "**/npm-shrinkwrap.json",
    "**/yarn.lock",
    "**/pnpm-lock.yaml",
    "**/poetry.lock",
    "**/uv.lock",
    "**/composer.lock",
    "**/go.sum",
]


class ConfigModel(BaseModel):
    """Configuration is user-authored, so typos must fail instead of disappearing."""

    model_config = ConfigDict(extra="forbid")


class Execution(StrEnum):
    """How much the checkout is trusted to run its own commands.

    Shared by every stage that executes repository-controlled code, so a project
    that trusts one of them phrases the decision the same way for all of them.
    """

    AUTO = "auto"
    TRUSTED = "trusted"
    OFF = "off"


StaticExecution = Execution
"""The name the static section has always used. Kept so ``--trust-static`` and
existing configuration keep meaning what they meant."""


class ForgeCheckout(StrEnum):
    """Whether a forge review may fetch a throwaway checkout of what it reviews.

    Deliberately not an ``Execution``: nothing here runs repository-controlled
    code, so ``trusted`` would have nothing to mean. The question is narrower --
    may the blast-radius pass reach the network for a tree it can search.
    """

    AUTO = "auto"
    OFF = "off"


class InvestigateConfig(ConfigModel):
    """Bounds on the evidence-gathering stage that runs before findings are validated.

    The stage exists to settle a small number of candidates whose severity turns on
    context the prompt did not carry -- a caller, an implementation, a contract.
    Every number below is a ceiling rather than a target: investigation costs a
    model call on top of the review, so it has to stay cheap enough that nobody
    reaches for the switch. A candidate whose budget runs out is left exactly as it
    was, never confirmed or dropped on the strength of an investigation that did
    not finish.
    """

    enabled: bool = True

    max_candidates: int = Field(default=5, ge=1)
    """Findings put to the stage. Selected by what the evidence policy would do to
    them, so the ones that reach it are those where another look changes the
    verdict rather than the wording."""

    max_rounds: int = Field(default=2, ge=1)
    """Request/result exchanges before the model must decide. One round answers a
    direct question; the second exists for the follow-up it implies."""

    max_requests_per_round: int = Field(default=4, ge=1)
    max_files: int = Field(default=10, ge=1)
    """Distinct files the whole investigation may open, across every round."""

    max_lines_per_read: int = Field(default=200, ge=1)
    max_search_results: int = Field(default=20, ge=1)

    max_output_chars: int = Field(default=4000, ge=1)
    """Ceiling on a single operation's result. What does not fit is truncated and
    labelled as truncated, so the model is never left reading a silent tail."""

    token_budget: int = Field(default=20000, ge=0)
    """Prompt tokens the accumulated evidence may occupy. Once spent, the stage
    stops asking and decides on what it has."""

    timeout_seconds: int = Field(default=30, ge=1)
    """Wall clock for the repository operations, not for the model calls."""


class ReviewConfig(ConfigModel):
    categories: list[Category] = Field(
        default_factory=lambda: [
            Category.SECURITY,
            Category.BUG,
            Category.PERFORMANCE,
            Category.LOGIC,
            Category.RELIABILITY,
        ]
    )
    severity_floor: Severity = Severity.MINOR
    block_on: Severity = Severity.CRITICAL
    """The floor the pre-merge verdict is judged against when ``--fail-on`` is not
    given. Distinct from ``severity_floor``, which decides what gets *reported* at
    all: a finding below that never reaches a renderer, let alone a verdict."""

    max_findings: int = Field(default=25, ge=1)
    committable_suggestions: bool = True
    min_confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    require_evidence: bool = True
    """Require a critical or major model finding to name what makes it true. One
    that cannot is demoted to a `minor` `verification_needed` rather than dropped --
    it may be real, but a self-assigned confidence score is not grounds to fail a
    build. Static findings are exempt: a tool ran."""

    full_file: bool = False
    """Allow findings on lines the change did not touch. Off by default: it is the
    main source of noise, since untouched code is not what the author asked about."""

    check_requirements: bool = True
    """Let the model report requirements the change does not implement. Only ever
    consulted when ``--issue`` supplied something to check against."""

    include_discussions: bool = True
    """Use relevant forge discussion as bounded, untrusted review context."""

    investigate: InvestigateConfig = Field(default_factory=InvestigateConfig)
    """Bounded repository reads that settle a candidate before the evidence policy
    judges it. The one section nested inside another: it tunes what ``review``
    already decides rather than naming a stage of its own."""


class StaticConfig(ConfigModel):
    enabled: bool = True
    execution: Execution = Execution.AUTO
    """``auto`` is direct for local work and sandboxed in CI; ``trusted`` is an
    explicit opt-in to direct execution, and ``off`` disables static tools."""
    tools: list[str] | None = None
    """``None`` means autodetect whatever is on PATH."""

    timeout_seconds: int = Field(default=90, ge=1)
    feed_to_llm: bool = True
    max_findings_in_prompt: int = Field(default=40, ge=0)


class VerificationCommand(ConfigModel):
    """One test command, and the changed paths that make it worth running."""

    paths: list[str] = Field(min_length=1)
    """Globs, matched against repo-relative changed paths. ``**/`` is optional at
    the front of a pattern, matching ``ignore_paths`` and rule ``paths``."""

    command: list[str] = Field(min_length=1)
    """Argv, never a shell string. A command assembled from a string would let a
    pipeline, a redirect or a ``;`` ride along inside what reads as one check."""

    name: str = ""
    """What to call this check in the report. Defaults to the command itself."""

    @field_validator("command")
    @classmethod
    def _no_blank_arguments(cls, command: list[str]) -> list[str]:
        """A blank argument is never what a project meant, and argv hides it."""
        if any(not argument.strip() for argument in command):
            raise ValueError("verification commands cannot contain an empty argument.")
        return command


class VerificationConfig(ConfigModel):
    """Running the project's own tests as part of a review.

    Off until a project says otherwise -- not by a flag, but by there being
    nothing to run. ``commands`` and ``fallback`` are both empty by default, and
    an empty selection is how the stage stays absent from a review that never
    asked for it.
    """

    enabled: bool = True
    execution: Execution = Execution.AUTO
    """``auto`` is direct for local work and sandboxed in CI; ``trusted`` is an
    explicit opt-in to direct execution, and ``off`` disables verification."""

    commands: list[VerificationCommand] = Field(default_factory=list)
    fallback: list[str] = Field(default_factory=list)
    """The broad check: what to run when no targeted command matches the change,
    and what ``broaden_paths`` escalates to. Empty means there is no broad check,
    and a change matching nothing is simply not verified."""

    broaden_paths: list[str] = Field(default_factory=list)
    """Globs for the files a targeted check cannot speak for -- a shared contract,
    a schema, a migration, the build configuration. One of these in the change
    replaces the targeted selection with ``fallback``."""

    timeout_seconds: int = Field(default=300, ge=1)
    max_commands: int = Field(default=4, ge=1)
    """Ceiling on how many commands one review runs, after selection."""

    max_output_lines: int = Field(default=40, ge=1)
    """Lines of output kept per command. It is a citation, not a build log."""

    feed_to_llm: bool = True

    @field_validator("fallback")
    @classmethod
    def _no_blank_arguments(cls, command: list[str]) -> list[str]:
        """The broad check is argv too, and a blank argument in it is just as invisible."""
        if any(not argument.strip() for argument in command):
            raise ValueError("verification commands cannot contain an empty argument.")
        return command


class ImpactConfig(ConfigModel):
    """Bounds on the blast-radius search.

    Every number here is a ceiling rather than a target: the analysis is a
    best-effort pass that runs before every review, so it has to be cheap enough
    that nobody thinks about switching it off. What it cannot finish it reports as
    truncated instead of pretending it looked.
    """

    enabled: bool = True
    max_nodes: int = Field(default=12, ge=1)
    """Changed symbols and contracts to trace. Beyond this the largest change would
    spend more on mapping itself than on being reviewed."""

    max_consumers_per_node: int = Field(default=5, ge=1)
    max_files_scanned: int = Field(default=2000, ge=1)
    """Ceiling on the fallback walk, matching ``sources.paths.MAX_FILES``."""

    max_snippet_lines: int = Field(default=6, ge=1)
    token_budget: int = Field(default=1500, ge=0)
    """Prompt tokens the consumer snippets may occupy. Reserved out of the diff
    budget up front, so adding this section can never squeeze out a changed file."""

    timeout_seconds: int = Field(default=10, ge=1)
    """Wall clock for the reference search, whether it runs as ``git grep`` or as the
    fallback walk. A file count bounds how many files are opened, not how long
    reading them takes."""

    forge_checkout: ForgeCheckout = ForgeCheckout.AUTO
    """Whether a merge or pull request whose head commit is not in the local
    repository may be fetched into a temporary checkout to search. ``auto`` fetches
    one; ``off`` leaves the stage unavailable, as it was before. This is the only
    part of a review that reaches the network for repository *content*, so it is
    the one to turn off where that is not acceptable."""

    forge_checkout_timeout_seconds: int = Field(default=60, ge=1)
    """Wall clock for that fetch -- the whole attempt, including the fallback ref
    and the checkout, not each git command. Separate from ``timeout_seconds``
    because a network clone and a local search fail on completely different
    scales, and one number would have to be wrong for one of them."""


class SupplyChainConfig(ConfigModel):
    """Bounds on the dependency and infrastructure analysis.

    The stage exists because ``ignore_paths`` is right to exclude lockfiles from
    the prompt and wrong to make them invisible. It reads the manifest/lock pairs
    a change touched, straight from git, and reduces them to a delta small enough
    to sit beside the diff. Every number below is a ceiling: what does not fit is
    reported as truncated rather than passed off as "nothing else changed".
    """

    enabled: bool = True

    max_changes: int = Field(default=40, ge=0)
    """Dependency movements carried into the report. A lockfile regeneration can
    move a thousand packages, and the first forty are the ones anyone reads."""

    max_assets: int = Field(default=20, ge=1)
    """Manifest, lock and infrastructure files parsed per review."""

    timeout_seconds: int = Field(default=10, ge=1)
    """Wall clock for reading the base revision. Reading two sides of a large
    lockfile is cheap; a repository that has to fetch them is not."""

    feed_to_llm: bool = True
    token_budget: int = Field(default=1200, ge=0)
    """Prompt tokens the dependency section may occupy. Reserved out of the diff
    budget up front, so adding this section can never squeeze out a changed file."""


class LLMConfig(ConfigModel):
    model: str = "anthropic/claude-sonnet-5"
    fallback_models: list[str] = Field(default_factory=list)
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    max_tokens: int = Field(default=8000, ge=1)
    context_budget: int | None = Field(default=None, ge=1000)
    """Prompt token ceiling. ``None`` derives it from the model's known window."""

    api_keys: dict[str, SecretStr] = Field(default_factory=dict)
    """Provider name to key, e.g. ``{"anthropic": "sk-ant-..."}``. Takes precedence
    over the provider's environment variable; omit a provider to keep using it.
    ``SecretStr`` keeps the value out of ``config show``, logs and tracebacks."""

    api_base: str | None = None
    """Endpoint override applied to every call: an OpenAI-compatible proxy, an
    Azure deployment, or a local Ollama."""

    timeout_seconds: int = Field(default=180, ge=1)
    max_retries: int = Field(default=2, ge=0)


class OutputConfig(ConfigModel):
    walkthrough: bool = True
    """Spend a second model call on an overview of the change. Worth it for the
    summary comment a review posts; turn it off to halve the cost of a run."""

    confirm_post: bool = True
    """At the end of an interactive review, offer to publish the result. Only ever
    consulted on a terminal: a script or a CI job is never prompted."""

    panels: bool = False
    """Print the review as rich panels instead of the report -- one finding to a
    bordered panel, which is the denser way to read a long review. Not what gets
    published either way."""

    post_check: bool = True
    """Post the pre-merge verdict to the forge as a commit status when publishing.
    Off makes ``--post`` leave comments only; the rendered verdict stays either way."""

    full: bool = False
    """Show the sections the terminal report leaves out: the agent prompt under
    each finding and the review-info tree. They are written for a machine, and
    opened out on a screen they bury the review. What a reader must not lose --
    an omitted file, a skipped file, an error -- is in the footer regardless."""


class ForgeConfig(ConfigModel):
    tokens: dict[str, SecretStr] = Field(default_factory=dict)
    """Provider name to token, e.g. ``{"gitlab": "glpat-..."}``. Takes precedence
    over the provider's environment variable; omit a provider to keep using it.
    ``SecretStr`` keeps the value out of ``config show``, logs and tracebacks."""

    hosts: dict[str, str] = Field(default_factory=dict)
    """Provider name to the instance's domain, e.g. ``{"gitlab": "gitlab.acme.com"}``.
    Only consulted when the repository's git remote does not answer the question, so a
    domain set user-wide can never hijack a repo whose remote says otherwise."""

    max_recovered_file_bytes: int = Field(default=1_048_576, ge=1)
    """Largest forge-truncated text file we will fetch to reconstruct a patch."""

    @field_validator("hosts")
    @classmethod
    def _normalise_hosts(cls, hosts: dict[str, str]) -> dict[str, str]:
        """Reduce each value to ``host[:port]``, keeping only a non-default scheme.

        Storing the bare netloc is what lets ``Target.host`` stay the plain domain
        everywhere it is shown or used as a key, with ``https`` simply assumed.
        """
        cleaned: dict[str, str] = {}
        for provider, raw in hosts.items():
            value = raw.strip().rstrip("/")
            scheme = ""
            for prefix in ("https://", "http://"):
                if value.lower().startswith(prefix):
                    scheme = "" if prefix == "https://" else prefix
                    value = value[len(prefix) :]
                    break
            if not value:
                raise ValueError(f"forge.hosts.{provider} is empty.")
            if "/" in value:
                raise ValueError(
                    f"forge.hosts.{provider} must be a domain like 'gitlab.acme.com', "
                    f"optionally with a scheme and port, not a URL path: {raw!r}."
                )
            cleaned[provider] = scheme + value
        return cleaned


class Config(ConfigModel):
    version: Literal[1] = 1
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    static: StaticConfig = Field(default_factory=StaticConfig)
    verification: VerificationConfig = Field(default_factory=VerificationConfig)
    impact: ImpactConfig = Field(default_factory=ImpactConfig)
    supply_chain: SupplyChainConfig = Field(default_factory=SupplyChainConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    forge: ForgeConfig = Field(default_factory=ForgeConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    ignore_paths: list[str] = Field(default_factory=lambda: list(DEFAULT_IGNORE_PATHS))
    rules_dir: str = ".roborak/rules"
    language_instructions: dict[str, str] = Field(default_factory=dict)
    """Extra prompt guidance keyed by language, e.g. ``{"php": "This is Laravel 10."}``."""

    @property
    def model(self) -> str:
        return self.llm.model


def project_config_path(repo: Path, explicit_path: Path | None = None) -> Path | None:
    """Select the project layer consistently for loading and source reporting."""
    if explicit_path is not None:
        return explicit_path
    if in_ci():
        return None
    return next((repo / name for name in PROJECT_CONFIG_NAMES if (repo / name).is_file()), None)


def load_config(repo: Path, explicit_path: Path | None = None) -> Config:
    """Merge every configuration layer into one ``Config``."""
    layers: list[dict[str, Any]] = []

    if USER_CONFIG_PATH.is_file():
        layers.append(_read_yaml(USER_CONFIG_PATH))

    project_path = project_config_path(repo, explicit_path)
    if project_path is not None:
        if not project_path.is_file():
            raise FileNotFoundError(f"Config file not found: {project_path}")
        layers.append(_read_yaml(project_path))
    elif in_ci():
        log.debug(
            "ignoring working-tree project configuration in CI; pass --config with a trusted "
            "path or use environment/user configuration"
        )

    layers.append(_env_layer())

    merged: dict[str, Any] = {}
    for layer in layers:
        merged = _deep_merge(merged, layer)
    return Config.model_validate(merged)


def load_verification(
    repo: Path, *, ref: str = "HEAD", explicit_path: Path | None = None
) -> tuple[VerificationConfig, str, list[str]]:
    """The verification section, read from somewhere the change cannot have written.

    Every other section is layered from the working tree, because the worst a
    hostile one can do is ask for a different model or a lower severity floor.
    This section names executables and their arguments, so a checked-out branch
    that could write it would be a branch that runs whatever it likes on the
    machine reviewing it -- which is precisely the review a person is most likely
    to be running on somebody else's code.

    So the project layer comes from the *base revision* instead, exactly as
    ``rules.loader.load_rules_at_ref`` reads a team's rules from the revision
    being merged into. The consequence is worth stating plainly: a change that
    adds a verification command does not get verified by it until it lands. An
    explicit ``--config`` still wins, since choosing that path is itself the
    trust decision, and the user's own configuration and environment are theirs.

    ``ref`` defaults to ``HEAD`` because the commonest local review is a working
    tree against the commit behind it, and there the edits under review are
    exactly what ``HEAD`` does not contain. An empty ``ref`` means the caller has
    no revision to offer -- a directory with no git history -- and then there is
    no trusted project layer at all rather than a fallback to the working tree.

    Returns the section, where its project layer came from, and any note a reader
    needs in order to understand why a command they configured did not run.
    """
    notes: list[str] = []
    layers: list[dict[str, Any]] = []
    project: dict[str, Any] = {}

    if USER_CONFIG_PATH.is_file():
        layers.append(_verification_of(_read_yaml(USER_CONFIG_PATH)))

    if explicit_path is not None:
        if not explicit_path.is_file():
            raise FileNotFoundError(f"Config file not found: {explicit_path}")
        project = _verification_of(_read_yaml(explicit_path))
        source = f"{explicit_path}"
    elif ref:
        at_ref = _project_config_at_ref(repo, ref)
        if at_ref is None:
            source = "user and environment configuration"
            notes.append(
                f"No project configuration could be read from {ref}, so only user and "
                "environment configuration was consulted."
            )
        else:
            project = _verification_of(at_ref)
            source = f"base revision {ref[:12]}"
    else:
        source = "user and environment configuration"
        notes.append(
            "There is no base revision to read trusted verification commands from, so only "
            "user and environment configuration was consulted."
        )

    layers.append(project)
    layers.append(_verification_of(_env_layer()))

    merged: dict[str, Any] = {}
    for layer in layers:
        merged = _deep_merge(merged, layer)
    config = VerificationConfig.model_validate(merged)

    # Only worth saying when the checkout is asking for something *different*. A
    # committed configuration reads identically from both places, and a note on
    # every run would train the reader to skip the one run where it matters.
    working_tree = _working_tree_verification(repo)
    if explicit_path is None and working_tree is not None and working_tree != project:
        notes.append(
            "Verification commands in the working tree's project configuration were not used: "
            "they are read from the base revision, so a change cannot define the command that "
            "verifies it. Commit them, or pass --config with a path you trust."
        )
    return config, source, notes


def _verification_of(data: dict[str, Any]) -> dict[str, Any]:
    """The ``verification`` section of a parsed config file, or ``{}`` when there is none."""
    section = data.get("verification")
    return dict(section) if isinstance(section, dict) else {}


def _project_config_at_ref(repo: Path, ref: str) -> dict[str, Any] | None:
    """The project configuration as of ``ref``, or ``None`` if it cannot be read.

    ``None`` covers a directory that is not a git repository, a revision that no
    longer exists, and a repository with no commits yet. All three mean the same
    thing here -- there is no trusted copy -- and none of them is an error.
    """
    for name in PROJECT_CONFIG_NAMES:
        try:
            shown = subprocess.run(
                ["git", "show", f"{ref}:{name}"],
                cwd=repo,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if shown.returncode != 0:
            continue
        try:
            data = yaml.safe_load(shown.stdout)
        except yaml.YAMLError as exc:
            log.warning("%s at %s is not valid YAML; ignoring it: %s", name, ref, exc)
            return None
        if data is None:
            return {}
        if not isinstance(data, dict):
            log.warning("%s at %s is not a YAML mapping; ignoring it", name, ref)
            return None
        return data
    return None


def _working_tree_verification(repo: Path) -> dict[str, Any] | None:
    """What the checkout asks for, so we can say when we did not use it.

    ``None`` means the checkout asks for nothing, which is the ordinary case and
    is never worth a note.
    """
    for name in PROJECT_CONFIG_NAMES:
        candidate = repo / name
        if not candidate.is_file():
            continue
        try:
            data = yaml.safe_load(candidate.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            return None
        section = _verification_of(data) if isinstance(data, dict) else {}
        return section or None
    return None


def _read_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping, got {type(data).__name__}.")
    _warn_if_others_can_read_keys(path, data)
    return data


_WINDOWS = os.name == "nt"


def _warn_if_others_can_read_keys(path: Path, data: dict[str, Any]) -> None:
    """A file holding literal secrets has no business being readable by others.

    POSIX only. Windows governs access through ACLs and synthesises ``st_mode``
    as 0o666 or 0o444 whatever the ACL actually says, so the group and other bits
    carry no information there -- checking them reports every config as exposed,
    and ``chmod 600`` is not advice a Windows user can act on.
    """
    if _WINDOWS:
        return
    llm = data.get("llm")
    forge = data.get("forge")
    holds_secrets = (isinstance(llm, dict) and bool(llm.get("api_keys"))) or (
        isinstance(forge, dict) and bool(forge.get("tokens"))
    )
    if not holds_secrets:
        return
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if mode & 0o077:
        log.warning("%s holds secrets and is readable by other accounts; chmod 600 it.", path)


def _env_layer() -> dict[str, Any]:
    """Map the handful of env vars worth supporting onto the config tree."""
    layer: dict[str, Any] = {}
    if model := os.getenv("ROBORAK_MODEL"):
        layer.setdefault("llm", {})["model"] = model
    if floor := os.getenv("ROBORAK_SEVERITY_FLOOR"):
        layer.setdefault("review", {})["severity_floor"] = floor
    forge: dict[str, dict[str, str]] = {}
    for provider in ("gitlab", "github"):
        if token := os.getenv(f"ROBORAK_{provider.upper()}_TOKEN"):
            forge.setdefault("tokens", {})[provider] = token
        if host := os.getenv(f"ROBORAK_{provider.upper()}_HOST"):
            forge.setdefault("hosts", {})[provider] = host
    if forge:
        layer["forge"] = forge
    if (static_off := os.getenv("ROBORAK_NO_STATIC")) and static_off not in {"0", "false", ""}:
        layer.setdefault("static", {})["enabled"] = False
    if execution := os.getenv("ROBORAK_STATIC_EXECUTION"):
        layer.setdefault("static", {})["execution"] = execution
    if (verify_off := os.getenv("ROBORAK_NO_VERIFY")) and verify_off not in {"0", "false", ""}:
        layer.setdefault("verification", {})["enabled"] = False
    if execution := os.getenv("ROBORAK_VERIFY_EXECUTION"):
        layer.setdefault("verification", {})["execution"] = execution
    if (impact_off := os.getenv("ROBORAK_NO_IMPACT")) and impact_off not in {"0", "false", ""}:
        layer.setdefault("impact", {})["enabled"] = False
    if checkout := os.getenv("ROBORAK_IMPACT_FORGE_CHECKOUT"):
        layer.setdefault("impact", {})["forge_checkout"] = checkout
    supply_off = os.getenv("ROBORAK_NO_SUPPLY_CHAIN")
    if supply_off and supply_off not in {"0", "false", ""}:
        layer.setdefault("supply_chain", {})["enabled"] = False
    investigate_off = os.getenv("ROBORAK_NO_INVESTIGATE")
    if investigate_off and investigate_off not in {"0", "false", ""}:
        layer.setdefault("review", {}).setdefault("investigate", {})["enabled"] = False
    return layer


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Merge mappings recursively; lists and scalars are replaced, not appended."""
    out = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out
