from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, func, insert, select
from sqlalchemy.orm import Session

from pdf_bridge.persistence.models import (
    Decision,
    DecisionAction,
    DeletionPhase,
    Document,
    DocumentState,
    ExtractedPage,
    IdempotencyRecord,
    OperationPhase,
    OperationPriority,
    OperationState,
    OperationType,
    PreparedRevision,
    PublicationRecord,
    PublicationStatus,
    RevisionStatus,
    ScanState,
    TerminalDisposition,
    Tombstone,
    WorkOperation,
    utc_now,
)
from pdf_bridge.services import catalog as catalog_service
from pdf_bridge.services import intake

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
ACTOR = "operator@example.test"
ACTIVE = "customer-pdfs-v7"
SCREENING = "private-screening"


def _document(
    *,
    state: DocumentState = DocumentState.PREFLIGHTING,
    sha256: str = HASH_A,
    collection_key: str = "customer",
) -> Document:
    return Document(
        collection_key=collection_key,
        original_filename=f"{uuid.uuid4()}.pdf",
        normalized_filename="guide.pdf",
        size_bytes=123,
        sha256=sha256,
        storage_key=f"objects/{uuid.uuid4()}.pdf",
        state=state,
        scan_state=ScanState.CLEAN,
        scan_engine="test-clamd",
        scanned_at=utc_now(),
        created_by=ACTOR,
    )


def _sealed_revision(
    document: Document,
    *,
    number: int = 1,
    active: str = ACTIVE,
    clear: bool = False,
    manifest: str = HASH_B,
) -> PreparedRevision:
    return PreparedRevision(
        document=document,
        revision_number=number,
        status=RevisionStatus.SEALED,
        active_qdrant_collection=active,
        content_profile_id="sha256:content",
        index_profile_id="sha256:index",
        preflight_policy_id="sha256:policy",
        formatter_model_id="formatter@commit",
        dense_model_id="mpnet@commit",
        dense_dimension=768,
        sparse_model_id="bm25@commit",
        native_text_eligible=True,
        formatter_complete=True,
        vector_complete=True,
        candidate_discovery_complete=True,
        advisory_complete=True,
        clear_for_publication=clear,
        incomplete_reasons=[],
        page_count=1,
        chunk_count=0,
        expected_point_count=0,
        extraction_sha256=HASH_A,
        markdown_sha256=HASH_B,
        vector_manifest_sha256=HASH_C,
        evidence_manifest_sha256=HASH_A,
        manifest_sha256=manifest,
        completed_at=utc_now(),
        sealed_at=utc_now(),
    )


def _publication(
    document: Document,
    revision: PreparedRevision,
    *,
    status: PublicationStatus,
) -> PublicationRecord:
    verified = status is PublicationStatus.VERIFIED
    return PublicationRecord(
        document=document,
        prepared_revision=revision,
        active_qdrant_collection=revision.active_qdrant_collection,
        status=status,
        expected_points=revision.expected_point_count or 0,
        verified_points=(revision.expected_point_count or 0) if verified else None,
        payload_revision_verified=verified,
        vector_schema_verified=verified,
        screening_zero_verified=verified,
        verified_at=utc_now() if verified else None,
        failure_code="publish_failed" if status is PublicationStatus.FAILED else None,
    )


def _reserve(
    session: Session,
    key: str,
    *,
    action: str,
    material: dict[str, object] | None = None,
) -> IdempotencyRecord:
    result = intake.reserve_idempotency(
        session,
        key=key,
        action=action,
        actor_id=ACTOR,
        request_material=material or {"document_id": str(uuid.uuid4())},
    )
    assert isinstance(result, IdempotencyRecord)
    return result


def _persist_document_with_revision(
    session: Session,
    *,
    state: DocumentState,
    clear: bool = False,
    sha256: str = HASH_A,
) -> tuple[Document, PreparedRevision]:
    document = _document(state=state, sha256=sha256)
    revision = _sealed_revision(document, clear=clear)
    session.add_all([document, revision])
    session.commit()
    return document, revision


def test_idempotency_replays_exact_response_and_conflicts_on_any_identity_change(
    session: Session,
) -> None:
    material = {"action": "KEEP", "prepared_revision_id": str(uuid.uuid4())}
    record = _reserve(
        session,
        "decision-key-0001",
        action="document-decision",
        material=material,
    )
    response = {"document": {"state": "PUBLISHING"}, "operation_id": "op-1"}
    resource_id = uuid.uuid4()
    intake.complete_idempotency(
        record,
        status=202,
        body=response,
        resource_type="document",
        resource_id=resource_id,
    )
    response["operation_id"] = "mutated-by-caller"
    session.commit()

    replay = intake.reserve_idempotency(
        session,
        key="decision-key-0001",
        action="document-decision",
        actor_id=ACTOR,
        request_material=dict(reversed(list(material.items()))),
    )
    assert isinstance(replay, intake.IdempotencyReplay)
    assert replay.status == 202
    assert replay.body["operation_id"] == "op-1"

    with pytest.raises(intake.LifecycleError, match="different request") as conflict:
        intake.reserve_idempotency(
            session,
            key="decision-key-0001",
            action="document-decision",
            actor_id=ACTOR,
            request_material={**material, "action": "CANCEL"},
        )
    assert conflict.value.code == "idempotency_conflict"

    with pytest.raises(intake.LifecycleError, match="cannot be changed"):
        intake.complete_idempotency(
            record,
            status=202,
            body={"operation_id": "different"},
            resource_type="document",
            resource_id=resource_id,
        )

    pending_material = {"document_id": str(uuid.uuid4())}
    pending = _reserve(
        session,
        "pending-key-0001",
        action="retry",
        material=pending_material,
    )
    session.commit()
    with pytest.raises(intake.LifecycleError) as in_progress:
        intake.reserve_idempotency(
            session,
            key=pending.key,
            action="retry",
            actor_id=ACTOR,
            request_material=pending_material,
        )
    assert in_progress.value.code == "idempotency_in_progress"


def test_enqueue_enforces_work_class_priority_and_increments_attempts(
    session: Session,
) -> None:
    document = _document()
    replacement_target = _document(state=DocumentState.READY, sha256=HASH_B)
    session.add_all([document, replacement_target])
    session.commit()

    first = intake.enqueue_operation(
        session,
        document=document,
        operation_type=OperationType.PREFLIGHT,
        priority=OperationPriority.NORMAL,
    )
    first.state = OperationState.FAILED
    intake.set_operation_phase(first, OperationPhase.EXTRACTING)
    session.commit()
    second = intake.enqueue_operation(
        session,
        document=document,
        operation_type=OperationType.PREFLIGHT,
        priority=OperationPriority.NORMAL,
        phase=OperationPhase.EXTRACTING,
    )
    replacement = intake.enqueue_operation(
        session,
        document=document,
        operation_type=OperationType.PUBLISH,
        priority=OperationPriority.REPLACEMENT,
        replacement_target_document_id=replacement_target.id,
    )
    assert (first.attempt, second.attempt) == (1, 2)
    assert second.phase is OperationPhase.EXTRACTING
    assert second.phase_started_at == second.created_at
    assert replacement.priority == int(OperationPriority.REPLACEMENT)

    with pytest.raises(intake.LifecycleError) as wrong_priority:
        intake.enqueue_operation(
            session,
            document=document,
            operation_type=OperationType.DELETE,
            priority=OperationPriority.REPLACEMENT,
        )
    assert wrong_priority.value.code == "operation_priority_invalid"


def test_clear_complete_candidate_free_revision_auto_publishes_once(
    session: Session,
) -> None:
    document, revision = _persist_document_with_revision(
        session, state=DocumentState.PREFLIGHTING, clear=True
    )
    operation = intake.queue_clear_publication(
        session, document=document, revision=revision
    )
    session.commit()

    assert document.state is DocumentState.PUBLISHING
    assert operation.operation_type is OperationType.PUBLISH
    assert operation.priority == int(OperationPriority.PUBLISH)
    publication = session.scalar(
        select(PublicationRecord).where(
            PublicationRecord.prepared_revision_id == revision.id
        )
    )
    assert publication is not None
    assert publication.active_qdrant_collection == ACTIVE

    replay = intake.queue_clear_publication(
        session, document=document, revision=revision
    )
    assert replay.id == operation.id
    assert session.scalar(select(func.count()).select_from(WorkOperation)) == 1


def test_keep_is_bound_to_current_sealed_revision_and_queues_exact_publication(
    session: Session,
) -> None:
    document, revision = _persist_document_with_revision(
        session, state=DocumentState.REVIEW_REQUIRED
    )
    idempotency = _reserve(session, "keep-key-0001", action="document-decision")
    outcome = intake.record_decision(
        session,
        document_id=document.id,
        prepared_revision_id=revision.id,
        action=DecisionAction.KEEP,
        target_document_id=None,
        idempotency=idempotency,
        actor_type="operator",
        actor_id=ACTOR,
        screening_qdrant_collection=SCREENING,
    )
    session.commit()

    decision = session.scalar(select(Decision).where(Decision.document_id == document.id))
    assert decision is not None
    assert decision.prepared_revision_id == revision.id
    assert decision.prepared_manifest_sha256 == revision.manifest_sha256
    assert outcome.document.state is DocumentState.PUBLISHING
    assert outcome.operation is not None
    assert outcome.operation.prepared_revision_id == revision.id
    assert outcome.operation.priority == int(OperationPriority.PUBLISH)


def test_stale_revision_decision_is_rejected(session: Session) -> None:
    document = _document(state=DocumentState.REVIEW_REQUIRED)
    old_revision = _sealed_revision(document, number=1, manifest=HASH_A)
    current_revision = _sealed_revision(document, number=2, manifest=HASH_B)
    session.add_all([document, old_revision, current_revision])
    session.commit()
    idempotency = _reserve(session, "stale-key-0001", action="document-decision")

    with pytest.raises(intake.LifecycleError) as stale:
        intake.record_decision(
            session,
            document_id=document.id,
            prepared_revision_id=old_revision.id,
            action=DecisionAction.KEEP,
            target_document_id=None,
            idempotency=idempotency,
            actor_type="operator",
            actor_id=ACTOR,
            screening_qdrant_collection=SCREENING,
        )
    assert stale.value.code == "stale_prepared_revision"


def test_cancel_decision_enters_async_deletion_with_cancelled_disposition(
    session: Session,
) -> None:
    document, revision = _persist_document_with_revision(
        session, state=DocumentState.REVIEW_REQUIRED
    )
    idempotency = _reserve(session, "cancel-key-0001", action="document-decision")
    outcome = intake.record_decision(
        session,
        document_id=document.id,
        prepared_revision_id=revision.id,
        action=DecisionAction.CANCEL,
        target_document_id=None,
        idempotency=idempotency,
        actor_type="operator",
        actor_id=ACTOR,
        screening_qdrant_collection=SCREENING,
    )
    session.commit()

    assert outcome.document.state is DocumentState.DELETING
    assert outcome.document.terminal_disposition is TerminalDisposition.CANCELLED
    assert outcome.operation is not None
    assert outcome.operation.operation_type is OperationType.DELETE
    assert outcome.operation.priority == int(OperationPriority.HIGH)
    assert document.deletion_progress is not None
    assert document.deletion_progress.prepared_revision_id == revision.id


def test_replace_queues_old_deletion_before_new_publication_as_one_workflow(
    session: Session,
) -> None:
    incoming, incoming_revision = _persist_document_with_revision(
        session, state=DocumentState.REVIEW_REQUIRED
    )
    old = _document(state=DocumentState.READY, sha256=HASH_B)
    old_revision = _sealed_revision(old, active="persisted-old-active")
    old_publication = _publication(
        old, old_revision, status=PublicationStatus.VERIFIED
    )
    session.add_all([old, old_revision, old_publication])
    session.commit()
    idempotency = _reserve(session, "replace-key-0001", action="document-decision")

    outcome = intake.record_decision(
        session,
        document_id=incoming.id,
        prepared_revision_id=incoming_revision.id,
        action=DecisionAction.REPLACE,
        target_document_id=old.id,
        idempotency=idempotency,
        actor_type="operator",
        actor_id=ACTOR,
        screening_qdrant_collection=SCREENING,
    )
    session.commit()

    operation = outcome.operation
    assert operation is not None
    assert operation.document_id == incoming.id
    assert operation.operation_type is OperationType.PUBLISH
    assert operation.priority == int(OperationPriority.REPLACEMENT)
    assert operation.prepared_revision_id == incoming_revision.id
    assert operation.replacement_target_document_id == old.id
    assert incoming.state is DocumentState.PUBLISHING
    assert old.state is DocumentState.DELETING
    assert old.terminal_disposition is TerminalDisposition.DELETED
    assert old.replaced_by_document_id == incoming.id
    assert old.deletion_progress is not None
    assert old.deletion_progress.active_qdrant_collection == "persisted-old-active"
    assert old.deletion_progress.publication_record_id == old_publication.id
    assert (
        session.scalar(
            select(func.count())
            .select_from(WorkOperation)
            .where(
                WorkOperation.document_id == old.id,
                WorkOperation.operation_type == OperationType.DELETE,
            )
        )
        == 0
    )


def test_publish_retry_resumes_exact_failed_phase_revision_priority_and_attempt(
    session: Session,
) -> None:
    document, revision = _persist_document_with_revision(
        session, state=DocumentState.PUBLISH_FAILED
    )
    document.failure_code = "qdrant_unavailable"
    document.failure_retryable = True
    publication = _publication(document, revision, status=PublicationStatus.FAILED)
    failed = WorkOperation(
        document=document,
        prepared_revision=revision,
        operation_type=OperationType.PUBLISH,
        priority=int(OperationPriority.PUBLISH),
        state=OperationState.FAILED,
        phase=OperationPhase.VERIFY_ACTIVE_POINTS,
        attempt=1,
        retryable=True,
        failure_code="qdrant_unavailable",
    )
    session.add_all([publication, failed])
    session.commit()
    idempotency = _reserve(session, "retry-key-0001", action="document-retry")

    outcome = intake.request_retry(
        session,
        document_id=document.id,
        idempotency=idempotency,
        actor_type="operator",
        actor_id=ACTOR,
    )
    session.commit()

    operation = outcome.operation
    assert operation is not None
    assert operation.attempt == 2
    assert operation.phase is OperationPhase.VERIFY_ACTIVE_POINTS
    assert operation.prepared_revision_id == revision.id
    assert operation.priority == int(OperationPriority.PUBLISH)
    assert document.state is DocumentState.PUBLISHING
    assert publication.status is PublicationStatus.PENDING


def test_replacement_target_retry_resumes_the_owning_publication(
    session: Session,
) -> None:
    incoming, incoming_revision = _persist_document_with_revision(
        session, state=DocumentState.REVIEW_REQUIRED
    )
    old = _document(state=DocumentState.READY, sha256=HASH_B)
    old_revision = _sealed_revision(old, active="persisted-old-active")
    old_publication = _publication(
        old, old_revision, status=PublicationStatus.VERIFIED
    )
    session.add_all([old, old_revision, old_publication])
    session.commit()
    decision_key = _reserve(
        session, "replace-retry-decision-0001", action="document-decision"
    )
    accepted = intake.record_decision(
        session,
        document_id=incoming.id,
        prepared_revision_id=incoming_revision.id,
        action=DecisionAction.REPLACE,
        target_document_id=old.id,
        idempotency=decision_key,
        actor_type="operator",
        actor_id=ACTOR,
        screening_qdrant_collection=SCREENING,
    )
    session.commit()
    failed = accepted.operation
    assert failed is not None
    publication = session.scalar(
        select(PublicationRecord).where(
            PublicationRecord.prepared_revision_id == incoming_revision.id
        )
    )
    assert publication is not None

    failed.state = OperationState.FAILED
    intake.set_operation_phase(failed, OperationPhase.VERIFY_ACTIVE_ZERO)
    failed.retryable = True
    failed.failure_code = "qdrant_unavailable"
    incoming.state = DocumentState.PUBLISH_FAILED
    incoming.failure_code = "qdrant_unavailable"
    incoming.failure_retryable = True
    publication.status = PublicationStatus.FAILED
    publication.failure_code = "qdrant_unavailable"
    old.state = DocumentState.DELETE_FAILED
    old.failure_code = "qdrant_unavailable"
    old.failure_retryable = True
    assert old.deletion_progress is not None
    old.deletion_progress.failure_code = "qdrant_unavailable"
    session.commit()

    aggregate = catalog_service.document_aggregate(session, old.id)
    assert aggregate.operation is not None
    assert aggregate.operation.id == failed.id

    retry_key = _reserve(
        session, "replace-target-retry-0001", action="document-retry"
    )
    retried = intake.request_retry(
        session,
        document_id=old.id,
        idempotency=retry_key,
        actor_type="operator",
        actor_id=ACTOR,
    )
    session.commit()

    operation = retried.operation
    assert operation is not None
    assert retried.document.id == old.id
    assert operation.document_id == incoming.id
    assert operation.replacement_target_document_id == old.id
    assert operation.operation_type is OperationType.PUBLISH
    assert operation.phase is OperationPhase.VERIFY_ACTIVE_ZERO
    assert operation.priority == int(OperationPriority.REPLACEMENT)
    assert operation.attempt == 2
    assert old.state is DocumentState.DELETING
    assert incoming.state is DocumentState.PUBLISHING
    assert publication.status is PublicationStatus.PENDING


def test_delete_retry_uses_durable_deletion_phase(session: Session) -> None:
    document, revision = _persist_document_with_revision(
        session, state=DocumentState.REVIEW_REQUIRED
    )
    first_idempotency = _reserve(session, "delete-key-0001", action="document-delete")
    initial = intake.request_deletion(
        session,
        document_id=document.id,
        idempotency=first_idempotency,
        actor_type="operator",
        actor_id=ACTOR,
        active_qdrant_collection="configuration-drift",
        screening_qdrant_collection=SCREENING,
    )
    session.commit()
    assert initial.operation is not None
    assert document.deletion_progress is not None
    initial.operation.state = OperationState.FAILED
    intake.set_operation_phase(initial.operation, OperationPhase.VERIFY_SCREENING_ZERO)
    initial.operation.retryable = True
    document.deletion_progress.phase = DeletionPhase.VERIFY_SCREENING_ZERO
    document.deletion_progress.failure_code = "qdrant_unavailable"
    document.state = DocumentState.DELETE_FAILED
    document.failure_code = "qdrant_unavailable"
    document.failure_retryable = True
    session.commit()
    retry_idempotency = _reserve(session, "retry-delete-0001", action="document-retry")

    retried = intake.request_retry(
        session,
        document_id=document.id,
        idempotency=retry_idempotency,
        actor_type="operator",
        actor_id=ACTOR,
    )
    session.commit()

    assert retried.operation is not None
    assert retried.operation.attempt == 2
    assert retried.operation.phase is OperationPhase.VERIFY_SCREENING_ZERO
    assert retried.operation.priority == int(OperationPriority.HIGH)
    assert retried.operation.prepared_revision_id == revision.id
    assert document.state is DocumentState.DELETING


@pytest.mark.parametrize(
    ("initial_state", "publication_status"),
    [
        (DocumentState.READY, PublicationStatus.VERIFIED),
        (DocumentState.REVIEW_REQUIRED, None),
        (DocumentState.PUBLISH_FAILED, PublicationStatus.FAILED),
    ],
)
def test_deletion_uses_persisted_target_for_every_eligible_stable_state(
    session: Session,
    initial_state: DocumentState,
    publication_status: PublicationStatus | None,
) -> None:
    document, revision = _persist_document_with_revision(
        session, state=initial_state
    )
    if publication_status is not None:
        session.add(_publication(document, revision, status=publication_status))
        session.commit()
    idempotency = _reserve(
        session,
        f"delete-{initial_state.value.lower()}-0001",
        action="document-delete",
    )

    outcome = intake.request_deletion(
        session,
        document_id=document.id,
        idempotency=idempotency,
        actor_type="operator",
        actor_id=ACTOR,
        active_qdrant_collection="changed-config-must-not-win",
        screening_qdrant_collection=SCREENING,
    )
    session.commit()

    assert outcome.operation is not None
    assert outcome.operation.operation_type is OperationType.DELETE
    assert outcome.operation.priority == int(OperationPriority.HIGH)
    assert document.state is DocumentState.DELETING
    assert document.deletion_progress is not None
    assert document.deletion_progress.active_qdrant_collection == ACTIVE
    assert document.deletion_progress.prepared_revision_id == revision.id


def test_repeated_deletion_reuses_operation_during_and_after_tombstone(
    session: Session,
) -> None:
    document, _revision = _persist_document_with_revision(
        session, state=DocumentState.REVIEW_REQUIRED
    )
    first_idempotency = _reserve(session, "delete-repeat-0001", action="document-delete")
    first = intake.request_deletion(
        session,
        document_id=document.id,
        idempotency=first_idempotency,
        actor_type="operator",
        actor_id=ACTOR,
        active_qdrant_collection="configuration-drift",
        screening_qdrant_collection=SCREENING,
    )
    session.commit()
    assert first.operation is not None

    second_idempotency = _reserve(session, "delete-repeat-0002", action="document-delete")
    second = intake.request_deletion(
        session,
        document_id=document.id,
        idempotency=second_idempotency,
        actor_type="operator",
        actor_id=ACTOR,
        active_qdrant_collection="different-again",
        screening_qdrant_collection=SCREENING,
    )
    assert second.operation is not None
    assert second.operation.id == first.operation.id
    assert session.scalar(select(func.count()).select_from(WorkOperation)) == 1

    progress = document.deletion_progress
    assert progress is not None
    progress.active_zero_verified_at = utc_now()
    progress.screening_zero_verified_at = utc_now()
    progress.storage_purged_at = utc_now()
    document.storage_key = None
    first.operation.state = OperationState.SUCCEEDED
    intake.set_operation_phase(first.operation, OperationPhase.COMPLETE)
    intake.commit_tombstone(
        session,
        document=document,
        reason_code="operator_delete",
        actor_type="operator",
        actor_id=ACTOR,
    )
    session.commit()
    assert document.state is DocumentState.DELETED

    third_idempotency = _reserve(session, "delete-repeat-0003", action="document-delete")
    third = intake.request_deletion(
        session,
        document_id=document.id,
        idempotency=third_idempotency,
        actor_type="operator",
        actor_id=ACTOR,
        active_qdrant_collection="ignored-after-terminal",
        screening_qdrant_collection=SCREENING,
    )
    assert third.operation is not None
    assert third.operation.id == first.operation.id
    assert session.scalar(select(func.count()).select_from(WorkOperation)) == 1


def test_rejection_cleanup_is_asynchronous_and_retains_bounded_reason(
    session: Session,
) -> None:
    document = _document(state=DocumentState.PREFLIGHTING)
    session.add(document)
    session.commit()

    operation = intake.queue_rejection_cleanup(
        session,
        document=document,
        reason_code="non_english_or_image_only",
        active_qdrant_collection=ACTIVE,
        screening_qdrant_collection=SCREENING,
        prepared_revision_id=None,
    )
    session.commit()

    assert document.state is DocumentState.DELETING
    assert document.terminal_disposition is TerminalDisposition.REJECTED
    assert document.failure_code == "non_english_or_image_only"
    assert operation.operation_type is OperationType.DELETE
    assert operation.priority == int(OperationPriority.HIGH)


def test_content_read_gates_fail_closed_by_scan_and_lifecycle_state() -> None:
    document = _document(state=DocumentState.PREFLIGHTING)
    assert intake.can_serve_source(document)
    assert not intake.can_serve_prepared_content(document)

    document.state = DocumentState.REVIEW_REQUIRED
    assert intake.can_serve_source(document)
    assert intake.can_serve_prepared_content(document)

    document.scan_state = ScanState.INFECTED
    assert not intake.can_serve_source(document)
    assert not intake.can_serve_prepared_content(document)

    document.scan_state = ScanState.CLEAN
    document.state = DocumentState.DELETING
    document.terminal_disposition = TerminalDisposition.DELETED
    assert not intake.can_serve_source(document)
    assert not intake.can_serve_prepared_content(document)


def test_tombstone_requires_both_index_zero_proofs_and_storage_purge(
    session: Session,
) -> None:
    document, revision = _persist_document_with_revision(
        session, state=DocumentState.REVIEW_REQUIRED
    )
    idempotency = _reserve(session, "delete-proof-0001", action="document-delete")
    intake.request_deletion(
        session,
        document_id=document.id,
        idempotency=idempotency,
        actor_type="operator",
        actor_id=ACTOR,
        active_qdrant_collection="configuration-drift",
        screening_qdrant_collection=SCREENING,
    )
    session.commit()
    progress = document.deletion_progress
    assert progress is not None

    with pytest.raises(intake.LifecycleError) as storage_checkpoint:
        intake.commit_tombstone(
            session,
            document=document,
            reason_code="operator_delete",
            actor_type="operator",
            actor_id=ACTOR,
        )
    assert storage_checkpoint.value.code == "deletion_checkpoint_incomplete"

    progress.storage_purged_at = utc_now()
    with pytest.raises(intake.LifecycleError) as index_checkpoint:
        intake.commit_tombstone(
            session,
            document=document,
            reason_code="operator_delete",
            actor_type="operator",
            actor_id=ACTOR,
        )
    assert index_checkpoint.value.code == "index_cleanup_incomplete"

    progress.active_zero_verified_at = utc_now()
    progress.screening_zero_verified_at = utc_now()
    with pytest.raises(intake.LifecycleError) as source_checkpoint:
        intake.commit_tombstone(
            session,
            document=document,
            reason_code="operator_delete",
            actor_type="operator",
            actor_id=ACTOR,
        )
    assert source_checkpoint.value.code == "source_not_purged"

    document.storage_key = None
    session.execute(
        insert(ExtractedPage).values(
            id=uuid.uuid4(),
            prepared_revision_id=revision.id,
            page_number=1,
            layout_text="Residual extracted content.",
            character_count=27,
            text_sha256=HASH_A,
        )
    )
    with pytest.raises(intake.LifecycleError) as content_checkpoint:
        intake.commit_tombstone(
            session,
            document=document,
            reason_code="operator_delete",
            actor_type="operator",
            actor_id=ACTOR,
        )
    assert content_checkpoint.value.code == "content_purge_incomplete"

    session.execute(
        delete(ExtractedPage).where(ExtractedPage.prepared_revision_id == revision.id)
    )
    tombstone = intake.commit_tombstone(
        session,
        document=document,
        reason_code="operator_delete",
        actor_type="operator",
        actor_id=ACTOR,
    )
    session.commit()

    assert isinstance(tombstone, Tombstone)
    assert tombstone.manifest_sha256 == revision.manifest_sha256
    assert document.state is DocumentState.DELETED
    assert progress.phase is DeletionPhase.COMMIT_TOMBSTONE
    assert progress.tombstoned_at is not None
