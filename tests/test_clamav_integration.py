from __future__ import annotations

import os
from pathlib import Path

import pytest

from pdf_bridge.persistence.models import ScanState
from pdf_bridge.services.scanner import clamd_scan_path

pytestmark = pytest.mark.clamav


@pytest.mark.skipif(
    os.getenv("PDF_BRIDGE_RUN_CLAMAV_TESTS") != "1",
    reason="set PDF_BRIDGE_RUN_CLAMAV_TESTS=1 with clamd running",
)
def test_live_clamav_accepts_pdf_and_detects_eicar(tmp_path: Path) -> None:
    host = os.getenv("PDF_BRIDGE_CLAMD_HOST", "127.0.0.1")
    port = int(os.getenv("PDF_BRIDGE_CLAMD_PORT", "3310"))
    clean = tmp_path / "clean.pdf"
    clean.write_bytes(b"%PDF-1.4\n% clean integration fixture\n%%EOF\n")
    eicar = tmp_path / "eicar.pdf"
    eicar.write_bytes(
        b"%PDF-1.4\nX5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*\n"
    )
    assert clamd_scan_path(clean, host=host, port=port, timeout=20).state == ScanState.CLEAN
    assert clamd_scan_path(eicar, host=host, port=port, timeout=20).state == ScanState.INFECTED
