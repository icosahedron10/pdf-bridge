"""Transactional API-v2 lifecycle and durable queue transitions."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from pdf_bridge.persistence.models import (
    AuditEvent,
    CandidateEvidence,
    Decision,
    DecisionAction,
    DeletionPhase,
    DeletionProgress,
    Document,
    DocumentState,
    ExtractedPage,
    FormatterBatch,
    IdempotencyRecord,
    OperationPhase,
    OperationPriority,
    OperationState,
    OperationType,
    PreparedCandidate,
    PreparedChunk,
    PreparedChunkVector,
    PreparedPage,
    PreparedRevision,
    PublicationRecord,
    PublicationStatus,
    RevisionArtifact,
    RevisionStatus,
    ScanState,
    TerminalDisposition,
    Tombstone,
    WorkOperation,
    priority_for_operation,
    utc_now,
)


class LifecycleError(RuntimeError):
    """A deliberate, sanitized lifecycle conflict suitable for an API error."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 409,
        retryable: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.retryable = retryable
        self.extra = extra or {}


@dataclass(frozen=True, slots=True)
class IdempotencyReplay:
    """Completed response material for an identical repeated mutation."""

    status: int
    body: dict[str, Any]


@dataclass(frozen=True, slots=True)
class MutationOutcome:
    document: Document
    operation: WorkOperation | None
    idempotency: IdempotencyRecord


_PREFLIGHT_OPERATION_PHASES = frozenset(
    {
        OperationPhase.QUEUED,
        OperationPhase.EXTRACTING,
        OperationPhase.CHECKING_ELIGIBILITY,
        OperationPhase.PACKING_FORMATTER_BATCHES,
        OperationPhase.FORMATTING_MARKDOWN,
        OperationPhase.VALIDATING_MARKDOWN,
        OperationPhase.CHUNKING_MARKDOWN,
        OperationPhase.EMBEDDING_DENSE,
        OperationPhase.EMBEDDING_SPARSE,
        OperationPhase.UPSERTING_SCREENING_POINTS,
        OperationPhase.DISCOVERING_CANDIDATES,
        OperationPhase.CLASSIFYING_CANDIDATES,
        OperationPhase.SEALING_REVISION,
    }
)
_PUBLICATION_OPERATION_PHASES = frozenset(
    {
        OperationPhase.QUEUED,
        OperationPhase.UPSERT_ACTIVE_POINTS,
        OperationPhase.VERIFY_ACTIVE_POINTS,
        OperationPhase.REMOVE_SCREENING_POINTS,
        OperationPhase.VERIFY_SCREENING_REMOVAL,
    }
)
_DELETION_OPERATION_PHASES = frozenset(
    {OperationPhase.QUEUED}
    | {OperationPhase(phase.value) for phase in DeletionPhase}
)


def canonical_request_sha256(action: str, material: dict[str, Any]) -> str:
    """Hash one mutation's strict canonical request identity."""

    payload = json.dumps(
        {"action": action, "request": material},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def reserve_idempotency(
    session: Session,
    *,
    key: str,
    action: str,
    actor_id: str,
    request_material: dict[str, Any],
) -> IdempotencyRecord | IdempotencyReplay:
    """Reserve a globally unique key or replay its identical completed response."""

    if not 8 <= len(key) <= 128 or any(
        character.isspace() or not character.isprintable() for character in key
    ):
        raise LifecycleError(
            "invalid_idempotency_key",
            "Idempotency-Key must contain 8 to 128 visible characters.",
            status=400,
        )
    if not action or len(action) > 64 or not actor_id or len(actor_id) > 255:
        raise LifecycleError(
            "invalid_idempotency_identity",
            "The idempotency action and actor identity are invalid.",
            status=400,
        )
    request_hash = canonical_request_sha256(action, request_material)
    existing = session.scalar(
        select(IdempotencyRecord).where(IdempotencyRecord.key == key).with_for_update()
    )
    if existing is not None:
        if (
            existing.action != action
            or existing.actor_id != actor_id
            or existing.request_sha256 != request_hash
        ):
            raise LifecycleError(
                "idempotency_conflict",
                "The idempotency key was already used for a different request.",
            )
        if existing.response_status is None or existing.response_body is None:
            raise LifecycleError(
                "idempotency_in_progress",
                "The original request is still being committed; retry shortly.",
                retryable=True,
            )
        return IdempotencyReplay(
            status=existing.response_status,
            body=json.loads(json.dumps(existing.response_body)),
        )
    record = IdempotencyRecord(
        key=key,
        action=action,
        request_sha256=request_hash,
        actor_id=actor_id,
    )
    session.add(record)
    session.flush()
    return record


def complete_idempotency(
    record: IdempotencyRecord,
    *,
    status: int,
    body: dict[str, Any],
    resource_type: str,
    resource_id: uuid.UUID,
) -> None:
    """Seal replay material once; an exact duplicate completion is a no-op."""

    if not 100 <= status <= 599 or not resource_type or len(resource_type) > 64:
        raise LifecycleError(
            "invalid_idempotency_response",
            "The idempotency response metadata is invalid.",
            status=500,
        )
    try:
        body_snapshot = json.loads(
            json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        )
    except (TypeError, ValueError) as exc:
        raise LifecycleError(
            "invalid_idempotency_response",
            "The idempotency response body is not valid JSON.",
            status=500,
        ) from exc
    if not isinstance(body_snapshot, dict):
        raise LifecycleError(
            "invalid_idempotency_response",
            "The idempotency response body must be a JSON object.",
            status=500,
        )
    if record.completed_at is not None:
        if (
            record.response_status == status
            and record.response_body == body_snapshot
            and record.resource_type == resource_type
            and record.resource_id == resource_id
        ):
            return
        raise LifecycleError(
            "idempotency_completion_conflict",
            "Completed idempotency replay material cannot be changed.",
        )
    record.response_status = status
    record.response_body = body_snapshot
    record.resource_type = resource_type
    record.resource_id = resource_id
    record.completed_at = utc_now()


def audit(
    session: Session,
    *,
    event_type: str,
    actor_type: str,
    actor_id: str,
    document_id: uuid.UUID | None = None,
    operation_id: uuid.UUID | None = None,
    details: dict[str, Any] | None = None,
) -> AuditEvent:
    """Append a content-free lifecycle event."""

    event = AuditEvent(
        document_id=document_id,
        operation_id=operation_id,
        event_type=event_type,
        actor_type=actor_type,
        actor_id=actor_id,
        details=details or {},
    )
    session.add(event)
    return event


def next_attempt(
    session: Session,
    document_id: uuid.UUID,
    operation_type: OperationType,
) -> int:
    maximum = session.scalar(
        select(func.max(WorkOperation.attempt)).where(
            WorkOperation.document_id == document_id,
            WorkOperation.operation_type == operation_type,
        )
    )
    return int(maximum or 0) + 1


def set_operation_phase(
    operation: WorkOperation,
    phase: OperationPhase,
    *,
    now: datetime | None = None,
) -> bool:
    """Advance the durable phase clock only when the operation phase changes."""

    if operation.phase is phase:
        return False
    transition_at = now or utc_now()
    if transition_at.tzinfo is None:
        raise ValueError("operation phase transition timestamps must be timezone-aware")
    operation.phase = phase
    operation.phase_started_at = transition_at.astimezone(UTC)
    return True


def enqueue_operation(
    session: Session,
    *,
    document: Document,
    operation_type: OperationType,
    priority: OperationPriority,
    prepared_revision_id: uuid.UUID | None = None,
    replacement_target_document_id: uuid.UUID | None = None,
    idempotency_record_id: uuid.UUID | None = None,
    phase: OperationPhase = OperationPhase.QUEUED,
) -> WorkOperation:
    """Create one durable immediately eligible operation."""

    replacement_delete = replacement_target_document_id is not None
    try:
        expected_priority = priority_for_operation(
            operation_type, replacement_delete=replacement_delete
        )
    except ValueError as exc:
        raise LifecycleError(
            "operation_binding_invalid",
            "Only a publication operation can own replacement deletion.",
            status=500,
        ) from exc
    if priority is not expected_priority:
        raise LifecycleError(
            "operation_priority_invalid",
            "The operation priority does not match its durable work class.",
            status=500,
        )
    if replacement_target_document_id == document.id:
        raise LifecycleError(
            "operation_binding_invalid",
            "A replacement operation cannot target its incoming document.",
            status=500,
        )
    allowed_phases = {
        OperationType.PREFLIGHT: _PREFLIGHT_OPERATION_PHASES,
        OperationType.PUBLISH: (
            _PUBLICATION_OPERATION_PHASES | _DELETION_OPERATION_PHASES
            if replacement_delete
            else _PUBLICATION_OPERATION_PHASES
        ),
        OperationType.DELETE: _DELETION_OPERATION_PHASES,
    }[operation_type]
    if phase not in allowed_phases:
        raise LifecycleError(
            "operation_phase_invalid",
            "The resume phase does not belong to the operation work class.",
            status=500,
        )

    now = utc_now()
    operation = WorkOperation(
        document_id=document.id,
        prepared_revision_id=prepared_revision_id,
        replacement_target_document_id=replacement_target_document_id,
        idempotency_record_id=idempotency_record_id,
        operation_type=operation_type,
        priority=int(priority),
        state=OperationState.QUEUED,
        phase=phase,
        phase_started_at=now,
        attempt=next_attempt(session, document.id, operation_type),
        created_at=now,
        updated_at=now,
    )
    session.add(operation)
    session.flush()
    audit(
        session,
        event_type=f"{operation_type.value.lower()}_queued",
        actor_type="system",
        actor_id="pdf-bridge",
        document_id=document.id,
        operation_id=operation.id,
        details={
            "priority": int(priority),
            "attempt": operation.attempt,
            "phase": phase.value,
            "replacement_target_document_id": (
                str(replacement_target_document_id)
                if replacement_target_document_id is not None
                else None
            ),
        },
    )
    return operation


def latest_operation(
    session: Session,
    document_id: uuid.UUID,
    operation_type: OperationType | None = None,
) -> WorkOperation | None:
    statement = select(WorkOperation).where(WorkOperation.document_id == document_id)
    if operation_type is not None:
        statement = statement.where(WorkOperation.operation_type == operation_type)
    if operation_type is not None:
        statement = statement.order_by(
            WorkOperation.attempt.desc(),
            WorkOperation.created_at.desc(),
            WorkOperation.id.desc(),
        )
    else:
        statement = statement.order_by(
            WorkOperation.created_at.desc(), WorkOperation.id.desc()
        )
    return session.scalar(statement)


def latest_sealed_revision(
    session: Session, document_id: uuid.UUID
) -> PreparedRevision | None:
    return session.scalar(
        select(PreparedRevision)
        .where(
            PreparedRevision.document_id == document_id,
            PreparedRevision.status == RevisionStatus.SEALED,
        )
        .order_by(PreparedRevision.revision_number.desc())
    )


def latest_revision(session: Session, document_id: uuid.UUID) -> PreparedRevision | None:
    """Return the newest preparation attempt, including an incomplete one."""

    return session.scalar(
        select(PreparedRevision)
        .where(PreparedRevision.document_id == document_id)
        .order_by(PreparedRevision.revision_number.desc())
    )


def get_document_for_update(session: Session, document_id: uuid.UUID) -> Document:
    document = session.scalar(
        select(Document).where(Document.id == document_id).with_for_update()
    )
    if document is None:
        raise LifecycleError("document_not_found", "The document was not found.", status=404)
    return document


def _publication_for_revision(
    session: Session, prepared_revision_id: uuid.UUID
) -> PublicationRecord | None:
    return session.scalar(
        select(PublicationRecord).where(
            PublicationRecord.prepared_revision_id == prepared_revision_id
        )
    )


def _ensure_publication_record(
    session: Session,
    *,
    document: Document,
    revision: PreparedRevision,
) -> PublicationRecord:
    """Create or validate the one exact-target publication checkpoint."""

    expected_points = revision.expected_point_count
    if (
        revision.document_id != document.id
        or revision.status is not RevisionStatus.SEALED
        or expected_points is None
    ):
        raise LifecycleError(
            "publication_binding_invalid",
            "Publication requires a sealed revision belonging to the document.",
            status=500,
        )
    publication = _publication_for_revision(session, revision.id)
    if publication is None:
        publication = PublicationRecord(
            document_id=document.id,
            prepared_revision_id=revision.id,
            active_qdrant_collection=revision.active_qdrant_collection,
            status=PublicationStatus.PENDING,
            expected_points=expected_points,
        )
        session.add(publication)
        return publication
    if (
        publication.document_id != document.id
        or publication.active_qdrant_collection != revision.active_qdrant_collection
        or publication.expected_points != expected_points
    ):
        raise LifecycleError(
            "publication_binding_invalid",
            "The publication checkpoint does not match its sealed revision.",
            status=500,
        )
    if publication.status is not PublicationStatus.PENDING:
        raise LifecycleError(
            "publication_already_started",
            "The prepared revision already has a non-pending publication checkpoint.",
        )
    return publication


def _open_publication_operation(
    session: Session, *, document_id: uuid.UUID, prepared_revision_id: uuid.UUID
) -> WorkOperation | None:
    return session.scalar(
        select(WorkOperation)
        .where(
            WorkOperation.document_id == document_id,
            WorkOperation.prepared_revision_id == prepared_revision_id,
            WorkOperation.operation_type == OperationType.PUBLISH,
            WorkOperation.replacement_target_document_id.is_(None),
            WorkOperation.state.in_((OperationState.QUEUED, OperationState.RUNNING)),
        )
        .order_by(WorkOperation.attempt.desc())
    )


def queue_clear_publication(
    session: Session,
    *,
    document: Document,
    revision: PreparedRevision,
) -> WorkOperation:
    """Queue automatic publication for a complete candidate-free revision."""

    if document.state is DocumentState.PUBLISHING:
        existing = _open_publication_operation(
            session,
            document_id=document.id,
            prepared_revision_id=revision.id,
        )
        if existing is not None:
            return existing
        raise LifecycleError(
            "publication_operation_missing",
            "The publishing document has no active publication operation.",
            status=500,
        )
    candidate_count = session.scalar(
        select(func.count())
        .select_from(PreparedCandidate)
        .where(PreparedCandidate.prepared_revision_id == revision.id)
    )
    if (
        document.state is not DocumentState.PREFLIGHTING
        or revision.document_id != document.id
        or revision.status is not RevisionStatus.SEALED
        or not revision.clear_for_publication
        or not revision.formatter_complete
        or not revision.vector_complete
        or not revision.candidate_discovery_complete
        or not revision.advisory_complete
        or bool(revision.incomplete_reasons)
        or int(candidate_count or 0) != 0
    ):
        raise LifecycleError(
            "revision_not_publishable", "The prepared revision is not clear for publication."
        )
    _ensure_publication_record(session, document=document, revision=revision)
    document.state = DocumentState.PUBLISHING
    document.failure_code = None
    document.failure_message = None
    document.failure_retryable = False
    return enqueue_operation(
        session,
        document=document,
        operation_type=OperationType.PUBLISH,
        priority=OperationPriority.PUBLISH,
        prepared_revision_id=revision.id,
    )


def _require_current_review_revision(
    session: Session,
    document: Document,
    prepared_revision_id: uuid.UUID,
) -> PreparedRevision:
    if document.state != DocumentState.REVIEW_REQUIRED:
        raise LifecycleError(
            "document_state_conflict",
            "A review decision is not allowed in the document's current state.",
        )
    revision = session.scalar(
        select(PreparedRevision).where(
            PreparedRevision.id == prepared_revision_id,
            PreparedRevision.document_id == document.id,
        )
    )
    current = latest_sealed_revision(session, document.id)
    if (
        revision is None
        or revision.status != RevisionStatus.SEALED
        or current is None
        or current.id != revision.id
        or revision.manifest_sha256 is None
    ):
        raise LifecycleError(
            "stale_prepared_revision",
            "The decision does not name the current sealed prepared revision.",
        )
    if revision.decision is not None:
        raise LifecycleError("decision_exists", "This prepared revision already has a decision.")
    return revision


def _validate_replacement_target(
    session: Session,
    *,
    incoming: Document,
    target_id: uuid.UUID,
) -> tuple[Document, PreparedRevision, PublicationRecord]:
    if target_id == incoming.id:
        raise LifecycleError("invalid_replacement_target", "A document cannot replace itself.")
    target = session.scalar(select(Document).where(Document.id == target_id).with_for_update())
    if target is None or target.collection_key != incoming.collection_key:
        raise LifecycleError(
            "invalid_replacement_target",
            "The replacement target must be in the same configured collection.",
        )
    if target.state != DocumentState.READY:
        raise LifecycleError(
            "replacement_target_unavailable", "The replacement target is no longer READY."
        )
    if target.replaced_by_document_id is not None:
        raise LifecycleError(
            "replacement_target_busy",
            "The replacement target is already bound to another incoming document.",
        )
    conflicting = session.scalar(
        select(WorkOperation.id).where(
            or_(
                WorkOperation.document_id == target.id,
                WorkOperation.replacement_target_document_id == target.id,
            ),
            WorkOperation.state.in_((OperationState.QUEUED, OperationState.RUNNING)),
        )
    )
    if conflicting is not None:
        raise LifecycleError(
            "replacement_target_busy", "The replacement target is already being changed."
        )
    revision, publication = _verified_ready_binding(session, target)
    return target, revision, publication


def _verified_ready_binding(
    session: Session, document: Document
) -> tuple[PreparedRevision, PublicationRecord]:
    """Resolve the successful publication target for a READY document."""

    publication = session.scalar(
        select(PublicationRecord)
        .where(
            PublicationRecord.document_id == document.id,
            PublicationRecord.status == PublicationStatus.VERIFIED,
        )
        .order_by(PublicationRecord.verified_at.desc(), PublicationRecord.created_at.desc())
    )
    if publication is None:
        raise LifecycleError(
            "publication_record_missing",
            "A READY document has no verified publication checkpoint.",
            status=500,
        )
    revision = session.get(PreparedRevision, publication.prepared_revision_id)
    if (
        revision is None
        or revision.document_id != document.id
        or revision.status is not RevisionStatus.SEALED
        or revision.active_qdrant_collection != publication.active_qdrant_collection
        or revision.expected_point_count != publication.expected_points
    ):
        raise LifecycleError(
            "publication_binding_invalid",
            "The verified publication checkpoint is not bound to its sealed revision.",
            status=500,
        )
    return revision, publication


def _validate_deletion_binding(
    session: Session,
    *,
    document: Document,
    prepared_revision_id: uuid.UUID | None,
    publication_record_id: uuid.UUID | None,
    active_qdrant_collection: str,
) -> None:
    """Fail hard when a deletion target is not pinned to the named catalog rows."""

    revision = (
        session.get(PreparedRevision, prepared_revision_id)
        if prepared_revision_id is not None
        else None
    )
    if prepared_revision_id is not None and (
        revision is None
        or revision.document_id != document.id
        or revision.active_qdrant_collection != active_qdrant_collection
    ):
        raise LifecycleError(
            "deletion_binding_invalid",
            "The deletion revision does not match the document and active target.",
            status=500,
        )
    publication = (
        session.get(PublicationRecord, publication_record_id)
        if publication_record_id is not None
        else None
    )
    if publication_record_id is not None and (
        publication is None
        or publication.document_id != document.id
        or publication.prepared_revision_id != prepared_revision_id
        or publication.active_qdrant_collection != active_qdrant_collection
    ):
        raise LifecycleError(
            "deletion_binding_invalid",
            "The deletion publication checkpoint does not match the exact target.",
            status=500,
        )


def _latest_deletion_operation(
    session: Session, document_id: uuid.UUID
) -> WorkOperation | None:
    """Find either a direct delete or the publish operation owning replacement cleanup."""

    return session.scalar(
        select(WorkOperation)
        .where(
            or_(
                (
                    (WorkOperation.document_id == document_id)
                    & (WorkOperation.operation_type == OperationType.DELETE)
                ),
                (
                    (WorkOperation.replacement_target_document_id == document_id)
                    & (WorkOperation.operation_type == OperationType.PUBLISH)
                ),
            )
        )
        .order_by(WorkOperation.created_at.desc(), WorkOperation.id.desc())
    )


def begin_deletion(
    session: Session,
    *,
    document: Document,
    terminal_disposition: TerminalDisposition,
    active_qdrant_collection: str,
    screening_qdrant_collection: str,
    prepared_revision_id: uuid.UUID | None,
    publication_record_id: uuid.UUID | None,
    priority: OperationPriority,
    actor_type: str,
    actor_id: str,
    idempotency_record_id: uuid.UUID | None = None,
) -> WorkOperation:
    """Block reads and durably queue point-first terminal cleanup."""

    if priority is not OperationPriority.HIGH:
        raise LifecycleError(
            "operation_priority_invalid",
            "Direct deletion must use HIGH priority.",
            status=500,
        )
    if active_qdrant_collection == screening_qdrant_collection:
        raise LifecycleError(
            "collection_configuration_invalid",
            "Active and screening collection targets must be distinct.",
            status=500,
        )
    _validate_deletion_binding(
        session,
        document=document,
        prepared_revision_id=prepared_revision_id,
        publication_record_id=publication_record_id,
        active_qdrant_collection=active_qdrant_collection,
    )
    progress = document.deletion_progress
    if progress is not None:
        if (
            document.state not in {DocumentState.DELETING, DocumentState.DELETE_FAILED}
            or document.terminal_disposition is not progress.terminal_disposition
        ):
            raise LifecycleError(
                "deletion_binding_invalid",
                "The deletion checkpoint does not match the document lifecycle.",
                status=500,
            )
        if (
            progress.terminal_disposition != terminal_disposition
            or progress.active_qdrant_collection != active_qdrant_collection
            or progress.screening_qdrant_collection != screening_qdrant_collection
            or progress.prepared_revision_id != prepared_revision_id
            or progress.publication_record_id != publication_record_id
        ):
            raise LifecycleError(
                "deletion_binding_conflict",
                "The document already has a different terminal cleanup binding.",
            )
        operation = _latest_deletion_operation(session, document.id)
        if operation is None:
            raise LifecycleError(
                "deletion_operation_missing",
                "The deletion checkpoint has no durable owning operation.",
                status=500,
            )
        return operation
    if document.state in {
        DocumentState.DELETING,
        DocumentState.DELETE_FAILED,
        DocumentState.REJECTED,
        DocumentState.CANCELLED,
        DocumentState.DELETED,
    }:
        raise LifecycleError(
            "deletion_binding_invalid",
            "The document deletion lifecycle is missing its durable checkpoint.",
            status=500,
        )
    progress = DeletionProgress(
        document_id=document.id,
        prepared_revision_id=prepared_revision_id,
        publication_record_id=publication_record_id,
        terminal_disposition=terminal_disposition,
        active_qdrant_collection=active_qdrant_collection,
        screening_qdrant_collection=screening_qdrant_collection,
        phase=DeletionPhase.DELETE_ACTIVE_POINTS,
    )
    document.deletion_progress = progress
    session.add(progress)
    document.state = DocumentState.DELETING
    document.terminal_disposition = terminal_disposition
    document.failure_code = None
    document.failure_message = None
    document.failure_retryable = False
    operation = enqueue_operation(
        session,
        document=document,
        operation_type=OperationType.DELETE,
        priority=priority,
        prepared_revision_id=prepared_revision_id,
        idempotency_record_id=idempotency_record_id,
    )
    audit(
        session,
        event_type="deletion_accepted",
        actor_type=actor_type,
        actor_id=actor_id,
        document_id=document.id,
        operation_id=operation.id,
        details={"terminal_disposition": terminal_disposition.value},
    )
    return operation


def _begin_replacement(
    session: Session,
    *,
    incoming: Document,
    incoming_revision: PreparedRevision,
    target: Document,
    target_revision: PreparedRevision,
    target_publication: PublicationRecord,
    screening_qdrant_collection: str,
    actor_type: str,
    actor_id: str,
) -> WorkOperation:
    """Queue one workflow that proves old deletion before publishing the incoming revision."""

    if target.deletion_progress is not None:
        raise LifecycleError(
            "replacement_target_busy",
            "The replacement target already has a deletion checkpoint.",
        )
    if target_publication.active_qdrant_collection == screening_qdrant_collection:
        raise LifecycleError(
            "collection_configuration_invalid",
            "Active and screening collection targets must be distinct.",
            status=500,
        )
    _ensure_publication_record(
        session, document=incoming, revision=incoming_revision
    )
    progress = DeletionProgress(
        document_id=target.id,
        prepared_revision_id=target_revision.id,
        publication_record_id=target_publication.id,
        terminal_disposition=TerminalDisposition.DELETED,
        active_qdrant_collection=target_publication.active_qdrant_collection,
        screening_qdrant_collection=screening_qdrant_collection,
        phase=DeletionPhase.DELETE_ACTIVE_POINTS,
    )
    target.deletion_progress = progress
    target.state = DocumentState.DELETING
    target.terminal_disposition = TerminalDisposition.DELETED
    target.failure_code = None
    target.failure_message = None
    target.failure_retryable = False
    target.replaced_by_document_id = incoming.id
    incoming.state = DocumentState.PUBLISHING
    incoming.failure_code = None
    incoming.failure_message = None
    incoming.failure_retryable = False
    session.add(progress)
    operation = enqueue_operation(
        session,
        document=incoming,
        operation_type=OperationType.PUBLISH,
        priority=OperationPriority.REPLACEMENT,
        prepared_revision_id=incoming_revision.id,
        replacement_target_document_id=target.id,
    )
    audit(
        session,
        event_type="replacement_old_deletion_accepted",
        actor_type=actor_type,
        actor_id=actor_id,
        document_id=target.id,
        operation_id=operation.id,
        details={
            "incoming_document_id": str(incoming.id),
            "active_qdrant_collection": target_publication.active_qdrant_collection,
            "screening_qdrant_collection": screening_qdrant_collection,
        },
    )
    return operation


def record_decision(
    session: Session,
    *,
    document_id: uuid.UUID,
    prepared_revision_id: uuid.UUID,
    action: DecisionAction,
    target_document_id: uuid.UUID | None,
    idempotency: IdempotencyRecord,
    actor_type: str,
    actor_id: str,
    screening_qdrant_collection: str,
) -> MutationOutcome:
    """Bind a Keep/Replace/Cancel action to the exact inspected manifest."""

    if not isinstance(action, DecisionAction):
        raise LifecycleError(
            "decision_action_invalid", "The decision action is not supported.", status=400
        )
    if idempotency.actor_id != actor_id or idempotency.completed_at is not None:
        raise LifecycleError(
            "idempotency_binding_invalid",
            "The decision idempotency reservation does not belong to this actor or request.",
            status=500,
        )
    document = get_document_for_update(session, document_id)
    revision = _require_current_review_revision(session, document, prepared_revision_id)
    if action == DecisionAction.REPLACE:
        if target_document_id is None:
            raise LifecycleError(
                "replacement_target_required", "Replace requires one target document."
            )
        target, target_revision, target_publication = _validate_replacement_target(
            session, incoming=document, target_id=target_document_id
        )
    else:
        if target_document_id is not None:
            raise LifecycleError(
                "unexpected_replacement_target", "Keep and Cancel do not accept a target."
            )
        target = None
        target_revision = None
        target_publication = None

    decision = Decision(
        document_id=document.id,
        prepared_revision_id=revision.id,
        prepared_manifest_sha256=revision.manifest_sha256 or "",
        action=action,
        target_document_id=target.id if target is not None else None,
        idempotency_record_id=idempotency.id,
        actor_type=actor_type,
        actor_id=actor_id,
    )
    session.add(decision)
    session.flush()
    operation: WorkOperation
    if action == DecisionAction.CANCEL:
        operation = begin_deletion(
            session,
            document=document,
            terminal_disposition=TerminalDisposition.CANCELLED,
            active_qdrant_collection=revision.active_qdrant_collection,
            screening_qdrant_collection=screening_qdrant_collection,
            prepared_revision_id=revision.id,
            publication_record_id=None,
            priority=OperationPriority.HIGH,
            actor_type=actor_type,
            actor_id=actor_id,
        )
    elif action == DecisionAction.REPLACE:
        if target is None or target_revision is None or target_publication is None:
            raise LifecycleError(
                "replacement_binding_invalid",
                "The replacement target binding was not retained.",
                status=500,
            )
        operation = _begin_replacement(
            session,
            incoming=document,
            incoming_revision=revision,
            target=target,
            target_revision=target_revision,
            target_publication=target_publication,
            screening_qdrant_collection=screening_qdrant_collection,
            actor_type=actor_type,
            actor_id=actor_id,
        )
    else:
        _ensure_publication_record(session, document=document, revision=revision)
        document.state = DocumentState.PUBLISHING
        operation = enqueue_operation(
            session,
            document=document,
            operation_type=OperationType.PUBLISH,
            priority=OperationPriority.PUBLISH,
            prepared_revision_id=revision.id,
        )
    audit(
        session,
        event_type="review_decision_recorded",
        actor_type=actor_type,
        actor_id=actor_id,
        document_id=document.id,
        operation_id=operation.id,
        details={
            "action": action.value,
            "prepared_revision_id": str(revision.id),
            "target_document_id": str(target.id) if target is not None else None,
        },
    )
    return MutationOutcome(document=document, operation=operation, idempotency=idempotency)


def request_retry(
    session: Session,
    *,
    document_id: uuid.UUID,
    idempotency: IdempotencyRecord,
    actor_type: str,
    actor_id: str,
) -> MutationOutcome:
    """Queue only the failed phase, preserving approval and sealed artifacts."""

    if idempotency.actor_id != actor_id or idempotency.completed_at is not None:
        raise LifecycleError(
            "idempotency_binding_invalid",
            "The retry idempotency reservation does not belong to this actor or request.",
            status=500,
        )
    requested_document = get_document_for_update(session, document_id)
    if not requested_document.failure_retryable:
        raise LifecycleError(
            "retry_not_allowed", "The document does not have a retryable failure."
        )
    document = requested_document
    if (
        requested_document.state is DocumentState.DELETE_FAILED
        and requested_document.replaced_by_document_id is not None
    ):
        document = get_document_for_update(
            session, requested_document.replaced_by_document_id
        )
        replacement_operation = latest_operation(
            session, document.id, OperationType.PUBLISH
        )
        if (
            document.state is not DocumentState.PUBLISH_FAILED
            or not document.failure_retryable
            or replacement_operation is None
            or replacement_operation.replacement_target_document_id
            != requested_document.id
        ):
            raise LifecycleError(
                "replacement_retry_binding_invalid",
                "The failed replacement does not have one retryable owning publication.",
                status=500,
            )
    if document.state == DocumentState.PREFLIGHT_FAILED:
        operation_type = OperationType.PREFLIGHT
        next_state = DocumentState.PREFLIGHTING
    elif document.state == DocumentState.PUBLISH_FAILED:
        operation_type = OperationType.PUBLISH
        next_state = DocumentState.PUBLISHING
    elif document.state == DocumentState.DELETE_FAILED:
        operation_type = OperationType.DELETE
        next_state = DocumentState.DELETING
    else:
        raise LifecycleError(
            "retry_not_allowed", "Retry is only available for an explicit failed state."
        )
    failed = latest_operation(session, document.id, operation_type)
    if (
        failed is None
        or failed.state is not OperationState.FAILED
        or not failed.retryable
        or failed.phase
        in {OperationPhase.QUEUED, OperationPhase.COMPLETE, OperationPhase.AWAITING_DECISION}
    ):
        raise LifecycleError(
            "retry_checkpoint_missing",
            "The document has no retryable failed operation checkpoint.",
            status=500,
        )
    try:
        priority = OperationPriority(failed.priority)
    except ValueError as exc:
        raise LifecycleError(
            "operation_priority_invalid",
            "The failed operation has an unknown durable priority.",
            status=500,
        ) from exc
    phase = failed.phase
    revision_id = failed.prepared_revision_id
    replacement_target_document_id = failed.replacement_target_document_id

    if (
        requested_document.id != document.id
        and replacement_target_document_id != requested_document.id
    ):
        raise LifecycleError(
            "replacement_retry_binding_invalid",
            "The failed publication no longer owns the requested replacement cleanup.",
            status=500,
        )

    if operation_type is OperationType.PREFLIGHT:
        if replacement_target_document_id is not None:
            raise LifecycleError(
                "operation_binding_invalid",
                "Preflight retry cannot own replacement deletion.",
                status=500,
            )
    elif operation_type is OperationType.PUBLISH:
        if revision_id is None:
            raise LifecycleError(
                "publication_record_missing",
                "The failed publication has no prepared revision checkpoint.",
                status=500,
            )
        publication = _publication_for_revision(session, revision_id)
        revision = session.get(PreparedRevision, revision_id)
        if (
            publication is None
            or revision is None
            or publication.document_id != document.id
            or revision.document_id != document.id
            or revision.status is not RevisionStatus.SEALED
            or publication.status is PublicationStatus.VERIFIED
            or publication.active_qdrant_collection
            != revision.active_qdrant_collection
            or publication.expected_points != revision.expected_point_count
        ):
            raise LifecycleError(
                "publication_record_missing",
                "The failed publication has no matching durable revision record.",
                status=500,
            )
        publication.status = PublicationStatus.PENDING
        publication.failure_code = None
        if replacement_target_document_id is not None:
            replacement_target = session.get(Document, replacement_target_document_id)
            if replacement_target is None or replacement_target.deletion_progress is None:
                raise LifecycleError(
                    "deletion_checkpoint_missing",
                    "Replacement retry has no old-document deletion checkpoint.",
                    status=500,
                )
            if replacement_target.state is DocumentState.DELETE_FAILED:
                replacement_target.state = DocumentState.DELETING
                replacement_target.failure_code = None
                replacement_target.failure_message = None
                replacement_target.failure_retryable = False
                replacement_target.deletion_progress.failure_code = None
    else:
        progress = document.deletion_progress
        if (
            progress is None
            or progress.prepared_revision_id != revision_id
            or replacement_target_document_id is not None
        ):
            raise LifecycleError(
                "deletion_checkpoint_missing",
                "The failed deletion has no matching durable checkpoint.",
                status=500,
            )
        phase = OperationPhase(progress.phase.value)
        progress.failure_code = None

    document.state = next_state
    document.failure_code = None
    document.failure_message = None
    document.failure_retryable = False
    operation = enqueue_operation(
        session,
        document=document,
        operation_type=operation_type,
        priority=priority,
        prepared_revision_id=revision_id,
        replacement_target_document_id=replacement_target_document_id,
        idempotency_record_id=idempotency.id,
        phase=phase,
    )
    audit(
        session,
        event_type="retry_accepted",
        actor_type=actor_type,
        actor_id=actor_id,
        document_id=document.id,
        operation_id=operation.id,
        details={
            "operation_type": operation_type.value,
            "failed_operation_id": str(failed.id),
            "requested_document_id": str(requested_document.id),
            "resume_phase": phase.value,
            "attempt": operation.attempt,
        },
    )
    if requested_document.id != document.id:
        audit(
            session,
            event_type="replacement_retry_accepted",
            actor_type=actor_type,
            actor_id=actor_id,
            document_id=requested_document.id,
            operation_id=operation.id,
            details={
                "incoming_document_id": str(document.id),
                "resume_phase": phase.value,
                "attempt": operation.attempt,
            },
        )
    return MutationOutcome(
        document=requested_document,
        operation=operation,
        idempotency=idempotency,
    )


def _deletion_target_for_state(
    session: Session,
    *,
    document: Document,
    configured_active_qdrant_collection: str,
) -> tuple[str, PreparedRevision | None, PublicationRecord | None]:
    """Resolve the persisted physical target without re-resolving stable work."""

    if document.state is DocumentState.READY:
        revision, publication = _verified_ready_binding(session, document)
        return publication.active_qdrant_collection, revision, publication

    if document.state is DocumentState.REVIEW_REQUIRED:
        revision = latest_sealed_revision(session, document.id)
        if revision is None:
            raise LifecycleError(
                "prepared_revision_missing",
                "The review document has no sealed prepared revision.",
                status=500,
            )
        publication = _publication_for_revision(session, revision.id)
        if publication is not None and (
            publication.document_id != document.id
            or publication.active_qdrant_collection != revision.active_qdrant_collection
            or publication.expected_points != revision.expected_point_count
        ):
            raise LifecycleError(
                "publication_binding_invalid",
                "The review publication checkpoint does not match its revision.",
                status=500,
            )
        return revision.active_qdrant_collection, revision, publication

    if document.state is DocumentState.PUBLISH_FAILED:
        publication = session.scalar(
            select(PublicationRecord)
            .where(PublicationRecord.document_id == document.id)
            .order_by(PublicationRecord.created_at.desc())
        )
        revision = (
            session.get(PreparedRevision, publication.prepared_revision_id)
            if publication is not None
            else None
        )
        if (
            publication is None
            or revision is None
            or revision.document_id != document.id
            or revision.status is not RevisionStatus.SEALED
            or publication.status is PublicationStatus.VERIFIED
            or publication.active_qdrant_collection
            != revision.active_qdrant_collection
            or publication.expected_points != revision.expected_point_count
        ):
            raise LifecycleError(
                "publication_record_missing",
                "The failed publication has no matching exact-target checkpoint.",
                status=500,
            )
        return publication.active_qdrant_collection, revision, publication

    revision = latest_revision(session, document.id)
    if revision is not None:
        return revision.active_qdrant_collection, revision, None
    if not configured_active_qdrant_collection:
        raise LifecycleError(
            "collection_configuration_invalid",
            "The configured active collection target is empty.",
            status=500,
        )
    return configured_active_qdrant_collection, None, None


def request_deletion(
    session: Session,
    *,
    document_id: uuid.UUID,
    idempotency: IdempotencyRecord,
    actor_type: str,
    actor_id: str,
    active_qdrant_collection: str,
    screening_qdrant_collection: str,
) -> MutationOutcome:
    """Accept idempotent deletion from READY or stable unpublished states."""

    if idempotency.actor_id != actor_id or idempotency.completed_at is not None:
        raise LifecycleError(
            "idempotency_binding_invalid",
            "The deletion idempotency reservation does not belong to this actor or request.",
            status=500,
        )
    document = get_document_for_update(session, document_id)
    if document.state in {DocumentState.DELETING, DocumentState.DELETE_FAILED}:
        if document.terminal_disposition is not TerminalDisposition.DELETED:
            raise LifecycleError(
                "deletion_disposition_conflict",
                "The document is already being purged for a different disposition.",
            )
        operation = _latest_deletion_operation(session, document.id)
        if operation is None:
            raise LifecycleError(
                "deletion_operation_missing",
                "The deleting document has no durable owning operation.",
                status=500,
            )
        return MutationOutcome(document=document, operation=operation, idempotency=idempotency)
    if document.state == DocumentState.DELETED:
        operation = _latest_deletion_operation(session, document.id)
        if operation is None:
            raise LifecycleError(
                "deletion_operation_missing",
                "The deleted document has no retained deletion operation.",
                status=500,
            )
        return MutationOutcome(document=document, operation=operation, idempotency=idempotency)
    eligible = {
        DocumentState.READY,
        DocumentState.PREFLIGHT_FAILED,
        DocumentState.REVIEW_REQUIRED,
        DocumentState.PUBLISH_FAILED,
    }
    if document.state not in eligible:
        raise LifecycleError(
            "document_state_conflict",
            "The document cannot be deleted from its current state.",
        )
    target, revision, publication = _deletion_target_for_state(
        session,
        document=document,
        configured_active_qdrant_collection=active_qdrant_collection,
    )
    operation = begin_deletion(
        session,
        document=document,
        terminal_disposition=TerminalDisposition.DELETED,
        active_qdrant_collection=target,
        screening_qdrant_collection=screening_qdrant_collection,
        prepared_revision_id=revision.id if revision is not None else None,
        publication_record_id=publication.id if publication is not None else None,
        priority=OperationPriority.HIGH,
        actor_type=actor_type,
        actor_id=actor_id,
        idempotency_record_id=idempotency.id,
    )
    return MutationOutcome(document=document, operation=operation, idempotency=idempotency)


def queue_rejection_cleanup(
    session: Session,
    *,
    document: Document,
    reason_code: str,
    active_qdrant_collection: str,
    screening_qdrant_collection: str,
    prepared_revision_id: uuid.UUID | None,
) -> WorkOperation:
    """Keep REJECTED truthful by purging asynchronously through DELETING first."""

    if not reason_code or len(reason_code) > 100:
        raise LifecycleError(
            "rejection_reason_invalid",
            "Rejection cleanup requires a bounded reason code.",
            status=500,
        )
    if document.state in {DocumentState.DELETING, DocumentState.DELETE_FAILED}:
        if document.terminal_disposition is not TerminalDisposition.REJECTED:
            raise LifecycleError(
                "deletion_disposition_conflict",
                "The document is already being purged for a different disposition.",
            )
        if document.failure_code not in {None, reason_code}:
            raise LifecycleError(
                "rejection_reason_conflict",
                "The rejection cleanup already has a different terminal reason.",
            )
    elif document.state not in {
        DocumentState.PREFLIGHTING,
        DocumentState.PREFLIGHT_FAILED,
    }:
        raise LifecycleError(
            "document_state_conflict",
            "Only preflight work can transition to asynchronous rejection cleanup.",
        )
    operation = begin_deletion(
        session,
        document=document,
        terminal_disposition=TerminalDisposition.REJECTED,
        active_qdrant_collection=active_qdrant_collection,
        screening_qdrant_collection=screening_qdrant_collection,
        prepared_revision_id=prepared_revision_id,
        publication_record_id=None,
        priority=OperationPriority.HIGH,
        actor_type="system",
        actor_id="pdf-bridge",
    )
    # The cleanup transition clears transient worker failure fields. Restore the
    # bounded terminal reason so the worker can carry it into the tombstone.
    document.failure_code = reason_code
    return operation


def commit_tombstone(
    session: Session,
    *,
    document: Document,
    reason_code: str | None,
    actor_type: str,
    actor_id: str,
) -> Tombstone:
    """Commit the final content-free state after verified point/storage purge."""

    if reason_code is not None and (not reason_code or len(reason_code) > 100):
        raise LifecycleError(
            "tombstone_reason_invalid",
            "The tombstone reason code is invalid.",
            status=500,
        )
    progress = document.deletion_progress
    existing = document.tombstone
    if existing is not None:
        if (
            progress is None
            or existing.disposition is not progress.terminal_disposition
            or document.state.value != existing.disposition.value
            or progress.tombstoned_at is None
        ):
            raise LifecycleError(
                "tombstone_binding_invalid",
                "The retained tombstone does not match the document lifecycle.",
                status=500,
            )
        return existing
    if document.state not in {DocumentState.DELETING, DocumentState.DELETE_FAILED}:
        raise LifecycleError(
            "document_state_conflict",
            "A tombstone can only finish an active deletion workflow.",
            status=500,
        )
    if progress is None or progress.storage_purged_at is None:
        raise LifecycleError(
            "deletion_checkpoint_incomplete",
            "Storage purge must be verified before a terminal tombstone is committed.",
            status=500,
        )
    if document.terminal_disposition is not progress.terminal_disposition:
        raise LifecycleError(
            "deletion_disposition_conflict",
            "The deletion checkpoint disposition does not match the document.",
            status=500,
        )
    if progress.active_zero_verified_at is None or progress.screening_zero_verified_at is None:
        raise LifecycleError(
            "index_cleanup_incomplete",
            "Both active and screening point counts must be zero before tombstoning.",
            status=500,
        )
    if document.storage_key is not None:
        raise LifecycleError(
            "source_not_purged",
            "The source object still exists in the catalog.",
            status=500,
        )
    revision = (
        session.get(PreparedRevision, progress.prepared_revision_id)
        if progress.prepared_revision_id is not None
        else None
    )
    if progress.prepared_revision_id is not None and (
        revision is None or revision.document_id != document.id
    ):
        raise LifecycleError(
            "deletion_binding_invalid",
            "The deletion checkpoint revision no longer matches the document.",
            status=500,
        )
    revision_ids = tuple(
        session.scalars(
            select(PreparedRevision.id).where(PreparedRevision.document_id == document.id)
        ).all()
    )
    remaining_content = {
        model.__tablename__: count
        for model in (
            RevisionArtifact,
            ExtractedPage,
            FormatterBatch,
            PreparedPage,
            PreparedChunk,
            PreparedChunkVector,
            PreparedCandidate,
            CandidateEvidence,
        )
        if (
            count := session.scalar(
                select(func.count())
                .select_from(model)
                .where(model.prepared_revision_id.in_(revision_ids))
            )
            or 0
        )
    }
    if remaining_content:
        raise LifecycleError(
            "content_purge_incomplete",
            "Revision content remains after the storage purge checkpoint.",
            status=500,
            extra={"remaining_content": remaining_content},
        )
    tombstone = Tombstone(
        document_id=document.id,
        collection_key=document.collection_key,
        disposition=progress.terminal_disposition,
        source_sha256=document.sha256,
        manifest_sha256=revision.manifest_sha256 if revision is not None else None,
        reason_code=reason_code,
        actor_type=actor_type,
        actor_id=actor_id,
    )
    document.tombstone = tombstone
    session.add(tombstone)
    terminal_state = DocumentState(progress.terminal_disposition.value)
    document.state = terminal_state
    document.terminal_disposition = progress.terminal_disposition
    document.failure_retryable = False
    document.failure_message = None
    document.failure_code = (
        reason_code if terminal_state is DocumentState.REJECTED else None
    )
    now = utc_now()
    if terminal_state == DocumentState.REJECTED:
        document.rejected_at = now
    elif terminal_state == DocumentState.CANCELLED:
        document.cancelled_at = now
    else:
        document.deleted_at = now
    progress.phase = DeletionPhase.COMMIT_TOMBSTONE
    progress.failure_code = None
    progress.tombstoned_at = now
    audit(
        session,
        event_type="tombstone_committed",
        actor_type=actor_type,
        actor_id=actor_id,
        document_id=document.id,
        details={"disposition": progress.terminal_disposition.value},
    )
    return tombstone


def can_serve_source(document: Document) -> bool:
    """Fail-closed source gate for clean, retained catalog states."""

    return (
        document.scan_state is ScanState.CLEAN
        and document.storage_key is not None
        and document.state
        in {
            DocumentState.PREFLIGHTING,
            DocumentState.PREFLIGHT_FAILED,
            DocumentState.REVIEW_REQUIRED,
            DocumentState.PUBLISHING,
            DocumentState.PUBLISH_FAILED,
            DocumentState.READY,
        }
    )


def can_serve_prepared_content(document: Document) -> bool:
    """Gate Markdown/chunks to states that require a sealed revision."""

    return can_serve_source(document) and document.state in {
        DocumentState.REVIEW_REQUIRED,
        DocumentState.PUBLISHING,
        DocumentState.PUBLISH_FAILED,
        DocumentState.READY,
    }


def utc_timestamp(value: datetime | None) -> str | None:
    """Stable RFC 3339 helper used in idempotent response snapshots."""

    if value is None:
        return None
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat().replace("+00:00", "Z")
