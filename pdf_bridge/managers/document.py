"""API-v2 document admission and mutation transaction coordination."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Protocol

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from pdf_bridge.contracts.schemas import (
    DecisionRequest,
    MutationResponse,
    NameCheckMatch,
    NameCheckResponse,
    UploadAcceptedResponse,
)
from pdf_bridge.core.config import Settings
from pdf_bridge.persistence.models import Document, IdempotencyRecord
from pdf_bridge.presentation.api_serializers import (
    SerializationError,
    mutation_response,
)
from pdf_bridge.services import document as document_service
from pdf_bridge.services import intake
from pdf_bridge.services.filenames import profile_filename
from pdf_bridge.services.intake import IdempotencyReplay, LifecycleError, MutationOutcome
from pdf_bridge.services.scanner import Scanner
from pdf_bridge.services.storage import BinaryReadable


class WorkerNotifier(Protocol):
    """Minimal durable-worker wake-up boundary used after a successful commit."""

    def notify(self) -> None: ...


TransitionLock = AbstractContextManager[object]
Mutation = Callable[[IdempotencyRecord], MutationOutcome]


def _notify(worker: WorkerNotifier | None) -> None:
    if worker is not None:
        worker.notify()


def _require_worker(worker: WorkerNotifier | None) -> WorkerNotifier:
    if worker is None:
        raise LifecycleError(
            "worker_unavailable",
            "Lifecycle mutations are unavailable while processing is disabled.",
            status=503,
            retryable=True,
        )
    return worker


def _screening_collection(settings: Settings) -> str:
    value = settings.qdrant_screening_collection_name
    if value is None:
        raise LifecycleError(
            "collection_configuration_invalid",
            "The screening collection is not configured.",
            status=500,
        )
    return value


def _upload_replay(replay: IdempotencyReplay) -> UploadAcceptedResponse:
    if replay.status != 202:
        raise SerializationError("stored upload replay has an unexpected HTTP status")
    try:
        response = UploadAcceptedResponse.model_validate(replay.body)
    except ValidationError as exc:
        raise SerializationError("stored upload replay is not an API v2 response") from exc
    return response.model_copy(update={"idempotent_replay": True})


def _mutation_replay(replay: IdempotencyReplay) -> MutationResponse:
    if replay.status != 202:
        raise SerializationError("stored mutation replay has an unexpected HTTP status")
    try:
        response = MutationResponse.model_validate(replay.body)
    except ValidationError as exc:
        raise SerializationError("stored mutation replay is not an API v2 response") from exc
    return response.model_copy(update={"idempotent_replay": True})


def _reserve_idempotency(
    session: Session,
    *,
    key: str,
    action: str,
    actor_id: str,
    request_material: dict[str, object],
) -> IdempotencyRecord | IdempotencyReplay:
    """Resolve the losing side of a cross-process unique-key race once."""

    try:
        return intake.reserve_idempotency(
            session,
            key=key,
            action=action,
            actor_id=actor_id,
            request_material=request_material,
        )
    except IntegrityError:
        session.rollback()
        return intake.reserve_idempotency(
            session,
            key=key,
            action=action,
            actor_id=actor_id,
            request_material=request_material,
        )


def _run_mutation(
    session: Session,
    *,
    transition_lock: TransitionLock,
    worker: WorkerNotifier | None,
    idempotency_key: str,
    action: str,
    actor_id: str,
    request_material: dict[str, object],
    mutate: Mutation,
) -> MutationResponse:
    active_worker = _require_worker(worker)
    with transition_lock:
        try:
            reservation = _reserve_idempotency(
                session,
                key=idempotency_key,
                action=action,
                actor_id=actor_id,
                request_material=request_material,
            )
            if isinstance(reservation, IdempotencyReplay):
                response = _mutation_replay(reservation)
                session.rollback()
                return response

            outcome = mutate(reservation)
            if outcome.operation is None:
                raise SerializationError("accepted mutation has no durable operation")
            if outcome.idempotency.id != reservation.id:
                raise SerializationError(
                    "accepted mutation is bound to a different idempotency record"
                )
            response = mutation_response(outcome.document, outcome.operation)
            intake.complete_idempotency(
                reservation,
                status=202,
                body=response.model_dump(mode="json"),
                resource_type="document",
                resource_id=outcome.document.id,
            )
            session.commit()
        except Exception:
            session.rollback()
            raise
    _notify(active_worker)
    return response


def name_check(
    session: Session,
    *,
    settings: Settings,
    collection_key: str,
    filename: str,
) -> NameCheckResponse:
    """Return filename-only warnings; semantic preflight always remains mandatory."""

    normalized, matches = document_service.filename_advisory(
        session,
        definitions=list(settings.collections),
        collection_key=collection_key,
        filename=filename,
        limit=100,
    )
    items: list[NameCheckMatch] = []
    for match in matches:
        exact = profile_filename(match.original_filename).normalized == profile_filename(
            filename
        ).normalized
        items.append(
            NameCheckMatch(
                kind="EXACT_NAME" if exact else "FILENAME_FAMILY",
                document_id=match.document_id,
                original_filename=match.original_filename,
                state=match.state,
                similarity=1.0 if exact else match.score,
            )
        )
    return NameCheckResponse(
        collection_key=collection_key,
        normalized_filename=normalized,
        matches=items,
    )


def upload_document(
    session: Session,
    *,
    settings: Settings,
    scanner: Scanner,
    transition_lock: TransitionLock,
    worker: WorkerNotifier | None,
    file: BinaryReadable,
    filename: str,
    content_type: str | None,
    collection_key: str,
    idempotency_key: str,
    actor_type: str,
    actor_id: str,
) -> UploadAcceptedResponse:
    """Stream and scan outside the short admission transaction, then enqueue."""

    _require_worker(worker)
    prepared = document_service.prepare_admission(
        settings=settings,
        scanner=scanner,
        file=file,
        filename=filename,
        content_type=content_type,
        collection_key=collection_key,
    )
    outcome: document_service.AdmissionOutcome | None = None
    try:
        with transition_lock:
            try:
                try:
                    registration = document_service.register_admission(
                        session,
                        prepared=prepared,
                        idempotency_key=idempotency_key,
                        actor_type=actor_type,
                        actor_id=actor_id,
                    )
                except IntegrityError:
                    session.rollback()
                    if not prepared.staged.path.is_file():
                        raise
                    registration = document_service.register_admission(
                        session,
                        prepared=prepared,
                        idempotency_key=idempotency_key,
                        actor_type=actor_type,
                        actor_id=actor_id,
                    )
                if isinstance(registration, IdempotencyReplay):
                    response = _upload_replay(registration)
                    session.rollback()
                    return response
                outcome = registration
                try:
                    response = UploadAcceptedResponse.model_validate(outcome.response_body)
                except ValidationError as exc:
                    raise SerializationError(
                        "admission produced an invalid API v2 response"
                    ) from exc
                session.commit()
            except Exception:
                session.rollback()
                if outcome is not None:
                    document_service.compensate_failed_commit(outcome)
                raise
    finally:
        document_service.discard_prepared_admission(prepared)

    _notify(worker)
    return response


def decide_document(
    session: Session,
    *,
    settings: Settings,
    transition_lock: TransitionLock,
    worker: WorkerNotifier | None,
    document_id: uuid.UUID,
    request: DecisionRequest,
    idempotency_key: str,
    actor_type: str,
    actor_id: str,
) -> MutationResponse:
    """Bind an immutable decision to the exact inspected prepared revision."""

    return _run_mutation(
        session,
        transition_lock=transition_lock,
        worker=worker,
        idempotency_key=idempotency_key,
        action="decide_document",
        actor_id=actor_id,
        request_material={
            "document_id": str(document_id),
            "prepared_revision_id": str(request.prepared_revision_id),
            "action": request.action.value,
            "target_document_id": (
                str(request.target_document_id)
                if request.target_document_id is not None
                else None
            ),
        },
        mutate=lambda reservation: intake.record_decision(
            session,
            document_id=document_id,
            prepared_revision_id=request.prepared_revision_id,
            action=request.action,
            target_document_id=request.target_document_id,
            idempotency=reservation,
            actor_type=actor_type,
            actor_id=actor_id,
            screening_qdrant_collection=_screening_collection(settings),
        ),
    )


def retry_document(
    session: Session,
    *,
    transition_lock: TransitionLock,
    worker: WorkerNotifier | None,
    document_id: uuid.UUID,
    idempotency_key: str,
    actor_type: str,
    actor_id: str,
) -> MutationResponse:
    """Resume only the exact durable failed checkpoint."""

    return _run_mutation(
        session,
        transition_lock=transition_lock,
        worker=worker,
        idempotency_key=idempotency_key,
        action="retry_document",
        actor_id=actor_id,
        request_material={"document_id": str(document_id)},
        mutate=lambda reservation: intake.request_retry(
            session,
            document_id=document_id,
            idempotency=reservation,
            actor_type=actor_type,
            actor_id=actor_id,
        ),
    )


def delete_document(
    session: Session,
    *,
    settings: Settings,
    transition_lock: TransitionLock,
    worker: WorkerNotifier | None,
    document_id: uuid.UUID,
    idempotency_key: str,
    actor_type: str,
    actor_id: str,
) -> MutationResponse:
    """Block reads and queue verified point-first deletion."""

    def request(reservation: IdempotencyRecord) -> MutationOutcome:
        existing = session.get(Document, document_id)
        if existing is None:
            raise LifecycleError(
                "document_not_found", "The document was not found.", status=404
            )
        definition = document_service.configured_collection(
            list(settings.collections), existing.collection_key
        )
        return intake.request_deletion(
            session,
            document_id=document_id,
            idempotency=reservation,
            actor_type=actor_type,
            actor_id=actor_id,
            active_qdrant_collection=definition.qdrant_collection_name,
            screening_qdrant_collection=_screening_collection(settings),
        )

    return _run_mutation(
        session,
        transition_lock=transition_lock,
        worker=worker,
        idempotency_key=idempotency_key,
        action="delete_document",
        actor_id=actor_id,
        request_material={"document_id": str(document_id)},
        mutate=request,
    )
