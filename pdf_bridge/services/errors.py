"""Framework-neutral errors raised by application services."""

from __future__ import annotations

from typing import Any


class ServiceError(RuntimeError):
    """A deliberate service failure that a transport may map to its error contract."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        status: int,
        title: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.title = title
        self.extra = extra
