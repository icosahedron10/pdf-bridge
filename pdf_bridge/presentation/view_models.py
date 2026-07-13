"""Presentation-only dictionaries for the deliberately simple Jinja frontend."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pdf_bridge.persistence.models import AuditEvent, Document, DocumentState, QueueOperation

PREVIEW_BLOCKED_STATES = frozenset(
    {
        DocumentState.INGEST_FAILED,
        DocumentState.DELETE_FAILED,
        DocumentState.DELETE_CLEANUP,
        DocumentState.DELETED,
        DocumentState.CANCEL_CLEANUP,
        DocumentState.CANCELLED,
    }
)
CLEANUP_PENDING_STATES = frozenset(
    {DocumentState.DELETE_CLEANUP, DocumentState.CANCEL_CLEANUP}
)


def format_size(size_bytes: int) -> str:
    """Format a byte count with compact binary units for browser display."""

    if size_bytes < 1024:
        return f"{size_bytes} B"
    value = float(size_bytes)
    for unit in ("KiB", "MiB", "GiB"):
        value /= 1024
        if value < 1024 or unit == "GiB":
            precision = 0 if value >= 100 else 1
            return f"{value:.{precision}f} {unit}"
    raise AssertionError("unreachable")


def format_time(value: datetime | None) -> str | None:
    """Format an optional timestamp consistently in UTC."""

    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def document_view(document: Document) -> dict[str, Any]:
    """Convert a document model into the compatibility-rich template mapping."""

    # Templates intentionally consume both domain-oriented and legacy display
    # aliases; centralizing them keeps Jinja pages free of lifecycle logic.
    can_preview = (
        document.scan_state.value == "CLEAN"
        and document.storage_key is not None
        and document.state not in PREVIEW_BLOCKED_STATES
    )
    return {
        "id": str(document.id),
        "document_id": str(document.id),
        "original_filename": document.original_filename,
        "filename": document.original_filename,
        "normalized_filename": document.normalized_filename,
        "size_bytes": document.size_bytes,
        "size_display": format_size(document.size_bytes),
        "size_bytes_display": format_size(document.size_bytes),
        "sha256": document.sha256,
        "status": document.state.value,
        "state": document.state.value,
        "scan_status": document.scan_state.value,
        "scan_state": document.scan_state.value,
        "scan_engine": document.scan_engine,
        "scan_engine_version": document.scan_engine,
        "scan_signature": document.scan_signature,
        "scan_completed_at": document.scanned_at,
        "scan_completed_at_display": format_time(document.scanned_at),
        "media_type": document.content_type,
        "content_type": document.content_type,
        "created_at": document.uploaded_at,
        "created_at_display": format_time(document.uploaded_at),
        "uploaded_at": document.uploaded_at,
        "uploaded_by": document.uploader_identity,
        "uploader_identity": document.uploader_identity,
        "ingested_at": document.ingested_at,
        "ingested_at_display": format_time(document.ingested_at),
        "deleted_at": document.deleted_at,
        "deleted_at_display": format_time(document.deleted_at),
        "chunk_count": document.chunk_count,
        "pipeline_run_id": document.pipeline_run_id,
        "pipeline_metadata": document.pipeline_metadata,
        "error_message": document.last_error,
        "collection_key": document.collection_key,
        "can_preview": can_preview,
        "cleanup_pending": document.state in CLEANUP_PENDING_STATES,
    }


def operation_view(operation: QueueOperation) -> dict[str, Any]:
    """Convert a queue operation and its document into template-ready data."""

    return {
        "id": str(operation.id),
        "operation_id": str(operation.id),
        "document": document_view(operation.document),
        "document_id": str(operation.document_id),
        "operation_type": operation.operation_type.value,
        "type": operation.operation_type.value,
        "status": operation.document.state.value,
        "operation_status": operation.state.value,
        "state": operation.state.value,
        "attempt": operation.attempt,
        "created_at": operation.created_at,
        "created_at_display": format_time(operation.created_at),
        "claimed_at": operation.claimed_at,
        "claimed_at_display": format_time(operation.claimed_at),
        "staged_at": operation.staged_at,
        "staged_at_display": format_time(operation.staged_at),
        "completed_at": operation.completed_at,
        "completed_at_display": format_time(operation.completed_at),
        "lease_expires_at": operation.lease_expires_at,
        "lease_expires_at_display": format_time(operation.lease_expires_at),
        "batch_id": str(operation.batch_id) if operation.batch_id else None,
        "error": operation.error,
        "error_message": operation.error,
        "component_results": operation.component_results,
    }


def audit_event_view(event: AuditEvent) -> dict[str, Any]:
    """Convert an audit event into its concise timeline representation."""

    details = event.details or {}
    return {
        "id": event.id,
        "event_type": event.event_type,
        "title": details.get("title") or event.event_type,
        "status": details.get("status"),
        "occurred_at": event.occurred_at,
        "occurred_at_display": format_time(event.occurred_at),
        "actor": event.actor_id,
        "actor_display": event.actor_id,
        "actor_type": event.actor_type,
        "batch_id": str(event.batch_id) if event.batch_id else None,
        "pipeline_run_id": details.get("pipeline_run_id"),
        "detail": details.get("detail"),
    }


def components_view(operation: QueueOperation | None) -> list[dict[str, Any]]:
    """Return pipeline component rows for an operation when reported."""

    if not operation or not operation.component_results:
        return []
    return [
        {
            "name": component.get("name", "unknown"),
            "status": component.get("status", "not_reported"),
            "detail": component.get("detail"),
            "item_count": component.get("item_count"),
            "completed_at_display": format_time(operation.completed_at),
        }
        for component in operation.component_results
    ]
