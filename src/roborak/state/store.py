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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from roborak.core.models import ChangeSet, Finding

log = logging.getLogger(__name__)

STATE_DIR = ".roborak"
STATE_FILE = "state.json"
SCHEMA_VERSION = 1


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


@dataclass
class ChunkResult:
    """The reusable output of one successful primary review unit."""

    findings: list[dict[str, object]] = field(default_factory=list)
    requirements: list[dict[str, str]] = field(default_factory=list)
    compatibility: list[dict[str, str]] = field(default_factory=list)

    def to_json(self) -> dict[str, object]:
        return {
            "findings": self.findings,
            "requirements": self.requirements,
            "compatibility": self.compatibility,
        }

    @classmethod
    def from_json(cls, data: object) -> ChunkResult | None:
        if not isinstance(data, dict):
            return None
        findings = data.get("findings")
        requirements = data.get("requirements")
        compatibility = data.get("compatibility")
        if not isinstance(findings, list):
            return None
        requirement_items = requirements if isinstance(requirements, list) else []
        compatibility_items = compatibility if isinstance(compatibility, list) else []
        return cls(
            findings=[item for item in findings if isinstance(item, dict)],
            requirements=[item for item in requirement_items if isinstance(item, dict)],
            compatibility=[item for item in compatibility_items if isinstance(item, dict)],
        )


@dataclass
class ReviewCheckpoint:
    """Progress for one exact review plan, saved after every model pass."""

    signature: str
    completed: dict[str, ChunkResult] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, object]:
        return {
            "signature": self.signature,
            "completed": {key: value.to_json() for key, value in self.completed.items()},
            "failed": self.failed,
        }

    @classmethod
    def from_json(cls, data: object, signature: str) -> ReviewCheckpoint:
        if not isinstance(data, dict) or data.get("signature") != signature:
            return cls(signature=signature)
        raw_completed = data.get("completed")
        completed: dict[str, ChunkResult] = {}
        if isinstance(raw_completed, dict):
            for key, value in raw_completed.items():
                parsed = ChunkResult.from_json(value)
                if isinstance(key, str) and parsed is not None:
                    completed[key] = parsed
        raw_failed = data.get("failed")
        failed = (
            {str(key): str(value) for key, value in raw_failed.items()}
            if isinstance(raw_failed, dict)
            else {}
        )
        return cls(signature=signature, completed=completed, failed=failed)


@dataclass
class StateStore:
    repo: Path

    @property
    def path(self) -> Path:
        return self.repo / STATE_DIR / STATE_FILE

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
        data = self._load_all()
        checkpoints = data.setdefault("checkpoints", {})
        if not isinstance(checkpoints, dict):
            checkpoints = {}
            data["checkpoints"] = checkpoints
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
        data = self._load_all()
        reviews = data.setdefault("reviews", {})
        if not isinstance(reviews, dict):
            reviews = {}
            data["reviews"] = reviews

        record = self.get(key)
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

    def _save(self, data: dict[str, object]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
            temporary.replace(self.path)
        except OSError as exc:
            log.warning("could not save review state: %s", exc)


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
