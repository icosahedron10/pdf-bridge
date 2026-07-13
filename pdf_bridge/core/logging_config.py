"""Minimal structured logging without introducing another runtime dependency."""

from __future__ import annotations

import json
import logging
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class JsonFormatter(logging.Formatter):
    """Serialize log records to a compact structured JSON event."""

    def format(self, record: logging.LogRecord) -> str:
        """Render a log record with safe context and exception frame metadata."""

        event: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("request_id", "document_id", "operation_id", "batch_id", "outcome"):
            value = getattr(record, key, None)
            if value is not None:
                event[key] = value
        if record.exc_info:
            exception_type, _exception, exception_traceback = record.exc_info
            frames = traceback.extract_tb(exception_traceback)
            event["exception"] = {
                "type": exception_type.__name__,
                "frames": [
                    {
                        "file": Path(frame.filename).name,
                        "line": frame.lineno,
                        "function": frame.name,
                    }
                    for frame in frames
                ],
            }
        return json.dumps(event, ensure_ascii=True)


def configure_logging(level: str = "INFO") -> None:
    """Replace root handlers with the structured JSON stream handler."""

    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
