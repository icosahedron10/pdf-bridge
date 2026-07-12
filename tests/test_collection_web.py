from __future__ import annotations

import asyncio
import json
import uuid

import httpx
from litestar.testing import TestClient
from sqlalchemy.orm import Session, sessionmaker

from pdf_bridge.models import (
    Document,
    DocumentState,
    LanguageCode,
    LanguageStatus,
    ScanState,
    utc_now,
)


def _document(
    *,
    filename: str,
    collection_key: str | None,
    language: LanguageCode,
    state: DocumentState = DocumentState.INGESTED,
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
        idempotency_key=f"web-test:{document_id}",
        state=state,
        collection_key=collection_key,
        language=language,
        language_status=(
            LanguageStatus.REVIEW_REQUIRED
            if state == DocumentState.CLASSIFICATION_REVIEW or language == LanguageCode.UND
            else LanguageStatus.DETECTED
        ),
        language_reason=(
            "low_confidence"
            if state == DocumentState.CLASSIFICATION_REVIEW or language == LanguageCode.UND
            else None
        ),
        scan_state=ScanState.CLEAN,
        scan_engine="test-clamd",
        scanned_at=utc_now(),
        uploader_identity="web-test",
        uploaded_at=utc_now(),
        ingested_at=(utc_now() if state == DocumentState.INGESTED else None),
    )


def test_collection_overview_and_browse_are_strictly_scoped(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    customer = _document(
        filename="customer-product.pdf",
        collection_key="customer",
        language=LanguageCode.EN,
    )
    internal = _document(
        filename="<script>alert(1)</script>-benefits.pdf",
        collection_key="internal",
        language=LanguageCode.FR,
    )
    with session_factory() as session:
        session.add_all([customer, internal])
        session.commit()

    overview = client.get("/library")
    assert overview.status_code == 200
    assert "Customer Product" in overview.text
    assert "HR &amp; Internal" in overview.text
    assert "Customer-facing" in overview.text
    assert "Internal only" in overview.text
    assert customer.original_filename not in overview.text

    customer_page = client.get("/library/customer")
    assert customer_page.status_code == 200
    assert customer.original_filename in customer_page.text
    assert internal.original_filename not in customer_page.text
    assert "Customer-facing" in customer_page.text
    assert "<code>customer</code>" in customer_page.text

    internal_page = client.get("/library/internal?language=fr")
    assert internal_page.status_code == 200
    assert "&lt;script&gt;alert(1)&lt;/script&gt;-benefits.pdf" in internal_page.text
    assert "<script>alert(1)</script>" not in internal_page.text
    assert client.get("/library/not-configured").status_code == 404


def test_root_search_shows_authoritative_collection_counts_and_preserves_query(
    app,
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    internal = _document(
        filename="employee-benefits.pdf",
        collection_key="internal",
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
        response = client.get("/library?q=employee+benefits&mode=keyword&language=en")
        assert response.status_code == 200
        assert "0" in response.text
        assert "1" in response.text
        assert "matching documents" in response.text
        expected_link = "/library/customer?q=employee+benefits&amp;mode=keyword&amp;language=en"
        assert expected_link in response.text
    finally:
        asyncio.run(search_client.aclose())


def test_collection_page_rejects_cross_collection_search_hits(
    app,
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    internal = _document(
        filename="private-hr-policy.pdf",
        collection_key="internal",
        language=LanguageCode.EN,
    )
    with session_factory() as session:
        session.add(internal)
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
                                "snippet": "private HR result",
                            }
                        ],
                    }
                ],
            },
        )

    search_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app.state.search_http_client = search_client
    try:
        response = client.get("/library/customer?q=benefits")
        assert response.status_code == 502
        assert "No partial results or fallback search were shown" in response.text
        assert internal.original_filename not in response.text
        assert "private HR result" not in response.text
    finally:
        asyncio.run(search_client.aclose())


def test_root_search_rejects_all_counts_atomically(
    app,
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    internal = _document(
        filename="employee-policy.pdf",
        collection_key="internal",
        language=LanguageCode.EN,
    )
    with session_factory() as session:
        session.add(internal)
        session.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "query": payload["query"],
                "mode": payload["mode"],
                "language": payload["language"],
                "groups": [
                    {"collection_key": "customer", "total": 0, "hits": []},
                    {"collection_key": "internal", "total": 2, "hits": []},
                ],
            },
        )

    search_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app.state.search_http_client = search_client
    try:
        response = client.get("/library?q=employee+policy")
        assert response.status_code == 502
        assert response.text.count("count unavailable") == 2
        assert "matching document" not in response.text
    finally:
        asyncio.run(search_client.aclose())


def test_undetermined_deletion_record_is_not_retrieval_eligible(
    app,
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    held = _document(
        filename="held-undetermined.pdf",
        collection_key="customer",
        language=LanguageCode.UND,
        state=DocumentState.DELETE_QUEUED,
    )
    with session_factory() as session:
        session.add(held)
        session.commit()

    overview = client.get("/library")
    customer_entry = overview.text.split("collection-entry--customer", 1)[1].split(
        "</article>", 1
    )[0]
    assert "<dt>Available</dt><dd>0</dd>" in customer_entry
    assert "<dt>Processing</dt><dd>1</dd>" in customer_entry

    api_customer = next(
        item for item in client.get("/api/v1/collections").json()["items"]
        if item["key"] == "customer"
    )
    assert api_customer["available_documents"] == 0
    assert api_customer["processing_documents"] == 1

    browse = client.get("/library/customer")
    assert browse.status_code == 200
    assert held.original_filename not in browse.text

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
                                "document_id": str(held.id),
                                "score": 0.8,
                                "snippet": "must remain outside retrieval",
                            }
                        ],
                    }
                ],
            },
        )

    search_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app.state.search_http_client = search_client
    try:
        search = client.get("/library/customer?q=held")
        assert search.status_code == 502
        assert held.original_filename not in search.text
        assert "must remain outside retrieval" not in search.text
    finally:
        asyncio.run(search_client.aclose())


def test_review_and_upload_surfaces_make_collection_assignment_explicit(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    review_document = _document(
        filename="legacy-policy.pdf",
        collection_key=None,
        language=LanguageCode.UND,
        state=DocumentState.CLASSIFICATION_REVIEW,
    )
    with session_factory() as session:
        session.add(review_document)
        session.commit()

    review = client.get("/review?collection=unassigned")
    assert review.status_code == 200
    assert "Not exposed to retrieval" in review.text
    assert "No collection assigned" in review.text
    assert 'name="action" value="detect"' in review.text
    assert 'name="action" value="override"' in review.text
    assert 'name="reason"' in review.text

    upload = client.get("/upload")
    assert upload.status_code == 200
    assert upload.text.count('name="collection_key"') == 2
    assert 'data-collection-choice' in upload.text
    collection_fieldset = upload.text.split("data-collection-selector", 1)[1].split(
        "</fieldset>", 1
    )[0]
    assert "checked" not in collection_fieldset

    selected_upload = client.get("/upload?collection=internal")
    assert selected_upload.status_code == 200
    assert 'value="internal" data-collection-choice' in selected_upload.text
    internal_choice = selected_upload.text.split('value="internal"', 1)[1].split(">", 1)[0]
    assert "checked" in internal_choice
