from __future__ import annotations

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
            state=DocumentState.ANALYZING,
        ),
        _catalog_document(
            filename="customer-cleanup.pdf",
            collection_key="customer",
            state=DocumentState.CLEANUP_PENDING,
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


def test_upload_requires_configured_collection_and_scopes_duplicates(
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
    assert first.status_code == 202, first.text
    document_id = first.json()["upload"]["upload_id"]

    conflicting_replay = upload_pdf(
        key="cross-collection-idempotency",
        collection="internal",
    )
    assert conflicting_replay.status_code == 409
    assert conflicting_replay.json()["code"] == "idempotency-key-conflict"

    exact_duplicate = upload_pdf(key="same-collection-checksum", collection="customer")
    assert exact_duplicate.status_code == 409
    assert exact_duplicate.json()["code"] == "exact-duplicate"
    assert exact_duplicate.json()["duplicate"] == {
        "document_id": document_id,
        "filename": "example.pdf",
        "size_bytes": len(PDF_A),
        "state": "ANALYZING",
        "collection_key": "customer",
        "detail_url": f"/documents/{document_id}",
    }

    cross_collection_copy = upload_pdf(
        key="cross-collection-checksum",
        collection="internal",
    )
    assert cross_collection_copy.status_code == 202, cross_collection_copy.text
    assert cross_collection_copy.json()["upload"]["document"]["collection_key"] == "internal"


def test_preflight_filename_warnings_are_collection_scoped(
    client: TestClient,
    csrf_headers: dict[str, str],
    session_factory: sessionmaker[Session],
) -> None:
    existing = _catalog_document(
        filename="Customer Monthly Report May 2026.pdf",
        collection_key="customer",
        state=DocumentState.INGESTED,
    )
    with session_factory() as session:
        session.add(existing)
        session.commit()

    request = {
        "filename": "Customer Monthly Report June 2026.pdf",
        "size_bytes": len(PDF_A),
    }
    customer = client.post(
        "/api/v1/uploads/preflight",
        headers=csrf_headers,
        json={**request, "collection_key": "customer"},
    )
    internal = client.post(
        "/api/v1/uploads/preflight",
        headers=csrf_headers,
        json={**request, "collection_key": "internal"},
    )

    assert customer.status_code == 200, customer.text
    assert customer.json()["warnings"]
    assert customer.json()["warnings"][0]["matched"]["document_id"] == str(existing.id)
    assert customer.json()["warnings"][0]["matched"]["collection_key"] == "customer"
    assert internal.status_code == 200, internal.text
    assert internal.json()["warnings"] == []


def test_preflight_checks_the_full_collection_for_filename_families(
    client: TestClient,
    csrf_headers: dict[str, str],
    session_factory: sessionmaker[Session],
) -> None:
    older_match = _catalog_document(
        filename="Customer Monthly Report May 2026.pdf",
        collection_key="customer",
        state=DocumentState.INGESTED,
    )
    newer_nonmatches = [
        _catalog_document(
            filename=f"Unrelated Archive Item {index:04d}.pdf",
            collection_key="customer",
            state=DocumentState.INGESTED,
        )
        for index in range(2_000)
    ]
    with session_factory() as session:
        session.add_all([older_match, *newer_nonmatches])
        session.commit()

    response = client.post(
        "/api/v1/uploads/preflight",
        headers=csrf_headers,
        json={
            "filename": "Customer Monthly Report June 2026.pdf",
            "size_bytes": len(PDF_A),
            "collection_key": "customer",
        },
    )

    assert response.status_code == 200, response.text
    matched_ids = {
        warning["matched"]["document_id"] for warning in response.json()["warnings"]
    }
    assert str(older_match.id) in matched_ids


def test_legacy_search_and_classification_fields_are_rejected(
    client: TestClient,
    csrf_headers: dict[str, str],
    upload_pdf,
) -> None:
    uploaded = upload_pdf(key="reject-legacy-fields", collection="customer")
    assert uploaded.status_code == 202
    document_id = uploaded.json()["upload"]["upload_id"]

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
        f"/api/v1/documents/{document_id}/classification",
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

    search_client = httpx.Client(transport=httpx.MockTransport(handler))
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
        search_client.close()


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

    search_client = httpx.Client(transport=httpx.MockTransport(handler))
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
        search_client.close()
