from __future__ import annotations

import uuid

from litestar.testing import TestClient

from pdf_bridge.persistence.models import (
    AnalysisCandidate,
    AnalysisStatus,
    Document,
    DocumentAnalysis,
    DocumentState,
    OperationState,
    OperationType,
    ScanState,
    WorkOperation,
    utc_now,
)

from .conftest import PDF_A, PDF_B


def _seed_review(session_factory, document_id: uuid.UUID, *, candidate: Document | None = None):
    with session_factory() as session:
        document = session.get(Document, document_id)
        assert document is not None
        operation = next(
            item
            for item in document.operations
            if item.operation_type == OperationType.ANALYZE
        )
        operation.state = OperationState.SUCCEEDED
        operation.completed_at = utc_now()
        analysis = DocumentAnalysis(
            document=document,
            revision=1,
            status=AnalysisStatus.COMPLETE,
            collection_epoch=document.collection_epoch or 1,
            semantic_complete=candidate is None,
            classification_complete=candidate is None,
            auto_ingest_eligible=False,
            candidate_count=1 if candidate else 0,
            completed_at=utc_now(),
        )
        session.add(analysis)
        session.flush()
        if candidate is not None:
            session.add(
                AnalysisCandidate(
                    analysis=analysis,
                    matched_document_id=candidate.id,
                    source="active",
                    rank=1,
                    reasons=["filename_family"],
                    document_snapshot={
                        "document_id": str(candidate.id),
                        "filename": candidate.original_filename,
                        "collection_key": candidate.collection_key,
                    },
                )
            )
        document.analysis_revision = 1
        document.state = DocumentState.REVIEW_REQUIRED
        session.commit()
        return analysis.id


def _ingested_document(
    session_factory,
    *,
    filename: str = "existing report.pdf",
    collection_key: str = "customer",
    sha256: str = "a" * 64,
) -> Document:
    with session_factory() as session:
        document = Document(
            original_filename=filename,
            normalized_filename=filename.casefold(),
            size_bytes=100,
            sha256=sha256,
            idempotency_key=f"seed-{uuid.uuid4()}",
            state=DocumentState.INGESTED,
            scan_state=ScanState.CLEAN,
            uploader_identity="seed",
            collection_key=collection_key,
            collection_epoch=1,
            ingested_at=utc_now(),
        )
        session.add(document)
        session.commit()
        session.refresh(document)
        session.expunge(document)
        return document


def test_upload_returns_202_analysis_resource_and_persists_operation(
    upload_pdf, session_factory
) -> None:
    response = upload_pdf(filename="Quarterly Plan.pdf")

    assert response.status_code == 202
    body = response.json()
    upload = body["upload"]
    assert body["idempotent_replay"] is False
    assert upload["document"]["state"] == "ANALYZING"
    assert upload["operation"]["operation_type"] == "ANALYZE"
    assert upload["operation"]["state"] == "QUEUED"
    assert upload["operation"]["phase"] == "QUEUED"
    assert upload["status_url"].endswith(upload["upload_id"])

    with session_factory() as session:
        operation = session.get(WorkOperation, uuid.UUID(upload["operation"]["id"]))
        assert operation is not None
        assert operation.document_id == uuid.UUID(upload["upload_id"])


def test_upload_idempotency_replays_only_the_identical_request(upload_pdf) -> None:
    first = upload_pdf(filename="same.pdf", key="same-upload-key")
    replay = upload_pdf(filename="same.pdf", key="same-upload-key")
    conflict = upload_pdf(
        filename="different.pdf", contents=PDF_B, key="same-upload-key"
    )

    assert first.status_code == 202
    assert replay.status_code == 202
    assert replay.json()["idempotent_replay"] is True
    assert replay.json()["upload"]["upload_id"] == first.json()["upload"]["upload_id"]
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "idempotency-key-conflict"


def test_exact_bytes_block_only_within_the_selected_collection(upload_pdf) -> None:
    first = upload_pdf(filename="customer.pdf", key="customer-copy", collection="customer")
    same_collection = upload_pdf(
        filename="renamed.pdf", key="customer-duplicate", collection="customer"
    )
    other_collection = upload_pdf(
        filename="internal.pdf", key="internal-copy", collection="internal"
    )

    assert first.status_code == 202
    assert same_collection.status_code == 409
    assert same_collection.json()["code"] == "exact-duplicate"
    assert same_collection.json()["duplicate"]["collection_key"] == "customer"
    assert other_collection.status_code == 202


def test_preflight_returns_typed_collection_scoped_filename_warnings(
    client: TestClient, csrf_headers, session_factory
) -> None:
    _ingested_document(
        session_factory,
        filename="Customer Monthly Report May 2026.pdf",
        collection_key="customer",
    )

    customer = client.post(
        "/api/v1/uploads/preflight",
        headers=csrf_headers,
        json={
            "filename": "Customer Monthly Report June 2026.pdf",
            "size_bytes": 100,
            "collection_key": "customer",
        },
    )
    internal = client.post(
        "/api/v1/uploads/preflight",
        headers=csrf_headers,
        json={
            "filename": "Customer Monthly Report June 2026.pdf",
            "size_bytes": 100,
            "collection_key": "internal",
        },
    )

    assert customer.status_code == 200
    assert customer.json()["warnings"]
    assert customer.json()["warnings"][0]["matched"]["collection_key"] == "customer"
    assert internal.status_code == 200
    assert internal.json()["warnings"] == []


def test_upload_validation_fails_closed_without_catalog_mutation(
    client: TestClient, csrf_headers, session_factory
) -> None:
    missing_key = client.post(
        "/api/v1/uploads",
        headers=csrf_headers,
        files={"file": ("one.pdf", PDF_A, "application/pdf")},
        data={"collection_key": "customer"},
    )
    wrong_type = client.post(
        "/api/v1/uploads",
        headers={**csrf_headers, "Idempotency-Key": "wrong-type-key"},
        files={"file": ("one.pdf", PDF_A, "text/plain")},
        data={"collection_key": "customer"},
    )
    unknown_collection = client.post(
        "/api/v1/uploads",
        headers={**csrf_headers, "Idempotency-Key": "unknown-collection"},
        files={"file": ("one.pdf", PDF_A, "application/pdf")},
        data={"collection_key": "unknown"},
    )

    assert missing_key.status_code == 422
    assert missing_key.json()["code"] == "invalid-idempotency-key"
    assert wrong_type.status_code == 422
    assert wrong_type.json()["code"] == "invalid-content-type"
    assert unknown_collection.status_code == 422
    with session_factory() as session:
        assert session.query(Document).count() == 0


def test_delete_upload_cancels_unpublished_queued_work(
    client: TestClient, csrf_headers, upload_pdf, session_factory
) -> None:
    accepted = upload_pdf(key="cancel-upload-key")
    upload_id = accepted.json()["upload"]["upload_id"]

    response = client.delete(
        f"/api/v1/uploads/{upload_id}", headers=csrf_headers
    )

    assert response.status_code == 200
    assert response.json()["document"]["state"] == "CLEANUP_PENDING"
    with session_factory() as session:
        operations = session.query(WorkOperation).filter_by(
            document_id=uuid.UUID(upload_id)
        ).all()
        assert {item.operation_type for item in operations} == {
            OperationType.ANALYZE,
            OperationType.CLEANUP,
        }
        assert next(
            item for item in operations if item.operation_type == OperationType.ANALYZE
        ).state == OperationState.CANCELLED


def test_keep_decision_is_revision_bound_and_idempotent(
    client: TestClient, csrf_headers, upload_pdf, session_factory
) -> None:
    accepted = upload_pdf(key="review-upload-key")
    upload_id = uuid.UUID(accepted.json()["upload"]["upload_id"])
    _seed_review(session_factory, upload_id)
    headers = {**csrf_headers, "Idempotency-Key": "keep-decision-key"}

    stale = client.post(
        f"/api/v1/uploads/{upload_id}/decision",
        headers=headers,
        json={"analysis_revision": 2, "action": "keep"},
    )
    kept = client.post(
        f"/api/v1/uploads/{upload_id}/decision",
        headers=headers,
        json={"analysis_revision": 1, "action": "keep"},
    )
    replay = client.post(
        f"/api/v1/uploads/{upload_id}/decision",
        headers=headers,
        json={"analysis_revision": 1, "action": "keep"},
    )

    assert stale.status_code == 409
    assert stale.json()["code"] == "stale-analysis-revision"
    assert kept.status_code == 200
    assert kept.json()["document"]["state"] == "INGESTING"
    assert replay.status_code == 200
    assert replay.json()["idempotent_replay"] is True


def test_unstarted_ingestion_can_still_be_cancelled(
    client: TestClient, csrf_headers, upload_pdf, session_factory
) -> None:
    accepted = upload_pdf(key="cancel-ingestion-upload")
    upload_id = uuid.UUID(accepted.json()["upload"]["upload_id"])
    _seed_review(session_factory, upload_id)
    kept = client.post(
        f"/api/v1/uploads/{upload_id}/decision",
        headers={**csrf_headers, "Idempotency-Key": "cancel-ingestion-keep"},
        json={"analysis_revision": 1, "action": "keep"},
    )
    assert kept.status_code == 200
    assert kept.json()["document"]["state"] == "INGESTING"

    cancelled = client.delete(
        f"/api/v1/uploads/{upload_id}", headers=csrf_headers
    )

    assert cancelled.status_code == 200
    assert cancelled.json()["document"]["state"] == "CLEANUP_PENDING"
    with session_factory() as session:
        ingest = session.query(WorkOperation).filter_by(
            document_id=upload_id,
            operation_type=OperationType.INGEST,
        ).one()
        assert ingest.state == OperationState.CANCELLED


def test_decision_contract_forbids_rationale_and_invalid_targets(
    client: TestClient, csrf_headers, upload_pdf, session_factory
) -> None:
    accepted = upload_pdf(key="decision-contract-key")
    upload_id = uuid.UUID(accepted.json()["upload"]["upload_id"])
    _seed_review(session_factory, upload_id)
    headers = {**csrf_headers, "Idempotency-Key": "decision-shape-key"}

    rationale = client.post(
        f"/api/v1/uploads/{upload_id}/decision",
        headers=headers,
        json={"analysis_revision": 1, "action": "keep", "rationale": "legacy"},
    )
    missing_target = client.post(
        f"/api/v1/uploads/{upload_id}/decision",
        headers=headers,
        json={"analysis_revision": 1, "action": "replace"},
    )
    keep_target = client.post(
        f"/api/v1/uploads/{upload_id}/decision",
        headers=headers,
        json={
            "analysis_revision": 1,
            "action": "keep",
            "target_document_id": str(uuid.uuid4()),
        },
    )

    assert rationale.status_code == 400
    assert missing_target.status_code == 400
    assert keep_target.status_code == 400


def test_replace_requires_an_ingested_same_collection_candidate(
    client: TestClient, csrf_headers, upload_pdf, session_factory
) -> None:
    target = _ingested_document(session_factory, filename="old.pdf")
    cross_collection = _ingested_document(
        session_factory,
        filename="internal.pdf",
        collection_key="internal",
        sha256="b" * 64,
    )
    accepted = upload_pdf(key="replacement-upload-key", contents=PDF_B)
    upload_id = uuid.UUID(accepted.json()["upload"]["upload_id"])
    _seed_review(session_factory, upload_id, candidate=target)

    cross = client.post(
        f"/api/v1/uploads/{upload_id}/decision",
        headers={**csrf_headers, "Idempotency-Key": "replace-cross-key"},
        json={
            "analysis_revision": 1,
            "action": "replace",
            "target_document_id": str(cross_collection.id),
        },
    )
    replaced = client.post(
        f"/api/v1/uploads/{upload_id}/decision",
        headers={**csrf_headers, "Idempotency-Key": "replace-valid-key"},
        json={
            "analysis_revision": 1,
            "action": "replace",
            "target_document_id": str(target.id),
        },
    )

    assert cross.status_code == 422
    assert cross.json()["code"] == "replacement-cross-collection"
    assert replaced.status_code == 200
    assert replaced.json()["document"]["state"] == "REPLACING"


def test_open_upload_listing_restores_review_work(
    client: TestClient, upload_pdf, session_factory
) -> None:
    first = upload_pdf(key="open-first-key")
    second = upload_pdf(key="open-second-key", contents=PDF_B)
    second_id = uuid.UUID(second.json()["upload"]["upload_id"])
    _seed_review(session_factory, second_id)

    response = client.get("/api/v1/uploads?open=true&page_size=100")

    assert response.status_code == 200
    rows = {item["upload_id"]: item for item in response.json()["items"]}
    assert set(rows) == {
        first.json()["upload"]["upload_id"],
        second.json()["upload"]["upload_id"],
    }
    assert rows[str(second_id)]["review_required"] is True
    assert rows[str(second_id)]["analysis_url"].endswith("/analysis")
