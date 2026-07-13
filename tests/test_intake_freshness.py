from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from threading import Barrier, RLock

import pytest
from litestar.testing import TestClient
from sqlalchemy.orm import Session, sessionmaker

from pdf_bridge.managers import document as document_manager
from pdf_bridge.persistence.models import (
    AnalysisCandidate,
    AnalysisStatus,
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
from pdf_bridge.services.intake import LifecycleError
from tests.conftest import PDF_A, clean_scanner


def _document(*, filename: str, collection_key: str, state: DocumentState) -> Document:
    document_id = uuid.uuid4()
    return Document(
        id=document_id,
        original_filename=filename,
        normalized_filename=filename.casefold(),
        size_bytes=1024,
        sha256=document_id.hex * 2,
        idempotency_key=f"freshness-{document_id}",
        state=state,
        collection_key=collection_key,
        collection_epoch=1,
        scan_state=ScanState.CLEAN,
        scan_engine="test-clamd",
        scanned_at=utc_now(),
        uploader_identity="freshness-test",
        ingested_at=utc_now() if state == DocumentState.INGESTED else None,
    )


def _complete_analysis(
    session: Session,
    document: Document,
    *,
    revision: int,
    collection_epoch: int,
    candidate_count: int = 0,
) -> DocumentAnalysis:
    analysis = DocumentAnalysis(
        document=document,
        revision=revision,
        status=AnalysisStatus.COMPLETE,
        pipeline_fingerprint=f"freshness-v{revision}",
        collection_epoch=collection_epoch,
        page_count=1,
        chunk_count=1,
        semantic_complete=False,
        classification_complete=False,
        incomplete_reasons=["test advisory"],
        auto_ingest_eligible=False,
        candidate_count=candidate_count,
        classified_count=candidate_count,
        completed_at=utc_now(),
    )
    session.add(analysis)
    session.flush()
    return analysis


def _put_upload_in_review(
    session_factory: sessionmaker[Session], upload_id: uuid.UUID
) -> DocumentAnalysis:
    with session_factory() as session:
        document = session.get(Document, upload_id)
        assert document is not None
        operation = (
            session.query(WorkOperation)
            .filter_by(
                document_id=upload_id,
                operation_type=OperationType.ANALYZE,
            )
            .one()
        )
        operation.state = OperationState.SUCCEEDED
        operation.phase = OperationPhase.AWAITING_DECISION
        operation.completed_at = utc_now()
        analysis = _complete_analysis(
            session,
            document,
            revision=1,
            collection_epoch=1,
        )
        document.analysis_revision = 1
        document.state = DocumentState.REVIEW_REQUIRED
        session.commit()
        session.expunge(analysis)
        return analysis


def _replacement_cancellation_scenario(
    session_factory: sessionmaker[Session],
    *,
    workflow_state: ReplacementState,
    old_state: DocumentState,
    operation_state: OperationState,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    with session_factory() as session:
        old_document = _document(
            filename="Old replacement target.pdf",
            collection_key="customer",
            state=old_state,
        )
        if old_state in {
            DocumentState.DELETING,
            DocumentState.DELETE_FAILED,
            DocumentState.DELETED,
        }:
            old_document.ingested_at = utc_now()
        if old_state == DocumentState.DELETED:
            old_document.deleted_at = utc_now()
        new_document = _document(
            filename="Incoming replacement.pdf",
            collection_key="customer",
            state=(
                DocumentState.REPLACE_FAILED
                if operation_state == OperationState.FAILED
                else DocumentState.REPLACING
            ),
        )
        session.add_all([old_document, new_document])
        session.flush()
        analysis = _complete_analysis(
            session,
            new_document,
            revision=1,
            collection_epoch=1,
            candidate_count=1,
        )
        new_document.analysis_revision = 1
        decision = IntakeDecision(
            document=new_document,
            analysis_id=analysis.id,
            analysis_revision=1,
            action=DecisionAction.REPLACE,
            target_document_id=old_document.id,
            idempotency_key=f"replacement-decision-{new_document.id}",
            advisory_override=False,
            actor_type="session",
            actor_id="replacement-cancellation-test",
        )
        session.add(decision)
        session.flush()
        workflow = ReplacementWorkflow(
            new_document_id=new_document.id,
            old_document_id=old_document.id,
            decision_id=decision.id,
            state=workflow_state,
            error=(
                "old deletion outcome is uncertain"
                if operation_state == OperationState.FAILED
                else None
            ),
        )
        session.add(workflow)
        session.flush()
        operation = WorkOperation(
            document=new_document,
            operation_type=OperationType.INGEST,
            state=operation_state,
            phase=OperationPhase.DELETING_EXISTING,
            attempt=1,
            replacement_id=workflow.id,
            error="old deletion outcome is uncertain"
            if operation_state == OperationState.FAILED
            else None,
            retryable=True,
            completed_at=(utc_now() if operation_state == OperationState.FAILED else None),
        )
        session.add(operation)
        session.commit()
        return old_document.id, new_document.id, workflow.id, operation.id


@pytest.mark.parametrize(
    ("idempotency_keys", "expected"),
    [
        (
            ("concurrent-upload-one", "concurrent-upload-two"),
            {("accepted", False), ("error", "exact-duplicate")},
        ),
        (
            ("concurrent-replay-key", "concurrent-replay-key"),
            {("accepted", False), ("accepted", True)},
        ),
    ],
)
def test_pending_upload_registration_serializes_duplicate_and_replay_races(
    settings,
    session_factory: sessionmaker[Session],
    idempotency_keys: tuple[str, str],
    expected: set[tuple[str, bool | str]],
) -> None:
    transition_lock = RLock()
    both_scanned = Barrier(2)

    def synchronized_scanner(path):
        result = clean_scanner(path)
        both_scanned.wait(timeout=5)
        return result

    def upload(idempotency_key: str) -> tuple[str, bool | str]:
        with session_factory() as session:
            try:
                response = document_manager.upload_document(
                    session,
                    settings=settings,
                    scanner=synchronized_scanner,
                    transition_lock=transition_lock,
                    worker=None,
                    file=BytesIO(PDF_A),
                    filename="concurrent.pdf",
                    content_type="application/pdf",
                    collection_key="customer",
                    header_idempotency_key=idempotency_key,
                    form_idempotency_key=idempotency_key,
                    actor_type="session",
                    actor_id="concurrency-test",
                )
            except LifecycleError as exc:
                return "error", exc.code
            return "accepted", response.idempotent_replay

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = set(executor.map(upload, idempotency_keys))

    assert outcomes == expected
    with session_factory() as session:
        documents = session.query(Document).all()
        operations = session.query(WorkOperation).all()
        assert len(documents) == 1
        assert documents[0].state == DocumentState.ANALYZING
        assert len(operations) == 1
        assert operations[0].operation_type == OperationType.ANALYZE


def test_retry_considers_only_latest_analysis_and_refreshes_a_stale_epoch(
    client: TestClient,
    csrf_headers: dict[str, str],
    upload_pdf,
    session_factory: sessionmaker[Session],
) -> None:
    accepted = upload_pdf(key="epoch-freshness-upload")
    assert accepted.status_code == 202, accepted.text
    upload_id = uuid.UUID(accepted.json()["upload"]["upload_id"])

    with session_factory() as session:
        document = session.get(Document, upload_id)
        assert document is not None
        first_operation = (
            session.query(WorkOperation)
            .filter_by(
                document_id=upload_id,
                operation_type=OperationType.ANALYZE,
            )
            .one()
        )
        first_operation.state = OperationState.SUCCEEDED
        first_operation.phase = OperationPhase.AWAITING_DECISION
        first_operation.completed_at = utc_now()
        _complete_analysis(session, document, revision=1, collection_epoch=1)
        session.add(
            WorkOperation(
                document=document,
                operation_type=OperationType.ANALYZE,
                state=OperationState.SUCCEEDED,
                phase=OperationPhase.AWAITING_DECISION,
                attempt=2,
                completed_at=utc_now(),
            )
        )
        _complete_analysis(session, document, revision=2, collection_epoch=2)
        document.analysis_revision = 2
        document.state = DocumentState.REVIEW_REQUIRED
        epoch = session.get(CollectionEpoch, "customer")
        assert epoch is not None
        epoch.epoch = 2
        session.commit()

    current_analysis_cannot_retry = client.post(
        f"/api/v1/uploads/{upload_id}/retry",
        headers=csrf_headers,
    )
    assert current_analysis_cannot_retry.status_code == 409
    assert current_analysis_cannot_retry.json()["code"] == "operation-not-retryable"

    with session_factory() as session:
        epoch = session.get(CollectionEpoch, "customer")
        assert epoch is not None
        epoch.epoch = 3
        session.commit()

    stale_decision = client.post(
        f"/api/v1/uploads/{upload_id}/decision",
        headers={**csrf_headers, "Idempotency-Key": "stale-epoch-decision"},
        json={"analysis_revision": 2, "action": "keep"},
    )
    assert stale_decision.status_code == 409
    assert stale_decision.json()["code"] == "stale-collection-epoch"

    retried = client.post(
        f"/api/v1/uploads/{upload_id}/retry",
        headers=csrf_headers,
    )
    assert retried.status_code == 200, retried.text
    assert retried.json()["document"]["state"] == "ANALYZING"

    with session_factory() as session:
        operations = (
            session.query(WorkOperation)
            .filter_by(
                document_id=upload_id,
                operation_type=OperationType.ANALYZE,
            )
            .order_by(WorkOperation.attempt)
            .all()
        )
        assert [operation.attempt for operation in operations] == [1, 2, 3]
        assert operations[-1].state == OperationState.QUEUED
        assert session.query(IntakeDecision).filter_by(document_id=upload_id).count() == 0


def test_reused_decision_key_with_different_payload_is_a_conflict(
    client: TestClient,
    csrf_headers: dict[str, str],
    upload_pdf,
    session_factory: sessionmaker[Session],
) -> None:
    accepted = upload_pdf(key="decision-conflict-upload")
    upload_id = uuid.UUID(accepted.json()["upload"]["upload_id"])
    _put_upload_in_review(session_factory, upload_id)
    headers = {**csrf_headers, "Idempotency-Key": "decision-conflict-key"}

    kept = client.post(
        f"/api/v1/uploads/{upload_id}/decision",
        headers=headers,
        json={"analysis_revision": 1, "action": "keep"},
    )
    conflict = client.post(
        f"/api/v1/uploads/{upload_id}/decision",
        headers=headers,
        json={"analysis_revision": 1, "action": "cancel"},
    )

    assert kept.status_code == 200, kept.text
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "idempotency-key-conflict"
    with session_factory() as session:
        assert session.query(IntakeDecision).filter_by(document_id=upload_id).count() == 1
        assert (
            session.query(WorkOperation)
            .filter_by(
                document_id=upload_id,
                operation_type=OperationType.INGEST,
            )
            .count()
            == 1
        )


def test_analysis_candidates_are_paginated_with_live_replacement_eligibility(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    incoming = _document(
        filename="Incoming policy.pdf",
        collection_key="customer",
        state=DocumentState.REVIEW_REQUIRED,
    )
    eligible = _document(
        filename="Current policy.pdf",
        collection_key="customer",
        state=DocumentState.INGESTED,
    )
    no_longer_ingested = _document(
        filename="Deleting policy.pdf",
        collection_key="customer",
        state=DocumentState.DELETING,
    )
    cross_collection = _document(
        filename="Internal policy.pdf",
        collection_key="internal",
        state=DocumentState.INGESTED,
    )
    pending = _document(
        filename="Pending policy.pdf",
        collection_key="customer",
        state=DocumentState.REVIEW_REQUIRED,
    )

    with session_factory() as session:
        session.add_all([incoming, eligible, no_longer_ingested, cross_collection, pending])
        session.flush()
        analysis = _complete_analysis(
            session,
            incoming,
            revision=1,
            collection_epoch=1,
            candidate_count=4,
        )
        incoming.analysis_revision = 1
        targets = [eligible, no_longer_ingested, cross_collection, pending]
        for rank, target in enumerate(targets, start=1):
            session.add(
                AnalysisCandidate(
                    analysis=analysis,
                    matched_document_id=target.id,
                    source="active" if rank < 4 else "screening",
                    rank=rank,
                    reasons=["filename_family"],
                    classified=True,
                    document_snapshot={
                        "document_id": str(target.id),
                        "filename": target.original_filename,
                        "size_bytes": target.size_bytes,
                        "state": "INGESTED" if rank == 2 else target.state.value,
                        "collection_key": target.collection_key,
                    },
                )
            )
        session.commit()

    first_page = client.get(f"/api/v1/uploads/{incoming.id}/analysis?page=1&page_size=2")
    second_page = client.get(f"/api/v1/uploads/{incoming.id}/analysis?page=2&page_size=2")

    assert first_page.status_code == 200, first_page.text
    assert second_page.status_code == 200, second_page.text
    first = first_page.json()
    second = second_page.json()
    assert (first["total_candidates"], first["pages"], first["page_size"]) == (4, 2, 2)
    assert [candidate["rank"] for candidate in first["candidates"]] == [1, 2]
    assert [candidate["replacement_eligible"] for candidate in first["candidates"]] == [
        True,
        False,
    ]
    assert [candidate["rank"] for candidate in second["candidates"]] == [3, 4]
    assert [candidate["replacement_eligible"] for candidate in second["candidates"]] == [
        False,
        False,
    ]

    with session_factory() as session:
        competing_upload = _document(
            filename="Competing replacement.pdf",
            collection_key="customer",
            state=DocumentState.REPLACING,
        )
        session.add(competing_upload)
        session.flush()
        competing_decision = IntakeDecision(
            document_id=competing_upload.id,
            analysis_id=uuid.uuid4(),
            analysis_revision=1,
            action=DecisionAction.REPLACE,
            target_document_id=eligible.id,
            idempotency_key=f"competing-replacement-{uuid.uuid4()}",
            advisory_override=True,
            actor_type="user",
            actor_id="freshness-test",
        )
        session.add(competing_decision)
        session.flush()
        session.add(
            ReplacementWorkflow(
                new_document_id=competing_upload.id,
                old_document_id=eligible.id,
                decision_id=competing_decision.id,
                state=ReplacementState.PREPARING,
            )
        )
        session.commit()

    busy_target = client.get(f"/api/v1/uploads/{incoming.id}/analysis?page=1&page_size=1")
    assert busy_target.status_code == 200, busy_target.text
    assert busy_target.json()["candidates"][0]["replacement_eligible"] is False

    with session_factory() as session:
        live_target = session.get(Document, eligible.id)
        assert live_target is not None
        live_target.state = DocumentState.DELETING
        session.commit()

    refreshed = client.get(f"/api/v1/uploads/{incoming.id}/analysis?page=1&page_size=1")
    assert refreshed.status_code == 200
    assert refreshed.json()["candidates"][0]["replacement_eligible"] is False


def test_verified_publication_with_unfinished_operation_remains_open(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    document = _document(
        filename="Published before operation checkpoint.pdf",
        collection_key="customer",
        state=DocumentState.INGESTED,
    )
    operation = WorkOperation(
        document=document,
        operation_type=OperationType.INGEST,
        state=OperationState.RUNNING,
        phase=OperationPhase.INGESTING,
        attempt=1,
        worker_id="crashed-worker",
        started_at=utc_now(),
    )
    with session_factory() as session:
        session.add_all([document, operation])
        session.commit()
        operation_id = operation.id

    open_response = client.get("/api/v1/uploads?open=true&page_size=100")

    assert open_response.status_code == 200, open_response.text
    open_items = {item["upload_id"]: item for item in open_response.json()["items"]}
    assert open_items[str(document.id)]["open"] is True

    with session_factory() as session:
        operation = session.get(WorkOperation, operation_id)
        assert operation is not None
        operation.state = OperationState.SUCCEEDED
        operation.phase = OperationPhase.COMPLETE
        operation.completed_at = utc_now()
        session.commit()

    closed_response = client.get("/api/v1/uploads?open=true&page_size=100")
    assert closed_response.status_code == 200, closed_response.text
    assert str(document.id) not in {item["upload_id"] for item in closed_response.json()["items"]}


def test_cancelling_preparation_terminalizes_replacement_and_releases_old_target(
    client: TestClient,
    csrf_headers: dict[str, str],
    session_factory: sessionmaker[Session],
) -> None:
    old_id, new_id, workflow_id, replacement_operation_id = _replacement_cancellation_scenario(
        session_factory,
        workflow_state=ReplacementState.PREPARING,
        old_state=DocumentState.INGESTED,
        operation_state=OperationState.QUEUED,
    )

    cancelled = client.delete(f"/api/v1/uploads/{new_id}", headers=csrf_headers)

    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["document"]["state"] == "CLEANUP_PENDING"
    with session_factory() as session:
        old_document = session.get(Document, old_id)
        workflow = session.get(ReplacementWorkflow, workflow_id)
        replacement_operation = session.get(WorkOperation, replacement_operation_id)
        cleanup = (
            session.query(WorkOperation)
            .filter_by(
                document_id=new_id,
                operation_type=OperationType.CLEANUP,
            )
            .one()
        )
        event = (
            session.query(AuditEvent)
            .filter_by(
                document_id=new_id,
                event_type="upload_cancelled",
            )
            .one()
        )
        assert old_document is not None and old_document.state == DocumentState.INGESTED
        assert workflow is not None and workflow.state == ReplacementState.FAILED
        assert workflow.completed_at is not None
        assert workflow.error == "Replacement cancelled before old-document deletion."
        assert replacement_operation is not None
        assert replacement_operation.state == OperationState.CANCELLED
        assert cleanup.state == OperationState.QUEUED
        assert event.details["replacement_stage"] == "PREPARING"
        assert event.details["replacement_old_state"] == "INGESTED"
        assert event.details["replacement_destructive_boundary_crossed"] is False

        followup = _document(
            filename="Follow-up replacement.pdf",
            collection_key="customer",
            state=DocumentState.REVIEW_REQUIRED,
        )
        session.add(followup)
        session.flush()
        analysis = _complete_analysis(
            session,
            followup,
            revision=1,
            collection_epoch=1,
            candidate_count=1,
        )
        followup.analysis_revision = 1
        session.add(
            AnalysisCandidate(
                analysis=analysis,
                matched_document_id=old_id,
                source="active",
                rank=1,
                reasons=["filename_family"],
                document_snapshot={
                    "document_id": str(old_id),
                    "filename": old_document.original_filename,
                    "size_bytes": old_document.size_bytes,
                    "state": old_document.state.value,
                    "collection_key": old_document.collection_key,
                },
            )
        )
        session.commit()
        followup_id = followup.id

    replacement = client.post(
        f"/api/v1/uploads/{followup_id}/decision",
        headers={**csrf_headers, "Idempotency-Key": "followup-replacement-key"},
        json={
            "analysis_revision": 1,
            "action": "replace",
            "target_document_id": str(old_id),
        },
    )
    assert replacement.status_code == 200, replacement.text
    assert replacement.json()["document"]["state"] == "REPLACING"


def test_cancelling_after_old_deletion_keeps_old_deleted_and_cleans_new(
    client: TestClient,
    csrf_headers: dict[str, str],
    session_factory: sessionmaker[Session],
) -> None:
    old_id, new_id, workflow_id, _operation_id = _replacement_cancellation_scenario(
        session_factory,
        workflow_state=ReplacementState.INGESTING_NEW,
        old_state=DocumentState.DELETED,
        operation_state=OperationState.QUEUED,
    )

    cancelled = client.delete(f"/api/v1/uploads/{new_id}", headers=csrf_headers)

    assert cancelled.status_code == 200, cancelled.text
    with session_factory() as session:
        old_document = session.get(Document, old_id)
        new_document = session.get(Document, new_id)
        workflow = session.get(ReplacementWorkflow, workflow_id)
        event = (
            session.query(AuditEvent)
            .filter_by(
                document_id=new_id,
                event_type="upload_cancelled",
            )
            .one()
        )
        assert old_document is not None and old_document.state == DocumentState.DELETED
        assert new_document is not None
        assert new_document.state == DocumentState.CLEANUP_PENDING
        assert workflow is not None and workflow.state == ReplacementState.FAILED
        assert workflow.completed_at is not None
        assert "after verified old-document deletion" in (workflow.error or "")
        assert event.details["replacement_stage"] == "INGESTING_NEW"
        assert event.details["replacement_old_state"] == "DELETED"
        assert event.details["replacement_destructive_boundary_crossed"] is True


@pytest.mark.parametrize(
    "old_state",
    [DocumentState.DELETING, DocumentState.DELETE_FAILED],
)
def test_cancelling_during_uncertain_old_deletion_is_rejected_but_retryable(
    old_state: DocumentState,
    client: TestClient,
    csrf_headers: dict[str, str],
    session_factory: sessionmaker[Session],
) -> None:
    old_id, new_id, workflow_id, failed_operation_id = _replacement_cancellation_scenario(
        session_factory,
        workflow_state=ReplacementState.DELETING_OLD,
        old_state=old_state,
        operation_state=OperationState.FAILED,
    )

    rejected = client.delete(f"/api/v1/uploads/{new_id}", headers=csrf_headers)

    assert rejected.status_code == 409
    assert rejected.json()["code"] == "replacement-cancellation-unsafe"
    with session_factory() as session:
        old_document = session.get(Document, old_id)
        new_document = session.get(Document, new_id)
        workflow = session.get(ReplacementWorkflow, workflow_id)
        failed_operation = session.get(WorkOperation, failed_operation_id)
        assert old_document is not None and old_document.state == old_state
        assert new_document is not None and new_document.state == DocumentState.REPLACE_FAILED
        assert workflow is not None and workflow.state == ReplacementState.DELETING_OLD
        assert workflow.completed_at is None
        assert failed_operation is not None
        assert failed_operation.state == OperationState.FAILED
        assert (
            session.query(WorkOperation)
            .filter_by(
                document_id=new_id,
                operation_type=OperationType.CLEANUP,
            )
            .count()
            == 0
        )

    retried = client.post(f"/api/v1/uploads/{new_id}/retry", headers=csrf_headers)
    assert retried.status_code == 200, retried.text
    assert retried.json()["document"]["state"] == "REPLACING"
    with session_factory() as session:
        operations = (
            session.query(WorkOperation)
            .filter_by(
                document_id=new_id,
                operation_type=OperationType.INGEST,
            )
            .order_by(WorkOperation.attempt)
            .all()
        )
        assert [operation.attempt for operation in operations] == [1, 2]
        assert operations[-1].state == OperationState.QUEUED
