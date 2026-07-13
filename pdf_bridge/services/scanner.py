"""Minimal clamd client using the streaming protocol over a TCP socket."""

from __future__ import annotations

import socket
import struct
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path

from pdf_bridge.core.config import Settings
from pdf_bridge.persistence.models import ScanState, utc_now

CLAMD_CHUNK_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 16 * 1024


class ScannerError(RuntimeError):
    """Base failure raised by the malware-scanning boundary."""


class ScannerUnavailableError(ScannerError):
    """Raised when clamd cannot be reached or completes no exchange."""


class ScannerProtocolError(ScannerError):
    """Raised when clamd returns a malformed or unsafe response."""


@dataclass(frozen=True, slots=True)
class ScanResult:
    """Normalized malware scan outcome stored with a document."""

    state: ScanState
    engine: str
    scanned_at: datetime
    signature: str | None = None


Scanner = Callable[[Path], ScanResult]


def _receive_response(connection: socket.socket) -> str:
    response = bytearray()
    while len(response) < MAX_RESPONSE_BYTES:
        chunk = connection.recv(min(4096, MAX_RESPONSE_BYTES - len(response)))
        if not chunk:
            break
        response.extend(chunk)
        if b"\0" in chunk or b"\n" in chunk:
            break
    if not response:
        raise ScannerProtocolError("clamd closed the connection without a response")
    if len(response) >= MAX_RESPONSE_BYTES:
        raise ScannerProtocolError("clamd response exceeded the safety limit")
    try:
        return bytes(response).rstrip(b"\0\r\n").decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ScannerProtocolError("clamd returned a non-UTF-8 response") from exc


def _parse_scan_response(response: str) -> ScanResult:
    _stream_name, separator, result = response.partition(":")
    if not separator:
        raise ScannerProtocolError("clamd returned a malformed scan response")
    result = result.strip()
    if result == "OK":
        return ScanResult(state=ScanState.CLEAN, engine="clamd", scanned_at=utc_now())
    if result.endswith(" FOUND"):
        signature = result[: -len(" FOUND")].strip()
        if not signature:
            raise ScannerProtocolError("clamd reported malware without a signature")
        return ScanResult(
            state=ScanState.INFECTED,
            engine="clamd",
            signature=signature[:255],
            scanned_at=utc_now(),
        )
    if result.endswith(" ERROR"):
        raise ScannerProtocolError("clamd could not complete the scan")
    raise ScannerProtocolError("clamd returned an unrecognized scan response")


def clamd_scan_path(
    path: Path,
    *,
    host: str,
    port: int,
    timeout: float,
    chunk_bytes: int = CLAMD_CHUNK_BYTES,
) -> ScanResult:
    """Scan a file via clamd INSTREAM so the daemon never receives a host path."""

    if chunk_bytes <= 0:
        raise ValueError("chunk_bytes must be positive")
    if not path.is_file():
        raise ScannerError("scan target is not a regular file")

    try:
        with socket.create_connection((host, port), timeout=timeout) as connection:
            connection.settimeout(timeout)
            connection.sendall(b"zINSTREAM\0")
            with path.open("rb") as file:
                while chunk := file.read(chunk_bytes):
                    connection.sendall(struct.pack("!I", len(chunk)))
                    connection.sendall(chunk)
            connection.sendall(struct.pack("!I", 0))
            response = _receive_response(connection)
    except (TimeoutError, socket.timeout, ConnectionError, OSError) as exc:
        raise ScannerUnavailableError("malware scanner is unavailable") from exc
    return _parse_scan_response(response)


def scanner_from_settings(settings: Settings) -> Scanner:
    """Build an injectable scanner callable from application settings."""

    return partial(
        clamd_scan_path,
        host=settings.clamd_host,
        port=settings.clamd_port,
        timeout=settings.clamd_timeout,
    )


def clamd_ping(*, host: str, port: int, timeout: float) -> bool:
    """Return readiness of the configured clamd dependency."""

    try:
        with socket.create_connection((host, port), timeout=timeout) as connection:
            connection.settimeout(timeout)
            connection.sendall(b"zPING\0")
            return _receive_response(connection) == "PONG"
    except (ScannerError, TimeoutError, socket.timeout, ConnectionError, OSError):
        return False
