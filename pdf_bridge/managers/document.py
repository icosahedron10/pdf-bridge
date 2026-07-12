"""Transaction and workflow orchestration for document use cases."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from threading import RLock
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from pdf_bridge.contracts.schemas import (
    DocumentMutationResponse,
    UploadPreflightResponse,
    UploadResponse,
)
from pdf_bridge.core.config import CollectionDefinition, Settings
from pdf_bridge.persistence.models import LanguageCode
from pdf_bridge.services import document
from pdf_bridge.services.scanner import Scanner
from pdf_bridge.services.storage import AsyncReadable


def preflight_upload(
    session: Session,
    *,
    definitions: Sequence[CollectionDefinition],
    filename: str,
    size_bytes: int,
    collection_key: str,
) -> UploadPreflightResponse:
    return document.preflight_upload(
        session,
        definitions=definitions,
        filename=filename,
        size_bytes=size_bytes,
        collection_key=collection_key,
    )


async def upload_document(
    session: Session,
    *,
    settings: Settings,
    scanner: Scanner,
    transition_lock: RLock,
    file: AsyncReadable,
    filename: str,
    content_type: str | None,
    collection_key: str,
    possible_duplicate_confirmed: bool,
    header_idempotency_key: str | None,
    form_idempotency_key: str | None,
    actor_type: str,
    actor_id: str,
) -> UploadResponse:
    idempotency_key = document.validate_idempotency_key(
        header_value=header_idempotency_key,
        form_value=form_idempotency_key,
    )
    prepared = await document.prepare_upload(
        settings=settings,
        scanner=scanner,
        file=file,
        filename=filename,
        content_type=content_type,
        collection_key=collection_key,
    )
    registration = None
    try:
        with transition_lock:
            try:
                registration = document.register_upload(
                    session,
                    prepared=prepared,
                    possible_duplicate_confirmed=possible_duplicate_confirmed,
                    idempotency_key=idempotency_key,
                    actor_type=actor_type,
                    actor_id=actor_id,
                )
                if registration.idempotent_replay:
                    prepared.staged.path.unlink(missing_ok=True)
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                if registration is not None and registration.promoted is not None:
                    registration.promoted.path.unlink(missing_ok=True)
                return document.resolve_idempotency_conflict(
                    session,
                    prepared=prepared,
                    idempotency_key=idempotency_key,
                    cause=exc,
                )
            except Exception:
                session.rollback()
                raise
    finally:
        prepared.staged.path.unlink(missing_ok=True)

    if registration is None:
        raise RuntimeError("upload registration unexpectedly missing")
    return document.upload_response(registration)


def content(session: Session, *, document_id: UUID, storage_root: Path):
    return document.content(
        session,
        document_id=document_id,
        storage_root=storage_root,
    )


def cancel_queue_item(
    session: Session,
    *,
    transition_lock: RLock,
    storage_root: Path,
    operation_id: UUID,
    actor_type: str,
    actor_id: str,
    remove_file: Callable[..., None],
) -> DocumentMutationResponse:
    with transition_lock:
        try:
            record, storage_key = document.begin_queue_cancellation(
                session,
                operation_id=operation_id,
                actor_type=actor_type,
                actor_id=actor_id,
            )
            session.commit()
        except Exception:
            session.rollback()
            raise

    if storage_key:
        document.remove_cancelled_storage(
            storage_root=storage_root,
            document_id=record.id,
            operation_id=operation_id,
            storage_key=storage_key,
            remove_file=remove_file,
        )
        with transition_lock:
            try:
                record = document.finish_queue_cancellation(
                    session,
                    document_id=record.id,
                    storage_key=storage_key,
                    actor_type=actor_type,
                    actor_id=actor_id,
                )
                session.commit()
            except Exception:
                session.rollback()
                raise
    return document.mutation_response(record)


def retry_queue_item(
    session: Session,
    *,
    transition_lock: RLock,
    operation_id: UUID,
    actor_type: str,
    actor_id: str,
) -> DocumentMutationResponse:
    with transition_lock:
        try:
            response = document.retry_queue_item(
                session,
                operation_id=operation_id,
                actor_type=actor_type,
                actor_id=actor_id,
            )
            session.commit()
        except Exception:
            session.rollback()
            raise
    return response


def resolve_classification(
    session: Session,
    *,
    definitions: Sequence[CollectionDefinition],
    transition_lock: RLock,
    document_id: UUID,
    collection_key: str | None,
    language: LanguageCode | None,
    reason: str | None,
    actor_type: str,
    actor_id: str,
) -> DocumentMutationResponse:
    with transition_lock:
        try:
            response = document.resolve_classification(
                session,
                definitions=definitions,
                document_id=document_id,
                collection_key=collection_key,
                language=language,
                reason=reason,
                actor_type=actor_type,
                actor_id=actor_id,
            )
            session.commit()
        except Exception:
            session.rollback()
            raise
    return response


def request_deletion(
    session: Session,
    *,
    transition_lock: RLock,
    document_id: UUID,
    actor_type: str,
    actor_id: str,
    reason: str | None,
) -> DocumentMutationResponse:
    with transition_lock:
        try:
            response = document.request_deletion(
                session,
                document_id=document_id,
                actor_type=actor_type,
                actor_id=actor_id,
                reason=reason,
            )
            session.commit()
        except Exception:
            session.rollback()
            raise
    return response
