"""Presentation-only dictionaries for the deliberately simple Jinja frontend."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pdf_bridge.persistence.models import (
    AuditEvent,
    Document,
    DocumentAnalysis,
    DocumentState,
    IntakeDecision,
    WorkOperation,
)

PREVIEW_BLOCKED_STATES = frozenset(
    {
        DocumentState.CLEANUP_PENDING,
        DocumentState.CLEANUP_FAILED,
        DocumentState.REJECTED,
        DocumentState.DELETED,
        DocumentState.CANCELLED,
    }
)
CLEANUP_PENDING_STATES = frozenset(
    {DocumentState.CLEANUP_PENDING, DocumentState.CLEANUP_FAILED}
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
    """Convert a document model into template-ready lifecycle data."""

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
        "rejected_at": document.rejected_at,
        "rejected_at_display": format_time(document.rejected_at),
        "cancelled_at": document.cancelled_at,
        "cancelled_at_display": format_time(document.cancelled_at),
        "deleted_at": document.deleted_at,
        "deleted_at_display": format_time(document.deleted_at),
        "page_count": document.page_count,
        "chunk_count": document.chunk_count,
        "text_sha256": document.text_sha256,
        "analysis_revision": document.analysis_revision,
        "analysis_manifest_hash": document.analysis_manifest_hash,
        "rejection_reason": document.rejection_reason,
        "replaced_by_document_id": (
            str(document.replaced_by_document_id)
            if document.replaced_by_document_id
            else None
        ),
        "error_message": document.last_error,
        "collection_key": document.collection_key,
        "can_preview": can_preview,
        "cleanup_pending": document.state in CLEANUP_PENDING_STATES,
    }


def operation_view(operation: WorkOperation) -> dict[str, Any]:
    """Convert a durable worker operation into template-ready data."""

    return {
        "id": str(operation.id),
        "operation_id": str(operation.id),
        "document": document_view(operation.document),
        "document_id": str(operation.document_id),
        "operation_type": operation.operation_type.value,
        "type": operation.operation_type.value,
        "status": operation.document.state.value,
        "phase": operation.phase.value,
        "operation_status": operation.state.value,
        "state": operation.state.value,
        "attempt": operation.attempt,
        "retryable": operation.retryable,
        "created_at": operation.created_at,
        "created_at_display": format_time(operation.created_at),
        "started_at": operation.started_at,
        "started_at_display": format_time(operation.started_at),
        "heartbeat_at": operation.heartbeat_at,
        "heartbeat_at_display": format_time(operation.heartbeat_at),
        "updated_at": operation.updated_at,
        "updated_at_display": format_time(operation.updated_at),
        "completed_at": operation.completed_at,
        "completed_at_display": format_time(operation.completed_at),
        "lease_expires_at": operation.lease_expires_at,
        "lease_expires_at_display": format_time(operation.lease_expires_at),
        "worker_id": operation.worker_id,
        "error": operation.error,
        "error_message": operation.error,
    }


def analysis_view(analysis: DocumentAnalysis | None) -> dict[str, Any] | None:
    """Convert the latest persisted analysis into a concise page summary."""

    if analysis is None:
        return None
    return {
        "id": str(analysis.id),
        "revision": analysis.revision,
        "status": analysis.status.value,
        "pipeline_fingerprint": analysis.pipeline_fingerprint,
        "page_count": analysis.page_count,
        "chunk_count": analysis.chunk_count,
        "semantic_complete": analysis.semantic_complete,
        "classification_complete": analysis.classification_complete,
        "incomplete_reasons": list(analysis.incomplete_reasons),
        "auto_ingest_eligible": analysis.auto_ingest_eligible,
        "candidate_count": analysis.candidate_count,
        "classified_count": analysis.classified_count,
        "overflow_count": analysis.overflow_count,
        "created_at_display": format_time(analysis.created_at),
        "completed_at_display": format_time(analysis.completed_at),
    }


def decision_view(decision: IntakeDecision) -> dict[str, Any]:
    """Convert one immutable operator decision for the document ledger."""

    return {
        "id": str(decision.id),
        "action": decision.action.value,
        "analysis_revision": decision.analysis_revision,
        "target_document_id": (
            str(decision.target_document_id) if decision.target_document_id else None
        ),
        "advisory_override": decision.advisory_override,
        "actor_id": decision.actor_id,
        "created_at_display": format_time(decision.created_at),
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
        "operation_id": str(event.operation_id) if event.operation_id else None,
        "detail": details.get("detail"),
        "analysis_revision": details.get("analysis_revision"),
        "target_document_id": details.get("target_document_id"),
    }
