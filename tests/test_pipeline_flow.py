from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import UUID, uuid4

import httpx
from fastapi.testclient import TestClient

from pdf_bridge.models import (
    BatchState,
    Document,
    DocumentState,
    JobBatch,
    OperationState,
    OperationType,
    QueueOperation,
    ScanState,
    utc_now,
)
from tests.conftest import PDF_B


def _components(*, failed: str | None = None) -> dict[str, str]:
    values = {
        "pdf_source": "succeeded",
        "markdown": "succeeded",
        "bm25": "succeeded",
        "dense": "succeeded",
    }
    if failed:
        values[failed] = "failed"
    return values


def _detected_language(language: str = "en") -> dict[str, object]:
    return {
        "language": language,
        "status": "detected",
        "method": "test-parser",
        "confidence": 0.99,
    }


def _claim_and_stage(
    client: TestClient, job_headers: dict[str, str], request_id: str
) -> tuple[dict, dict]:
    claim = client.post(
        "/api/v1/jobs/batches/claim",
        headers=job_headers,
        json={"request_id": request_id, "limit": 100},
    )
    assert claim.status_code == 200, claim.text
    batch = claim.json()
    manifest_response = client.get(
        f"/api/v1/jobs/batches/{batch['batch_id']}/manifest", headers=job_headers
    )
    assert manifest_response.status_code == 200
    manifest = manifest_response.json()
    operation_ids = [item["operation_id"] for item in manifest["operations"]]
    staged = client.post(
        f"/api/v1/jobs/batches/{batch['batch_id']}/staged",
        headers=job_headers,
        json={"operation_ids": operation_ids},
    )
    assert staged.status_code == 200, staged.text
    return batch, manifest


def test_end_to_end_ingest_search_and_delete(
    app,
    client: TestClient,
    csrf_headers: dict[str, str],
    upload_pdf,
    job_headers: dict[str, str],
) -> None:
    uploaded = upload_pdf(filename="handbook.pdf", key="pipeline-key-1")
    document_id = uploaded.json()["document"]["id"]
    batch, manifest = _claim_and_stage(client, job_headers, "jenkins-ingest-001")
    ingest_item = manifest["operations"][0]
    assert manifest["version"] == 2
    assert ingest_item["operation_type"] == "INGEST"
    assert ingest_item["collection_key"] == "customer"
    assert ingest_item["language"] == "und"
    assert ingest_item["classification_required"] is True
    assert ingest_item["relative_path"] == f"pdfs/und/customer/{document_id}.pdf"

    downloaded = client.get(ingest_item["download_url"], headers=job_headers)
    assert downloaded.status_code == 200
    assert len(downloaded.content) == ingest_item["size_bytes"]
    assert hashlib.sha256(downloaded.content).hexdigest() == ingest_item["sha256"]

    results = {
        "pipeline_run_id": "pipeline-run-001",
        "results": [
            {
                "operation_id": ingest_item["operation_id"],
                "outcome": "succeeded",
                "chunk_count": 17,
                "components": _components(),
                "classification": _detected_language(),
            }
        ],
    }
    reported = client.post(
        f"/api/v1/jobs/batches/{batch['batch_id']}/results",
        headers=job_headers,
        json=results,
    )
    assert reported.status_code == 200, reported.text
    assert reported.json()["state"] == "COMPLETED"

    replay_stage = client.post(
        f"/api/v1/jobs/batches/{batch['batch_id']}/staged",
        headers=job_headers,
        json={"operation_ids": [ingest_item["operation_id"]]},
    )
    assert replay_stage.status_code == 200
    assert replay_stage.json()["idempotent_replay"] is True
    replay_result = client.post(
        f"/api/v1/jobs/batches/{batch['batch_id']}/results",
        headers=job_headers,
        json=results,
    )
    assert replay_result.status_code == 200
    assert replay_result.json()["idempotent_replay"] is True
    conflicting_replay = client.post(
        f"/api/v1/jobs/batches/{batch['batch_id']}/results",
        headers=job_headers,
        json={
            **results,
            "results": [{**results["results"][0], "chunk_count": 999}],
        },
    )
    assert conflicting_replay.status_code == 409
    assert conflicting_replay.json()["code"] == "batch-result-conflict"

    document = client.get(f"/api/v1/documents/{document_id}")
    assert document.status_code == 200
    assert document.json()["state"] == "INGESTED"
    assert document.json()["chunk_count"] == 17
    assert document.json()["operations"][-1]["pipeline_run_id"] == "pipeline-run-001"
    assert document.json()["operations"][-1]["component_results"][0] == {
        "name": "pdf_source",
        "status": "succeeded",
    }

    def search_handler(request: httpx.Request) -> httpx.Response:
        request_payload = __import__("json").loads(request.content)
        groups = []
        for collection_key in request_payload["collections"]:
            matches_customer = collection_key == "customer"
            hits = (
                [
                    {
                        "document_id": document_id,
                        "score": 0.91,
                        "snippet": "Quarterly retention policy",
                        "match_metadata": {"chunk": 3},
                    }
                ]
                if matches_customer and request_payload["include_hits"]
                else []
            )
            groups.append(
                {
                    "collection_key": collection_key,
                    "total": 1 if matches_customer else 0,
                    "hits": hits,
                }
            )
        return httpx.Response(
            200,
            json={
                "query": request_payload["query"],
                "mode": request_payload["mode"],
                "language": request_payload.get("language"),
                "groups": groups,
            },
        )

    search_client = httpx.AsyncClient(transport=httpx.MockTransport(search_handler))
    app.state.search_http_client = search_client
    try:
        for mode in ("keyword", "semantic", "hybrid"):
            search = client.post(
                "/api/v1/search",
                headers=csrf_headers,
                json={
                    "query": "retention",
                    "mode": mode,
                    "collections": ["customer"],
                    "include_hits": True,
                    "page_size": 10,
                },
            )
            assert search.status_code == 200, search.text
            assert search.json()["groups"][0]["hits"][0]["document_id"] == document_id
        library = client.get("/library?q=retention&mode=hybrid")
        assert library.status_code == 200
        assert "matching document" in library.text
        collection = client.get("/library/customer?q=retention&mode=hybrid")
        assert collection.status_code == 200
        assert "Quarterly retention policy" in collection.text
    finally:
        import asyncio

        asyncio.run(search_client.aclose())

    deletion = client.post(f"/api/v1/documents/{document_id}/deletion", headers=csrf_headers)
    assert deletion.status_code == 200
    assert deletion.json()["document"]["state"] == "DELETE_QUEUED"

    delete_batch, delete_manifest = _claim_and_stage(client, job_headers, "jenkins-delete-001")
    delete_item = delete_manifest["operations"][0]
    assert delete_item["operation_type"] == "DELETE"
    assert delete_item["download_url"] is None
    assert delete_item["collection_key"] == "customer"
    assert delete_item["language"] == "en"
    assert delete_item["classification_required"] is False
    assert delete_item["relative_path"] == f"pdfs/en/customer/{document_id}.pdf"
    deleted = client.post(
        f"/api/v1/jobs/batches/{delete_batch['batch_id']}/results",
        headers=job_headers,
        json={
            "pipeline_run_id": "pipeline-delete-001",
            "results": [
                {
                    "operation_id": delete_item["operation_id"],
                    "outcome": "succeeded",
                    "components": _components(),
                }
            ],
        },
    )
    assert deleted.status_code == 200, deleted.text
    tombstone = client.get(f"/api/v1/documents/{document_id}")
    assert tombstone.json()["state"] == "DELETED"
    assert tombstone.json()["detail_url"] == f"/documents/{document_id}"
    assert client.get(f"/api/v1/documents/{document_id}/content").status_code == 409
    reuploaded = upload_pdf(filename="handbook.pdf", key="pipeline-key-2")
    assert reuploaded.status_code == 201
    assert reuploaded.json()["document"]["id"] != document_id


def test_failed_ingestion_can_retry(
    client: TestClient,
    csrf_headers: dict[str, str],
    upload_pdf,
    job_headers: dict[str, str],
) -> None:
    uploaded = upload_pdf(filename="failure.pdf", key="failure-key-1")
    document_id = uploaded.json()["document"]["id"]
    batch, manifest = _claim_and_stage(client, job_headers, "jenkins-failure-001")
    operation_id = manifest["operations"][0]["operation_id"]
    failed = client.post(
        f"/api/v1/jobs/batches/{batch['batch_id']}/results",
        headers=job_headers,
        json={
            "pipeline_run_id": "pipeline-failure-001",
            "results": [
                {
                    "operation_id": operation_id,
                    "outcome": "failed",
                    "components": _components(failed="dense"),
                    "error": "Dense index write failed",
                }
            ],
        },
    )
    assert failed.status_code == 200
    assert client.get(f"/api/v1/documents/{document_id}").json()["state"] == "INGEST_FAILED"
    assert client.get(f"/api/v1/documents/{document_id}/content").status_code == 409

    retried = client.post(f"/api/v1/queue/{operation_id}/retry", headers=csrf_headers)
    assert retried.status_code == 200
    assert retried.json()["document"]["state"] == "QUEUED"
    assert retried.json()["operation_id"] != operation_id


def test_stale_failed_attempt_cannot_be_retried(
    app,
    client: TestClient,
    csrf_headers: dict[str, str],
) -> None:
    document_id = uuid4()
    document = Document(
        id=document_id,
        original_filename="superseded.pdf",
        normalized_filename="superseded.pdf",
        storage_key=f"objects/{document_id}.pdf",
        size_bytes=100,
        sha256="f" * 64,
        idempotency_key="superseded-upload-key",
        state=DocumentState.INGEST_FAILED,
        scan_state=ScanState.CLEAN,
        scan_engine="test-clamd",
        scanned_at=utc_now(),
        uploader_identity="test-user",
    )
    older = QueueOperation(
        document=document,
        operation_type=OperationType.INGEST,
        state=OperationState.FAILED,
        attempt=1,
        error="first failure",
        completed_at=utc_now() - timedelta(minutes=2),
    )
    latest = QueueOperation(
        document=document,
        operation_type=OperationType.INGEST,
        state=OperationState.FAILED,
        attempt=2,
        error="second failure",
        completed_at=utc_now(),
    )
    with app.state.test_session_factory() as session:
        session.add_all([older, latest])
        session.commit()

    stale = client.post(f"/api/v1/queue/{older.id}/retry", headers=csrf_headers)
    assert stale.status_code == 409
    assert stale.json()["code"] == "operation-superseded"
    retried = client.post(f"/api/v1/queue/{latest.id}/retry", headers=csrf_headers)
    assert retried.status_code == 200
    assert retried.json()["document"]["state"] == "QUEUED"


def test_mixed_batch_results_are_recorded_as_partial(
    client: TestClient,
    upload_pdf,
    job_headers: dict[str, str],
) -> None:
    first = upload_pdf(filename="partial-success.pdf", key="partial-success-key")
    second = upload_pdf(filename="partial-failure.pdf", contents=PDF_B, key="partial-failure-key")
    batch, manifest = _claim_and_stage(client, job_headers, "jenkins-partial-batch")
    operations = manifest["operations"]
    reported = client.post(
        f"/api/v1/jobs/batches/{batch['batch_id']}/results",
        headers=job_headers,
        json={
            "pipeline_run_id": "pipeline-partial-run",
            "results": [
                {
                    "operation_id": operations[0]["operation_id"],
                    "outcome": "succeeded",
                    "chunk_count": 3,
                    "components": _components(),
                    "classification": _detected_language(),
                },
                {
                    "operation_id": operations[1]["operation_id"],
                    "outcome": "failed",
                    "components": _components(failed="dense"),
                    "error": "Dense write failed",
                },
            ],
        },
    )
    assert reported.status_code == 200
    assert reported.json()["state"] == "PARTIAL"
    assert reported.json()["succeeded"] == 1
    assert reported.json()["failed"] == 1
    states = {
        client.get(f"/api/v1/documents/{document_id}").json()["state"]
        for document_id in (first.json()["document"]["id"], second.json()["document"]["id"])
    }
    assert states == {"INGESTED", "INGEST_FAILED"}


def test_failed_deletion_retains_canonical_pdf_and_can_retry(
    app,
    client: TestClient,
    csrf_headers: dict[str, str],
    upload_pdf,
    job_headers: dict[str, str],
) -> None:
    uploaded = upload_pdf(filename="delete-failure.pdf", key="delete-failure-key")
    document_id = uploaded.json()["document"]["id"]
    ingest_batch, ingest_manifest = _claim_and_stage(
        client, job_headers, "jenkins-delete-failure-ingest"
    )
    assert (
        client.post(
            f"/api/v1/jobs/batches/{ingest_batch['batch_id']}/results",
            headers=job_headers,
            json={
                "pipeline_run_id": "delete-failure-ingest-run",
                "results": [
                    {
                        "operation_id": ingest_manifest["operations"][0]["operation_id"],
                        "outcome": "succeeded",
                        "chunk_count": 5,
                        "components": _components(),
                        "classification": _detected_language(),
                    }
                ],
            },
        ).status_code
        == 200
    )
    deletion = client.post(f"/api/v1/documents/{document_id}/deletion", headers=csrf_headers)
    delete_operation_id = deletion.json()["operation_id"]
    delete_batch, delete_manifest = _claim_and_stage(
        client, job_headers, "jenkins-delete-failure-delete"
    )
    failed = client.post(
        f"/api/v1/jobs/batches/{delete_batch['batch_id']}/results",
        headers=job_headers,
        json={
            "pipeline_run_id": "delete-failure-run",
            "results": [
                {
                    "operation_id": delete_manifest["operations"][0]["operation_id"],
                    "outcome": "failed",
                    "components": _components(failed="bm25"),
                    "error": "BM25 removal failed",
                }
            ],
        },
    )
    assert failed.status_code == 200
    assert failed.json()["state"] == "FAILED"
    assert client.get(f"/api/v1/documents/{document_id}").json()["state"] == "DELETE_FAILED"
    assert client.get(f"/api/v1/documents/{document_id}/content").status_code == 409
    with app.state.test_session_factory() as session:
        document = session.get(Document, UUID(document_id))
        assert document is not None and document.storage_key is not None

    retried = client.post(f"/api/v1/queue/{delete_operation_id}/retry", headers=csrf_headers)
    assert retried.status_code == 200
    assert retried.json()["document"]["state"] == "DELETE_QUEUED"
    assert retried.json()["operation_id"] != delete_operation_id


def test_job_authentication_is_always_required(client: TestClient) -> None:
    missing = client.post(
        "/api/v1/jobs/batches/claim",
        json={"request_id": "jenkins-no-auth", "limit": 10},
    )
    assert missing.status_code == 401
    assert missing.json()["code"] == "job-authentication-failed"


def test_concurrent_claims_never_overlap(
    app,
    client: TestClient,
    upload_pdf,
    job_headers: dict[str, str],
) -> None:
    assert upload_pdf(filename="first.pdf", key="concurrent-upload-1").status_code == 201
    assert (
        upload_pdf(filename="second.pdf", contents=PDF_B, key="concurrent-upload-2").status_code
        == 201
    )

    def claim(request_id: str) -> dict:
        with TestClient(app) as worker:
            response = worker.post(
                "/api/v1/jobs/batches/claim",
                headers=job_headers,
                json={"request_id": request_id, "limit": 1},
            )
            assert response.status_code == 200, response.text
            batch = response.json()
            manifest = worker.get(
                f"/api/v1/jobs/batches/{batch['batch_id']}/manifest",
                headers=job_headers,
            )
            assert manifest.status_code == 200
            return manifest.json()

    with ThreadPoolExecutor(max_workers=2) as executor:
        manifests = list(executor.map(claim, ["concurrent-claim-a", "concurrent-claim-b"]))
    operation_ids = [manifest["operations"][0]["operation_id"] for manifest in manifests]
    assert len(set(operation_ids)) == 2


def test_empty_claim_request_is_an_idempotent_tombstone(
    client: TestClient, upload_pdf, job_headers: dict[str, str]
) -> None:
    payload = {"request_id": "jenkins-empty-claim", "limit": 10}
    assert (
        client.post("/api/v1/jobs/batches/claim", headers=job_headers, json=payload).status_code
        == 204
    )
    assert upload_pdf(filename="later.pdf", key="later-upload-key").status_code == 201
    assert (
        client.post("/api/v1/jobs/batches/claim", headers=job_headers, json=payload).status_code
        == 204
    )
    fresh = client.post(
        "/api/v1/jobs/batches/claim",
        headers=job_headers,
        json={"request_id": "jenkins-fresh-claim", "limit": 10},
    )
    assert fresh.status_code == 200
    assert fresh.json()["operation_count"] == 1


def test_expired_lease_blocks_replay_manifest_and_download(
    app,
    client: TestClient,
    upload_pdf,
    job_headers: dict[str, str],
) -> None:
    uploaded = upload_pdf(filename="leased.pdf", key="leased-upload-key")
    operation_id = uploaded.json()["operation_id"]
    claim_payload = {"request_id": "jenkins-expiring-lease", "limit": 10}
    claimed = client.post("/api/v1/jobs/batches/claim", headers=job_headers, json=claim_payload)
    assert claimed.status_code == 200
    batch_id = claimed.json()["batch_id"]
    manifest = client.get(f"/api/v1/jobs/batches/{batch_id}/manifest", headers=job_headers).json()
    download_url = manifest["operations"][0]["download_url"]

    with app.state.test_session_factory() as session:
        batch = session.get(JobBatch, UUID(batch_id))
        assert batch is not None
        batch.lease_expires_at = utc_now() - timedelta(seconds=1)
        session.commit()

    replay = client.post("/api/v1/jobs/batches/claim", headers=job_headers, json=claim_payload)
    assert replay.status_code == 409
    assert replay.json()["code"] == "batch-request-expired"
    expired_manifest = client.get(f"/api/v1/jobs/batches/{batch_id}/manifest", headers=job_headers)
    assert expired_manifest.status_code == 409
    assert expired_manifest.json()["code"] == "batch-lease-expired"
    expired_download = client.get(download_url, headers=job_headers)
    assert expired_download.status_code == 409
    assert expired_download.json()["code"] == "batch-lease-expired"

    fresh = client.post(
        "/api/v1/jobs/batches/claim",
        headers=job_headers,
        json={"request_id": "jenkins-lease-reclaim", "limit": 10},
    )
    assert fresh.status_code == 200
    fresh_manifest = client.get(
        f"/api/v1/jobs/batches/{fresh.json()['batch_id']}/manifest", headers=job_headers
    ).json()
    assert fresh_manifest["operations"][0]["operation_id"] == operation_id
    with app.state.test_session_factory() as session:
        old_batch = session.get(JobBatch, UUID(batch_id))
        assert old_batch is not None
        assert old_batch.state == BatchState.EXPIRED


def test_expired_lease_is_durably_requeued_when_staging_is_attempted(
    app,
    client: TestClient,
    upload_pdf,
    job_headers: dict[str, str],
) -> None:
    uploaded = upload_pdf(filename="stage-expiry.pdf", key="stage-expiry-key")
    document_id = uploaded.json()["document"]["id"]
    claimed = client.post(
        "/api/v1/jobs/batches/claim",
        headers=job_headers,
        json={"request_id": "jenkins-stage-expiry", "limit": 10},
    ).json()
    manifest = client.get(
        f"/api/v1/jobs/batches/{claimed['batch_id']}/manifest", headers=job_headers
    ).json()
    with app.state.test_session_factory() as session:
        batch = session.get(JobBatch, UUID(claimed["batch_id"]))
        assert batch is not None
        batch.lease_expires_at = utc_now() - timedelta(seconds=1)
        session.commit()

    staged = client.post(
        f"/api/v1/jobs/batches/{claimed['batch_id']}/staged",
        headers=job_headers,
        json={"operation_ids": [manifest["operations"][0]["operation_id"]]},
    )
    assert staged.status_code == 409
    assert staged.json()["code"] == "batch-lease-expired"
    with app.state.test_session_factory() as session:
        batch = session.get(JobBatch, UUID(claimed["batch_id"]))
        document = session.get(Document, UUID(document_id))
        assert batch is not None and batch.state == BatchState.EXPIRED
        assert document is not None and document.state == DocumentState.QUEUED


def test_deletion_cleanup_failure_can_be_replayed(
    app,
    client: TestClient,
    csrf_headers: dict[str, str],
    upload_pdf,
    job_headers: dict[str, str],
    monkeypatch,
) -> None:
    uploaded = upload_pdf(filename="delete-cleanup.pdf", key="delete-cleanup-key")
    document_id = uploaded.json()["document"]["id"]
    ingest_batch, ingest_manifest = _claim_and_stage(client, job_headers, "jenkins-cleanup-ingest")
    ingest_result = {
        "pipeline_run_id": "cleanup-ingest-run",
        "results": [
            {
                "operation_id": ingest_manifest["operations"][0]["operation_id"],
                "outcome": "succeeded",
                "chunk_count": 2,
                "components": _components(),
                "classification": _detected_language(),
            }
        ],
    }
    assert (
        client.post(
            f"/api/v1/jobs/batches/{ingest_batch['batch_id']}/results",
            headers=job_headers,
            json=ingest_result,
        ).status_code
        == 200
    )
    assert (
        client.post(f"/api/v1/documents/{document_id}/deletion", headers=csrf_headers).json()[
            "document"
        ]["state"]
        == "DELETE_QUEUED"
    )
    delete_batch, delete_manifest = _claim_and_stage(client, job_headers, "jenkins-cleanup-delete")
    result_payload = {
        "pipeline_run_id": "cleanup-delete-run",
        "results": [
            {
                "operation_id": delete_manifest["operations"][0]["operation_id"],
                "outcome": "succeeded",
                "components": _components(),
            }
        ],
    }

    from pdf_bridge import jobs

    original_remove = jobs.remove_storage_key

    def unavailable_storage(*_args, **_kwargs):
        raise OSError("storage unavailable")

    monkeypatch.setattr(jobs, "remove_storage_key", unavailable_storage)
    failed = client.post(
        f"/api/v1/jobs/batches/{delete_batch['batch_id']}/results",
        headers=job_headers,
        json=result_payload,
    )
    assert failed.status_code == 500
    assert failed.json()["code"] == "storage-cleanup-failed"
    with app.state.test_session_factory() as session:
        document = session.get(Document, UUID(document_id))
        assert document is not None
        assert document.state == DocumentState.DELETE_CLEANUP
        assert document.storage_key is not None

    monkeypatch.setattr(jobs, "remove_storage_key", original_remove)
    replay = client.post(
        f"/api/v1/jobs/batches/{delete_batch['batch_id']}/results",
        headers=job_headers,
        json=result_payload,
    )
    assert replay.status_code == 200
    assert replay.json()["idempotent_replay"] is True
    with app.state.test_session_factory() as session:
        document = session.get(Document, UUID(document_id))
        assert document is not None
        assert document.state == DocumentState.DELETED
        assert document.storage_key is None
