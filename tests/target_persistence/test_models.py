from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from pdf_bridge.persistence.db import Base, build_engine
from pdf_bridge.persistence.models import (
    AuditEvent,
    Decision,
    DecisionAction,
    DeletionPhase,
    Document,
    DocumentState,
    ExtractedPage,
    IdempotencyRecord,
    OperationPhase,
    OperationPriority,
    OperationType,
    PreparedRevision,
    RevisionStatus,
    ScanState,
    TerminalDisposition,
    Tombstone,
    priority_for_operation,
    utc_now,
)

HASH_A = "a" * 64
HASH_B = "b" * 64
TARGET_TABLES = {
    "audit_events",
    "candidate_evidence",
    "decisions",
    "deletion_progress",
    "documents",
    "extracted_pages",
    "formatter_batches",
    "idempotency_records",
    "index_outbox",
    "prepared_candidates",
    "prepared_chunk_vectors",
    "prepared_chunks",
    "prepared_pages",
    "prepared_revisions",
    "publication_records",
    "revision_artifacts",
    "tombstones",
    "work_operations",
}


@pytest.fixture
def session() -> Session:
    engine = build_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as active_session:
        yield active_session
    Base.metadata.drop_all(engine)
    engine.dispose()


def _document(*, state: DocumentState = DocumentState.PREFLIGHTING) -> Document:
    return Document(
        collection_key="customer",
        original_filename="guide.pdf",
        normalized_filename="guide.pdf",
        size_bytes=123,
        sha256=HASH_A,
        storage_key=f"objects/ab/{uuid.uuid4()}.pdf",
        state=state,
        scan_state=ScanState.CLEAN,
        scan_engine="test-clamd",
        scanned_at=utc_now(),
        created_by="operator@example.test",
    )


def _revision(document: Document) -> PreparedRevision:
    return PreparedRevision(
        document=document,
        revision_number=1,
        active_qdrant_collection="customer-pdfs",
        content_profile_id="sha256:content",
        index_profile_id="sha256:index",
        preflight_policy_id="sha256:policy",
        formatter_model_id="formatter@commit",
        dense_model_id="sentence-transformers/all-mpnet-base-v2@commit",
        dense_dimension=768,
        sparse_model_id="Qdrant/bm25@commit",
    )


def _seal(session: Session, revision: PreparedRevision) -> None:
    revision.page_count = 1
    revision.chunk_count = 0
    revision.expected_point_count = 0
    revision.manifest_sha256 = HASH_B
    revision.completed_at = utc_now()
    revision.sealed_at = utc_now()
    revision.status = RevisionStatus.SEALED
    session.commit()


def test_target_enums_are_exact_and_priorities_are_orderable() -> None:
    assert {state.value for state in DocumentState} == {
        "PREFLIGHTING",
        "PREFLIGHT_FAILED",
        "REVIEW_REQUIRED",
        "PUBLISHING",
        "PUBLISH_FAILED",
        "READY",
        "DELETING",
        "DELETE_FAILED",
        "REJECTED",
        "CANCELLED",
        "DELETED",
    }
    assert {operation.value for operation in OperationType} == {
        "PREFLIGHT",
        "PUBLISH",
        "DELETE",
    }
    assert int(OperationPriority.HIGH) < int(OperationPriority.PUBLISH)
    assert int(OperationPriority.PUBLISH) < int(OperationPriority.NORMAL)
    assert priority_for_operation(OperationType.DELETE) is OperationPriority.HIGH
    assert priority_for_operation(OperationType.PUBLISH) is OperationPriority.PUBLISH
    assert priority_for_operation(OperationType.PREFLIGHT) is OperationPriority.NORMAL
    assert (
        priority_for_operation(OperationType.PUBLISH, replacement_delete=True)
        is OperationPriority.REPLACEMENT
    )
    assert {phase.value for phase in DeletionPhase} == {
        "DELETE_ACTIVE_POINTS",
        "VERIFY_ACTIVE_ZERO",
        "DELETE_SCREENING_POINTS",
        "VERIFY_SCREENING_ZERO",
        "PURGE_STORAGE",
        "COMMIT_TOMBSTONE",
    }
    assert {phase.value for phase in DeletionPhase} <= {phase.value for phase in OperationPhase}


def test_metadata_create_and_drop_contains_only_target_tables() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    assert set(inspect(engine).get_table_names()) == TARGET_TABLES
    assert "collection_epochs" not in Base.metadata.tables
    assert "document_analyses" not in Base.metadata.tables
    Base.metadata.drop_all(engine)
    assert inspect(engine).get_table_names() == []
    engine.dispose()


def test_deleting_document_requires_a_terminal_disposition(session: Session) -> None:
    document = _document(state=DocumentState.DELETING)
    session.add(document)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()

    document = _document(state=DocumentState.DELETING)
    document.terminal_disposition = TerminalDisposition.CANCELLED
    session.add(document)
    session.commit()
    assert document.terminal_disposition is TerminalDisposition.CANCELLED


def test_sealed_revision_and_content_are_immutable_but_content_can_be_purged(
    session: Session,
) -> None:
    document = _document()
    revision = _revision(document)
    page = ExtractedPage(
        prepared_revision=revision,
        page_number=1,
        layout_text="Source text",
        character_count=11,
        text_sha256=HASH_A,
    )
    session.add_all([document, revision, page])
    session.commit()
    _seal(session, revision)

    revision.failure_code = "must-not-change"
    with pytest.raises(RuntimeError, match="sealed prepared revisions"):
        session.commit()
    session.rollback()

    page = session.get(ExtractedPage, page.id)
    assert page is not None
    page.layout_text = "changed"
    with pytest.raises(RuntimeError, match="sealed prepared revision"):
        session.commit()
    session.rollback()

    session.add(
        ExtractedPage(
            prepared_revision_id=revision.id,
            page_number=2,
            layout_text="late content",
            character_count=12,
            text_sha256=HASH_B,
        )
    )
    with pytest.raises(RuntimeError, match="sealed prepared revision"):
        session.commit()
    session.rollback()

    page = session.get(ExtractedPage, page.id)
    assert page is not None
    session.delete(page)
    session.commit()
    assert session.get(PreparedRevision, revision.id) is not None
    assert session.get(ExtractedPage, page.id) is None


def test_decision_audit_and_tombstone_are_immutable(session: Session) -> None:
    document = _document()
    revision = _revision(document)
    session.add_all([document, revision])
    session.commit()
    _seal(session, revision)
    collection_key = document.collection_key
    source_sha256 = document.sha256
    manifest_sha256 = revision.manifest_sha256

    idempotency = IdempotencyRecord(
        key="decision-0001",
        action="document-decision",
        request_sha256=HASH_A,
        actor_id="operator@example.test",
    )
    decision = Decision(
        document=document,
        prepared_revision=revision,
        prepared_manifest_sha256=revision.manifest_sha256 or "",
        action=DecisionAction.KEEP,
        idempotency_record=idempotency,
        actor_type="operator",
        actor_id="operator@example.test",
    )
    audit = AuditEvent(
        document=document,
        event_type="decision_recorded",
        actor_type="operator",
        actor_id="operator@example.test",
        details={"decision_id": str(decision.id)},
    )
    tombstone = Tombstone(
        document=document,
        collection_key=collection_key,
        disposition=TerminalDisposition.CANCELLED,
        source_sha256=source_sha256,
        manifest_sha256=manifest_sha256,
        actor_type="operator",
        actor_id="operator@example.test",
    )
    session.add_all([idempotency, decision, audit, tombstone])
    session.commit()

    decision.actor_id = "somebody-else"
    with pytest.raises(RuntimeError, match="Decision records are immutable"):
        session.commit()
    session.rollback()

    audit = session.get(AuditEvent, audit.id)
    assert audit is not None
    session.delete(audit)
    with pytest.raises(RuntimeError, match="AuditEvent records are immutable"):
        session.commit()
    session.rollback()

    tombstone = session.get(Tombstone, tombstone.id)
    assert tombstone is not None
    tombstone.reason_code = "changed"
    with pytest.raises(RuntimeError, match="Tombstone records are immutable"):
        session.commit()


def test_clean_alembic_baseline_upgrades_and_downgrades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "target-migration.sqlite3"
    database_url = f"sqlite+pysqlite:///{database.as_posix()}"
    monkeypatch.setenv("PDF_BRIDGE_DATABASE_URL", database_url)
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))

    command.upgrade(config, "head")
    engine = create_engine(database_url)
    inspector = inspect(engine)
    assert set(inspector.get_table_names()) == {"alembic_version", *TARGET_TABLES}
    operation_columns = {
        column["name"]: column for column in inspector.get_columns("work_operations")
    }
    assert operation_columns["phase_started_at"]["nullable"] is False
    operation_checks = {
        constraint["name"] for constraint in inspector.get_check_constraints("work_operations")
    }
    assert "ck_work_operations_phase_started_not_before_creation" in operation_checks
    engine.dispose()

    command.downgrade(config, "base")
    engine = create_engine(database_url)
    assert inspect(engine).get_table_names() == ["alembic_version"]
    engine.dispose()
