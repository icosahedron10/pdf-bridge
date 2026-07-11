from __future__ import annotations

import pytest

from pdf_bridge.models import ScanState
from pdf_bridge.scanner import ScannerProtocolError, _parse_scan_response


def test_clamd_response_parser_handles_clean_and_infected() -> None:
    clean = _parse_scan_response("stream: OK")
    assert clean.state == ScanState.CLEAN
    infected = _parse_scan_response("stream: Eicar-Signature FOUND")
    assert infected.state == ScanState.INFECTED
    assert infected.signature == "Eicar-Signature"


@pytest.mark.parametrize(
    "response",
    ["malformed", "stream:  ERROR", "stream: scanner ERROR", "stream: MAYBE"],
)
def test_clamd_response_parser_rejects_invalid_protocol(response: str) -> None:
    with pytest.raises(ScannerProtocolError):
        _parse_scan_response(response)
