"""API-v2 read coordinators over the authoritative catalog query service."""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from pdf_bridge.contracts.schemas import (
    ChunkListResponse,
    CollectionDetail,
    CollectionListResponse,
    CollectionPhysicalTarget,
    DocumentDetail,
    DocumentListResponse,
    EventListResponse,
    HistoryResponse,
    MarkdownDocument,
    OperationDetail,
    OperationMetricBucket,
    OperationMetricsResponse,
    PreflightCandidatePage,
    PreflightResponse,
    SourceMetadata,
)
from pdf_bridge.core.config import CollectionDefinition
from pdf_bridge.persistence.models import (
    Decision,
    DecisionAction,
    Document,
    DocumentState,
    OperationState,
    TerminalDisposition,
    WorkOperation,
)
from pdf_bridge.presentation import api_serializers
from pdf_bridge.services import catalog
from pdf_bridge.services import document as document_service
from pdf_bridge.services.intake import LifecycleError


def _validate_limit(limit: int) -> None:
    if not 1 <= limit <= 100:
        raise LifecycleError(
            "invalid_limit",
            "Pagination limit must be between 1 and 100.",
            status=400,
        )


def _encode_cursor(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        {"v": 1, **payload},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str, *, fields: set[str]) -> dict[str, object]:
    if not cursor or len(cursor) > 2_048:
        raise LifecycleError("invalid_cursor", "The pagination cursor is invalid.", status=400)
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        if not isinstance(payload, dict) or set(payload) != {"v", *fields}:
            raise ValueError
        if payload["v"] != 1:
            raise ValueError
    except (
        binascii.Error,
        ValueError,
        TypeError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        raise LifecycleError(
            "invalid_cursor", "The pagination cursor is invalid.", status=400
        ) from exc
    return payload


def list_collections(
    session: Session,
    definitions: Sequence[CollectionDefinition],
    *,
    cursor: str | None,
    limit: int,
) -> CollectionListResponse:
    """Return one stable configuration-ordered page of enabled collections."""

    _validate_limit(limit)
    records = catalog.list_collections(session, list(definitions))
    start = 0
    if cursor is not None:
        payload = _decode_cursor(cursor, fields={"kind", "after"})
        if payload["kind"] != "collections" or not isinstance(payload["after"], str):
            raise LifecycleError(
                "invalid_cursor", "The pagination cursor is invalid.", status=400
            )
        positions = {
            record.definition.key: index for index, record in enumerate(records)
        }
        try:
            start = positions[payload["after"]] + 1
        except KeyError as exc:
            raise LifecycleError(
                "invalid_cursor", "The pagination cursor is invalid.", status=400
            ) from exc

    selected = records[start : start + limit]
    has_more = start + len(selected) < len(records)
    next_cursor = (
        _encode_cursor(
            {"kind": "collections", "after": selected[-1].definition.key}
        )
        if has_more and selected
        else None
    )
    return CollectionListResponse(
        items=[
            api_serializers.collection_summary(record.definition, record.counts)
            for record in selected
        ],
        limit=limit,
        next_cursor=next_cursor,
        has_more=has_more,
    )


def get_collection(
    session: Session,
    definitions: Sequence[CollectionDefinition],
    key: str,
    *,
    target: CollectionPhysicalTarget,
) -> CollectionDetail:
    """Combine configured display metadata, counts, and probed target status."""

    record = catalog.get_collection(session, list(definitions), key)
    if target.qdrant_collection_name != record.definition.qdrant_collection_name:
        raise api_serializers.SerializationError(
            "collection status describes a different physical Qdrant target"
        )
    summary = api_serializers.collection_summary(record.definition, record.counts)
    return CollectionDetail(**summary.model_dump(), target=target)


def list_documents(
    session: Session,
    *,
    definitions: Sequence[CollectionDefinition],
    collection_key: str,
    state: DocumentState | None,
    cursor: str | None,
    limit: int,
) -> DocumentListResponse:
    """Return current collection documents without terminal history rows."""

    catalog.get_collection(session, list(definitions), collection_key)
    page = catalog.list_documents(
        session,
        collection_key=collection_key,
        state=state,
        cursor=cursor,
        limit=limit,
    )
    return DocumentListResponse(
        items=[api_serializers.document_summary(item) for item in page.items],
        limit=limit,
        next_cursor=page.next_cursor,
        has_more=page.next_cursor is not None,
    )


def _replacement_summary(
    session: Session,
    *,
    document: Document,
    decision: Decision | None,
):
    if decision is not None and decision.action is DecisionAction.REPLACE:
        if decision.target_document_id is None:
            raise api_serializers.SerializationError(
                "replacement decision is missing its old document"
            )
        new_document = document
        old_document = session.get(Document, decision.target_document_id)
        if old_document is None:
            raise api_serializers.SerializationError(
                "replacement decision references a missing old document"
            )
    elif document.replaced_by_document_id is not None:
        old_document = document
        new_document = session.get(Document, document.replaced_by_document_id)
        if new_document is None:
            raise api_serializers.SerializationError(
                "replacement linkage references a missing incoming document"
            )
        decision = session.scalar(
            select(Decision).where(
                Decision.document_id == new_document.id,
                Decision.action == DecisionAction.REPLACE,
                Decision.target_document_id == old_document.id,
            )
        )
        if decision is None:
            raise api_serializers.SerializationError(
                "replacement linkage has no immutable decision"
            )
    else:
        return None

    operation = session.scalar(
        select(WorkOperation)
        .where(
            WorkOperation.document_id == new_document.id,
            WorkOperation.replacement_target_document_id == old_document.id,
        )
        .order_by(WorkOperation.created_at.desc(), WorkOperation.id.desc())
        .limit(1)
    )
    if operation is None:
        raise api_serializers.SerializationError(
            "replacement decision has no durable replacement operation"
        )
    return api_serializers.replacement_summary(
        decision=decision,
        old_document=old_document,
        new_document=new_document,
        operation=operation,
    )


def get_document(session: Session, document_id: uuid.UUID) -> DocumentDetail:
    """Return one composed lifecycle view without protected artifacts."""

    aggregate = catalog.document_aggregate(session, document_id)
    replacement = _replacement_summary(
        session,
        document=aggregate.document,
        decision=aggregate.decision,
    )
    return api_serializers.document_detail(
        aggregate.document,
        operation=aggregate.operation,
        revision=aggregate.revision,
        decision=aggregate.decision,
        publication=aggregate.publication,
        deletion=aggregate.deletion,
        replacement=replacement,
    )


def get_source_metadata(session: Session, document_id: uuid.UUID) -> SourceMetadata:
    """Return immutable source facts while keeping paths and scanner internals private."""

    aggregate = catalog.document_aggregate(session, document_id)
    return api_serializers.source_metadata(aggregate.document)


def get_markdown(session: Session, document_id: uuid.UUID) -> MarkdownDocument:
    """Return validated canonical Markdown and page provenance."""

    record = catalog.markdown_record(session, document_id)
    return api_serializers.markdown_document(record.prepared_revision, record.markdown)


def list_chunks(
    session: Session,
    *,
    document_id: uuid.UUID,
    cursor: str | None,
    limit: int,
) -> ChunkListResponse:
    """Return public chunk material without loading or exposing numeric vectors."""

    page = catalog.list_chunks(
        session,
        document_id=document_id,
        cursor=cursor,
        limit=limit,
    )
    if not page.items:
        revision = catalog.preflight_revision(session, document_id)
        revision_id = revision.id
    else:
        revision_id = page.items[0].prepared_revision_id
        if any(item.prepared_revision_id != revision_id for item in page.items):
            raise api_serializers.SerializationError(
                "chunk page crosses prepared revision boundaries"
            )
    return ChunkListResponse(
        document_id=document_id,
        prepared_revision_id=revision_id,
        items=[api_serializers.chunk(item) for item in page.items],
        limit=limit,
        next_cursor=page.next_cursor,
        has_more=page.next_cursor is not None,
    )


def _candidate_page_start(
    cursor: str | None,
    *,
    revision_id: uuid.UUID,
    candidates: list,
) -> int:
    if cursor is None:
        return 0
    payload = _decode_cursor(cursor, fields={"kind", "revision_id", "rank", "id"})
    try:
        cursor_revision_id = uuid.UUID(str(payload["revision_id"]))
        candidate_id = uuid.UUID(str(payload["id"]))
        rank = int(payload["rank"])
    except (TypeError, ValueError) as exc:
        raise LifecycleError(
            "invalid_cursor", "The pagination cursor is invalid.", status=400
        ) from exc
    if payload["kind"] != "preflight" or cursor_revision_id != revision_id or rank < 1:
        raise LifecycleError("invalid_cursor", "The pagination cursor is invalid.", status=400)
    for index, candidate in enumerate(candidates):
        if candidate.id == candidate_id and candidate.rank == rank:
            return index + 1
    raise LifecycleError("invalid_cursor", "The pagination cursor is invalid.", status=400)


def get_preflight(
    session: Session,
    *,
    document_id: uuid.UUID,
    cursor: str | None,
    limit: int,
) -> PreflightResponse:
    """Return one revision-bound page of retained preflight candidates."""

    _validate_limit(limit)
    revision = catalog.preflight_revision(session, document_id)
    incoming = session.get(Document, document_id)
    if incoming is None or revision.document_id != incoming.id:
        raise api_serializers.SerializationError(
            "prepared revision is not bound to the requested document"
        )
    candidates = sorted(revision.candidates, key=lambda item: (item.rank, str(item.id)))
    start = _candidate_page_start(
        cursor,
        revision_id=revision.id,
        candidates=candidates,
    )
    selected = candidates[start : start + limit]
    has_more = start + len(selected) < len(candidates)
    next_cursor = (
        _encode_cursor(
            {
                "kind": "preflight",
                "revision_id": str(revision.id),
                "rank": selected[-1].rank,
                "id": str(selected[-1].id),
            }
        )
        if has_more and selected
        else None
    )
    completeness = api_serializers.preflight_completeness(revision)
    return PreflightResponse(
        document_id=document_id,
        prepared_revision=api_serializers.prepared_revision_summary(
            revision,
            document=incoming,
        ),
        completeness=completeness,
        candidate_count=len(candidates),
        candidates=PreflightCandidatePage(
            items=[
                api_serializers.preflight_candidate(item, incoming_document=incoming)
                for item in selected
            ],
            limit=limit,
            next_cursor=next_cursor,
            has_more=has_more,
        ),
    )


def list_events(
    session: Session,
    *,
    document_id: uuid.UUID,
    cursor: str | None,
    limit: int,
) -> EventListResponse:
    """Return a content-free page of lifecycle audit events."""

    page = catalog.list_events(
        session,
        document_id=document_id,
        cursor=cursor,
        limit=limit,
    )
    return EventListResponse(
        document_id=document_id,
        items=[api_serializers.audit_event(item) for item in page.items],
        limit=limit,
        next_cursor=page.next_cursor,
        has_more=page.next_cursor is not None,
    )


def get_operation(
    session: Session,
    operation_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> OperationDetail:
    """Return queue position, phase age, and a sanitized failure when present."""

    operation = catalog.get_operation(session, operation_id)
    return api_serializers.operation_detail(
        operation,
        queue_position=catalog.queue_position(session, operation),
        now=now or datetime.now(UTC),
    )


def operation_metrics(
    session: Session,
    *,
    definitions: Sequence[CollectionDefinition],
    now: datetime | None = None,
) -> OperationMetricsResponse:
    """Return bounded queue and phase aggregates for visible collections."""

    generated_at = now or datetime.now(UTC)
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=UTC)
    generated_at = generated_at.astimezone(UTC)
    visible_collection_keys = tuple(
        definition.key for definition in definitions if definition.enabled
    )
    rows = catalog.operation_metric_rows(
        session, visible_collection_keys=visible_collection_keys
    )
    buckets: list[OperationMetricBucket] = []
    state_counts = {state: 0 for state in OperationState}
    queued_ages: list[float] = []
    for operation_type, state, phase, count, oldest_created, oldest_phase_started in rows:
        created_at = oldest_created
        phase_started_at = oldest_phase_started
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        if phase_started_at.tzinfo is None:
            phase_started_at = phase_started_at.replace(tzinfo=UTC)
        operation_age = max(
            (generated_at - created_at.astimezone(UTC)).total_seconds(), 0.0
        )
        phase_age = max(
            (generated_at - phase_started_at.astimezone(UTC)).total_seconds(), 0.0
        )
        bucket = OperationMetricBucket(
            operation_type=operation_type,
            state=state,
            phase=phase,
            count=int(count),
            oldest_operation_age_seconds=operation_age,
            oldest_phase_age_seconds=phase_age,
        )
        buckets.append(bucket)
        state_counts[state] += bucket.count
        if state is OperationState.QUEUED:
            queued_ages.append(operation_age)
    return OperationMetricsResponse(
        generated_at=generated_at,
        total=sum(bucket.count for bucket in buckets),
        queued=state_counts[OperationState.QUEUED],
        running=state_counts[OperationState.RUNNING],
        failed=state_counts[OperationState.FAILED],
        oldest_queued_age_seconds=max(queued_ages) if queued_ages else None,
        buckets=buckets,
    )


def list_history(
    session: Session,
    *,
    definitions: Sequence[CollectionDefinition],
    collection_key: str | None,
    disposition: TerminalDisposition | None,
    cursor: str | None,
    limit: int,
) -> HistoryResponse:
    """Return terminal content-free tombstones, never deleted artifacts."""

    visible_collection_keys = tuple(
        definition.key for definition in definitions if definition.enabled
    )
    if collection_key is not None:
        document_service.configured_collection(list(definitions), collection_key)
    page = catalog.list_history(
        session,
        visible_collection_keys=visible_collection_keys,
        collection_key=collection_key,
        disposition=disposition,
        cursor=cursor,
        limit=limit,
    )
    return HistoryResponse(
        items=[api_serializers.tombstone_summary(item) for item in page.items],
        limit=limit,
        next_cursor=page.next_cursor,
        has_more=page.next_cursor is not None,
    )
