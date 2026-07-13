"""Transactional intake, decision, and lifecycle transitions.

Every function here mutates the session and flushes without committing;
managers own the transaction boundary. Hard failures (exact same-collection
duplicates, unusable PDFs) block; everything semantic is advisory and flows
through review decisions.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from pdf_bridge.persistence.models import (
    OPEN_UPLOAD_STATES,
    RETAINED_DOCUMENT_STATES,
    AuditEvent,
    CollectionEpoch,
    DecisionAction,
    Document,
    DocumentAnalysis,
    DocumentState,
    IntakeDecision,
    OperationPhase,
    OperationState,
    OperationType,
    ReplacementState,
    ReplacementWorkflow,
    ScanState,
    WorkOperation,
    utc_now,
)
from pdf_bridge.services.filenames import (
    FilenameMatch,
    compare_filenames,
    profile_filename,
)
from pdf_bridge.services.scanner import ScanResult
from pdf_bridge.services.storage import (
    PromotedFile,
    StagedFile,
    StorageLayout,
    normalize_filename,
    promote_staged_file,
    validate_pdf_filename,
)

# States a replacement target must currently hold: exactly one current,
# same-collection, ingested document.
REPLACEABLE_STATES = (DocumentState.INGESTED,)

CANCELLABLE_STATES = (
    DocumentState.ANALYZING,
    DocumentState.REVIEW_REQUIRED,
    DocumentState.INGESTING,
    DocumentState.INGEST_FAILED,
    DocumentState.REPLACING,
    DocumentState.REPLACE_FAILED,
    DocumentState.CLEANUP_FAILED,
)

RETRYABLE_DOCUMENT_STATES = {
    DocumentState.ANALYZING: (OperationType.ANALYZE, DocumentState.ANALYZING),
    DocumentState.INGEST_FAILED: (OperationType.INGEST, DocumentState.INGESTING),
    DocumentState.REPLACE_FAILED: (OperationType.INGEST, DocumentState.REPLACING),
    DocumentState.DELETE_FAILED: (OperationType.DELETE, DocumentState.DELETING),
    DocumentState.CLEANUP_FAILED: (OperationType.CLEANUP, DocumentState.CLEANUP_PENDING),
}


class LifecycleError(RuntimeError):
    """Deliberate catalog transition failure with a stable transport code."""

    def __init__(self, message: str, *, code: str, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class DuplicateDocumentError(LifecycleError):
    """Raised when the same collection already holds identical file bytes."""

    def __init__(self, document: Document) -> None:
        super().__init__(
            "This collection already contains a document with identical file contents.",
            code="exact-duplicate",
            status=409,
        )
        self.document = document


@dataclass(frozen=True, slots=True)
class UploadRegistration:
    """Document, operation, and storage result of registering an upload."""

    document: Document
    operation: WorkOperation
    promoted: PromotedFile | None
    idempotent_replay: bool = False


@dataclass(frozen=True, slots=True)
class DecisionOutcome:
    """Decision record plus the follow-up work it queued."""

    decision: IntakeDecision
    document: Document
    operation: WorkOperation | None
    replacement: ReplacementWorkflow | None
    idempotent_replay: bool = False


def audit(
    session: Session,
    *,
    event_type: str,
    actor_type: str,
    actor_id: str,
    document: Document | None = None,
    operation: WorkOperation | None = None,
    details: dict[str, Any] | None = None,
) -> AuditEvent:
    """Append one immutable audit event."""

    event = AuditEvent(
        document=document,
        document_id=document.id if document else None,
        operation_id=operation.id if operation else None,
        event_type=event_type,
        actor_type=actor_type,
        actor_id=actor_id,
        details=details or {},
    )
    session.add(event)
    return event


def collection_epoch(session: Session, collection_key: str) -> int:
    """Return the current epoch for a collection, creating epoch 1 lazily."""

    row = session.get(CollectionEpoch, collection_key)
    if row is None:
        row = CollectionEpoch(collection_key=collection_key, epoch=1)
        session.add(row)
        session.flush()
    return row.epoch


def validate_collection_references(session: Session, configured_keys: set[str]) -> None:
    """Fail startup when retained catalog rows escape configured collections."""

    unknown = list(
        session.scalars(
            select(Document.collection_key)
            .where(
                Document.state.in_(RETAINED_DOCUMENT_STATES),
                Document.collection_key.not_in(configured_keys),
            )
            .distinct()
        ).all()
    )
    if unknown:
        raise RuntimeError(
            "active documents reference unconfigured collections: " + ", ".join(sorted(unknown))
        )


def find_exact_collection_duplicate(
    session: Session, *, sha256: str, collection_key: str
) -> Document | None:
    """Find retained identical bytes in the same collection only.

    Cross-collection duplicates are deliberately permitted: collections are
    isolated corpora and never warn about or block each other.
    """

    return session.scalar(
        select(Document)
        .where(
            Document.sha256 == sha256,
            Document.collection_key == collection_key,
            Document.state.in_(RETAINED_DOCUMENT_STATES),
        )
        .order_by(Document.uploaded_at.desc())
        .limit(1)
    )


def find_filename_warnings(
    session: Session,
    *,
    collection_key: str,
    filename: str,
    exclude_document_id: uuid.UUID | None = None,
) -> list[tuple[Document, FilenameMatch]]:
    """Compare a filename against retained documents in one collection."""

    incoming = profile_filename(filename)
    query = (
        select(Document)
        .where(
            Document.collection_key == collection_key,
            Document.state.in_(RETAINED_DOCUMENT_STATES),
        )
        .order_by(Document.uploaded_at.desc())
    )
    if exclude_document_id is not None:
        query = query.where(Document.id != exclude_document_id)
    warnings: list[tuple[Document, FilenameMatch]] = []
    for existing in session.scalars(query).all():
        match = compare_filenames(incoming, profile_filename(existing.original_filename))
        if match is not None:
            warnings.append((existing, match))
    return warnings


def replacement_target_issue(
    session: Session,
    target: Document,
    *,
    collection_key: str,
) -> str | None:
    """Return why a live document cannot be selected for replacement."""

    if target.collection_key != collection_key:
        return "replacement-cross-collection"
    if target.state not in REPLACEABLE_STATES:
        return "replacement-target-not-ingested"
    target_operation = session.scalar(
        select(WorkOperation.id)
        .where(
            WorkOperation.document_id == target.id,
            WorkOperation.state.in_((OperationState.QUEUED, OperationState.RUNNING)),
        )
        .limit(1)
    )
    if target_operation is not None:
        return "replacement-target-busy"
    in_flight = session.scalar(
        select(ReplacementWorkflow.id)
        .where(
            ReplacementWorkflow.old_document_id == target.id,
            ReplacementWorkflow.state.not_in((ReplacementState.SUCCEEDED, ReplacementState.FAILED)),
        )
        .limit(1)
    )
    if in_flight is not None:
        return "replacement-target-busy"
    return None


def find_identical_text_documents(
    session: Session,
    *,
    collection_key: str,
    text_sha256: str,
    exclude_document_id: uuid.UUID,
) -> list[Document]:
    """Find retained same-collection documents with identical normalized text."""

    return list(
        session.scalars(
            select(Document).where(
                Document.collection_key == collection_key,
                Document.text_sha256 == text_sha256,
                Document.state.in_(RETAINED_DOCUMENT_STATES),
                Document.id != exclude_document_id,
            )
        ).all()
    )


def next_attempt(session: Session, document_id: uuid.UUID, operation_type: OperationType) -> int:
    """Number the next durable attempt for one document and operation type."""

    latest = session.scalar(
        select(func.max(WorkOperation.attempt)).where(
            WorkOperation.document_id == document_id,
            WorkOperation.operation_type == operation_type,
        )
    )
    return (latest or 0) + 1


def enqueue_operation(
    session: Session,
    document: Document,
    operation_type: OperationType,
    *,
    replacement_id: uuid.UUID | None = None,
) -> WorkOperation:
    """Create one queued durable operation for the internal worker."""

    operation = WorkOperation(
        document=document,
        operation_type=operation_type,
        state=OperationState.QUEUED,
        phase=OperationPhase.QUEUED,
        attempt=next_attempt(session, document.id, operation_type),
        replacement_id=replacement_id,
    )
    session.add(operation)
    session.flush()
    return operation


def latest_operation(
    session: Session,
    document_id: uuid.UUID,
    *,
    operation_type: OperationType | None = None,
) -> WorkOperation | None:
    """Return the newest operation for a document, optionally by type."""

    query = (
        select(WorkOperation)
        .where(WorkOperation.document_id == document_id)
        .order_by(WorkOperation.created_at.desc(), WorkOperation.attempt.desc())
        .limit(1)
    )
    if operation_type is not None:
        query = query.where(WorkOperation.operation_type == operation_type)
    return session.scalar(query)


def latest_analysis(session: Session, document_id: uuid.UUID) -> DocumentAnalysis | None:
    """Return the newest analysis revision for a document."""

    return session.scalar(
        select(DocumentAnalysis)
        .where(DocumentAnalysis.document_id == document_id)
        .order_by(DocumentAnalysis.revision.desc())
        .limit(1)
    )


def register_staged_upload(
    session: Session,
    *,
    staged: StagedFile,
    layout: StorageLayout,
    filename: str,
    collection_key: str,
    idempotency_key: str,
    actor_type: str,
    actor_id: str,
    scan_result: ScanResult,
) -> UploadRegistration:
    """Validate and atomically register a scanned upload for analysis."""

    existing = (
        session.execute(
            select(Document)
            .where(Document.idempotency_key == idempotency_key)
            .options(joinedload(Document.operations))
        )
        .unique()
        .scalar_one_or_none()
    )
    if existing is not None:
        if (
            existing.sha256 != staged.sha256
            or existing.size_bytes != staged.size_bytes
            or existing.normalized_filename != normalize_filename(filename)
            or existing.collection_key != collection_key
        ):
            raise LifecycleError(
                "The idempotency key was already used for a different file.",
                code="idempotency-key-conflict",
            )
        analyze_operations = [
            item for item in existing.operations if item.operation_type == OperationType.ANALYZE
        ]
        if not analyze_operations:
            raise LifecycleError(
                "The upload idempotency record has no analysis operation.",
                code="catalog-inconsistent",
                status=500,
            )
        operation = max(analyze_operations, key=lambda item: (item.attempt, item.created_at))
        return UploadRegistration(existing, operation, None, idempotent_replay=True)

    display_filename = validate_pdf_filename(filename)
    duplicate = find_exact_collection_duplicate(
        session, sha256=staged.sha256, collection_key=collection_key
    )
    if duplicate is not None:
        raise DuplicateDocumentError(duplicate)
    if scan_result.state != ScanState.CLEAN:
        raise LifecycleError(
            "Only files reported clean by the configured scanner may be queued.",
            code="scan-not-clean",
            status=422,
        )

    epoch = collection_epoch(session, collection_key)
    document = Document(
        id=uuid.uuid4(),
        original_filename=display_filename,
        normalized_filename=normalize_filename(display_filename),
        size_bytes=staged.size_bytes,
        sha256=staged.sha256,
        idempotency_key=idempotency_key,
        state=DocumentState.ANALYZING,
        scan_state=scan_result.state,
        scan_engine=scan_result.engine,
        scan_signature=scan_result.signature,
        scanned_at=scan_result.scanned_at,
        uploader_identity=actor_id,
        collection_key=collection_key,
        collection_epoch=epoch,
    )
    promoted: PromotedFile | None = None
    try:
        promoted = promote_staged_file(staged, layout, document.id)
        document.storage_key = promoted.storage_key
        session.add(document)
        session.flush()
        operation = enqueue_operation(session, document, OperationType.ANALYZE)
        audit(
            session,
            event_type="upload_received",
            actor_type=actor_type,
            actor_id=actor_id,
            document=document,
            operation=operation,
            details={
                "status": DocumentState.ANALYZING.value,
                "collection_key": collection_key,
                "detail": "PDF accepted and queued for analysis.",
            },
        )
        audit(
            session,
            event_type="malware_scan_clean",
            actor_type="scanner",
            actor_id=scan_result.engine,
            document=document,
            operation=operation,
            details={"status": scan_result.state.value},
        )
        session.flush()
    except Exception:
        if promoted is not None:
            promoted.path.unlink(missing_ok=True)
        raise
    return UploadRegistration(document, operation, promoted)


def get_upload_document(session: Session, upload_id: uuid.UUID) -> Document:
    """Load one upload's document or fail with a stable 404."""

    document = session.get(Document, upload_id)
    if document is None:
        raise LifecycleError("No upload exists for this ID.", code="upload-not-found", status=404)
    return document


def _require_review_state(document: Document) -> None:
    if document.state != DocumentState.REVIEW_REQUIRED:
        raise LifecycleError(
            "Only an upload awaiting review accepts a decision.",
            code="decision-not-applicable",
        )


def _current_complete_analysis(session: Session, document: Document) -> DocumentAnalysis:
    analysis = latest_analysis(session, document.id)
    if analysis is None or analysis.completed_at is None:
        raise LifecycleError(
            "This upload has no completed analysis to decide on.",
            code="analysis-not-complete",
        )
    return analysis


def record_decision(
    session: Session,
    *,
    document: Document,
    analysis_revision: int,
    action: DecisionAction,
    target_document_id: uuid.UUID | None,
    idempotency_key: str,
    actor_type: str,
    actor_id: str,
) -> DecisionOutcome:
    """Validate and record an immutable Keep, Replace, or Cancel decision."""

    replay = session.scalar(
        select(IntakeDecision).where(IntakeDecision.idempotency_key == idempotency_key)
    )
    if replay is not None:
        if (
            replay.document_id != document.id
            or replay.analysis_revision != analysis_revision
            or replay.action != action
            or replay.target_document_id != target_document_id
        ):
            raise LifecycleError(
                "The decision idempotency key was already used for a different decision.",
                code="idempotency-key-conflict",
            )
        operation = latest_operation(session, document.id)
        replacement = session.scalar(
            select(ReplacementWorkflow).where(ReplacementWorkflow.decision_id == replay.id)
        )
        return DecisionOutcome(
            decision=replay,
            document=document,
            operation=operation,
            replacement=replacement,
            idempotent_replay=True,
        )

    _require_review_state(document)
    analysis = _current_complete_analysis(session, document)
    if analysis.revision != analysis_revision:
        raise LifecycleError(
            "The reviewed analysis is no longer current; reload and review again.",
            code="stale-analysis-revision",
        )
    if analysis.collection_epoch != collection_epoch(session, document.collection_key):
        raise LifecycleError(
            "The collection was rebuilt after this analysis; retry the analysis.",
            code="stale-collection-epoch",
        )

    advisory_override = bool(
        analysis.candidate_count
        or not analysis.semantic_complete
        or not analysis.classification_complete
    )
    decision = IntakeDecision(
        document=document,
        document_id=document.id,
        analysis_id=analysis.id,
        analysis_revision=analysis.revision,
        action=action,
        target_document_id=target_document_id,
        idempotency_key=idempotency_key,
        advisory_override=advisory_override and action == DecisionAction.KEEP,
        actor_type=actor_type,
        actor_id=actor_id,
    )
    session.add(decision)
    session.flush()

    operation: WorkOperation | None = None
    replacement: ReplacementWorkflow | None = None
    if action == DecisionAction.KEEP:
        document.state = DocumentState.INGESTING
        operation = enqueue_operation(session, document, OperationType.INGEST)
        event_type = "decision_keep"
    elif action == DecisionAction.REPLACE:
        replacement = _begin_replacement(
            session,
            new_document=document,
            analysis=analysis,
            target_document_id=target_document_id,
            decision=decision,
        )
        document.state = DocumentState.REPLACING
        operation = enqueue_operation(
            session, document, OperationType.INGEST, replacement_id=replacement.id
        )
        event_type = "decision_replace"
    else:
        document.state = DocumentState.CLEANUP_PENDING
        document.cleanup_target = DocumentState.CANCELLED
        operation = enqueue_operation(session, document, OperationType.CLEANUP)
        event_type = "decision_cancel"

    audit(
        session,
        event_type=event_type,
        actor_type=actor_type,
        actor_id=actor_id,
        document=document,
        operation=operation,
        details={
            "status": document.state.value,
            "analysis_revision": analysis.revision,
            "advisory_override": decision.advisory_override,
            "target_document_id": str(target_document_id) if target_document_id else None,
            "collection_key": document.collection_key,
        },
    )
    session.flush()
    return DecisionOutcome(
        decision=decision,
        document=document,
        operation=operation,
        replacement=replacement,
    )


def _begin_replacement(
    session: Session,
    *,
    new_document: Document,
    analysis: DocumentAnalysis,
    target_document_id: uuid.UUID | None,
    decision: IntakeDecision,
) -> ReplacementWorkflow:
    if target_document_id is None:
        raise LifecycleError(
            "Replace decisions require a target document.",
            code="replacement-target-required",
            status=422,
        )
    target = session.get(Document, target_document_id)
    if target is None:
        raise LifecycleError(
            "The replacement target no longer exists.",
            code="replacement-target-not-found",
            status=404,
        )
    target_issue = replacement_target_issue(
        session,
        target,
        collection_key=new_document.collection_key,
    )
    if target_issue == "replacement-cross-collection":
        raise LifecycleError(
            "Replacement is same-collection only.",
            code="replacement-cross-collection",
            status=422,
        )
    if target_issue == "replacement-target-not-ingested":
        raise LifecycleError(
            "Only a currently ingested document can be replaced.",
            code="replacement-target-not-ingested",
        )
    if target_issue == "replacement-target-busy":
        raise LifecycleError(
            "The replacement target has active work or another replacement in progress.",
            code="replacement-target-busy",
        )
    candidate_ids = {candidate.matched_document_id for candidate in analysis.candidates}
    if target.id not in candidate_ids:
        raise LifecycleError(
            "The replacement target must be one of this analysis's candidates.",
            code="replacement-target-not-candidate",
            status=422,
        )
    workflow = ReplacementWorkflow(
        new_document_id=new_document.id,
        old_document_id=target.id,
        decision_id=decision.id,
        state=ReplacementState.PREPARING,
    )
    session.add(workflow)
    session.flush()
    return workflow


def retry_upload(
    session: Session,
    *,
    document: Document,
    actor_type: str,
    actor_id: str,
) -> WorkOperation:
    """Queue the next attempt for failed work or a stale completed analysis."""

    if document.state == DocumentState.REVIEW_REQUIRED:
        analysis = latest_analysis(session, document.id)
        current_epoch = collection_epoch(session, document.collection_key)
        if (
            analysis is None
            or analysis.completed_at is None
            or analysis.collection_epoch == current_epoch
        ):
            raise LifecycleError(
                "This upload has no retryable failed work.",
                code="operation-not-retryable",
            )
        previous = latest_operation(
            session,
            document.id,
            operation_type=OperationType.ANALYZE,
        )
        operation = enqueue_operation(session, document, OperationType.ANALYZE)
        document.state = DocumentState.ANALYZING
        document.last_error = None
        audit(
            session,
            event_type="operation_retried",
            actor_type=actor_type,
            actor_id=actor_id,
            document=document,
            operation=operation,
            details={
                "previous_operation_id": str(previous.id) if previous else None,
                "previous_analysis_id": str(analysis.id),
                "previous_analysis_revision": analysis.revision,
                "analysis_collection_epoch": analysis.collection_epoch,
                "current_collection_epoch": current_epoch,
                "attempt": operation.attempt,
            },
        )
        session.flush()
        return operation

    plan = RETRYABLE_DOCUMENT_STATES.get(document.state)
    if plan is None:
        raise LifecycleError(
            "This upload has no retryable failed work.", code="operation-not-retryable"
        )
    operation_type, next_state = plan
    failed = latest_operation(session, document.id, operation_type=operation_type)
    if failed is None or failed.state != OperationState.FAILED or not failed.retryable:
        raise LifecycleError(
            "Only the current failed operation can be retried.",
            code="operation-not-retryable",
        )
    operation = enqueue_operation(
        session, document, operation_type, replacement_id=failed.replacement_id
    )
    document.state = next_state
    document.last_error = None
    audit(
        session,
        event_type="operation_retried",
        actor_type=actor_type,
        actor_id=actor_id,
        document=document,
        operation=operation,
        details={"previous_operation_id": str(failed.id), "attempt": operation.attempt},
    )
    session.flush()
    return operation


def _prepare_replacement_cancellation(
    session: Session,
    document: Document,
    *,
    cancelled_at: datetime,
) -> dict[str, Any]:
    """Terminalize a safely cancellable replacement and describe the boundary."""

    workflow = session.scalar(
        select(ReplacementWorkflow).where(ReplacementWorkflow.new_document_id == document.id)
    )
    if workflow is None:
        return {}
    old_document = session.get(Document, workflow.old_document_id)
    if old_document is None:
        raise LifecycleError(
            "The replacement workflow references a missing old document.",
            code="catalog-inconsistent",
            status=500,
        )

    stage = workflow.state
    old_state = old_document.state
    if stage == ReplacementState.DELETING_OLD and old_state in {
        DocumentState.DELETING,
        DocumentState.DELETE_FAILED,
    }:
        raise LifecycleError(
            "The old document may already be partially removed. Retry the replacement "
            "until its verified deletion completes before cancelling.",
            code="replacement-cancellation-unsafe",
        )

    if stage == ReplacementState.PREPARING:
        boundary_crossed = False
        expected_old_states = {DocumentState.INGESTED}
    elif stage == ReplacementState.DELETING_OLD:
        boundary_crossed = old_state == DocumentState.DELETED
        expected_old_states = {DocumentState.INGESTED, DocumentState.DELETED}
    elif stage == ReplacementState.INGESTING_NEW:
        boundary_crossed = True
        expected_old_states = {DocumentState.DELETED}
    elif stage == ReplacementState.FAILED:
        boundary_crossed = old_state == DocumentState.DELETED
        expected_old_states = {DocumentState.INGESTED, DocumentState.DELETED}
    else:
        raise LifecycleError(
            "A completed replacement cannot be cancelled as pending work.",
            code="upload-not-cancellable",
        )

    if old_state not in expected_old_states:
        raise LifecycleError(
            "The replacement stage and old document state are inconsistent.",
            code="catalog-inconsistent",
            status=500,
        )

    if workflow.state != ReplacementState.FAILED:
        workflow.state = ReplacementState.FAILED
        workflow.error = (
            "Replacement cancelled after verified old-document deletion; incoming cleanup queued."
            if boundary_crossed
            else "Replacement cancelled before old-document deletion."
        )
        workflow.completed_at = cancelled_at

    return {
        "replacement_id": str(workflow.id),
        "replacement_stage": stage.value,
        "replacement_old_document_id": str(old_document.id),
        "replacement_old_state": old_state.value,
        "replacement_destructive_boundary_crossed": boundary_crossed,
    }


def cancel_upload(
    session: Session,
    *,
    document: Document,
    actor_type: str,
    actor_id: str,
) -> WorkOperation:
    """Cancel unpublished work and queue full cleanup of retained content."""

    if document.state not in CANCELLABLE_STATES:
        raise LifecycleError(
            "Only unpublished uploads can be cancelled.",
            code="upload-not-cancellable",
        )
    open_operations = session.scalars(
        select(WorkOperation).where(
            WorkOperation.document_id == document.id,
            WorkOperation.state.in_((OperationState.QUEUED, OperationState.RUNNING)),
        )
    ).all()
    if any(item.state == OperationState.RUNNING for item in open_operations):
        raise LifecycleError(
            "The upload is being processed right now; retry the cancellation shortly.",
            code="upload-busy",
        )
    now = utc_now()
    replacement_details = _prepare_replacement_cancellation(
        session,
        document,
        cancelled_at=now,
    )
    for item in open_operations:
        item.state = OperationState.CANCELLED
        item.completed_at = now
    document.state = DocumentState.CLEANUP_PENDING
    document.cleanup_target = DocumentState.CANCELLED
    operation = enqueue_operation(session, document, OperationType.CLEANUP)
    audit(
        session,
        event_type="upload_cancelled",
        actor_type=actor_type,
        actor_id=actor_id,
        document=document,
        operation=operation,
        details={"status": document.state.value, **replacement_details},
    )
    session.flush()
    return operation


def request_deletion(
    session: Session,
    *,
    document: Document,
    actor_type: str,
    actor_id: str,
    reason: str | None = None,
) -> WorkOperation:
    """Queue removal of an ingested document from retrieval and storage."""

    if document.state == DocumentState.DELETE_FAILED:
        return retry_upload(session, document=document, actor_type=actor_type, actor_id=actor_id)
    if document.state != DocumentState.INGESTED:
        raise LifecycleError(
            "Only an ingested document can be queued for deletion.",
            code="document-not-deletable",
        )
    in_flight = session.scalar(
        select(ReplacementWorkflow).where(
            ReplacementWorkflow.old_document_id == document.id,
            ReplacementWorkflow.state.not_in((ReplacementState.SUCCEEDED, ReplacementState.FAILED)),
        )
    )
    if in_flight is not None:
        raise LifecycleError(
            "This document is being replaced; the replacement owns its removal.",
            code="document-being-replaced",
        )
    open_operation = session.scalar(
        select(WorkOperation.id)
        .where(
            WorkOperation.document_id == document.id,
            WorkOperation.state.in_((OperationState.QUEUED, OperationState.RUNNING)),
        )
        .limit(1)
    )
    if open_operation is not None:
        raise LifecycleError(
            "This document still has active ingestion work; retry deletion after it finishes.",
            code="document-busy",
        )
    document.state = DocumentState.DELETING
    operation = enqueue_operation(session, document, OperationType.DELETE)
    audit(
        session,
        event_type="deletion_requested",
        actor_type=actor_type,
        actor_id=actor_id,
        document=document,
        operation=operation,
        details={
            "status": document.state.value,
            "detail": reason or "Deletion queued for the internal worker.",
            "collection_key": document.collection_key,
        },
    )
    session.flush()
    return operation


def can_serve_content(document: Document) -> bool:
    """Return whether a clean retained document may be served to a browser."""

    return (
        document.scan_state == ScanState.CLEAN
        and document.storage_key is not None
        and document.state in RETAINED_DOCUMENT_STATES
        and document.state not in {DocumentState.CLEANUP_PENDING, DocumentState.CLEANUP_FAILED}
    )


def is_open_upload(document: Document) -> bool:
    """Return whether a document still represents open intake work."""

    return document.state in OPEN_UPLOAD_STATES
