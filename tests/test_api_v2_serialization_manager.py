from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from pdf_bridge.contracts import schemas
from pdf_bridge.core.config import CollectionDefinition
from pdf_bridge.managers import catalog as catalog_manager
from pdf_bridge.persistence.models import (
    CandidateSource,
    DecisionAction,
    DocumentState,
    OperationPhase,
    OperationPriority,
    OperationState,
    OperationType,
    RevisionStatus,
    ScanState,
)
from pdf_bridge.presentation import api_serializers
from pdf_bridge.services import catalog as catalog_service
from pdf_bridge.services import document as document_service
from pdf_bridge.services.intake import LifecycleError

DOCUMENT_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
REVISION_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
OPERATION_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")
OLD_DOCUMENT_ID = uuid.UUID("44444444-4444-4444-8444-444444444444")
CANDIDATE_ID = uuid.UUID("55555555-5555-4555-8555-555555555555")
NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
HASH_A = "a" * 64
HASH_B = "b" * 64
PROFILE_A = "sha256:" + "a" * 64
PROFILE_B = "sha256:" + "b" * 64
PROFILE_C = "sha256:" + "c" * 64


def _document(**updates: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "id": DOCUMENT_ID,
        "collection_key": "customer",
        "original_filename": "guide.pdf",
        "normalized_filename": "guide.pdf",
        "content_type": "application/pdf",
        "size_bytes": 1_024,
        "sha256": HASH_A,
        "storage_key": "sources/1111.pdf",
        "state": DocumentState.PREFLIGHTING,
        "terminal_disposition": None,
        "scan_state": ScanState.CLEAN,
        "scan_engine": "clamav",
        "scan_signature": "daily-1",
        "scanned_at": NOW,
        "created_by": "operator@example.test",
        "created_at": NOW,
        "updated_at": NOW,
        "ready_at": None,
        "failure_code": None,
        "failure_message": None,
        "failure_retryable": False,
        "replaced_by_document_id": None,
    }
    values.update(updates)
    return SimpleNamespace(**values)


def _operation(**updates: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "id": OPERATION_ID,
        "document_id": DOCUMENT_ID,
        "prepared_revision_id": None,
        "replacement_target_document_id": None,
        "operation_type": OperationType.PREFLIGHT,
        "priority": int(OperationPriority.NORMAL),
        "state": OperationState.QUEUED,
        "phase": OperationPhase.QUEUED,
        "attempt": 1,
        "retryable": True,
        "failure_code": None,
        "failure_message": None,
        "created_at": NOW,
        "updated_at": NOW,
        "phase_started_at": NOW,
        "started_at": None,
        "completed_at": None,
    }
    values.update(updates)
    return SimpleNamespace(**values)


def _revision(**updates: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "id": REVISION_ID,
        "document_id": DOCUMENT_ID,
        "revision_number": 1,
        "status": RevisionStatus.SEALED,
        "active_qdrant_collection": "customer-product-pdfs",
        "content_profile_id": PROFILE_A,
        "index_profile_id": PROFILE_B,
        "preflight_policy_id": PROFILE_C,
        "formatter_model_id": "formatter-v1",
        "dense_model_id": "all-mpnet-base-v2",
        "dense_dimension": 768,
        "sparse_model_id": "Qdrant/bm25",
        "language_code": "en",
        "native_text_eligible": True,
        "formatter_complete": True,
        "vector_complete": True,
        "candidate_discovery_complete": True,
        "advisory_complete": True,
        "clear_for_publication": True,
        "incomplete_reasons": [],
        "page_count": 1,
        "chunk_count": 1,
        "expected_point_count": 1,
        "markdown_sha256": HASH_A,
        "manifest_sha256": HASH_B,
        "failure_code": None,
        "failure_message": None,
        "created_at": NOW,
        "sealed_at": NOW,
        "prepared_pages": [],
        "candidates": [],
    }
    values.update(updates)
    return SimpleNamespace(**values)


def _candidate(*, identifier: uuid.UUID = CANDIDATE_ID, rank: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        id=identifier,
        prepared_revision_id=REVISION_ID,
        source=CandidateSource.ACTIVE,
        rank=rank,
        reasons=["semantic_match"],
        max_cosine=0.96,
        bm25_score=4.5,
        fused_score=0.03,
        matched_chunk_pairs=[[0, str(OPERATION_ID)]],
        document_snapshot={
            "id": OLD_DOCUMENT_ID,
            "collection_key": "customer",
            "original_filename": "old-guide.pdf",
            "state": DocumentState.READY,
            "sha256": HASH_B,
        },
        evidence=[],
    )


def _definition(key: str = "customer") -> CollectionDefinition:
    return CollectionDefinition(
        key=key,
        display_name=f"{key.title()} Product",
        description=f"Approved {key} content.",
        audience=key,
        qdrant_collection_name=f"{key}-product-pdfs",
    )


def test_response_serializers_emit_exact_v2_shapes() -> None:
    document = _document()
    operation = _operation()

    upload = api_serializers.upload_accepted_response(document, operation)
    mutation = api_serializers.mutation_response(document, operation)
    stored = document_service.admission_response(document, operation)

    assert upload.document == mutation.document
    assert stored == upload.model_dump(mode="json")
    assert stored["operation"]["operation_type"] == "PREFLIGHT"
    assert stored["operation"]["state"] == "QUEUED"
    assert "type" not in stored["operation"]
    assert "status" not in stored["operation"]
    assert "created_by" in stored["document"]
    assert "updated_at" in stored["document"]


def test_document_and_operation_serializers_fail_closed_on_incomplete_failures() -> None:
    with pytest.raises(api_serializers.SerializationError, match="failure code"):
        api_serializers.document_summary(
            _document(state=DocumentState.PREFLIGHT_FAILED)
        )

    failed = _operation(state=OperationState.FAILED)
    with pytest.raises(api_serializers.SerializationError, match="failure code"):
        api_serializers.operation_detail(
            failed,
            queue_position=None,
            now=NOW + timedelta(seconds=3),
        )

    with pytest.raises(api_serializers.SerializationError, match="unknown durable"):
        api_serializers.operation_summary(_operation(priority=999))


def test_operation_detail_uses_durable_phase_start_instead_of_last_update() -> None:
    operation = _operation(
        created_at=NOW.replace(tzinfo=None),
        phase_started_at=(NOW + timedelta(seconds=1)).replace(tzinfo=None),
        updated_at=(NOW + timedelta(seconds=4)).replace(tzinfo=None),
    )
    result = api_serializers.operation_detail(
        operation,
        queue_position=2,
        now=NOW + timedelta(seconds=5),
    )

    assert result.priority is schemas.OperationPriorityName.NORMAL
    assert result.queue_position == 2
    assert result.queue_age_seconds == 5
    assert result.phase_age_seconds == 4
    assert result.created_at.tzinfo is UTC


def test_source_metadata_is_content_safe_and_requires_clean_scan_correlation() -> None:
    result = api_serializers.source_metadata(_document())
    dumped = result.model_dump(mode="json")

    assert result.available is True
    assert "storage_key" not in dumped
    assert "scan_signature" not in dumped

    with pytest.raises(api_serializers.SerializationError, match="clean scan verdict"):
        api_serializers.source_metadata(_document(scan_state=ScanState.ERROR))
    with pytest.raises(api_serializers.SerializationError, match="scanner correlation"):
        api_serializers.source_metadata(_document(scan_engine=None))


def test_collection_serializer_requires_every_target_state_count() -> None:
    counts = {state.value: 0 for state in DocumentState}
    counts[DocumentState.READY.value] = 2
    result = api_serializers.collection_summary(_definition(), counts)

    assert result.counts.total == 2
    assert result.counts.by_state[DocumentState.READY] == 2
    with pytest.raises(api_serializers.SerializationError, match="exact target state"):
        api_serializers.collection_summary(_definition(), {"READY": 2})


def test_markdown_and_chunk_serializers_verify_retained_hashes() -> None:
    page_markdown = "# Guide"
    page_hash = hashlib.sha256(page_markdown.encode()).hexdigest()
    rendered = f"<!-- page:1 -->\n\n{page_markdown}"
    rendered_hash = hashlib.sha256(rendered.encode()).hexdigest()
    page = SimpleNamespace(
        page_number=1,
        markdown=page_markdown,
        markdown_sha256=page_hash,
        source_projection_sha256=HASH_A,
        markdown_projection_sha256=HASH_B,
        slices=[{"slice": 1}],
    )
    revision = _revision(
        markdown_sha256=rendered_hash,
        prepared_pages=[page],
    )
    markdown = api_serializers.markdown_document(revision, rendered)
    chunk = SimpleNamespace(
        id=OPERATION_ID,
        prepared_revision_id=REVISION_ID,
        chunk_index=0,
        page_start=1,
        page_end=1,
        heading_path=["Guide"],
        token_count=2,
        text_sha256=page_hash,
        markdown=page_markdown,
    )

    assert markdown.pages[0].slice_count == 1
    assert "dense" not in api_serializers.chunk(chunk).model_dump()
    with pytest.raises(api_serializers.SerializationError, match="chunk 0 hash"):
        api_serializers.chunk(SimpleNamespace(**{**vars(chunk), "text_sha256": HASH_A}))


def test_candidate_serializer_exposes_bounded_snapshot_and_no_provider_payload() -> None:
    result = api_serializers.preflight_candidate(
        _candidate(),
        incoming_document=_document(state=DocumentState.REVIEW_REQUIRED),
    )

    assert result.replacement_eligible is True
    assert result.matched_chunk_pair_count == 1
    assert "matched_chunk_pairs" not in result.model_dump()
    assert "raw_output" not in result.model_dump()

    unsafe = _candidate()
    unsafe.document_snapshot = {"collection_key": "customer"}
    with pytest.raises(api_serializers.SerializationError, match="immutable public identity"):
        api_serializers.preflight_candidate(
            unsafe,
            incoming_document=_document(state=DocumentState.REVIEW_REQUIRED),
        )


def test_replacement_serializer_requires_exact_old_new_operation_linkage() -> None:
    incoming = _document(
        id=DOCUMENT_ID,
        state=DocumentState.PUBLISHING,
    )
    old = _document(
        id=OLD_DOCUMENT_ID,
        state=DocumentState.DELETING,
        replaced_by_document_id=DOCUMENT_ID,
    )
    decision = SimpleNamespace(
        id=CANDIDATE_ID,
        document_id=DOCUMENT_ID,
        prepared_revision_id=REVISION_ID,
        prepared_manifest_sha256=HASH_A,
        action=DecisionAction.REPLACE,
        target_document_id=OLD_DOCUMENT_ID,
        actor_type="operator",
        actor_id="operator@example.test",
        created_at=NOW,
    )
    operation = _operation(
        operation_type=OperationType.PUBLISH,
        priority=int(OperationPriority.REPLACEMENT),
        replacement_target_document_id=OLD_DOCUMENT_ID,
    )

    result = api_serializers.replacement_summary(
        decision=decision,
        old_document=old,
        new_document=incoming,
        operation=operation,
    )
    assert result.old_document_id == OLD_DOCUMENT_ID

    operation.replacement_target_document_id = uuid.uuid4()
    with pytest.raises(api_serializers.SerializationError, match="different old document"):
        api_serializers.replacement_summary(
            decision=decision,
            old_document=old,
            new_document=incoming,
            operation=operation,
        )


def test_collection_manager_paginates_configuration_with_opaque_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definitions = [_definition("customer"), _definition("internal")]
    counts = {state.value: 0 for state in DocumentState}
    records = [
        catalog_service.CollectionRecord(definition=item, counts=dict(counts))
        for item in definitions
    ]
    monkeypatch.setattr(
        catalog_manager.catalog,
        "list_collections",
        lambda session, configured: records,
    )

    first = catalog_manager.list_collections(
        object(), definitions, cursor=None, limit=1
    )
    second = catalog_manager.list_collections(
        object(), definitions, cursor=first.next_cursor, limit=1
    )

    assert [item.key for item in first.items] == ["customer"]
    assert first.has_more is True
    assert [item.key for item in second.items] == ["internal"]
    assert second.has_more is False

    with pytest.raises(LifecycleError, match="pagination cursor"):
        catalog_manager.list_collections(
            object(), definitions, cursor="bad", limit=1
        )


def test_preflight_manager_binds_candidate_cursor_to_exact_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidates = [
        _candidate(),
        _candidate(
            identifier=uuid.UUID("66666666-6666-4666-8666-666666666666"),
            rank=2,
        ),
    ]
    revision = _revision(candidates=candidates)
    incoming = _document(state=DocumentState.REVIEW_REQUIRED)
    monkeypatch.setattr(
        catalog_manager.catalog,
        "preflight_revision",
        lambda session, document_id: revision,
    )
    session = SimpleNamespace(get=lambda model, identifier: incoming)

    first = catalog_manager.get_preflight(
        session,
        document_id=DOCUMENT_ID,
        cursor=None,
        limit=1,
    )
    second = catalog_manager.get_preflight(
        session,
        document_id=DOCUMENT_ID,
        cursor=first.candidates.next_cursor,
        limit=1,
    )

    assert first.candidate_count == 2
    assert first.candidates.items[0].rank == 1
    assert second.candidates.items[0].rank == 2
    assert second.candidates.has_more is False

    other_revision = _revision(id=uuid.uuid4(), candidates=candidates)
    monkeypatch.setattr(
        catalog_manager.catalog,
        "preflight_revision",
        lambda session, document_id: other_revision,
    )
    with pytest.raises(LifecycleError, match="pagination cursor"):
        catalog_manager.get_preflight(
            session,
            document_id=DOCUMENT_ID,
            cursor=first.candidates.next_cursor,
            limit=1,
        )


@pytest.mark.parametrize(
    "state", [DocumentState.DELETING, DocumentState.DELETE_FAILED]
)
def test_preflight_evidence_is_blocked_as_soon_as_deletion_is_accepted(
    state: DocumentState,
) -> None:
    document = _document(state=state)
    session = SimpleNamespace(get=lambda model, identifier: document)

    with pytest.raises(LifecycleError) as raised:
        catalog_service.preflight_revision(session, DOCUMENT_ID)

    assert raised.value.code == "content_purged"
    assert raised.value.status == 410


def test_audit_serializer_rejects_nested_or_content_bearing_details() -> None:
    event = SimpleNamespace(
        id=1,
        document_id=DOCUMENT_ID,
        operation_id=OPERATION_ID,
        event_type="document_admitted",
        actor_type="operator",
        actor_id="operator@example.test",
        occurred_at=NOW,
        details={"state": "PREFLIGHTING", "attempt": 1},
    )
    assert api_serializers.audit_event(event).attributes["attempt"] == 1

    event.details = {"payload": {"markdown": "protected"}}
    with pytest.raises(api_serializers.SerializationError, match="content-free scalar"):
        api_serializers.audit_event(event)
