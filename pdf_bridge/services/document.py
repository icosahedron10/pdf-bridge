"""Document upload, content, and lifecycle use-case implementation."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from pdf_bridge.contracts.schemas import (
    DocumentMutationResponse,
    IdempotencyKey,
    UploadPreflightResponse,
    UploadResponse,
)
from pdf_bridge.core.config import CollectionDefinition, Settings
from pdf_bridge.persistence.models import Document, LanguageCode, OperationType, QueueOperation
from pdf_bridge.presentation.api_serializers import document_summary, duplicate_match
from pdf_bridge.services.catalog import (
    configured_collection,
    document_detail_record,
)
from pdf_bridge.services.errors import ServiceError
from pdf_bridge.services.lifecycle import (
    DuplicateDocumentError,
    LifecycleError,
    PossibleDuplicateError,
    UploadRegistration,
    can_serve_content,
    cancel_queued_document,
    finalize_cancelled_storage,
    find_preflight_duplicates,
    queue_classification_review,
    queue_document_deletion,
    register_staged_upload,
    retry_failed_document,
)
from pdf_bridge.services.scanner import Scanner, ScanResult
from pdf_bridge.services.storage import (
    AsyncReadable,
    InvalidFilenameError,
    StagedFile,
    StorageLayout,
    normalize_filename,
    resolve_storage_key,
    stream_upload,
    validate_pdf_filename,
)

logger = logging.getLogger(__name__)
_idempotency_adapter = TypeAdapter(IdempotencyKey)


@dataclass(frozen=True, slots=True)
class DocumentContent:
    """Canonical content metadata needed by an HTTP or job controller."""

    path: Path
    filename: str


@dataclass(frozen=True, slots=True)
class PreparedUpload:
    """A validated, staged, and scanned upload awaiting catalog registration."""

    staged: StagedFile
    layout: StorageLayout
    display_filename: str
    collection_key: str
    scan_result: ScanResult


def validate_idempotency_key(*, header_value: str | None, form_value: str | None) -> str:
    """Validate and reconcile the two supported idempotency-key inputs."""

    if header_value and form_value and header_value != form_value:
        raise ServiceError(
            "The header and form idempotency keys must be identical.",
            status=422,
            code="idempotency-key-mismatch",
            title="Idempotency keys did not match",
        )
    try:
        return _idempotency_adapter.validate_python(header_value or form_value)
    except ValidationError as exc:
        raise ServiceError(
            "Provide an 8–128 character Idempotency-Key header.",
            status=422,
            code="invalid-idempotency-key",
            title="Idempotency key was rejected",
        ) from exc


def preflight_upload(
    session: Session,
    *,
    definitions: Sequence[CollectionDefinition],
    filename: str,
    size_bytes: int,
    collection_key: str,
) -> UploadPreflightResponse:
    """Validate upload metadata and return possible catalog duplicates."""

    configured_collection(definitions, collection_key)
    try:
        normalized = normalize_filename(filename)
    except InvalidFilenameError as exc:
        raise ServiceError(
            str(exc),
            status=422,
            code="invalid-filename",
            title="Filename was rejected",
        ) from exc
    matches = find_preflight_duplicates(
        session,
        normalized_filename=normalized,
        size_bytes=size_bytes,
    )
    return UploadPreflightResponse(
        normalized_filename=normalized,
        requires_confirmation=bool(matches),
        possible_duplicates=[duplicate_match(document) for document in matches],
    )


async def prepare_upload(
    *,
    settings: Settings,
    scanner: Scanner,
    file: AsyncReadable,
    filename: str,
    content_type: str | None,
    collection_key: str,
) -> PreparedUpload:
    """Validate, stream, and scan an upload without opening a database transaction."""

    configured_collection(settings.collections, collection_key)
    try:
        display_filename = validate_pdf_filename(filename)
    except InvalidFilenameError as exc:
        raise ServiceError(
            str(exc),
            status=422,
            code="invalid-filename",
            title="Filename was rejected",
        ) from exc
    if content_type and content_type.casefold() not in {
        "application/pdf",
        "application/octet-stream",
    }:
        raise ServiceError(
            "The upload must use the application/pdf content type.",
            status=422,
            code="invalid-content-type",
            title="File type was rejected",
        )

    layout = StorageLayout.from_root(settings.storage_root)
    staged = await stream_upload(
        file,
        layout,
        max_bytes=settings.max_upload_bytes,
        chunk_bytes=settings.upload_chunk_bytes,
    )
    try:
        scan_result = await asyncio.to_thread(scanner, staged.path)
    except Exception:
        staged.path.unlink(missing_ok=True)
        raise
    return PreparedUpload(
        staged=staged,
        layout=layout,
        display_filename=display_filename,
        collection_key=collection_key,
        scan_result=scan_result,
    )


def register_upload(
    session: Session,
    *,
    prepared: PreparedUpload,
    possible_duplicate_confirmed: bool,
    idempotency_key: str,
    actor_type: str,
    actor_id: str,
) -> UploadRegistration:
    """Apply the catalog mutation for a prepared upload without committing it."""

    return register_staged_upload(
        session,
        staged=prepared.staged,
        layout=prepared.layout,
        filename=prepared.display_filename,
        collection_key=prepared.collection_key,
        idempotency_key=idempotency_key,
        actor_type=actor_type,
        actor_id=actor_id,
        scan_result=prepared.scan_result,
        allow_possible_duplicate=possible_duplicate_confirmed,
    )


def resolve_idempotency_conflict(
    session: Session,
    *,
    prepared: PreparedUpload,
    idempotency_key: str,
    cause: Exception,
) -> UploadResponse:
    """Resolve a commit race as an idempotent replay or a stable conflict."""

    existing = session.scalar(select(Document).where(Document.idempotency_key == idempotency_key))
    if existing is None:
        raise cause
    if (
        existing.sha256 != prepared.staged.sha256
        or existing.size_bytes != prepared.staged.size_bytes
        or existing.normalized_filename != normalize_filename(prepared.display_filename)
        or existing.collection_key != prepared.collection_key
    ):
        raise LifecycleError(
            "The idempotency key was already used for a different file.",
            code="idempotency-key-conflict",
        ) from cause
    operation = session.scalar(
        select(QueueOperation)
        .where(
            QueueOperation.document_id == existing.id,
            QueueOperation.operation_type == OperationType.INGEST,
        )
        .order_by(QueueOperation.attempt.desc(), QueueOperation.created_at.desc())
        .limit(1)
    )
    if operation is None:
        raise cause
    return UploadResponse(
        document=document_summary(existing),
        operation_id=operation.id,
        idempotent_replay=True,
    )


def upload_response(registration: UploadRegistration) -> UploadResponse:
    return UploadResponse(
        document=document_summary(registration.document),
        operation_id=registration.operation.id,
        idempotent_replay=registration.idempotent_replay,
    )


def content(session: Session, *, document_id: UUID, storage_root: Path) -> DocumentContent:
    document = session.get(Document, document_id)
    if document is None:
        raise ServiceError(
            "No catalog record exists for this document ID.",
            status=404,
            code="document-not-found",
            title="Document not found",
        )
    if not can_serve_content(document):
        raise ServiceError(
            "Only retained PDFs with a clean malware scan can be opened.",
            status=409,
            code="content-not-available",
            title="PDF content is not available",
        )
    layout = StorageLayout.from_root(storage_root)
    path = resolve_storage_key(layout, document.storage_key or "")
    if not path.is_file():
        raise ServiceError(
            "The catalog and canonical storage are inconsistent. Contact the operator.",
            status=500,
            code="stored-file-missing",
            title="Stored PDF is missing",
        )
    return DocumentContent(path=path, filename=document.original_filename)


def begin_queue_cancellation(
    session: Session,
    *,
    operation_id: UUID,
    actor_type: str,
    actor_id: str,
) -> tuple[Document, str | None]:
    """Record the durable cleanup-pending phase of a queue cancellation."""

    return cancel_queued_document(
        session,
        operation_id=operation_id,
        actor_type=actor_type,
        actor_id=actor_id,
    )


def remove_cancelled_storage(
    *,
    storage_root: Path,
    document_id: UUID,
    operation_id: UUID,
    storage_key: str,
    remove_file: Callable[..., None],
) -> None:
    """Remove canonical bytes for a cancellation already marked cleanup-pending."""

    layout = StorageLayout.from_root(storage_root)
    try:
        remove_file(layout, storage_key, missing_ok=True)
    except OSError as exc:
        logger.exception(
            "canonical cleanup failed",
            extra={"document_id": str(document_id), "operation_id": str(operation_id)},
        )
        raise ServiceError(
            "The cleanup remains pending and this cancellation can be retried.",
            status=500,
            code="storage-cleanup-failed",
            title="Queue cancellation cleanup failed",
        ) from exc


def finish_queue_cancellation(
    session: Session,
    *,
    document_id: UUID,
    storage_key: str,
    actor_type: str,
    actor_id: str,
) -> Document:
    """Apply the catalog mutation after canonical cancellation bytes are gone."""

    return finalize_cancelled_storage(
        session,
        document_id=document_id,
        storage_key=storage_key,
        actor_type=actor_type,
        actor_id=actor_id,
    )


def mutation_response(
    document: Document, *, operation_id: UUID | None = None
) -> DocumentMutationResponse:
    return DocumentMutationResponse(
        document=document_summary(document),
        operation_id=operation_id,
    )


def retry_queue_item(
    session: Session,
    *,
    operation_id: UUID,
    actor_type: str,
    actor_id: str,
) -> DocumentMutationResponse:
    operation = retry_failed_document(
        session,
        operation_id=operation_id,
        actor_type=actor_type,
        actor_id=actor_id,
    )
    return mutation_response(operation.document, operation_id=operation.id)


def resolve_classification(
    session: Session,
    *,
    definitions: Sequence[CollectionDefinition],
    document_id: UUID,
    collection_key: str | None,
    language: LanguageCode | None,
    reason: str | None,
    actor_type: str,
    actor_id: str,
) -> DocumentMutationResponse:
    existing = document_detail_record(session, document_id)
    resolved_collection_key = collection_key or existing.collection_key
    if resolved_collection_key is None:
        raise ServiceError(
            "Assign a configured collection before resolving this document.",
            status=422,
            code="collection-required",
            title="Collection is required",
        )
    configured_collection(definitions, resolved_collection_key)
    operation = queue_classification_review(
        session,
        document_id=document_id,
        collection_key=resolved_collection_key,
        language=language,
        reason=reason,
        actor_type=actor_type,
        actor_id=actor_id,
    )
    return mutation_response(operation.document, operation_id=operation.id)


def request_deletion(
    session: Session,
    *,
    document_id: UUID,
    actor_type: str,
    actor_id: str,
    reason: str | None,
) -> DocumentMutationResponse:
    operation = queue_document_deletion(
        session,
        document_id=document_id,
        actor_type=actor_type,
        actor_id=actor_id,
        reason=reason,
    )
    return mutation_response(operation.document, operation_id=operation.id)


def duplicate_error_extra(exc: LifecycleError) -> dict | None:
    """Serialize duplicate-specific lifecycle details for the HTTP error contract."""

    if isinstance(exc, DuplicateDocumentError):
        return {"duplicate": duplicate_match(exc.document).model_dump(mode="json")}
    if isinstance(exc, PossibleDuplicateError):
        return {
            "possible_duplicates": [
                duplicate_match(document).model_dump(mode="json") for document in exc.documents
            ]
        }
    return None
