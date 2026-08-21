"""The contract every change source implements."""

from __future__ import annotations

from typing import Protocol

from roborak.core.models import ChangeSet


class SourceError(RuntimeError):
    """Raised when a source cannot produce a changeset, with a user-facing message."""


class ChangeSource(Protocol):
    """Produces a ``ChangeSet``. The only thing the pipeline knows about origins."""

    def load(self) -> ChangeSet: ...
