"""Document upload, content, and decision use-case implementation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from pdf_bridge.contracts.schemas import (
    IdempotencyKey,
    UploadAcceptedResponse,
    UploadPreflightResponse,
)
from pdf_bridge.core.config import CollectionDefinition, Settings
from pdf_bridge.persistence.models import Document, OperationType
from pdf_bridge.presentation.api_serializers import (
    duplicate_match,
    filename_warning,
    upload_resource,
)
from pdf_bridge.services.catalog import configured_collection
from pdf_bridge.services.errors import ServiceError
from pdf_bridge.services.intake import (
    DuplicateDocumentError,
    LifecycleError,
    UploadRegistration,
    find_filename_warnings,
    register_staged_upload,
)
from pdf_bridge.services.scanner import Scanner, ScanResult
from pdf_bridge.services.storage import (
    BinaryReadable,
    InvalidFilenameError,
    StagedFile,
    StorageLayout,
    normalize_filename,
    resolve_storage_key,
    stream_upload,
    validate_pdf_filename,
)

_idempotency_adapter = TypeAdapter(IdempotencyKey)


@dataclass(frozen=True, slots=True)
class DocumentContent:
    """Canonical content metadata needed by an HTTP controller."""

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
    definitions: list[CollectionDefinition],
    filename: str,
    size_bytes: int,  # part of the stable request contract; warnings are name-based
    collection_key: str,
) -> UploadPreflightResponse:
    """Validate upload metadata and return typed advisory filename warnings."""

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
    warnings = find_filename_warnings(
        session, collection_key=collection_key, filename=filename
    )
    return UploadPreflightResponse(
        normalized_filename=normalized,
        warnings=[filename_warning(document, match) for document, match in warnings],
    )


def prepare_upload(
    *,
    settings: Settings,
    scanner: Scanner,
    file: BinaryReadable,
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
    staged = stream_upload(
        file,
        layout,
        max_bytes=settings.max_upload_bytes,
        chunk_bytes=settings.upload_chunk_bytes,
    )
    try:
        scan_result = scanner(staged.path)
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
    )


def discard_promoted_upload(registration: UploadRegistration | None) -> None:
    """Remove canonical bytes promoted by an upload whose transaction failed."""

    if registration is None or registration.promoted is None:
        return
    registration.promoted.path.unlink(missing_ok=True)


def resolve_idempotency_conflict(
    session: Session,
    *,
    prepared: PreparedUpload,
    idempotency_key: str,
    cause: Exception,
) -> UploadAcceptedResponse:
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
    operation = next(
        (
            item
            for item in sorted(
                existing.operations, key=lambda op: (op.attempt, op.created_at), reverse=True
            )
            if item.operation_type == OperationType.ANALYZE
        ),
        None,
    )
    if operation is None:
        raise cause
    return UploadAcceptedResponse(
        upload=upload_resource(
            existing,
            operation=operation,
            analysis=None,
            replacement=None,
            decision=None,
        ),
        idempotent_replay=True,
    )


def upload_accepted_response(registration: UploadRegistration) -> UploadAcceptedResponse:
    """Serialize a completed upload registration for the 202 response."""

    return UploadAcceptedResponse(
        upload=upload_resource(
            registration.document,
            operation=registration.operation,
            analysis=None,
            replacement=None,
            decision=None,
        ),
        idempotent_replay=registration.idempotent_replay,
    )


def content(session: Session, *, document_id: UUID, storage_root: Path) -> DocumentContent:
    """Resolve clean, retained document content from canonical storage."""

    from pdf_bridge.services.intake import can_serve_content

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


def duplicate_error_extra(exc: LifecycleError) -> dict | None:
    """Serialize duplicate-specific lifecycle details for the HTTP error contract."""

    if isinstance(exc, DuplicateDocumentError):
        return {"duplicate": duplicate_match(exc.document).model_dump(mode="json")}
    return None
