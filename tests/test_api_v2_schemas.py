from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from pdf_bridge.contracts import schemas
from pdf_bridge.persistence.models import (
    CandidateSource,
    DecisionAction,
    DeletionPhase,
    DocumentState,
    EvidenceKind,
    OperationPhase,
    OperationState,
    OperationType,
    PublicationStatus,
    RevisionStatus,
    ScanState,
    TerminalDisposition,
)

DOCUMENT_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
REVISION_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
OPERATION_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")
OTHER_ID = uuid.UUID("44444444-4444-4444-8444-444444444444")
NOW = datetime(2026, 7, 14, 5, 0, tzinfo=UTC)
HASH_A = "a" * 64
HASH_B = "b" * 64
PROFILE_A = "sha256:" + "a" * 64
PROFILE_B = "sha256:" + "b" * 64
PROFILE_C = "sha256:" + "c" * 64


def _failure() -> schemas.SanitizedFailure:
    return schemas.SanitizedFailure(
        code="formatter_invalid",
        message="The formatter response was invalid.",
        retryable=True,
        phase=OperationPhase.VALIDATING_MARKDOWN,
    )


def _operation() -> schemas.OperationSummary:
    return schemas.OperationSummary(
        id=OPERATION_ID,
        operation_type=OperationType.PREFLIGHT,
        state=OperationState.QUEUED,
        phase=OperationPhase.QUEUED,
        priority=schemas.OperationPriorityName.NORMAL,
        attempt=1,
        retryable=True,
        created_at=NOW,
        updated_at=NOW,
    )


def _document(**updates: object) -> schemas.DocumentSummary:
    values: dict[str, object] = {
        "id": DOCUMENT_ID,
        "collection_key": "customer",
        "original_filename": "guide.pdf",
        "content_type": "application/pdf",
        "size_bytes": 1_024,
        "sha256": HASH_A,
        "created_by": "operator@example.test",
        "state": DocumentState.PREFLIGHTING,
        "created_at": NOW,
        "updated_at": NOW,
        "allowed_actions": [schemas.AllowedAction.DELETE],
    }
    values.update(updates)
    return schemas.DocumentSummary(**values)


def _completeness() -> schemas.PreflightCompleteness:
    return schemas.PreflightCompleteness(
        native_text_eligible=True,
        formatter_complete=True,
        vector_complete=True,
        candidate_discovery_complete=True,
        advisory_complete=True,
        clear_for_publication=True,
    )


def _revision() -> schemas.PreparedRevisionSummary:
    return schemas.PreparedRevisionSummary(
        id=REVISION_ID,
        revision_number=1,
        status=RevisionStatus.SEALED,
        active_qdrant_collection="customer-product-pdfs",
        content_profile_id=PROFILE_A,
        index_profile_id=PROFILE_B,
        preflight_policy_id=PROFILE_C,
        formatter_model_id="formatter-model-v1",
        dense_model_id="sentence-transformers/all-mpnet-base-v2",
        dense_dimension=768,
        sparse_model_id="Qdrant/bm25",
        language_code="en",
        completeness=_completeness(),
        page_count=2,
        chunk_count=3,
        expected_point_count=3,
        markdown_sha256=HASH_A,
        manifest_sha256=HASH_B,
        created_at=NOW,
        sealed_at=NOW,
    )


def _all_state_counts(**updates: int) -> schemas.CollectionStateCounts:
    counts = {state: 0 for state in DocumentState}
    for name, count in updates.items():
        counts[DocumentState[name]] = count
    return schemas.CollectionStateCounts(total=sum(counts.values()), by_state=counts)


def test_only_api_v2_contract_names_remain() -> None:
    for target_name in (
        "ErrorResponse",
        "CollectionListResponse",
        "CollectionDetail",
        "NameCheckRequest",
        "UploadAcceptedResponse",
        "DocumentDetail",
        "MarkdownDocument",
        "ChunkListResponse",
        "PreflightResponse",
        "DecisionRequest",
        "MutationResponse",
        "EventListResponse",
        "OperationDetail",
        "HistoryResponse",
        "HealthResponse",
        "OperatorSearchRequest",
        "OperatorSearchResponse",
    ):
        assert hasattr(schemas, target_name), target_name

    for retired_name in (
        "ProblemDetail",
        "UploadResource",
        "UploadListResponse",
        "UploadPreflightRequest",
        "AnalysisSummary",
        "AnalysisDetailResponse",
        "HistoricalManifestDocument",
        "HistoricalImportManifest",
        "HistoricalImportResponse",
    ):
        assert not hasattr(schemas, retired_name), retired_name


def test_error_response_is_nested_sanitized_and_strict() -> None:
    response = schemas.ErrorResponse(
        error={
            "code": "document_state_conflict",
            "message": "The action is not valid in the current state.",
            "request_id": OPERATION_ID,
            "retryable": False,
        }
    )

    assert response.model_dump(mode="json") == {
        "error": {
            "code": "document_state_conflict",
            "message": "The action is not valid in the current state.",
            "request_id": str(OPERATION_ID),
            "retryable": False,
        }
    }
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        schemas.ErrorResponse(
            error={
                **response.error.model_dump(),
                "raw_provider_output": "secret",
            }
        )


def test_cursor_envelopes_are_opaque_bounded_and_consistent() -> None:
    page = schemas.DocumentListResponse(
        items=[_document()],
        limit=1,
        next_cursor="v2.eyJpZCI6IjEyMyJ9",
        has_more=True,
    )
    assert page.next_cursor == "v2.eyJpZCI6IjEyMyJ9"

    with pytest.raises(ValidationError, match="less than or equal to 100"):
        schemas.CursorQuery(limit=101)
    with pytest.raises(ValidationError, match="has_more"):
        schemas.DocumentListResponse(items=[], limit=10, has_more=True)
    with pytest.raises(ValidationError, match="pattern"):
        schemas.CursorQuery(cursor="not an opaque cursor")


def test_collection_resources_require_exact_state_counts_and_fixed_target() -> None:
    detail = schemas.CollectionDetail(
        key="customer",
        display_name="Customer Product",
        description="Approved customer-facing content.",
        audience="customer",
        enabled=True,
        counts=_all_state_counts(READY=2, REVIEW_REQUIRED=1),
        target={
            "qdrant_collection_name": "customer-product-pdfs",
            "schema_version": 2,
            "schema_compatible": True,
        },
    )
    response = schemas.CollectionListResponse(items=[detail], limit=50)

    assert response.items[0].counts.total == 3
    assert detail.target.qdrant_collection_name == "customer-product-pdfs"

    incomplete = {DocumentState.READY: 1}
    with pytest.raises(ValidationError, match="every target document state"):
        schemas.CollectionStateCounts(total=1, by_state=incomplete)


@pytest.mark.parametrize("filename", ["../guide.pdf", "folder/guide.pdf", "guide.txt", " \t "])
def test_name_check_accepts_only_bounded_path_free_pdf_names(filename: str) -> None:
    with pytest.raises(ValidationError, match="path-free PDF filename"):
        schemas.NameCheckRequest(filename=filename)


def test_name_check_result_is_collection_scoped_and_bounded() -> None:
    response = schemas.NameCheckResponse(
        collection_key="customer",
        normalized_filename="guide.pdf",
        matches=[
            {
                "kind": "EXACT_NAME",
                "document_id": DOCUMENT_ID,
                "original_filename": "Guide.pdf",
                "state": DocumentState.READY,
                "similarity": 1.0,
            }
        ],
    )
    assert response.matches[0].document_id == DOCUMENT_ID


def test_upload_and_mutation_responses_return_typed_document_and_operation() -> None:
    upload = schemas.UploadAcceptedResponse(document=_document(), operation=_operation())
    mutation = schemas.MutationResponse(document=_document(), operation=_operation())

    assert upload.document.state is DocumentState.PREFLIGHTING
    assert mutation.operation.operation_type is OperationType.PREFLIGHT


def test_document_detail_contains_only_public_nested_summaries() -> None:
    source = schemas.SourceMetadata(
        original_filename="guide.pdf",
        content_type="application/pdf",
        size_bytes=1_024,
        sha256=HASH_A,
        created_by="operator@example.test",
        created_at=NOW,
        scan_state=ScanState.CLEAN,
        scan_engine="clamav",
        scanned_at=NOW,
        available=True,
    )
    publication = schemas.PublicationSummary(
        id=OTHER_ID,
        prepared_revision_id=REVISION_ID,
        active_qdrant_collection="customer-product-pdfs",
        status=PublicationStatus.VERIFIED,
        expected_points=3,
        verified_points=3,
        payload_revision_verified=True,
        vector_schema_verified=True,
        screening_zero_verified=True,
        created_at=NOW,
        updated_at=NOW,
        verified_at=NOW,
    )
    detail = schemas.DocumentDetail(
        **_document(state=DocumentState.READY, ready_at=NOW).model_dump(),
        source=source,
        prepared_revision=_revision(),
        publication=publication,
        current_operation=_operation(),
    )
    dumped = detail.model_dump(mode="json")

    assert dumped["prepared_revision"]["dense_dimension"] == 768
    assert dumped["publication"]["verified_points"] == 3
    assert "storage_key" not in str(dumped)
    assert "raw_provider_output" not in str(dumped)


def test_document_summary_serializes_from_orm_style_attributes() -> None:
    source = SimpleNamespace(**_document().model_dump())
    result = schemas.DocumentSummary.model_validate(source)
    assert result.id == DOCUMENT_ID


def test_markdown_pages_must_be_complete_and_ordered() -> None:
    page = schemas.MarkdownPage(
        page_number=1,
        markdown="# Guide",
        markdown_sha256=HASH_A,
        source_projection_sha256=HASH_A,
        markdown_projection_sha256=HASH_A,
        slice_count=1,
    )
    document = schemas.MarkdownDocument(
        document_id=DOCUMENT_ID,
        prepared_revision_id=REVISION_ID,
        markdown_sha256=HASH_A,
        markdown="<!-- page:1 -->\n\n# Guide",
        pages=[page],
    )
    assert document.pages[0].page_number == 1

    with pytest.raises(ValidationError, match="complete and in one-based order"):
        schemas.MarkdownDocument(
            document_id=DOCUMENT_ID,
            prepared_revision_id=REVISION_ID,
            markdown_sha256=HASH_A,
            markdown="# Guide",
            pages=[page.model_copy(update={"page_number": 2})],
        )


def test_chunk_page_has_provenance_and_no_numeric_vectors() -> None:
    chunk = schemas.Chunk(
        id=OTHER_ID,
        prepared_revision_id=REVISION_ID,
        chunk_index=0,
        page_start=1,
        page_end=2,
        heading_path=["Guide", "Install"],
        token_count=320,
        text_sha256=HASH_A,
        markdown="## Install\n\nRun the installer.",
    )
    response = schemas.ChunkListResponse(
        document_id=DOCUMENT_ID,
        prepared_revision_id=REVISION_ID,
        items=[chunk],
        limit=50,
    )

    assert response.items[0].token_count == 320
    assert not {
        "dense",
        "sparse_indices",
        "sparse_values",
        "vector",
    } & set(schemas.Chunk.model_fields)


def test_preflight_response_has_typed_completeness_candidates_and_evidence() -> None:
    evidence = schemas.PreflightEvidence(
        id=OPERATION_ID,
        kind=EvidenceKind.CLASSIFIER,
        model_id="classifier-v1",
        valid=True,
        label="likely_revision",
        summary="The documents overlap substantially.",
        citations=[
            {
                "document_id": DOCUMENT_ID,
                "chunk_id": OTHER_ID,
                "page_start": 1,
                "page_end": 1,
                "excerpt": "A bounded source-backed excerpt.",
            }
        ],
        evidence_sha256=HASH_A,
        created_at=NOW,
    )
    candidate = schemas.PreflightCandidate(
        id=OTHER_ID,
        document={
            "id": DOCUMENT_ID,
            "collection_key": "customer",
            "original_filename": "old-guide.pdf",
            "state": DocumentState.READY,
            "sha256": HASH_B,
        },
        source=CandidateSource.ACTIVE,
        rank=1,
        reasons=["semantic_match"],
        max_cosine=0.96,
        bm25_score=7.5,
        fused_score=0.03,
        matched_chunk_pair_count=2,
        replacement_eligible=True,
        evidence=[evidence],
    )
    response = schemas.PreflightResponse(
        document_id=DOCUMENT_ID,
        prepared_revision=_revision(),
        completeness=_completeness(),
        candidate_count=1,
        candidates={"items": [candidate], "limit": 25},
    )

    assert response.candidates.items[0].evidence[0].kind is EvidenceKind.CLASSIFIER
    assert "prompt" not in schemas.PreflightEvidence.model_fields
    assert "raw_output" not in schemas.PreflightEvidence.model_fields


@pytest.mark.parametrize("action", [DecisionAction.KEEP, DecisionAction.CANCEL])
def test_keep_and_cancel_forbid_replacement_target(action: DecisionAction) -> None:
    with pytest.raises(ValidationError, match="only REPLACE"):
        schemas.DecisionRequest(
            prepared_revision_id=REVISION_ID,
            action=action,
            target_document_id=OTHER_ID,
        )


def test_revision_bound_decisions_use_uppercase_actions_and_exact_target_rules() -> None:
    request = schemas.DecisionRequest(
        prepared_revision_id=REVISION_ID,
        action="REPLACE",
        target_document_id=OTHER_ID,
    )
    assert request.action is DecisionAction.REPLACE

    with pytest.raises(ValidationError, match="REPLACE requires"):
        schemas.DecisionRequest(prepared_revision_id=REVISION_ID, action="REPLACE")
    with pytest.raises(ValidationError, match="KEEP|DecisionAction"):
        schemas.DecisionRequest(prepared_revision_id=REVISION_ID, action="keep")
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        schemas.DecisionRequest(
            prepared_revision_id=REVISION_ID,
            action="KEEP",
            analysis_revision=1,
        )


def test_retry_request_has_no_mutable_body_fields() -> None:
    assert schemas.RetryRequest().model_dump() == {}
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        schemas.RetryRequest(reason="try again")


def test_operation_detail_exposes_named_priority_queue_and_sanitized_failure() -> None:
    operation = schemas.OperationDetail(
        **_operation().model_dump(),
        document_id=DOCUMENT_ID,
        prepared_revision_id=REVISION_ID,
        queue_position=2,
        queue_age_seconds=1.5,
        phase_age_seconds=0.5,
        failure=_failure(),
    )
    assert operation.priority is schemas.OperationPriorityName.NORMAL
    assert operation.failure is not None
    assert operation.failure.code == "formatter_invalid"


def test_events_and_history_are_content_free_cursor_resources() -> None:
    event = schemas.AuditEventResponse(
        id=1,
        document_id=DOCUMENT_ID,
        operation_id=OPERATION_ID,
        event_type="DOCUMENT_ACCEPTED",
        actor_type="operator",
        actor_id="operator@example.test",
        occurred_at=NOW,
        attributes={"state": "PREFLIGHTING", "attempt": 1},
    )
    events = schemas.EventListResponse(
        document_id=DOCUMENT_ID,
        items=[event],
        limit=50,
    )
    tombstone = schemas.TombstoneSummary(
        id=OTHER_ID,
        document_id=DOCUMENT_ID,
        collection_key="customer",
        disposition=TerminalDisposition.DELETED,
        source_sha256=HASH_A,
        manifest_sha256=HASH_B,
        reason_code="operator_delete",
        actor_type="operator",
        actor_id="operator@example.test",
        occurred_at=NOW,
    )
    history = schemas.HistoryResponse(items=[tombstone], limit=50)

    assert events.items[0].attributes["attempt"] == 1
    assert history.items[0].disposition is TerminalDisposition.DELETED
    assert not {"markdown", "text", "vectors", "prompt"} & set(
        schemas.TombstoneSummary.model_fields
    )


def test_deletion_and_replacement_summaries_use_target_states_and_phases() -> None:
    deletion = schemas.DeletionSummary(
        terminal_disposition=TerminalDisposition.DELETED,
        phase=DeletionPhase.VERIFY_ACTIVE_ZERO,
        active_qdrant_collection="customer-product-pdfs",
        screening_qdrant_collection="pdf-bridge-screening",
        attempts=1,
        active_zero_verified_at=NOW,
        updated_at=NOW,
    )
    replacement = schemas.ReplacementSummary(
        decision_id=OPERATION_ID,
        old_document_id=DOCUMENT_ID,
        new_document_id=OTHER_ID,
        old_document_state=DocumentState.DELETING,
        new_document_state=DocumentState.REVIEW_REQUIRED,
        operation_id=OPERATION_ID,
        phase=OperationPhase.DELETE_ACTIVE_POINTS,
    )
    assert deletion.phase is DeletionPhase.VERIFY_ACTIVE_ZERO
    assert replacement.old_document_state is DocumentState.DELETING


def test_health_contract_contains_only_content_free_component_status() -> None:
    response = schemas.HealthResponse(
        status="NOT_READY",
        checks=[
            {
                "component": "qdrant.customer",
                "status": "NOT_READY",
                "failure_code": "schema_drift",
                "message": "The configured schema is incompatible.",
            }
        ],
    )
    assert response.checks[0].failure_code == "schema_drift"


def test_operator_search_is_single_collection_bounded_and_correlated() -> None:
    request = schemas.OperatorSearchRequest(
        collection_key="customer",
        query="  installation guide  ",
        mode="hybrid",
        limit=20,
    )
    hit = schemas.OperatorSearchHit(
        rank=1,
        document_id=DOCUMENT_ID,
        prepared_revision_id=REVISION_ID,
        collection_key="customer",
        original_filename="guide.pdf",
        chunk_id=OTHER_ID,
        page_start=1,
        page_end=2,
        heading_path=["Installation"],
        score=0.87,
        excerpt="Install the package.",
    )
    response = schemas.OperatorSearchResponse(
        collection_key="customer",
        query=request.query,
        mode=request.mode,
        results=[hit],
    )

    assert request.query == "installation guide"
    assert response.results[0].document_id == DOCUMENT_ID

    with pytest.raises(ValidationError, match="requested collection"):
        schemas.OperatorSearchResponse(
            collection_key="internal",
            query="guide",
            mode="hybrid",
            results=[hit],
        )
    with pytest.raises(ValidationError, match="finite number"):
        schemas.OperatorSearchHit(**{**hit.model_dump(), "score": float("nan")})


def test_uuid_inputs_are_canonical_and_timestamps_are_utc() -> None:
    with pytest.raises(ValidationError, match="lowercase canonical"):
        schemas.DecisionRequest(
            prepared_revision_id="AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
            action="KEEP",
        )

    non_utc = NOW.astimezone(timezone(timedelta(hours=1)))
    with pytest.raises(ValidationError, match="UTC"):
        _document(created_at=non_utc)
    with pytest.raises(ValidationError, match="timezone-aware"):
        _document(created_at=NOW.replace(tzinfo=None))


def test_all_request_models_forbid_unknown_fields() -> None:
    request_models = (
        schemas.CursorQuery,
        schemas.DocumentListQuery,
        schemas.NameCheckRequest,
        schemas.DecisionRequest,
        schemas.RetryRequest,
        schemas.HistoryQuery,
        schemas.OperatorSearchRequest,
    )
    for model in request_models:
        assert model.model_config["extra"] == "forbid"


def test_public_models_cannot_represent_protected_artifacts_or_numeric_vectors() -> None:
    public_models = (
        schemas.DocumentSummary,
        schemas.DocumentDetail,
        schemas.SourceMetadata,
        schemas.PreparedRevisionSummary,
        schemas.MarkdownPage,
        schemas.MarkdownDocument,
        schemas.Chunk,
        schemas.PreflightEvidence,
        schemas.PreflightCandidate,
        schemas.PublicationSummary,
        schemas.DeletionSummary,
        schemas.TombstoneSummary,
    )
    forbidden = {
        "storage_key",
        "layout_text",
        "request_body",
        "response_body",
        "prompt",
        "raw_output",
        "provider_output",
        "dense",
        "sparse_indices",
        "sparse_values",
        "vector",
        "vectors",
    }
    for model in public_models:
        assert not forbidden & set(model.model_fields), model.__name__
