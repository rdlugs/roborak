"""Remember what has already been said.

Without this, re-running a review on a merge request re-posts every finding on
every push, which is the fastest way to get a review bot muted. Fingerprints are
line-number independent, so an unrelated edit above a finding does not resurrect
it as "new".
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from roborak.core.models import Finding

log = logging.getLogger(__name__)

STATE_DIR = ".roborak"
STATE_FILE = "state.json"
SCHEMA_VERSION = 1


@dataclass
class ReviewRecord:
    fingerprints: set[str] = field(default_factory=set)
    last_head_sha: str = ""
    last_reviewed_at: str = ""

    def to_json(self) -> dict[str, object]:
        return {
            "fingerprints": sorted(self.fingerprints),
            "last_head_sha": self.last_head_sha,
            "last_reviewed_at": self.last_reviewed_at,
        }

    @classmethod
    def from_json(cls, data: dict[str, object]) -> ReviewRecord:
        raw = data.get("fingerprints")
        return cls(
            fingerprints=set(raw) if isinstance(raw, list) else set(),
            last_head_sha=str(data.get("last_head_sha") or ""),
            last_reviewed_at=str(data.get("last_reviewed_at") or ""),
        )


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
            # Corrupt state is not worth failing a review over; start fresh.
            log.warning("could not read %s; starting with empty state", self.path)
            return {"version": SCHEMA_VERSION, "reviews": {}}
        if not isinstance(data, dict) or data.get("version") != SCHEMA_VERSION:
            return {"version": SCHEMA_VERSION, "reviews": {}}
        return data

    def get(self, key: str) -> ReviewRecord:
        reviews = self._load_all().get("reviews")
        entry = reviews.get(key) if isinstance(reviews, dict) else None
        return ReviewRecord.from_json(entry) if isinstance(entry, dict) else ReviewRecord()

    def record(self, key: str, findings: list[Finding], head_sha: str) -> None:
        """Add this run's findings to what we have already said about ``key``."""
        data = self._load_all()
        reviews = data.setdefault("reviews", {})
        if not isinstance(reviews, dict):
            reviews = {}
            data["reviews"] = reviews

        record = self.get(key)
        record.fingerprints |= {f.fingerprint for f in findings}
        record.last_head_sha = head_sha
        record.last_reviewed_at = datetime.now(UTC).isoformat(timespec="seconds")
        reviews[key] = record.to_json()

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Write via a temporary file so an interrupted run cannot truncate state.
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
            temporary.replace(self.path)
        except OSError as exc:
            log.warning("could not save review state: %s", exc)

    def clear(self, key: str | None = None) -> None:
        data = self._load_all()
        reviews = data.get("reviews")
        if not isinstance(reviews, dict):
            return
        if key is None:
            data["reviews"] = {}
        else:
            reviews.pop(key, None)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError as exc:
            log.warning("could not clear review state: %s", exc)


def review_key(provider: str, host: str, project: str, number: int) -> str:
    """Identify one merge/pull request across runs and machines."""
    return f"{provider}:{host}:{project}#{number}"
