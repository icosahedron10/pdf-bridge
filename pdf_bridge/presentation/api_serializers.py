"""Fail-closed conversions from target catalog rows to API v2 contracts."""

from __future__ import annotations

import hashlib
import math
import uuid
from datetime import UTC, datetime

from pdf_bridge.contracts.schemas import (
    AllowedAction,
    AuditEventResponse,
    CandidateDocumentReference,
    Chunk,
    CollectionStateCounts,
    CollectionSummary,
    DecisionSummary,
    DeletionSummary,
    DocumentDetail,
    DocumentSummary,
    EvidenceCitation,
    MarkdownDocument,
    MarkdownPage,
    MutationResponse,
    OperationDetail,
    OperationPriorityName,
    OperationSummary,
    PreflightCandidate,
    PreflightCompleteness,
    PreflightEvidence,
    PreparedRevisionSummary,
    PublicationSummary,
    ReplacementSummary,
    SanitizedFailure,
    SourceMetadata,
    TombstoneSummary,
    UploadAcceptedResponse,
)
from pdf_bridge.core.config import CollectionDefinition
from pdf_bridge.persistence.models import (
    AuditEvent,
    CandidateEvidence,
    Decision,
    DecisionAction,
    DeletionProgress,
    Document,
    DocumentState,
    OperationPhase,
    OperationPriority,
    OperationState,
    PreparedCandidate,
    PreparedChunk,
    PreparedPage,
    PreparedRevision,
    PublicationRecord,
    PublicationStatus,
    RevisionStatus,
    ScanState,
    Tombstone,
    WorkOperation,
)


class SerializationError(RuntimeError):
    """Persistence data is incomplete or unsafe for the public contract."""


_PRIORITY_NAMES = {
    int(OperationPriority.HIGH): OperationPriorityName.HIGH,
    int(OperationPriority.REPLACEMENT): OperationPriorityName.REPLACEMENT,
    int(OperationPriority.PUBLISH): OperationPriorityName.PUBLISH,
    int(OperationPriority.NORMAL): OperationPriorityName.NORMAL,
}

_SOURCE_READABLE_STATES = {
    DocumentState.PREFLIGHTING,
    DocumentState.PREFLIGHT_FAILED,
    DocumentState.REVIEW_REQUIRED,
    DocumentState.PUBLISHING,
    DocumentState.PUBLISH_FAILED,
    DocumentState.READY,
}


def _utc(value: datetime | None, field_name: str) -> datetime | None:
    if value is None:
        return None
    # SQLite loses timezone metadata. Catalog DateTime values are defined as
    # UTC, so a naive value from SQLite is restored rather than guessed from
    # the host timezone.
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    try:
        return aware.astimezone(UTC)
    except (OverflowError, ValueError) as exc:
        raise SerializationError(f"{field_name} is not a valid catalog timestamp") from exc


def _required_utc(value: datetime | None, field_name: str) -> datetime:
    normalized = _utc(value, field_name)
    if normalized is None:
        raise SerializationError(f"{field_name} is required")
    return normalized


def _safe_failure(
    *,
    code: str | None,
    message: str | None,
    retryable: bool,
    phase: OperationPhase | None,
    required: bool = False,
    fallback_message: str | None = None,
) -> SanitizedFailure | None:
    if code is None:
        if required or message is not None:
            raise SerializationError("a failed resource is missing its sanitized failure code")
        return None
    public_message = message or fallback_message
    if public_message is None:
        raise SerializationError("a failure code is missing its sanitized public message")
    return SanitizedFailure(
        code=code,
        message=public_message,
        retryable=retryable,
        phase=phase,
    )


def _priority_name(priority: int) -> OperationPriorityName:
    try:
        return _PRIORITY_NAMES[priority]
    except KeyError as exc:
        raise SerializationError(f"unknown durable operation priority: {priority}") from exc


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def allowed_actions(document: Document) -> list[AllowedAction]:
    """Derive the complete lifecycle action set from current catalog truth."""

    if document.state is DocumentState.REVIEW_REQUIRED:
        return [
            AllowedAction.KEEP,
            AllowedAction.REPLACE,
            AllowedAction.CANCEL,
            AllowedAction.DELETE,
        ]
    if document.state in {DocumentState.PREFLIGHT_FAILED, DocumentState.PUBLISH_FAILED}:
        actions = [AllowedAction.DELETE]
        if document.failure_retryable:
            actions.insert(0, AllowedAction.RETRY)
        return actions
    if document.state is DocumentState.READY:
        return [AllowedAction.DELETE]
    if document.state is DocumentState.DELETE_FAILED:
        actions = [AllowedAction.DELETE]
        if document.failure_retryable:
            actions.insert(0, AllowedAction.RETRY)
        return actions
    return []


def operation_summary(operation: WorkOperation) -> OperationSummary:
    return OperationSummary(
        id=operation.id,
        operation_type=operation.operation_type,
        state=operation.state,
        phase=operation.phase,
        priority=_priority_name(operation.priority),
        attempt=operation.attempt,
        retryable=operation.retryable,
        created_at=_required_utc(operation.created_at, "operation.created_at"),
        updated_at=_required_utc(operation.updated_at, "operation.updated_at"),
        completed_at=_utc(operation.completed_at, "operation.completed_at"),
    )


def operation_detail(
    operation: WorkOperation,
    *,
    queue_position: int | None,
    now: datetime,
) -> OperationDetail:
    now_utc = _required_utc(now, "now")
    created_at = _required_utc(operation.created_at, "operation.created_at")
    phase_started_at = _required_utc(
        operation.phase_started_at, "operation.phase_started_at"
    )
    failed = operation.state is OperationState.FAILED
    failure = _safe_failure(
        code=operation.failure_code,
        message=operation.failure_message,
        retryable=operation.retryable,
        phase=operation.phase,
        required=failed,
    )
    return OperationDetail(
        **operation_summary(operation).model_dump(),
        document_id=operation.document_id,
        prepared_revision_id=operation.prepared_revision_id,
        replacement_target_document_id=operation.replacement_target_document_id,
        queue_position=queue_position,
        queue_age_seconds=max((now_utc - created_at).total_seconds(), 0.0),
        phase_age_seconds=max((now_utc - phase_started_at).total_seconds(), 0.0),
        started_at=_utc(operation.started_at, "operation.started_at"),
        failure=failure,
    )


def upload_accepted_response(
    document: Document,
    operation: WorkOperation,
    *,
    idempotent_replay: bool = False,
) -> UploadAcceptedResponse:
    """Build the exact durable response used for upload admission and replay."""

    return UploadAcceptedResponse(
        document=document_summary(document, operation=operation),
        operation=operation_summary(operation),
        idempotent_replay=idempotent_replay,
    )


def mutation_response(
    document: Document,
    operation: WorkOperation,
    *,
    idempotent_replay: bool = False,
) -> MutationResponse:
    """Build the shared durable response for decisions, retries, and deletes."""

    return MutationResponse(
        document=document_summary(document, operation=operation),
        operation=operation_summary(operation),
        idempotent_replay=idempotent_replay,
    )


def document_summary(
    document: Document,
    *,
    operation: WorkOperation | None = None,
) -> DocumentSummary:
    phase = operation.phase if operation is not None else None
    failure = _safe_failure(
        code=document.failure_code,
        message=document.failure_message,
        retryable=document.failure_retryable,
        phase=phase,
        required=(
            document.state
            in {
                DocumentState.PREFLIGHT_FAILED,
                DocumentState.PUBLISH_FAILED,
                DocumentState.DELETE_FAILED,
            }
        ),
    )
    return DocumentSummary(
        id=document.id,
        collection_key=document.collection_key,
        original_filename=document.original_filename,
        content_type=document.content_type,
        size_bytes=document.size_bytes,
        sha256=document.sha256,
        created_by=document.created_by,
        state=document.state,
        created_at=_required_utc(document.created_at, "document.created_at"),
        updated_at=_required_utc(document.updated_at, "document.updated_at"),
        ready_at=_utc(document.ready_at, "document.ready_at"),
        failure=failure,
        allowed_actions=allowed_actions(document),
    )


def source_metadata(document: Document) -> SourceMetadata:
    if document.scan_state is not ScanState.CLEAN:
        raise SerializationError("an admitted document does not have a clean scan verdict")
    if document.scanned_at is None or not document.scan_engine:
        raise SerializationError("clean source metadata is missing scanner correlation")
    return SourceMetadata(
        original_filename=document.original_filename,
        content_type=document.content_type,
        size_bytes=document.size_bytes,
        sha256=document.sha256,
        created_by=document.created_by,
        created_at=_required_utc(document.created_at, "document.created_at"),
        scan_state=document.scan_state,
        scan_engine=document.scan_engine,
        scanned_at=_required_utc(document.scanned_at, "document.scanned_at"),
        available=(
            document.storage_key is not None
            and document.state in _SOURCE_READABLE_STATES
        ),
    )


def preflight_completeness(revision: PreparedRevision) -> PreflightCompleteness:
    return PreflightCompleteness(
        native_text_eligible=revision.native_text_eligible,
        formatter_complete=revision.formatter_complete,
        vector_complete=revision.vector_complete,
        candidate_discovery_complete=revision.candidate_discovery_complete,
        advisory_complete=revision.advisory_complete,
        clear_for_publication=revision.clear_for_publication,
        incomplete_reasons=list(revision.incomplete_reasons),
    )


def prepared_revision_summary(
    revision: PreparedRevision,
    *,
    document: Document | None = None,
    operation: WorkOperation | None = None,
) -> PreparedRevisionSummary:
    failure = _safe_failure(
        code=revision.failure_code,
        message=revision.failure_message,
        retryable=document.failure_retryable if document is not None else False,
        phase=operation.phase if operation is not None else None,
        required=revision.status is RevisionStatus.FAILED,
    )
    return PreparedRevisionSummary(
        id=revision.id,
        revision_number=revision.revision_number,
        status=revision.status,
        active_qdrant_collection=revision.active_qdrant_collection,
        content_profile_id=revision.content_profile_id,
        index_profile_id=revision.index_profile_id,
        preflight_policy_id=revision.preflight_policy_id,
        formatter_model_id=revision.formatter_model_id,
        dense_model_id=revision.dense_model_id,
        dense_dimension=revision.dense_dimension,
        sparse_model_id=revision.sparse_model_id,
        language_code=revision.language_code,
        completeness=preflight_completeness(revision),
        page_count=revision.page_count,
        chunk_count=revision.chunk_count,
        expected_point_count=revision.expected_point_count,
        markdown_sha256=revision.markdown_sha256,
        manifest_sha256=revision.manifest_sha256,
        failure=failure,
        created_at=_required_utc(revision.created_at, "revision.created_at"),
        sealed_at=_utc(revision.sealed_at, "revision.sealed_at"),
    )


def publication_summary(
    publication: PublicationRecord,
    *,
    document: Document,
) -> PublicationSummary:
    failed = publication.status is PublicationStatus.FAILED
    failure = _safe_failure(
        code=publication.failure_code,
        message=None,
        retryable=document.failure_retryable,
        phase=OperationPhase.VERIFY_ACTIVE_POINTS,
        required=failed,
        fallback_message="Publication did not reach verified completeness.",
    )
    return PublicationSummary(
        id=publication.id,
        prepared_revision_id=publication.prepared_revision_id,
        active_qdrant_collection=publication.active_qdrant_collection,
        status=publication.status,
        expected_points=publication.expected_points,
        verified_points=publication.verified_points,
        payload_revision_verified=publication.payload_revision_verified,
        vector_schema_verified=publication.vector_schema_verified,
        screening_zero_verified=publication.screening_zero_verified,
        failure=failure,
        created_at=_required_utc(publication.created_at, "publication.created_at"),
        updated_at=_required_utc(publication.updated_at, "publication.updated_at"),
        verified_at=_utc(publication.verified_at, "publication.verified_at"),
    )


def deletion_summary(progress: DeletionProgress, *, document: Document) -> DeletionSummary:
    code = progress.failure_code or document.failure_code
    failed = document.state is DocumentState.DELETE_FAILED
    failure = _safe_failure(
        code=code,
        message=document.failure_message,
        retryable=document.failure_retryable,
        phase=OperationPhase(progress.phase.value),
        required=failed,
        fallback_message="Verified document deletion did not complete.",
    )
    return DeletionSummary(
        terminal_disposition=progress.terminal_disposition,
        phase=progress.phase,
        active_qdrant_collection=progress.active_qdrant_collection,
        screening_qdrant_collection=progress.screening_qdrant_collection,
        attempts=progress.attempts,
        active_zero_verified_at=_utc(
            progress.active_zero_verified_at, "deletion.active_zero_verified_at"
        ),
        screening_zero_verified_at=_utc(
            progress.screening_zero_verified_at,
            "deletion.screening_zero_verified_at",
        ),
        storage_purged_at=_utc(progress.storage_purged_at, "deletion.storage_purged_at"),
        tombstoned_at=_utc(progress.tombstoned_at, "deletion.tombstoned_at"),
        failure=failure,
        updated_at=_required_utc(progress.updated_at, "deletion.updated_at"),
    )


def decision_summary(decision: Decision) -> DecisionSummary:
    if (decision.action is DecisionAction.REPLACE) != (decision.target_document_id is not None):
        raise SerializationError("decision action and replacement target are inconsistent")
    return DecisionSummary(
        id=decision.id,
        prepared_revision_id=decision.prepared_revision_id,
        prepared_manifest_sha256=decision.prepared_manifest_sha256,
        action=decision.action,
        target_document_id=decision.target_document_id,
        actor_type=decision.actor_type,
        actor_id=decision.actor_id,
        created_at=_required_utc(decision.created_at, "decision.created_at"),
    )


def replacement_summary(
    *,
    decision: Decision,
    old_document: Document,
    new_document: Document,
    operation: WorkOperation | None,
) -> ReplacementSummary:
    if (
        decision.action is not DecisionAction.REPLACE
        or decision.document_id != new_document.id
        or decision.target_document_id != old_document.id
        or old_document.replaced_by_document_id != new_document.id
        or old_document.collection_key != new_document.collection_key
    ):
        raise SerializationError("replacement linkage is incomplete or contradictory")
    if (
        operation is not None
        and operation.replacement_target_document_id != old_document.id
    ):
        raise SerializationError("replacement operation targets a different old document")
    completed_at = (
        _utc(new_document.ready_at, "replacement.new_ready_at")
        if old_document.state is DocumentState.DELETED
        and new_document.state is DocumentState.READY
        else None
    )
    return ReplacementSummary(
        decision_id=decision.id,
        old_document_id=old_document.id,
        new_document_id=new_document.id,
        old_document_state=old_document.state,
        new_document_state=new_document.state,
        operation_id=operation.id if operation is not None else None,
        phase=operation.phase if operation is not None else None,
        completed_at=completed_at,
    )


def document_detail(
    document: Document,
    *,
    operation: WorkOperation | None,
    revision: PreparedRevision | None,
    decision: Decision | None,
    publication: PublicationRecord | None,
    deletion: DeletionProgress | None,
    replacement: ReplacementSummary | None,
) -> DocumentDetail:
    if decision is not None and revision is None:
        raise SerializationError("a decision exists without its prepared revision")
    if publication is not None and revision is None:
        raise SerializationError("a publication exists without its prepared revision")
    return DocumentDetail(
        **document_summary(document, operation=operation).model_dump(),
        source=source_metadata(document),
        terminal_disposition=document.terminal_disposition,
        current_operation=operation_summary(operation) if operation is not None else None,
        prepared_revision=(
            prepared_revision_summary(revision, document=document, operation=operation)
            if revision is not None
            else None
        ),
        publication=(
            publication_summary(publication, document=document)
            if publication is not None
            else None
        ),
        deletion=deletion_summary(deletion, document=document) if deletion is not None else None,
        replacement=replacement,
        decision=decision_summary(decision) if decision is not None else None,
    )


def collection_summary(
    definition: CollectionDefinition,
    counts: dict[str, int],
) -> CollectionSummary:
    expected = {state.value for state in DocumentState}
    if set(counts) != expected:
        raise SerializationError("collection counts do not cover the exact target state enum")
    typed_counts = {DocumentState(key): value for key, value in counts.items()}
    return CollectionSummary(
        key=definition.key,
        display_name=definition.display_name,
        description=definition.description,
        audience=definition.audience,
        enabled=definition.enabled,
        counts=CollectionStateCounts(total=sum(typed_counts.values()), by_state=typed_counts),
    )


def markdown_page(page: PreparedPage) -> MarkdownPage:
    if _sha256_text(page.markdown) != page.markdown_sha256:
        raise SerializationError(f"prepared page {page.page_number} Markdown hash is invalid")
    if not page.slices:
        raise SerializationError(f"prepared page {page.page_number} has no formatter slices")
    return MarkdownPage(
        page_number=page.page_number,
        markdown=page.markdown,
        markdown_sha256=page.markdown_sha256,
        source_projection_sha256=page.source_projection_sha256,
        markdown_projection_sha256=page.markdown_projection_sha256,
        slice_count=len(page.slices),
    )


def markdown_document(revision: PreparedRevision, markdown: str) -> MarkdownDocument:
    if revision.markdown_sha256 is None or _sha256_text(markdown) != revision.markdown_sha256:
        raise SerializationError("canonical document Markdown does not match its revision hash")
    pages = [
        markdown_page(page)
        for page in sorted(revision.prepared_pages, key=lambda item: item.page_number)
    ]
    if revision.page_count != len(pages):
        raise SerializationError("prepared page count does not match the revision manifest")
    return MarkdownDocument(
        document_id=revision.document_id,
        prepared_revision_id=revision.id,
        markdown_sha256=revision.markdown_sha256,
        markdown=markdown,
        pages=pages,
    )


def chunk(chunk_row: PreparedChunk) -> Chunk:
    if _sha256_text(chunk_row.markdown) != chunk_row.text_sha256:
        raise SerializationError(f"prepared chunk {chunk_row.chunk_index} hash is invalid")
    return Chunk(
        id=chunk_row.id,
        prepared_revision_id=chunk_row.prepared_revision_id,
        chunk_index=chunk_row.chunk_index,
        page_start=chunk_row.page_start,
        page_end=chunk_row.page_end,
        heading_path=list(chunk_row.heading_path),
        token_count=chunk_row.token_count,
        text_sha256=chunk_row.text_sha256,
        markdown=chunk_row.markdown,
    )


def evidence_citation(payload: dict[str, object]) -> EvidenceCitation:
    try:
        return EvidenceCitation.model_validate(payload)
    except (TypeError, ValueError) as exc:
        raise SerializationError("candidate evidence contains an unsafe citation") from exc


def preflight_evidence(record: CandidateEvidence) -> PreflightEvidence:
    return PreflightEvidence(
        id=record.id,
        kind=record.kind,
        model_id=record.model_id,
        valid=record.valid,
        label=record.label,
        summary=record.summary,
        citations=[evidence_citation(item) for item in record.evidence],
        failure_code=record.failure_code,
        evidence_sha256=record.evidence_sha256,
        created_at=_required_utc(record.created_at, "candidate_evidence.created_at"),
    )


def _candidate_document(snapshot: dict[str, object]) -> CandidateDocumentReference:
    required = {"id", "collection_key", "original_filename", "state", "sha256"}
    if not required.issubset(snapshot):
        raise SerializationError("candidate snapshot is missing immutable public identity fields")
    return CandidateDocumentReference(
        id=snapshot["id"],
        collection_key=snapshot["collection_key"],
        original_filename=snapshot["original_filename"],
        state=snapshot["state"],
        sha256=snapshot["sha256"],
    )


def _validate_matched_pairs(pairs: list[list[object]]) -> int:
    for pair in pairs:
        if len(pair) != 2 or type(pair[0]) is not int or pair[0] < 0:
            raise SerializationError("candidate matched-chunk correlation is invalid")
        try:
            uuid.UUID(str(pair[1]))
        except (TypeError, ValueError) as exc:
            raise SerializationError("candidate matched chunk ID is invalid") from exc
    return len(pairs)


def preflight_candidate(
    candidate: PreparedCandidate,
    *,
    incoming_document: Document,
) -> PreflightCandidate:
    matched = _candidate_document(candidate.document_snapshot)
    replacement_eligible = (
        matched.id != incoming_document.id
        and matched.collection_key == incoming_document.collection_key
        and matched.state is DocumentState.READY
    )
    for score_name, score in (
        ("max_cosine", candidate.max_cosine),
        ("bm25_score", candidate.bm25_score),
        ("fused_score", candidate.fused_score),
    ):
        if not math.isfinite(score):
            raise SerializationError(f"candidate {score_name} is not finite")
    return PreflightCandidate(
        id=candidate.id,
        document=matched,
        source=candidate.source,
        rank=candidate.rank,
        reasons=list(candidate.reasons),
        max_cosine=candidate.max_cosine,
        bm25_score=candidate.bm25_score,
        fused_score=candidate.fused_score,
        matched_chunk_pair_count=_validate_matched_pairs(candidate.matched_chunk_pairs),
        replacement_eligible=replacement_eligible,
        evidence=[preflight_evidence(item) for item in candidate.evidence],
    )


def audit_event(event: AuditEvent) -> AuditEventResponse:
    try:
        return AuditEventResponse(
            id=event.id,
            document_id=event.document_id,
            operation_id=event.operation_id,
            event_type=event.event_type,
            actor_type=event.actor_type,
            actor_id=event.actor_id,
            occurred_at=_required_utc(event.occurred_at, "audit_event.occurred_at"),
            attributes=dict(event.details),
        )
    except (TypeError, ValueError) as exc:
        raise SerializationError(
            "audit event details are not content-free scalar metadata"
        ) from exc


def tombstone_summary(tombstone: Tombstone) -> TombstoneSummary:
    return TombstoneSummary(
        id=tombstone.id,
        document_id=tombstone.document_id,
        collection_key=tombstone.collection_key,
        disposition=tombstone.disposition,
        source_sha256=tombstone.source_sha256,
        manifest_sha256=tombstone.manifest_sha256,
        reason_code=tombstone.reason_code,
        actor_type=tombstone.actor_type,
        actor_id=tombstone.actor_id,
        occurred_at=_required_utc(tombstone.occurred_at, "tombstone.occurred_at"),
    )
