"""Admission, filename advisory, and protected source access for API v2."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from pdf_bridge.core.config import CollectionDefinition, Settings
from pdf_bridge.persistence.models import (
    TERMINAL_DOCUMENT_STATES,
    Document,
    DocumentState,
    OperationPriority,
    OperationType,
    ScanState,
    WorkOperation,
)
from pdf_bridge.presentation.api_serializers import upload_accepted_response
from pdf_bridge.services.filenames import compare_filenames, profile_filename
from pdf_bridge.services.intake import (
    IdempotencyReplay,
    LifecycleError,
    audit,
    complete_idempotency,
    enqueue_operation,
    reserve_idempotency,
)
from pdf_bridge.services.scanner import Scanner, ScanResult
from pdf_bridge.services.storage import (
    BinaryReadable,
    PromotedFile,
    StagedFile,
    StorageLayout,
    normalize_filename,
    promote_staged_file,
    resolve_storage_key,
    stream_upload,
    validate_pdf_filename,
)


@dataclass(frozen=True, slots=True)
class PreparedAdmission:
    """Bounded, validated, clean upload awaiting a short catalog transaction."""

    staged: StagedFile
    layout: StorageLayout
    filename: str
    normalized_filename: str
    collection: CollectionDefinition
    scan: ScanResult


@dataclass(frozen=True, slots=True)
class AdmissionOutcome:
    document: Document
    operation: WorkOperation
    promoted: PromotedFile
    response_body: dict[str, object]


@dataclass(frozen=True, slots=True)
class SourceContent:
    path: Path
    filename: str


@dataclass(frozen=True, slots=True)
class FilenameAdvisoryMatch:
    document_id: uuid.UUID
    original_filename: str
    state: DocumentState
    match_type: str
    score: float


def configured_collection(
    definitions: tuple[CollectionDefinition, ...] | list[CollectionDefinition],
    key: str,
) -> CollectionDefinition:
    for definition in definitions:
        if definition.key == key and definition.enabled:
            return definition
    raise LifecycleError(
        "collection_not_found", "The configured collection was not found.", status=404
    )


def prepare_admission(
    *,
    settings: Settings,
    scanner: Scanner,
    file: BinaryReadable,
    filename: str,
    content_type: str | None,
    collection_key: str,
) -> PreparedAdmission:
    """Stream, shape-check, and scan one upload outside a DB transaction."""

    collection = configured_collection(settings.collections, collection_key)
    display_filename = validate_pdf_filename(filename)
    if content_type is not None and content_type.casefold() != "application/pdf":
        raise LifecycleError(
            "unsupported_media_type",
            "The upload must use the application/pdf media type.",
            status=415,
        )
    layout = StorageLayout.from_root(settings.storage_root)
    staged = stream_upload(
        file,
        layout,
        max_bytes=settings.max_upload_bytes,
        chunk_bytes=settings.upload_chunk_bytes,
    )
    try:
        scan = scanner(staged.path)
        if scan.state == ScanState.INFECTED:
            raise LifecycleError(
                "malware_detected", "The upload was rejected by malware screening.", status=422
            )
        if scan.state != ScanState.CLEAN:
            raise LifecycleError(
                "scanner_incomplete",
                "Malware screening did not produce a clean result.",
                status=503,
                retryable=True,
            )
    except Exception:
        staged.path.unlink(missing_ok=True)
        raise
    return PreparedAdmission(
        staged=staged,
        layout=layout,
        filename=display_filename,
        normalized_filename=normalize_filename(display_filename),
        collection=collection,
        scan=scan,
    )


def discard_prepared_admission(prepared: PreparedAdmission) -> None:
    prepared.staged.path.unlink(missing_ok=True)


def _find_exact_duplicate(
    session: Session,
    *,
    collection_key: str,
    sha256: str,
) -> Document | None:
    return session.scalar(
        select(Document)
        .where(
            Document.collection_key == collection_key,
            Document.sha256 == sha256,
            Document.state.not_in(TERMINAL_DOCUMENT_STATES),
        )
        .order_by(Document.created_at.asc())
    )


def admission_response(document: Document, operation: WorkOperation) -> dict[str, object]:
    """Build the exact stable response snapshot stored for idempotent replay."""

    response = upload_accepted_response(document, operation)
    return response.model_dump(mode="json")


def register_admission(
    session: Session,
    *,
    prepared: PreparedAdmission,
    idempotency_key: str,
    actor_type: str,
    actor_id: str,
) -> AdmissionOutcome | IdempotencyReplay:
    """Promote clean bytes and atomically register PREFLIGHTING + operation."""

    request = {
        "collection_key": prepared.collection.key,
        "filename": prepared.filename,
        "size_bytes": prepared.staged.size_bytes,
        "sha256": prepared.staged.sha256,
    }
    idempotency = reserve_idempotency(
        session,
        key=idempotency_key,
        action="upload_document",
        actor_id=actor_id,
        request_material=request,
    )
    if isinstance(idempotency, IdempotencyReplay):
        discard_prepared_admission(prepared)
        return idempotency

    duplicate = _find_exact_duplicate(
        session,
        collection_key=prepared.collection.key,
        sha256=prepared.staged.sha256,
    )
    if duplicate is not None:
        discard_prepared_admission(prepared)
        raise LifecycleError(
            "exact_duplicate",
            "The same PDF bytes are already retained in this collection.",
            extra={"existing_document_id": str(duplicate.id)},
        )

    document = Document(
        id=uuid.uuid4(),
        collection_key=prepared.collection.key,
        original_filename=prepared.filename,
        normalized_filename=prepared.normalized_filename,
        content_type="application/pdf",
        size_bytes=prepared.staged.size_bytes,
        sha256=prepared.staged.sha256,
        state=DocumentState.PREFLIGHTING,
        scan_state=prepared.scan.state,
        scan_engine=prepared.scan.engine,
        scan_signature=prepared.scan.signature,
        scanned_at=prepared.scan.scanned_at,
        created_by=actor_id,
    )
    promoted: PromotedFile | None = None
    try:
        promoted = promote_staged_file(prepared.staged, prepared.layout, document.id)
        document.storage_key = promoted.storage_key
        session.add(document)
        session.flush()
        operation = enqueue_operation(
            session,
            document=document,
            operation_type=OperationType.PREFLIGHT,
            priority=OperationPriority.NORMAL,
            idempotency_record_id=idempotency.id,
        )
        body = admission_response(document, operation)
        complete_idempotency(
            idempotency,
            status=202,
            body=body,
            resource_type="document",
            resource_id=document.id,
        )
        audit(
            session,
            event_type="document_admitted",
            actor_type=actor_type,
            actor_id=actor_id,
            document_id=document.id,
            operation_id=operation.id,
            details={"collection_key": document.collection_key},
        )
        return AdmissionOutcome(
            document=document,
            operation=operation,
            promoted=promoted,
            response_body=body,
        )
    except Exception:
        if promoted is not None:
            promoted.path.unlink(missing_ok=True)
        else:
            discard_prepared_admission(prepared)
        raise


def compensate_failed_commit(outcome: AdmissionOutcome) -> None:
    """Remove promoted bytes if the surrounding catalog commit fails."""

    outcome.promoted.path.unlink(missing_ok=True)


def filename_advisory(
    session: Session,
    *,
    definitions: tuple[CollectionDefinition, ...] | list[CollectionDefinition],
    collection_key: str,
    filename: str,
    limit: int = 20,
) -> tuple[str, list[FilenameAdvisoryMatch]]:
    """Return bounded same-collection filename-only warnings."""

    configured_collection(definitions, collection_key)
    normalized = normalize_filename(filename)
    incoming = profile_filename(filename)
    documents = session.scalars(
        select(Document)
        .where(
            Document.collection_key == collection_key,
            Document.state.not_in(TERMINAL_DOCUMENT_STATES),
        )
        .order_by(Document.created_at.desc())
        .limit(500)
    ).all()
    matches: list[FilenameAdvisoryMatch] = []
    for document in documents:
        match = compare_filenames(incoming, profile_filename(document.original_filename))
        if match is None:
            continue
        matches.append(
            FilenameAdvisoryMatch(
                document_id=document.id,
                original_filename=document.original_filename,
                state=document.state,
                match_type=match.kind,
                score=match.similarity,
            )
        )
    matches.sort(key=lambda item: (-item.score, item.original_filename.casefold()))
    return normalized, matches[:limit]


def source_content(
    session: Session,
    *,
    document_id: uuid.UUID,
    storage_root: Path,
) -> SourceContent:
    """Resolve source bytes only while the lifecycle access gate is open."""

    from pdf_bridge.services.intake import can_serve_source

    document = session.get(Document, document_id)
    if document is None:
        raise LifecycleError("document_not_found", "The document was not found.", status=404)
    if not can_serve_source(document):
        status = 410 if document.state in TERMINAL_DOCUMENT_STATES else 409
        code = "content_purged" if status == 410 else "content_blocked"
        raise LifecycleError(code, "The source PDF is not available.", status=status)
    layout = StorageLayout.from_root(storage_root)
    path = resolve_storage_key(layout, document.storage_key or "")
    if not path.is_file():
        raise LifecycleError(
            "stored_file_missing",
            "The catalog source object is unavailable.",
            status=500,
        )
    return SourceContent(path=path, filename=document.original_filename)
