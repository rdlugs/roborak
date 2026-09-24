"""Remember what has already been said.

Without this, re-running a review on a merge request re-posts every finding on
every push, which is the fastest way to get a review bot muted. Fingerprints are
line-number independent, so an unrelated edit above a finding does not resurrect
it as "new".
"""

from __future__ import annotations

import hashlib
import json
import logging
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from filelock import FileLock
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from roborak.core.models import ChangeSet, Finding

log = logging.getLogger(__name__)

STATE_DIR = ".roborak"
STATE_FILE = "state.json"
STATE_LOCK_FILE = "state.lock"
SCHEMA_VERSION = 1


class StateWriteError(RuntimeError):
    """The local review state could not be persisted atomically."""


@dataclass
class ReviewRecord:
    fingerprints: set[str] = field(default_factory=set)
    last_head_sha: str = ""
    last_reviewed_at: str = ""
    last_flow_digest: str = ""
    """Shape of the change the cached overview narrates."""

    last_walkthrough: dict[str, object] | None = None
    """The overview itself, so an unchanged change can be re-rendered without
    paying for a second model call. Local, so a fresh CI checkout simply has
    none and leaves the published comment alone instead."""

    def to_json(self) -> dict[str, object]:
        return {
            "fingerprints": sorted(self.fingerprints),
            "last_head_sha": self.last_head_sha,
            "last_reviewed_at": self.last_reviewed_at,
            "last_flow_digest": self.last_flow_digest,
            "last_walkthrough": self.last_walkthrough,
        }

    @classmethod
    def from_json(cls, data: dict[str, object]) -> ReviewRecord:
        raw = data.get("fingerprints")
        cached = data.get("last_walkthrough")
        return cls(
            fingerprints=set(raw) if isinstance(raw, list) else set(),
            last_head_sha=str(data.get("last_head_sha") or ""),
            last_reviewed_at=str(data.get("last_reviewed_at") or ""),
            last_flow_digest=str(data.get("last_flow_digest") or ""),
            last_walkthrough=cached if isinstance(cached, dict) else None,
        )


class RequirementEvidence(BaseModel):
    """Validated requirement evidence persisted between review runs."""

    model_config = ConfigDict(extra="forbid", strict=True)

    requirement: str = Field(min_length=1, max_length=300)
    file: str = Field(max_length=1024)
    evidence: str = Field(min_length=1, max_length=300)


class CompatibilityEvidence(BaseModel):
    """Validated compatibility evidence persisted between review runs."""

    model_config = ConfigDict(extra="forbid", strict=True)

    contract: str = Field(min_length=1, max_length=300)
    contract_file: str
    file: str = Field(max_length=1024)
    status: str = Field(min_length=1, max_length=300)
    evidence: str = Field(min_length=1, max_length=300)


class ChunkResult(BaseModel):
    """The reusable output of one successful primary review unit."""

    model_config = ConfigDict(extra="forbid", strict=True)

    findings: list[Finding] = Field(default_factory=list)
    requirements: list[RequirementEvidence] = Field(default_factory=list)
    compatibility: list[CompatibilityEvidence] = Field(default_factory=list)


class ReviewCheckpoint(BaseModel):
    """Progress for one exact review plan, saved after every model pass."""

    model_config = ConfigDict(extra="forbid", strict=True)

    signature: str
    completed: dict[str, ChunkResult] = Field(default_factory=dict)
    failed: dict[str, str] = Field(default_factory=dict)

    def to_json(self) -> dict[str, object]:
        return self.model_dump(mode="json")

    @classmethod
    def from_json(cls, data: object, signature: str) -> ReviewCheckpoint:
        try:
            checkpoint = cls.model_validate(data)
        except ValidationError:
            return cls(signature=signature)
        return checkpoint if checkpoint.signature == signature else cls(signature=signature)


@dataclass
class StateStore:
    repo: Path

    @property
    def path(self) -> Path:
        return self.repo / STATE_DIR / STATE_FILE

    @property
    def lock_path(self) -> Path:
        return self.repo / STATE_DIR / STATE_LOCK_FILE

    def _load_all(self) -> dict[str, object]:
        if not self.path.is_file():
            return {"version": SCHEMA_VERSION, "reviews": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.warning("could not read %s; starting with empty state", self.path)
            return {"version": SCHEMA_VERSION, "reviews": {}}
        if not isinstance(data, dict) or data.get("version") != SCHEMA_VERSION:
            return {"version": SCHEMA_VERSION, "reviews": {}}
        return data

    def get(self, key: str) -> ReviewRecord:
        reviews = self._load_all().get("reviews")
        entry = reviews.get(key) if isinstance(reviews, dict) else None
        return ReviewRecord.from_json(entry) if isinstance(entry, dict) else ReviewRecord()

    def get_checkpoint(self, key: str, signature: str) -> ReviewCheckpoint:
        checkpoints = self._load_all().get("checkpoints")
        entry = checkpoints.get(key) if isinstance(checkpoints, dict) else None
        return ReviewCheckpoint.from_json(entry, signature)

    def save_checkpoint(self, key: str, checkpoint: ReviewCheckpoint) -> None:
        with self._lock():
            data = self._load_all()
            checkpoints = data.setdefault("checkpoints", {})
            if not isinstance(checkpoints, dict):
                checkpoints = {}
                data["checkpoints"] = checkpoints
            stored = checkpoints.get(key)
            if isinstance(stored, dict) and stored.get("signature") == checkpoint.signature:
                previous = ReviewCheckpoint.from_json(stored, checkpoint.signature)
                checkpoint.completed = {**previous.completed, **checkpoint.completed}
                checkpoint.failed = {**previous.failed, **checkpoint.failed}
            for identity in checkpoint.completed:
                checkpoint.failed.pop(identity, None)
            checkpoints[key] = checkpoint.to_json()
            self._save(data)

    def record(
        self,
        key: str,
        findings: list[Finding],
        head_sha: str,
        *,
        flow_digest: str = "",
        walkthrough: dict[str, object] | None = None,
    ) -> None:
        """Add this run's findings to what we have already said about ``key``."""
        with self._lock():
            data = self._load_all()
            reviews = data.setdefault("reviews", {})
            if not isinstance(reviews, dict):
                reviews = {}
                data["reviews"] = reviews

            stored = reviews.get(key)
            record = ReviewRecord.from_json(stored) if isinstance(stored, dict) else ReviewRecord()
            record.fingerprints |= {
                identity
                for finding in findings
                for identity in (finding.fingerprint, finding.fingerprint_v2)
            }
            record.last_head_sha = head_sha
            record.last_reviewed_at = datetime.now(UTC).isoformat(timespec="seconds")
            if flow_digest and flow_digest != record.last_flow_digest:
                # The cached overview narrates the old shape; it is not reusable now.
                record.last_flow_digest = flow_digest
                record.last_walkthrough = None
            if walkthrough is not None:
                record.last_walkthrough = walkthrough
            reviews[key] = record.to_json()

            self._save(data)

    def clear(self, key: str | None = None) -> None:
        with self._lock():
            data = self._load_all()
            reviews = data.get("reviews")
            checkpoints = data.get("checkpoints")
            if not isinstance(reviews, dict):
                reviews = {}
                data["reviews"] = reviews
            if not isinstance(checkpoints, dict):
                checkpoints = {}
                data["checkpoints"] = checkpoints
            if key is None:
                data["reviews"] = {}
                data["checkpoints"] = {}
            else:
                reviews.pop(key, None)
                checkpoints.pop(key, None)
            self._save(data)

    @contextmanager
    def _lock(self) -> Iterator[None]:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with FileLock(self.lock_path):
                yield
        except OSError as exc:
            raise StateWriteError(
                f"could not lock review state at {self.lock_path}: {exc}"
            ) from exc

    def _save(self, data: dict[str, object]) -> None:
        temporary: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as output:
                temporary = Path(output.name)
                json.dump(data, output, indent=2)
            temporary.replace(self.path)
        except OSError as exc:
            raise StateWriteError(f"could not save review state to {self.path}: {exc}") from exc
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    log.warning("could not remove temporary state file %s", temporary)


def review_key(provider: str, host: str, project: str, number: int) -> str:
    """Identify one merge/pull request across runs and machines."""
    return f"{provider}:{host}:{project}#{number}"


def checkpoint_key(changeset: ChangeSet) -> str:
    """Identify a review stream inside this repository's local state file."""
    if changeset.forge_ref is not None:
        ref = changeset.forge_ref
        return review_key(ref.provider, ref.host, ref.project, ref.number)
    source = "|".join(
        (
            changeset.origin,
            changeset.base_ref or "",
            changeset.head_ref or "",
        )
    )
    return f"review:{hashlib.sha256(source.encode()).hexdigest()[:24]}"
