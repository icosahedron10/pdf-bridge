from __future__ import annotations

import asyncio
import json
import uuid

import httpx
from litestar.testing import TestClient
from sqlalchemy.orm import Session, sessionmaker

from pdf_bridge.persistence.models import Document, DocumentState, ScanState, utc_now
from tests.conftest import PDF_A


def _catalog_document(
    *,
    filename: str,
    collection_key: str,
    state: DocumentState,
) -> Document:
    document_id = uuid.uuid4()
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


def test_collections_endpoint_reports_only_available_and_processing_counts(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    documents = [
        _catalog_document(
            filename="customer-one.pdf",
            collection_key="customer",
            state=DocumentState.INGESTED,
        ),
        _catalog_document(
            filename="customer-two.pdf",
            collection_key="customer",
            state=DocumentState.INGESTED,
        ),
        _catalog_document(
            filename="customer-processing.pdf",
            collection_key="customer",
            state=DocumentState.QUEUED,
        ),
        _catalog_document(
            filename="customer-cleanup.pdf",
            collection_key="customer",
            state=DocumentState.DELETE_CLEANUP,
        ),
        _catalog_document(
            filename="internal.pdf",
            collection_key="internal",
            state=DocumentState.INGESTED,
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
        "detail_url": "/library/customer",
    }
    assert collections["internal"]["available_documents"] == 1
    assert collections["internal"]["processing_documents"] == 0


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
    assert exact_duplicate.json()["duplicate"] == {
        "document_id": document_id,
        "filename": "example.pdf",
        "size_bytes": len(PDF_A),
        "state": "QUEUED",
        "collection_key": "customer",
        "detail_url": f"/documents/{document_id}",
    }


def test_version_two_manifest_and_result_are_collection_only(
    client: TestClient,
    upload_pdf,
    job_headers: dict[str, str],
) -> None:
    uploaded = upload_pdf(key="collection-only-contract", collection="customer")
    assert uploaded.status_code == 201, uploaded.text
    document_id = uploaded.json()["document"]["id"]
    batch, manifest = _claim_manifest(
        client,
        job_headers,
        request_id="collection-only-contract-claim",
    )

    assert manifest["version"] == 2
    operation = manifest["operations"][0]
    assert set(operation) == {
        "operation_id",
        "document_id",
        "operation_type",
        "filename",
        "size_bytes",
        "sha256",
        "collection_key",
        "relative_path",
        "download_url",
    }
    assert operation["collection_key"] == "customer"
    assert operation["relative_path"] == f"pdfs/customer/{document_id}.pdf"

    _stage_manifest(client, job_headers, batch=batch, manifest=manifest)
    reported = client.post(
        f"/api/v1/jobs/batches/{batch['batch_id']}/results",
        headers=job_headers,
        json={
            "pipeline_run_id": "collection-only-run",
            "results": [
                {
                    "operation_id": operation["operation_id"],
                    "success": True,
                    "chunk_count": 3,
                    "components": {
                        "pdf_source": "succeeded",
                        "markdown": "succeeded",
                        "bm25": "succeeded",
                        "dense": "succeeded",
                    },
                }
            ],
        },
    )
    assert reported.status_code == 200, reported.text
    assert set(reported.json()) == {
        "batch_id",
        "state",
        "completed_at",
        "succeeded",
        "failed",
        "idempotent_replay",
    }
    assert reported.json()["succeeded"] == 1
    document = client.get(f"/api/v1/documents/{document_id}").json()
    assert document["state"] == "INGESTED"
    assert document["collection_key"] == "customer"
    assert document["pipeline_metadata"] == {
        "components": [
            {"name": "pdf_source", "status": "succeeded"},
            {"name": "markdown", "status": "succeeded"},
            {"name": "bm25", "status": "succeeded"},
            {"name": "dense", "status": "succeeded"},
        ],
        "collection_key": "customer",
    }


def test_legacy_result_and_search_fields_are_rejected(
    client: TestClient,
    csrf_headers: dict[str, str],
    upload_pdf,
    job_headers: dict[str, str],
) -> None:
    uploaded = upload_pdf(key="reject-legacy-result", collection="customer")
    assert uploaded.status_code == 201
    batch, manifest = _claim_manifest(
        client,
        job_headers,
        request_id="reject-legacy-result-claim",
    )
    _stage_manifest(client, job_headers, batch=batch, manifest=manifest)
    operation_id = manifest["operations"][0]["operation_id"]

    rejected_result = client.post(
        f"/api/v1/jobs/batches/{batch['batch_id']}/results",
        headers=job_headers,
        json={
            "pipeline_run_id": "legacy-result-run",
            "results": [
                {
                    "operation_id": operation_id,
                    "success": False,
                    "outcome": "failed",
                    "classification": {"language": "en"},
                    "components": {
                        "pdf_source": "failed",
                        "markdown": "not_applicable",
                        "bm25": "not_applicable",
                        "dense": "not_applicable",
                    },
                    "error": "parser failed",
                }
            ],
        },
    )
    assert rejected_result.status_code == 400

    rejected_search = client.post(
        "/api/v1/search",
        headers=csrf_headers,
        json={
            "query": "benefits",
            "mode": "hybrid",
            "collections": ["customer"],
            "language": "en",
        },
    )
    assert rejected_search.status_code == 400
    assert client.post(
        f"/api/v1/documents/{uploaded.json()['document']['id']}/classification",
        headers=csrf_headers,
        json={"language": "en"},
    ).status_code == 404


def test_root_search_counts_include_explicit_zero_and_positive_groups(
    app,
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    internal = _catalog_document(
        filename="internal-benefits.pdf",
        collection_key="internal",
        state=DocumentState.INGESTED,
    )
    with session_factory() as session:
        session.add(internal)
        session.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["collections"] == ["customer", "internal"]
        assert payload["include_hits"] is False
        assert "language" not in payload
        return httpx.Response(
            200,
            json={
                "query": payload["query"],
                "mode": payload["mode"],
                "groups": [
                    {"collection_key": "customer", "total": 0, "hits": []},
                    {"collection_key": "internal", "total": 1, "hits": []},
                ],
            },
        )

    search_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app.state.search_http_client = search_client
    try:
        response = client.get("/library?q=employee+benefits&mode=hybrid")
        assert response.status_code == 200
        customer_card = response.text.split(
            'aria-labelledby="collection-customer-title"', 1
        )[1].split("</article>", 1)[0]
        internal_card = response.text.split(
            'aria-labelledby="collection-internal-title"', 1
        )[1].split("</article>", 1)[0]
        assert "<strong>0</strong>" in customer_card
        assert "<strong>1</strong>" in internal_card
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
    )
    internal = _catalog_document(
        filename="private-hr-policy.pdf",
        collection_key="internal",
        state=DocumentState.INGESTED,
    )
    with session_factory() as session:
        session.add_all([customer, internal])
        session.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "query": payload["query"],
                "mode": payload["mode"],
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
                "include_hits": True,
                "page": 1,
                "page_size": 20,
            },
        )
        assert response.status_code == 502
        assert response.json()["code"] == "search-catalog-mismatch"
    finally:
        asyncio.run(search_client.aclose())
