"""The contract every change source implements."""

from __future__ import annotations

from typing import Protocol

from roborak.core.models import ChangeSet


class SourceError(RuntimeError):
    """Raised when a source cannot produce a changeset, with a user-facing message.

    ``status`` carries the HTTP status that caused it, when one did. It lets a
    caller tell an answer -- "this token may not ask that" -- apart from a forge
    that was simply unreachable.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class ChangeSource(Protocol):
    """Produces a ``ChangeSet``. The only thing the pipeline knows about origins."""

    def load(self) -> ChangeSet: ...
