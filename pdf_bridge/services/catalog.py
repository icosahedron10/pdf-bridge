"""Read-only API-v2 catalog queries and opaque cursor pagination."""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Generic, TypeVar

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, selectinload

from pdf_bridge.core.config import CollectionDefinition
from pdf_bridge.persistence.models import (
    TERMINAL_DOCUMENT_STATES,
    AuditEvent,
    Decision,
    DeletionProgress,
    Document,
    DocumentState,
    OperationState,
    PreparedChunk,
    PreparedRevision,
    PublicationRecord,
    TerminalDisposition,
    Tombstone,
    WorkOperation,
)
from pdf_bridge.services.document import configured_collection
from pdf_bridge.services.intake import (
    LifecycleError,
    can_serve_prepared_content,
    latest_sealed_revision,
)

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class CursorPage(Generic[T]):
    items: list[T]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class CollectionRecord:
    definition: CollectionDefinition
    counts: dict[str, int]


@dataclass(frozen=True, slots=True)
class DocumentAggregate:
    document: Document
    operation: WorkOperation | None
    revision: PreparedRevision | None
    decision: Decision | None
    publication: PublicationRecord | None
    deletion: DeletionProgress | None


@dataclass(frozen=True, slots=True)
class MarkdownRecord:
    prepared_revision: PreparedRevision
    markdown: str


def _encode_cursor(timestamp: datetime, identifier: str) -> str:
    aware = timestamp if timestamp.tzinfo is not None else timestamp.replace(tzinfo=UTC)
    payload = json.dumps(
        {"v": 1, "t": aware.astimezone(UTC).isoformat(), "id": identifier},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str, *, uuid_identifier: bool) -> tuple[datetime, str]:
    if not cursor or len(cursor) > 500:
        raise LifecycleError("invalid_cursor", "The pagination cursor is invalid.", status=400)
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        if set(payload) != {"v", "t", "id"} or payload["v"] != 1:
            raise ValueError
        timestamp = datetime.fromisoformat(str(payload["t"]).replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            raise ValueError
        identifier = str(payload["id"])
        if uuid_identifier:
            identifier = str(uuid.UUID(identifier))
        elif not identifier.isdecimal():
            raise ValueError
    except (
        binascii.Error,
        ValueError,
        TypeError,
        KeyError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        raise LifecycleError(
            "invalid_cursor", "The pagination cursor is invalid.", status=400
        ) from exc
    return timestamp.astimezone(UTC), identifier


def _validate_limit(limit: int) -> None:
    if not 1 <= limit <= 100:
        raise LifecycleError(
            "invalid_limit", "Pagination limit must be between 1 and 100.", status=400
        )


def list_collections(
    session: Session,
    definitions: tuple[CollectionDefinition, ...] | list[CollectionDefinition],
) -> list[CollectionRecord]:
    """Return enabled deployment collections with exact catalog state counts."""

    rows = session.execute(
        select(Document.collection_key, Document.state, func.count(Document.id)).group_by(
            Document.collection_key, Document.state
        )
    ).all()
    counts_by_collection: dict[str, dict[str, int]] = {}
    for collection_key, state, count in rows:
        counts_by_collection.setdefault(collection_key, {})[state.value] = int(count)
    return [
        CollectionRecord(
            definition=definition,
            counts={
                state.value: counts_by_collection.get(definition.key, {}).get(state.value, 0)
                for state in DocumentState
            },
        )
        for definition in definitions
        if definition.enabled
    ]


def get_collection(
    session: Session,
    definitions: tuple[CollectionDefinition, ...] | list[CollectionDefinition],
    key: str,
) -> CollectionRecord:
    definition = configured_collection(definitions, key)
    return next(item for item in list_collections(session, [definition]))


def list_documents(
    session: Session,
    *,
    collection_key: str,
    state: DocumentState | None,
    cursor: str | None,
    limit: int,
) -> CursorPage[Document]:
    """List nonterminal collection documents newest-first with a stable tie-breaker."""

    _validate_limit(limit)
    statement = select(Document).where(
        Document.collection_key == collection_key,
        Document.state.not_in(TERMINAL_DOCUMENT_STATES),
    )
    if state is not None:
        if state in TERMINAL_DOCUMENT_STATES:
            return CursorPage(items=[], next_cursor=None)
        statement = statement.where(Document.state == state)
    if cursor:
        timestamp, identifier = _decode_cursor(cursor, uuid_identifier=True)
        document_id = uuid.UUID(identifier)
        statement = statement.where(
            or_(
                Document.created_at < timestamp,
                and_(Document.created_at == timestamp, Document.id < document_id),
            )
        )
    items = list(
        session.scalars(
            statement.order_by(Document.created_at.desc(), Document.id.desc()).limit(limit + 1)
        ).all()
    )
    has_more = len(items) > limit
    items = items[:limit]
    next_cursor = (
        _encode_cursor(items[-1].created_at, str(items[-1].id)) if has_more and items else None
    )
    return CursorPage(items=items, next_cursor=next_cursor)


def document_aggregate(session: Session, document_id: uuid.UUID) -> DocumentAggregate:
    document = session.scalar(
        select(Document)
        .where(Document.id == document_id)
        .options(
            selectinload(Document.operations),
            selectinload(Document.prepared_revisions).selectinload(
                PreparedRevision.decision
            ),
            selectinload(Document.prepared_revisions).selectinload(
                PreparedRevision.publication
            ),
            selectinload(Document.deletion_progress),
            selectinload(Document.tombstone),
        )
    )
    if document is None:
        raise LifecycleError("document_not_found", "The document was not found.", status=404)
    revision = max(
        (item for item in document.prepared_revisions if item.status.value == "SEALED"),
        key=lambda item: item.revision_number,
        default=None,
    )
    operations = list(document.operations)
    replacement_operation = session.scalar(
        select(WorkOperation)
        .where(WorkOperation.replacement_target_document_id == document.id)
        .order_by(WorkOperation.created_at.desc(), WorkOperation.id.desc())
        .limit(1)
    )
    if replacement_operation is not None and all(
        item.id != replacement_operation.id for item in operations
    ):
        operations.append(replacement_operation)
    operation = max(
        operations, key=lambda item: (item.created_at, str(item.id)), default=None
    )
    return DocumentAggregate(
        document=document,
        operation=operation,
        revision=revision,
        decision=revision.decision if revision is not None else None,
        publication=revision.publication if revision is not None else None,
        deletion=document.deletion_progress,
    )


def markdown_record(session: Session, document_id: uuid.UUID) -> MarkdownRecord:
    document = session.get(Document, document_id)
    if document is None:
        raise LifecycleError("document_not_found", "The document was not found.", status=404)
    if not can_serve_prepared_content(document):
        if document.state in TERMINAL_DOCUMENT_STATES or document.state in {
            DocumentState.DELETING,
            DocumentState.DELETE_FAILED,
        }:
            raise LifecycleError("content_purged", "Prepared content is unavailable.", status=410)
        raise LifecycleError(
            "artifact_not_ready", "Canonical Markdown is not ready.", status=409
        )
    revision = latest_sealed_revision(session, document_id)
    if revision is None or not revision.prepared_pages:
        raise LifecycleError(
            "artifact_not_ready", "Canonical Markdown is not ready.", status=409
        )
    pages = sorted(revision.prepared_pages, key=lambda item: item.page_number)
    markdown = "\n\n".join(
        f"<!-- page:{page.page_number} -->\n\n{page.markdown}" for page in pages
    )
    return MarkdownRecord(prepared_revision=revision, markdown=markdown)


def list_chunks(
    session: Session,
    *,
    document_id: uuid.UUID,
    cursor: str | None,
    limit: int,
) -> CursorPage[PreparedChunk]:
    """Return stable chunk-index pages without ever loading vector rows."""

    _validate_limit(limit)
    document = session.get(Document, document_id)
    if document is None:
        raise LifecycleError("document_not_found", "The document was not found.", status=404)
    if not can_serve_prepared_content(document):
        if document.state in TERMINAL_DOCUMENT_STATES or document.state in {
            DocumentState.DELETING,
            DocumentState.DELETE_FAILED,
        }:
            raise LifecycleError("content_purged", "Prepared content is unavailable.", status=410)
        raise LifecycleError("artifact_not_ready", "Chunks are not ready.", status=409)
    revision = latest_sealed_revision(session, document_id)
    if revision is None:
        raise LifecycleError("artifact_not_ready", "Chunks are not ready.", status=409)
    after_index = -1
    if cursor:
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
            if set(payload) != {"v", "revision_id", "chunk_index"} or payload["v"] != 1:
                raise ValueError
            if uuid.UUID(str(payload["revision_id"])) != revision.id:
                raise ValueError
            after_index = int(payload["chunk_index"])
            if after_index < 0:
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
    items = list(
        session.scalars(
            select(PreparedChunk)
            .where(
                PreparedChunk.prepared_revision_id == revision.id,
                PreparedChunk.chunk_index > after_index,
            )
            .order_by(PreparedChunk.chunk_index.asc())
            .limit(limit + 1)
        ).all()
    )
    has_more = len(items) > limit
    items = items[:limit]
    next_cursor = None
    if has_more and items:
        payload = json.dumps(
            {
                "v": 1,
                "revision_id": str(revision.id),
                "chunk_index": items[-1].chunk_index,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        next_cursor = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return CursorPage(items=items, next_cursor=next_cursor)
def preflight_revision(session: Session, document_id: uuid.UUID) -> PreparedRevision:
    document = session.get(Document, document_id)
    if document is None:
        raise LifecycleError("document_not_found", "The document was not found.", status=404)
    if document.state in TERMINAL_DOCUMENT_STATES or document.state in {
        DocumentState.DELETING,
        DocumentState.DELETE_FAILED,
    }:
        raise LifecycleError("content_purged", "Preflight evidence was purged.", status=410)
    revision = latest_sealed_revision(session, document_id)
    if revision is None:
        raise LifecycleError(
            "artifact_not_ready", "Preflight evidence is not ready.", status=409
        )
    return revision


def list_events(
    session: Session,
    *,
    document_id: uuid.UUID,
    cursor: str | None,
    limit: int,
) -> CursorPage[AuditEvent]:
    _validate_limit(limit)
    if session.get(Document, document_id) is None:
        raise LifecycleError("document_not_found", "The document was not found.", status=404)
    statement = select(AuditEvent).where(AuditEvent.document_id == document_id)
    if cursor:
        timestamp, identifier = _decode_cursor(cursor, uuid_identifier=False)
        event_id = int(identifier)
        statement = statement.where(
            or_(
                AuditEvent.occurred_at < timestamp,
                and_(AuditEvent.occurred_at == timestamp, AuditEvent.id < event_id),
            )
        )
    items = list(
        session.scalars(
            statement.order_by(AuditEvent.occurred_at.desc(), AuditEvent.id.desc()).limit(
                limit + 1
            )
        ).all()
    )
    has_more = len(items) > limit
    items = items[:limit]
    next_cursor = (
        _encode_cursor(items[-1].occurred_at, str(items[-1].id))
        if has_more and items
        else None
    )
    return CursorPage(items=items, next_cursor=next_cursor)


def get_operation(session: Session, operation_id: uuid.UUID) -> WorkOperation:
    operation = session.get(WorkOperation, operation_id)
    if operation is None:
        raise LifecycleError("operation_not_found", "The operation was not found.", status=404)
    return operation


def operation_metric_rows(
    session: Session,
    *,
    visible_collection_keys: tuple[str, ...],
) -> list[tuple[object, ...]]:
    """Aggregate non-succeeded operations without loading document content."""

    if not visible_collection_keys:
        return []
    statement = (
        select(
            WorkOperation.operation_type,
            WorkOperation.state,
            WorkOperation.phase,
            func.count(WorkOperation.id),
            func.min(WorkOperation.created_at),
            func.min(WorkOperation.phase_started_at),
        )
        .join(Document, Document.id == WorkOperation.document_id)
        .where(
            Document.collection_key.in_(visible_collection_keys),
            WorkOperation.state.in_(
                (
                    OperationState.QUEUED,
                    OperationState.RUNNING,
                    OperationState.FAILED,
                )
            ),
        )
        .group_by(
            WorkOperation.operation_type,
            WorkOperation.state,
            WorkOperation.phase,
        )
        .order_by(
            WorkOperation.state,
            WorkOperation.operation_type,
            WorkOperation.phase,
        )
    )
    return [tuple(row) for row in session.execute(statement).all()]


def queue_position(session: Session, operation: WorkOperation) -> int | None:
    if operation.state.value != "QUEUED":
        return None
    ahead = session.scalar(
        select(func.count(WorkOperation.id)).where(
            WorkOperation.state == operation.state,
            or_(
                WorkOperation.priority < operation.priority,
                and_(
                    WorkOperation.priority == operation.priority,
                    or_(
                        WorkOperation.created_at < operation.created_at,
                        and_(
                            WorkOperation.created_at == operation.created_at,
                            WorkOperation.id < operation.id,
                        ),
                    ),
                ),
            ),
        )
    )
    return int(ahead or 0) + 1


def list_history(
    session: Session,
    *,
    visible_collection_keys: tuple[str, ...],
    collection_key: str | None,
    disposition: TerminalDisposition | None,
    cursor: str | None,
    limit: int,
) -> CursorPage[Tombstone]:
    _validate_limit(limit)
    if not visible_collection_keys:
        raise LifecycleError(
            "collection_not_found", "No configured collection is visible.", status=404
        )
    statement = select(Tombstone).where(
        Tombstone.collection_key.in_(visible_collection_keys)
    )
    if collection_key is not None:
        statement = statement.where(Tombstone.collection_key == collection_key)
    if disposition is not None:
        statement = statement.where(Tombstone.disposition == disposition)
    if cursor:
        timestamp, identifier = _decode_cursor(cursor, uuid_identifier=True)
        tombstone_id = uuid.UUID(identifier)
        statement = statement.where(
            or_(
                Tombstone.occurred_at < timestamp,
                and_(Tombstone.occurred_at == timestamp, Tombstone.id < tombstone_id),
            )
        )
    items = list(
        session.scalars(
            statement.order_by(Tombstone.occurred_at.desc(), Tombstone.id.desc()).limit(limit + 1)
        ).all()
    )
    has_more = len(items) > limit
    items = items[:limit]
    next_cursor = (
        _encode_cursor(items[-1].occurred_at, str(items[-1].id))
        if has_more and items
        else None
    )
    return CursorPage(items=items, next_cursor=next_cursor)
