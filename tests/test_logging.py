from __future__ import annotations

import json
import logging

from pdf_bridge.logging_config import JsonFormatter


def test_exception_logs_omit_exception_messages_and_local_paths(tmp_path) -> None:
    sensitive_path = tmp_path / "customer-private-name.pdf"
    try:
        raise OSError(f"could not open {sensitive_path}")
    except OSError:
        record = logging.LogRecord(
            name="pdf_bridge.test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="safe fixed message",
            args=(),
            exc_info=__import__("sys").exc_info(),
        )

    rendered = JsonFormatter().format(record)
    event = json.loads(rendered)
    assert event["message"] == "safe fixed message"
    assert event["exception"]["type"] == "OSError"
    assert str(tmp_path) not in rendered
    assert "customer-private-name.pdf" not in rendered
