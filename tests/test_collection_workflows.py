from __future__ import annotations

import asyncio
import json
import uuid

import httpx
import pytest
from litestar.testing import TestClient
from sqlalchemy.orm import Session, sessionmaker

from pdf_bridge.models import (
    Document,
    DocumentState,
    LanguageCode,
    LanguageStatus,
    OperationState,
    ScanState,
    utc_now,
)
from tests.conftest import PDF_A, PDF_B


def _catalog_document(
    *,
    filename: str,
    collection_key: str | None,
    state: DocumentState,
    language: LanguageCode,
) -> Document:
    document_id = uuid.uuid4()
    if state == DocumentState.CLASSIFICATION_REVIEW:
        language_status = LanguageStatus.REVIEW_REQUIRED
    elif language == LanguageCode.UND:
        language_status = LanguageStatus.PENDING
    else:
        language_status = LanguageStatus.DETECTED
    return Document(
        id=document_id,
        original_filename=filename,
        normalized_filename=filename.casefold(),
        storage_key=f"objects/{document_id}.pdf",
        size_bytes=1024,
        sha256=document_id.hex * 2,
        content_type="application/pdf",
        idempotency_key=f"collection-workflow:{document_id}",
        state=state,
        collection_key=collection_key,
        language=language,
        language_status=language_status,
        language_method=("acceptance-fixture" if language != LanguageCode.UND else None),
        language_reason=(
            "low_confidence" if state == DocumentState.CLASSIFICATION_REVIEW else None
        ),
        language_detected_at=(
            utc_now() if language_status != LanguageStatus.PENDING else None
        ),
        scan_state=ScanState.CLEAN,
        scan_engine="test-clamd",
        scanned_at=utc_now(),
        uploader_identity="collection-workflow-test",
        uploaded_at=utc_now(),
        ingested_at=(utc_now() if state == DocumentState.INGESTED else None),
    )


def _claim_manifest(
    client: TestClient,
    job_headers: dict[str, str],
    *,
    request_id: str,
) -> tuple[dict, dict]:
    claimed = client.post(
        "/api/v1/jobs/batches/claim",
        headers=job_headers,
        json={"request_id": request_id, "limit": 100},
    )
    assert claimed.status_code == 200, claimed.text
    batch = claimed.json()
    response = client.get(
        f"/api/v1/jobs/batches/{batch['batch_id']}/manifest",
        headers=job_headers,
    )
    assert response.status_code == 200, response.text
    return batch, response.json()


def _stage_manifest(
    client: TestClient,
    job_headers: dict[str, str],
    *,
    batch: dict,
    manifest: dict,
) -> None:
    response = client.post(
        f"/api/v1/jobs/batches/{batch['batch_id']}/staged",
        headers=job_headers,
        json={
            "operation_ids": [operation["operation_id"] for operation in manifest["operations"]]
        },
    )
    assert response.status_code == 200, response.text


def _send_uploaded_document_to_review(
    client: TestClient,
    upload_pdf,
    job_headers: dict[str, str],
    *,
    key: str,
) -> tuple[str, str]:
    uploaded = upload_pdf(filename="ambiguous.pdf", contents=PDF_B, key=key)
    assert uploaded.status_code == 201, uploaded.text
    document_id = uploaded.json()["document"]["id"]
    batch, manifest = _claim_manifest(
        client,
        job_headers,
        request_id=f"review-{key}",
    )
    _stage_manifest(client, job_headers, batch=batch, manifest=manifest)
    operation_id = manifest["operations"][0]["operation_id"]
    reported = client.post(
        f"/api/v1/jobs/batches/{batch['batch_id']}/results",
        headers=job_headers,
        json={
            "pipeline_run_id": f"pipeline-{key}",
            "results": [
                {
                    "operation_id": operation_id,
                    "outcome": "review_required",
                    "components": {
                        "pdf_source": "succeeded",
                        "markdown": "succeeded",
                        "bm25": "not_applicable",
                        "dense": "not_applicable",
                    },
                    "classification": {
                        "language": "und",
                        "status": "review_required",
                        "method": "downstream-parser",
                        "confidence": 0.41,
                        "reason": "low_confidence",
                    },
                }
            ],
        },
    )
    assert reported.status_code == 200, reported.text
    assert reported.json()["review_required"] == 1
    return document_id, operation_id


def test_collections_endpoint_reports_catalog_and_language_counts(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    documents = [
        _catalog_document(
            filename="customer-en.pdf",
            collection_key="customer",
            state=DocumentState.INGESTED,
            language=LanguageCode.EN,
        ),
        _catalog_document(
            filename="customer-fr.pdf",
            collection_key="customer",
            state=DocumentState.INGESTED,
            language=LanguageCode.FR,
        ),
        _catalog_document(
            filename="customer-processing.pdf",
            collection_key="customer",
            state=DocumentState.QUEUED,
            language=LanguageCode.UND,
        ),
        _catalog_document(
            filename="customer-cleanup.pdf",
            collection_key="customer",
            state=DocumentState.DELETE_CLEANUP,
            language=LanguageCode.EN,
        ),
        _catalog_document(
            filename="customer-review.pdf",
            collection_key="customer",
            state=DocumentState.CLASSIFICATION_REVIEW,
            language=LanguageCode.UND,
        ),
        _catalog_document(
            filename="internal-fr.pdf",
            collection_key="internal",
            state=DocumentState.INGESTED,
            language=LanguageCode.FR,
        ),
    ]
    with session_factory() as session:
        session.add_all(documents)
        session.commit()

    response = client.get("/api/v1/collections")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["total"] == 2
    collections = {item["key"]: item for item in payload["items"]}
    assert collections["customer"] == {
        "key": "customer",
        "display_name": "Customer Product",
        "description": "Approved customer-facing product content.",
        "audience": "customer",
        "available_documents": 2,
        "processing_documents": 2,
        "review_documents": 1,
        "languages": {"en": 1, "fr": 1, "und": 0},
        "detail_url": "/library/customer",
    }
    assert collections["internal"]["audience"] == "internal"
    assert collections["internal"]["available_documents"] == 1
    assert collections["internal"]["processing_documents"] == 0
    assert collections["internal"]["review_documents"] == 0
    assert collections["internal"]["languages"] == {"en": 0, "fr": 1, "und": 0}


def test_upload_requires_configured_collection_and_locks_idempotency_scope(
    client: TestClient,
    csrf_headers: dict[str, str],
    upload_pdf,
) -> None:
    missing_preflight = client.post(
        "/api/v1/uploads/preflight",
        headers=csrf_headers,
        json={"filename": "missing.pdf", "size_bytes": len(PDF_A)},
    )
    assert missing_preflight.status_code == 400
    assert missing_preflight.json()["status_code"] == 400

    unknown_preflight = client.post(
        "/api/v1/uploads/preflight",
        headers=csrf_headers,
        json={
            "filename": "unknown.pdf",
            "size_bytes": len(PDF_A),
            "collection_key": "partners",
        },
    )
    assert unknown_preflight.status_code == 422
    assert unknown_preflight.json()["code"] == "collection-not-configured"

    missing_upload = client.post(
        "/api/v1/uploads",
        headers={**csrf_headers, "Idempotency-Key": "missing-collection-key"},
        files={"file": ("missing.pdf", PDF_A, "application/pdf")},
        data={"idempotency_key": "missing-collection-key"},
    )
    assert missing_upload.status_code == 400
    assert missing_upload.json()["status_code"] == 400

    unknown_upload = upload_pdf(key="unknown-collection-key", collection="partners")
    assert unknown_upload.status_code == 422
    assert unknown_upload.json()["code"] == "collection-not-configured"

    first = upload_pdf(key="cross-collection-idempotency", collection="customer")
    assert first.status_code == 201, first.text
    document_id = first.json()["document"]["id"]

    conflicting_replay = upload_pdf(
        key="cross-collection-idempotency",
        collection="internal",
    )
    assert conflicting_replay.status_code == 409
    assert conflicting_replay.json()["code"] == "idempotency-key-conflict"

    exact_duplicate = upload_pdf(key="cross-collection-checksum", collection="internal")
    assert exact_duplicate.status_code == 409
    assert exact_duplicate.json()["code"] == "exact-duplicate"
    assert exact_duplicate.json()["duplicate"] == {
        "document_id": document_id,
        "filename": "example.pdf",
        "size_bytes": len(PDF_A),
        "state": "QUEUED",
        "collection_key": "customer",
        "language": "und",
        "detail_url": f"/documents/{document_id}",
    }

    persisted = client.get(f"/api/v1/documents/{document_id}")
    assert persisted.json()["collection_key"] == "customer"


def test_review_required_pipeline_result_holds_document_without_index_writes(
    client: TestClient,
    upload_pdf,
    job_headers: dict[str, str],
) -> None:
    document_id, operation_id = _send_uploaded_document_to_review(
        client,
        upload_pdf,
        job_headers,
        key="review-result-key",
    )

    response = client.get(f"/api/v1/documents/{document_id}")

    assert response.status_code == 200
    document = response.json()
    assert document["state"] == "CLASSIFICATION_REVIEW"
    assert document["collection_key"] == "customer"
    assert document["language"] == "und"
    assert document["language_status"] == "review_required"
    assert document["language_method"] == "downstream-parser"
    assert document["language_reason"] == "low_confidence"
    assert document["chunk_count"] is None
    operation = next(item for item in document["operations"] if item["id"] == operation_id)
    assert operation["state"] == OperationState.REVIEW_REQUIRED.value
    assert operation["chunk_count"] is None
    components = {item["name"]: item["status"] for item in operation["component_results"]}
    assert components["bm25"] == "not_applicable"
    assert components["dense"] == "not_applicable"


@pytest.mark.parametrize("language", ["en", "fr"])
def test_audited_language_override_requeues_with_classified_manifest(
    client: TestClient,
    csrf_headers: dict[str, str],
    upload_pdf,
    job_headers: dict[str, str],
    language: str,
) -> None:
    key = f"override-{language}-key"
    document_id, _operation_id = _send_uploaded_document_to_review(
        client,
        upload_pdf,
        job_headers,
        key=key,
    )
    reason = f"Operator verified {language.upper()} source text."

    resolved = client.post(
        f"/api/v1/documents/{document_id}/classification",
        headers=csrf_headers,
        json={"action": "override", "language": language, "reason": f"  {reason}  "},
    )

    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["document"]["state"] == "QUEUED"
    assert resolved.json()["document"]["collection_key"] == "customer"
    assert resolved.json()["document"]["language"] == language
    assert resolved.json()["document"]["language_status"] == "overridden"
    assert resolved.json()["document"]["language_method"] == "operator_override"
    assert resolved.json()["document"]["language_reason"] == reason

    detail = client.get(f"/api/v1/documents/{document_id}").json()
    audit = next(
        event for event in detail["audit_events"] if event["event_type"] == "language_overridden"
    )
    assert audit["actor_type"] == "anonymous"
    assert audit["details"] == {
        "collection_key": "customer",
        "language": language,
        "reason": reason,
    }

    _batch, manifest = _claim_manifest(
        client,
        job_headers,
        request_id=f"manifest-{key}",
    )
    operation = manifest["operations"][0]
    assert operation["document_id"] == document_id
    assert operation["collection_key"] == "customer"
    assert operation["language"] == language
    assert operation["classification_required"] is False
    assert operation["relative_path"] == f"pdfs/{language}/customer/{document_id}.pdf"


def test_pipeline_cannot_discard_an_operator_language_override(
    client: TestClient,
    csrf_headers: dict[str, str],
    upload_pdf,
    job_headers: dict[str, str],
) -> None:
    document_id, _operation_id = _send_uploaded_document_to_review(
        client,
        upload_pdf,
        job_headers,
        key="override-preservation-key",
    )
    overridden = client.post(
        f"/api/v1/documents/{document_id}/classification",
        headers=csrf_headers,
        json={
            "action": "override",
            "language": "en",
            "reason": "Operator verified the English source text.",
        },
    )
    assert overridden.status_code == 200, overridden.text

    batch, manifest = _claim_manifest(
        client,
        job_headers,
        request_id="override-preservation-manifest",
    )
    _stage_manifest(client, job_headers, batch=batch, manifest=manifest)
    operation_id = manifest["operations"][0]["operation_id"]
    rejected = client.post(
        f"/api/v1/jobs/batches/{batch['batch_id']}/results",
        headers=job_headers,
        json={
            "pipeline_run_id": "override-preservation-run",
            "results": [
                {
                    "operation_id": operation_id,
                    "outcome": "review_required",
                    "components": {
                        "pdf_source": "succeeded",
                        "markdown": "succeeded",
                        "bm25": "not_applicable",
                        "dense": "not_applicable",
                    },
                    "classification": {
                        "language": "und",
                        "status": "review_required",
                        "method": "downstream-parser",
                        "confidence": 0.4,
                        "reason": "low_confidence",
                    },
                }
            ],
        },
    )
    assert rejected.status_code == 422
    assert rejected.json()["code"] == "classification-override-mismatch"

    conflicting_failure = client.post(
        f"/api/v1/jobs/batches/{batch['batch_id']}/results",
        headers=job_headers,
        json={
            "pipeline_run_id": "override-conflict-run",
            "results": [
                {
                    "operation_id": operation_id,
                    "outcome": "failed",
                    "components": {
                        "pdf_source": "succeeded",
                        "markdown": "succeeded",
                        "bm25": "failed",
                        "dense": "not_applicable",
                    },
                    "classification": {
                        "language": "fr",
                        "status": "detected",
                        "method": "downstream-parser",
                        "confidence": 0.99,
                    },
                    "error": "BM25 write failed",
                }
            ],
        },
    )
    assert conflicting_failure.status_code == 422
    assert conflicting_failure.json()["code"] == "classification-override-mismatch"
    document = client.get(f"/api/v1/documents/{document_id}").json()
    assert document["language"] == "en"
    assert document["language_status"] == "overridden"
    assert document["language_reason"] == "Operator verified the English source text."


def test_pipeline_cannot_invent_an_operator_override_on_failure(
    client: TestClient,
    upload_pdf,
    job_headers: dict[str, str],
) -> None:
    uploaded = upload_pdf(
        contents=PDF_B,
        filename="pending-classification.pdf",
        key="invented-override-key",
    )
    assert uploaded.status_code == 201, uploaded.text
    batch, manifest = _claim_manifest(
        client,
        job_headers,
        request_id="invented-override-manifest",
    )
    _stage_manifest(client, job_headers, batch=batch, manifest=manifest)
    operation = manifest["operations"][0]
    response = client.post(
        f"/api/v1/jobs/batches/{batch['batch_id']}/results",
        headers=job_headers,
        json={
            "pipeline_run_id": "invented-override-run",
            "results": [
                {
                    "operation_id": operation["operation_id"],
                    "outcome": "failed",
                    "components": {
                        "pdf_source": "succeeded",
                        "markdown": "succeeded",
                        "bm25": "failed",
                        "dense": "not_applicable",
                    },
                    "classification": {
                        "language": "en",
                        "status": "overridden",
                        "method": "downstream-parser",
                    },
                    "error": "BM25 write failed",
                }
            ],
        },
    )
    assert response.status_code == 422
    assert response.json()["code"] == "classification-result-invalid"
    document = client.get(
        f"/api/v1/documents/{operation['document_id']}"
    ).json()
    assert document["language"] == "und"
    assert document["language_status"] == "pending"


def test_missing_classification_document_is_always_not_found(
    client: TestClient,
    csrf_headers: dict[str, str],
) -> None:
    response = client.post(
        f"/api/v1/documents/{uuid.uuid4()}/classification",
        headers=csrf_headers,
        json={
            "action": "override",
            "language": "fr",
            "reason": "Operator verified French text.",
        },
    )
    assert response.status_code == 404
    assert response.json()["code"] == "document-not-found"


def test_detect_action_assigns_legacy_unassigned_review_document(
    client: TestClient,
    csrf_headers: dict[str, str],
    job_headers: dict[str, str],
    session_factory: sessionmaker[Session],
) -> None:
    legacy = _catalog_document(
        filename="legacy-unassigned.pdf",
        collection_key=None,
        state=DocumentState.CLASSIFICATION_REVIEW,
        language=LanguageCode.UND,
    )
    with session_factory() as session:
        session.add(legacy)
        session.commit()

    response = client.post(
        f"/api/v1/documents/{legacy.id}/classification",
        headers=csrf_headers,
        json={"action": "detect", "collection_key": "internal"},
    )

    assert response.status_code == 200, response.text
    document = response.json()["document"]
    assert document["state"] == "QUEUED"
    assert document["collection_key"] == "internal"
    assert document["language"] == "und"
    assert document["language_status"] == "pending"

    detail = client.get(f"/api/v1/documents/{legacy.id}").json()
    audit = next(
        event
        for event in detail["audit_events"]
        if event["event_type"] == "language_redetection_requested"
    )
    assert audit["details"] == {"collection_key": "internal", "language": "und"}

    _batch, manifest = _claim_manifest(
        client,
        job_headers,
        request_id="legacy-redetect-manifest",
    )
    operation = manifest["operations"][0]
    assert operation["document_id"] == str(legacy.id)
    assert operation["collection_key"] == "internal"
    assert operation["language"] == "und"
    assert operation["classification_required"] is True
    assert operation["relative_path"] == f"pdfs/und/internal/{legacy.id}.pdf"


def test_root_search_counts_include_explicit_zero_and_positive_groups(
    app,
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    internal = _catalog_document(
        filename="internal-benefits.pdf",
        collection_key="internal",
        state=DocumentState.INGESTED,
        language=LanguageCode.EN,
    )
    with session_factory() as session:
        session.add(internal)
        session.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["collections"] == ["customer", "internal"]
        assert payload["include_hits"] is False
        return httpx.Response(
            200,
            json={
                "query": payload["query"],
                "mode": payload["mode"],
                "language": payload["language"],
                "groups": [
                    {"collection_key": "customer", "total": 0, "hits": []},
                    {"collection_key": "internal", "total": 1, "hits": []},
                ],
            },
        )

    search_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app.state.search_http_client = search_client
    try:
        response = client.get("/library?q=employee+benefits&mode=hybrid&language=en")
        assert response.status_code == 200
        customer_card = response.text.split(
            'aria-labelledby="collection-customer-title"', 1
        )[1].split("</article>", 1)[0]
        internal_card = response.text.split(
            'aria-labelledby="collection-internal-title"', 1
        )[1].split("</article>", 1)[0]
        assert "<strong>0</strong>" in customer_card
        assert "<span>matching documents</span>" in customer_card
        assert "<strong>1</strong>" in internal_card
        assert "<span>matching document</span>" in internal_card
    finally:
        asyncio.run(search_client.aclose())


def test_customer_search_rejects_forged_internal_document(
    app,
    client: TestClient,
    csrf_headers: dict[str, str],
    session_factory: sessionmaker[Session],
) -> None:
    customer = _catalog_document(
        filename="public-product.pdf",
        collection_key="customer",
        state=DocumentState.INGESTED,
        language=LanguageCode.EN,
    )
    internal = _catalog_document(
        filename="private-hr-policy.pdf",
        collection_key="internal",
        state=DocumentState.INGESTED,
        language=LanguageCode.EN,
    )
    with session_factory() as session:
        session.add_all([customer, internal])
        session.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["collections"] == ["customer"]
        return httpx.Response(
            200,
            json={
                "query": payload["query"],
                "mode": payload["mode"],
                "language": payload["language"],
                "groups": [
                    {
                        "collection_key": "customer",
                        "total": 1,
                        "hits": [
                            {
                                "document_id": str(internal.id),
                                "score": 0.99,
                                "snippet": "Forged internal result",
                            }
                        ],
                    }
                ],
            },
        )

    search_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app.state.search_http_client = search_client
    try:
        response = client.post(
            "/api/v1/search",
            headers=csrf_headers,
            json={
                "query": "benefits",
                "mode": "hybrid",
                "collections": ["customer"],
                "language": "en",
                "include_hits": True,
                "page": 1,
                "page_size": 20,
            },
        )
        assert response.status_code == 502
        assert response.json()["code"] == "search-catalog-mismatch"
        assert "No partial results were returned" in response.json()["detail"]
    finally:
        asyncio.run(search_client.aclose())


def test_pipeline_review_item_cannot_request_automatic_redetection(
    client: TestClient,
    upload_pdf,
    job_headers: dict[str, str],
    csrf_headers: dict[str, str],
) -> None:
    document_id, _operation_id = _send_uploaded_document_to_review(
        client,
        upload_pdf,
        job_headers,
        key="review-redetect-key",
    )

    response = client.post(
        f"/api/v1/documents/{document_id}/classification",
        headers=csrf_headers,
        json={"action": "detect", "collection_key": "customer"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "classification-detect-not-allowed"
    assert client.get(f"/api/v1/documents/{document_id}").json()["state"] == (
        "CLASSIFICATION_REVIEW"
    )


def test_review_removal_never_becomes_retrieval_eligible(
    app,
    client: TestClient,
    upload_pdf,
    job_headers: dict[str, str],
    csrf_headers: dict[str, str],
) -> None:
    document_id, _operation_id = _send_uploaded_document_to_review(
        client,
        upload_pdf,
        job_headers,
        key="review-removal-key",
    )
    deleted = client.post(
        f"/api/v1/documents/{document_id}/deletion",
        headers=csrf_headers,
        json={"reason": "Remove an unclassifiable PDF."},
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["document"]["state"] == "DELETE_QUEUED"

    collection = next(
        item
        for item in client.get("/api/v1/collections").json()["items"]
        if item["key"] == "customer"
    )
    assert collection["available_documents"] == 0
    assert collection["processing_documents"] == 1
    assert collection["languages"] == {"en": 0, "fr": 0, "und": 0}
    library = client.get("/api/v1/documents?scope=library&collection_key=customer").json()
    assert library["total"] == 0

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "query": payload["query"],
                "mode": payload["mode"],
                "language": payload["language"],
                "groups": [
                    {
                        "collection_key": "customer",
                        "total": 1,
                        "hits": [
                            {
                                "document_id": document_id,
                                "score": 0.99,
                                "snippet": "stale unclassified result",
                            }
                        ],
                    }
                ],
            },
        )

    search_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app.state.search_http_client = search_client
    try:
        response = client.post(
            "/api/v1/search",
            headers=csrf_headers,
            json={
                "query": "anything",
                "mode": "hybrid",
                "collections": ["customer"],
                "include_hits": True,
                "page": 1,
                "page_size": 20,
            },
        )
        assert response.status_code == 502
        assert response.json()["code"] == "search-catalog-mismatch"
    finally:
        asyncio.run(search_client.aclose())
