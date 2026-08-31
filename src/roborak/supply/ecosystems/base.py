"""The contract an ecosystem parser implements."""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from roborak.core.models import DependencyChangeKind

MUTABLE_GIT_REFS = frozenset({"main", "master", "head", "develop", "trunk", "latest", ""})
"""Git references that name a moving target rather than a commit.

A dependency pinned to one of these installs whatever that branch holds at install
time, which is not what anybody reviewed. A 40-character hex sha is the only
reference that cannot change under the project."""


@dataclass(frozen=True)
class Package:
    """One resolved package, as much as its ecosystem records about it."""

    version: str = ""
    source: str = ""
    """Registry URL, git remote, or local path. Empty means the ecosystem's
    default registry, which is itself the fact a source change moves away from."""

    integrity: str = ""
    """Checksum, hash or digest. Empty means nothing verifies what is installed."""

    direct: bool = False
    """Named by a manifest rather than pulled in by another package."""

    ref: str = ""
    """The git reference, for a package resolved from a repository rather than a
    registry. Kept separately from ``version`` because a branch name and a version
    number are not the same claim."""

    @property
    def mutable_ref(self) -> bool:
        """Whether this resolves to something that can change without the diff moving."""
        if not self.ref:
            return False
        return not (len(self.ref) == 40 and all(c in "0123456789abcdef" for c in self.ref.lower()))


Parser = Callable[[str], dict[str, Package]]
VersionCompatibility = Callable[[str, str], bool | None]


@dataclass(frozen=True)
class Ecosystem:
    """One package manager: the files it owns and how to read them."""

    name: str
    manifests: tuple[str, ...]
    """Basenames of human-authored manifests, e.g. ``package.json``."""

    locks: tuple[str, ...]
    """Basenames of resolver output, e.g. ``package-lock.json``."""

    parse_manifest: Parser
    parse_lock: Parser
    version_satisfies: VersionCompatibility | None = None
    """Whether a locked version satisfies a manifest constraint.

    ``None`` means this ecosystem cannot decide. Compatibility checks are
    deliberately conservative: an unfamiliar constraint must stay quiet rather
    than turn into a false drift warning.
    """

    manifest_prefixes: tuple[str, ...] = field(default=())
    """Basename prefixes that are also manifests, for the ecosystems that allow a
    family of them (``requirements-dev.txt`` next to ``requirements.txt``)."""

    def is_manifest(self, path: str) -> bool:
        base = posixpath.basename(path)
        if base in self.manifests:
            return True
        return any(
            base.startswith(prefix) and base.endswith(".txt") for prefix in self.manifest_prefixes
        )

    def is_lock(self, path: str) -> bool:
        return posixpath.basename(path) in self.locks

    def handles(self, path: str) -> bool:
        return self.is_manifest(path) or self.is_lock(path)

    def read(self, path: str, text: str) -> dict[str, Package]:
        """Parse ``text`` as whichever of the two kinds ``path`` is.

        Total by contract: a parser that raises on a file the ecosystem claimed is
        a bug in the parser, and the caller still gets an empty result rather than
        a failed review.
        """
        parser = self.parse_lock if self.is_lock(path) else self.parse_manifest
        try:
            return parser(text)
        except Exception:  # noqa: BLE001 - a resolver's format is not our invariant
            return {}

    def lock_satisfies(self, constraint: str, version: str) -> bool | None:
        """Whether ``version`` is allowed by ``constraint``, when knowable."""
        if not constraint or not version or self.version_satisfies is None:
            return None
        return self.version_satisfies(constraint, version)


_SEMVER = re.compile(
    r"^v?(?P<major>\d+)"
    r"(?:\.(?P<minor>\d+|[xX*]))?"
    r"(?:\.(?P<patch>\d+|[xX*]))?"
    r"(?:-(?P<prerelease>[0-9A-Za-z.-]+))?"
    r"(?:\+[0-9A-Za-z.-]+)?$"
)


def semver_satisfies(constraint: str, version: str) -> bool | None:
    """Evaluate the common SemVer range syntax shared by npm, Cargo and Composer.

    Exact versions, partial/wildcard versions, comparison sets, caret and tilde
    ranges, hyphen ranges and ``||`` alternatives are supported. Anything more
    ecosystem-specific returns ``None`` so drift never guesses.
    """
    locked = _semver(version)
    if locked is None or locked[1] != 3 or locked[2] is not None or locked[3]:
        return None
    value = locked[0]
    alternatives = [part.strip() for part in constraint.strip().split("||")]
    if not alternatives or any(not part for part in alternatives):
        return None
    outcomes = [_semver_group_satisfies(group, value) for group in alternatives]
    if any(outcome is True for outcome in outcomes):
        return True
    return None if any(outcome is None for outcome in outcomes) else False


def exact_version_satisfies(constraint: str, version: str) -> bool | None:
    """Compare ecosystems whose manifests already contain a resolved version."""
    wanted = constraint.strip()
    locked = version.strip()
    if not wanted or not locked:
        return None
    return wanted == locked


def _semver_group_satisfies(group: str, version: tuple[int, int, int]) -> bool | None:
    hyphen = re.fullmatch(r"\s*(\S+)\s+-\s+(\S+)\s*", group)
    if hyphen:
        lower = _semver(hyphen.group(1))
        upper = _semver(hyphen.group(2))
        if (
            lower is None
            or upper is None
            or lower[2] is not None
            or upper[2] is not None
            or lower[3]
            or upper[3]
        ):
            return None
        return lower[0] <= version <= _partial_upper(upper)

    tokens = [token for token in re.split(r"[\s,]+", group.strip()) if token]
    if not tokens:
        return None
    outcomes = [_semver_token_satisfies(token, version) for token in tokens]
    if any(outcome is False for outcome in outcomes):
        return False
    return None if any(outcome is None for outcome in outcomes) else True


def _semver_token_satisfies(token: str, version: tuple[int, int, int]) -> bool | None:
    if token in {"*", "x", "X"}:
        return True
    match = re.fullmatch(r"(?P<op>\^|~|>=|<=|>|<|=)?(?P<value>.+)", token)
    if match is None:
        return None
    operator = match.group("op") or ""
    parsed = _semver(match.group("value"))
    if parsed is None or parsed[3]:
        return None
    lower, parts, wildcard, _prerelease = parsed

    if operator in {">", ">=", "<", "<=", "="}:
        if wildcard is not None and operator != "=":
            return None
        return {
            ">": version > lower,
            ">=": version >= lower,
            "<": version < lower,
            "<=": version <= lower,
            "=": lower <= version <= _partial_upper(parsed),
        }[operator]
    if operator == "^":
        if wildcard is not None:
            return None
        # How far a caret range reaches depends on how much of the version was
        # written: `^0` allows all of 0.x, `^0.0` only 0.0.x, and `^0.0.3` only
        # 0.0.3 itself. Reading the components off `lower` alone would collapse
        # all three into the narrowest.
        if lower[0] > 0:
            upper = (lower[0] + 1, 0, 0)
        elif parts == 1:
            upper = (1, 0, 0)
        elif lower[1] > 0:
            upper = (0, lower[1] + 1, 0)
        elif parts == 2:
            upper = (0, 1, 0)
        else:
            upper = (0, 0, lower[2] + 1)
        return lower <= version < upper
    if operator == "~":
        if wildcard is not None:
            return None
        upper = (lower[0] + 1, 0, 0) if parts == 1 else (lower[0], lower[1] + 1, 0)
        return lower <= version < upper
    return lower <= version <= _partial_upper(parsed)


def _semver(value: str) -> tuple[tuple[int, int, int], int, int | None, bool] | None:
    match = _SEMVER.fullmatch(value.strip())
    if match is None:
        return None
    raw = (match.group("major"), match.group("minor"), match.group("patch"))
    wildcard = next((i for i, part in enumerate(raw) if part in {"x", "X", "*"}), None)
    if wildcard is not None and any(part not in {None, "x", "X", "*"} for part in raw[wildcard:]):
        return None
    parts = next((i for i, part in enumerate(raw) if part is None), 3)
    if wildcard is not None:
        parts = wildcard
    numbers = (
        int(raw[0]),
        int(raw[1]) if raw[1] and raw[1].isdigit() else 0,
        int(raw[2]) if raw[2] and raw[2].isdigit() else 0,
    )
    return (numbers, parts, wildcard, match.group("prerelease") is not None)


def _partial_upper(
    parsed: tuple[tuple[int, int, int], int, int | None, bool],
) -> tuple[int, int, int]:
    value, parts, wildcard, _prerelease = parsed
    wildcard = wildcard if wildcard is not None else (parts if parts < 3 else None)
    if wildcard == 0:
        return (999_999_999, 999_999_999, 999_999_999)
    if wildcard == 1:
        return (value[0], 999_999_999, 999_999_999)
    if wildcard == 2:
        return (value[0], value[1], 999_999_999)
    return value


def classify_version_move(old: str, new: str) -> DependencyChangeKind:
    """Whether a version string moved forwards or backwards.

    Compared component by component on the leading digits, which is enough for the
    question being asked -- a reviewer wants to know that something went *down*,
    and the exact semantics of a build suffix do not change that. Anything that
    does not compare numerically is reported as an upgrade rather than guessed at.
    """
    for old_part, new_part in zip(_numeric_parts(old), _numeric_parts(new), strict=False):
        if old_part == new_part:
            continue
        return (
            DependencyChangeKind.DOWNGRADED
            if new_part < old_part
            else DependencyChangeKind.UPGRADED
        )
    return DependencyChangeKind.UPGRADED


def _numeric_parts(version: str) -> list[int]:
    """The leading numeric components of a version, e.g. ``1.20.3-rc1`` -> ``[1, 20, 3]``."""
    parts: list[int] = []
    for chunk in version.lstrip("v=^~> ").split("."):
        digits = ""
        for char in chunk:
            if not char.isdigit():
                break
            digits += char
        if not digits:
            break
        parts.append(int(digits))
    return parts
