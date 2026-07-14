from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import timedelta
from importlib import import_module
from io import BytesIO
from pathlib import Path
from threading import RLock
from types import SimpleNamespace

import pytest
from litestar import Litestar
from litestar.datastructures import State
from litestar.di import Provide
from litestar.middleware.session.client_side import CookieBackendConfig
from litestar.testing import TestClient
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from pdf_bridge.contracts.schemas import (
    DecisionRequest,
    DocumentSummary,
    MutationResponse,
    OperationPriorityName,
    OperationSummary,
    UploadAcceptedResponse,
)
from pdf_bridge.controllers import api
from pdf_bridge.core.config import CollectionDefinition, Settings
from pdf_bridge.http.problems import exception_handlers
from pdf_bridge.managers import catalog as catalog_manager
from pdf_bridge.managers import document as document_manager
from pdf_bridge.persistence.db import Base, build_engine
from pdf_bridge.persistence.models import (
    DecisionAction,
    Document,
    DocumentState,
    IdempotencyRecord,
    OperationPhase,
    OperationPriority,
    OperationState,
    OperationType,
    PreparedRevision,
    RevisionStatus,
    ScanState,
    TerminalDisposition,
    Tombstone,
    WorkOperation,
    utc_now,
)
from pdf_bridge.services.intake import LifecycleError
from pdf_bridge.services.scanner import ScanResult
from pdf_bridge.services.storage import InvalidFilenameError

HASH_A = "a" * 64
HASH_B = "b" * 64
PROFILE_A = "sha256:" + "a" * 64
PROFILE_B = "sha256:" + "b" * 64
PROFILE_C = "sha256:" + "c" * 64
ACTOR_ID = "operator@example.test"


class Notifier:
    def __init__(self) -> None:
        self.notifications = 0

    def notify(self) -> None:
        self.notifications += 1


@pytest.fixture
def engine() -> Iterator[Engine]:
    active = build_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(active)
    yield active
    active.dispose()


def _definition() -> CollectionDefinition:
    return CollectionDefinition(
        key="customer",
        display_name="Customer Product",
        description="Approved customer-facing content.",
        audience="customer",
        qdrant_collection_name="customer-product-pdfs",
    )


def _settings(storage_root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        auth_mode="anonymous-poc",
        trusted_proxy_cidrs=(),
        trusted_identity_header="x-authenticated-user",
        collections=(_definition(),),
        storage_root=storage_root,
        max_upload_bytes=1_024 * 1_024,
        upload_chunk_bytes=1_024,
        qdrant_screening_collection_name="pdf-bridge-screening",
        worker_enabled=False,
    )


def _document(
    *,
    state: DocumentState,
    sha256: str = HASH_A,
    failure_retryable: bool = False,
) -> Document:
    failed = state in {
        DocumentState.PREFLIGHT_FAILED,
        DocumentState.PUBLISH_FAILED,
        DocumentState.DELETE_FAILED,
    }
    return Document(
        collection_key="customer",
        original_filename=f"{uuid.uuid4()}.pdf",
        normalized_filename=f"{uuid.uuid4()}.pdf",
        content_type="application/pdf",
        size_bytes=128,
        sha256=sha256,
        storage_key=f"objects/{uuid.uuid4()}.pdf",
        state=state,
        scan_state=ScanState.CLEAN,
        scan_engine="test-clamd",
        scanned_at=utc_now(),
        created_by=ACTOR_ID,
        failure_code="processing_failed" if failed else None,
        failure_message="Processing did not complete." if failed else None,
        failure_retryable=failure_retryable,
    )


def _operation_summary() -> OperationSummary:
    now = utc_now()
    return OperationSummary(
        id=uuid.uuid4(),
        operation_type=OperationType.PREFLIGHT,
        state=OperationState.QUEUED,
        phase=OperationPhase.QUEUED,
        priority=OperationPriorityName.NORMAL,
        attempt=1,
        retryable=True,
        created_at=now,
        updated_at=now,
    )


def _document_summary() -> DocumentSummary:
    now = utc_now()
    return DocumentSummary(
        id=uuid.uuid4(),
        collection_key="customer",
        original_filename="guide.pdf",
        content_type="application/pdf",
        size_bytes=128,
        sha256=HASH_A,
        created_by=ACTOR_ID,
        state=DocumentState.PREFLIGHTING,
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def transport_client(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    settings = _settings(tmp_path / "storage")

    def provide_db() -> Iterator[Session]:
        with Session(engine) as session:
            yield session

    monkeypatch.setattr(api, "validate_collection_schema", lambda client, name: None)
    session_config = CookieBackendConfig(
        secret=b"a" * 32,
        key="pdf_bridge_test_session",
        path="/",
        httponly=True,
        samesite="strict",
    )
    application = Litestar(
        route_handlers=api.create_api_routers(2 * 1_024 * 1_024),
        dependencies={"db": Provide(provide_db)},
        exception_handlers=exception_handlers,
        middleware=[session_config.middleware],
        openapi_config=None,
        state=State(
            {
                "settings": settings,
                "scanner": object(),
                "transition_lock": RLock(),
                "worker_providers": SimpleNamespace(qdrant=object()),
            }
        ),
    )
    with TestClient(application) as client:
        yield client


def test_authenticated_gets_initialize_and_reuse_cookie_csrf_session(
    transport_client: TestClient,
) -> None:
    first = transport_client.get("/api/v2/collections?limit=1")

    assert first.status_code == 200
    token = first.headers["x-csrf-token"]
    assert token
    assert "pdf_bridge_test_session" in transport_client.cookies
    assert first.json()["items"][0]["key"] == "customer"

    detail = transport_client.get("/api/v2/collections/customer")
    assert detail.status_code == 200
    assert detail.headers["x-csrf-token"] == token
    assert detail.json()["target"] == {
        "qdrant_collection_name": "customer-product-pdfs",
        "schema_version": 2,
        "schema_compatible": True,
        "failure": None,
    }


def test_integrated_create_app_keeps_json_openapi_but_no_html_routes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import_root = tmp_path / "import-storage"
    monkeypatch.setenv("PDF_BRIDGE_STORAGE_ROOT", str(import_root))
    monkeypatch.setenv("PDF_BRIDGE_APP_ENV", "test")
    monkeypatch.setenv("PDF_BRIDGE_WORKER_ENABLED", "false")
    monkeypatch.setenv(
        "PDF_BRIDGE_SESSION_SECRET", "test-import-session-secret-32-characters"
    )
    monkeypatch.setenv("PDF_BRIDGE_ALLOWED_HOSTS", '["testserver.local"]')
    monkeypatch.setenv(
        "PDF_BRIDGE_COLLECTIONS",
        '[{"key":"customer","display_name":"Customer Product",'
        '"description":"Approved customer content.","audience":"customer",'
        '"qdrant_collection_name":"customer-product-pdfs"}]',
    )
    app_module = import_module("pdf_bridge.app")
    storage_root = tmp_path / "integrated-storage"
    settings = Settings(
        _env_file=None,
        app_env="test",
        auth_mode="anonymous-poc",
        storage_root=storage_root,
        database_url="sqlite+pysqlite:///:memory:",
        session_secret=SecretStr("test-session-secret-not-for-production"),
        allowed_hosts=["testserver.local"],
        worker_enabled=False,
        collections=[_definition()],
    )
    integrated_engine = build_engine(settings.database_url)
    Base.metadata.create_all(integrated_engine)

    def provide_db() -> Iterator[Session]:
        with Session(integrated_engine) as session:
            yield session

    application = app_module.create_app(
        settings,
        scanner=lambda path: ScanResult(
            state=ScanState.CLEAN,
            engine="test-clamd",
            scanned_at=utc_now(),
        ),
        db_provider=provide_db,
    )
    try:
        with TestClient(application, base_url="http://testserver.local") as client:
            docs = client.get("/api/docs")
            schema = client.get("/api/openapi.json")
            retired = client.get("/api/v1/collections")

            assert docs.status_code == 404
            assert docs.headers["content-type"].startswith("application/json")
            assert docs.json()["error"]["code"] == "route_not_found"
            assert schema.status_code == 200
            assert "json" in schema.headers["content-type"]
            assert retired.status_code == 404
            assert retired.json()["error"]["code"] == "route_not_found"
    finally:
        integrated_engine.dispose()


@pytest.mark.parametrize("path", ["/", "/api/v1/collections", "/api/docs"])
def test_retired_and_html_routes_are_absent_with_strict_errors(
    transport_client: TestClient,
    path: str,
) -> None:
    response = transport_client.get(path)

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["code"] == "route_not_found"
    assert set(response.json()) == {"error"}


def test_framework_405_and_query_or_json_validation_use_error_response(
    transport_client: TestClient,
) -> None:
    method = transport_client.patch("/api/v2/health/live")
    query = transport_client.get("/api/v2/collections?limit=0")
    token = transport_client.get("/api/v2/collections?limit=1").headers[
        "x-csrf-token"
    ]
    body = transport_client.post(
        "/api/v2/collections/customer/name-check",
        headers={"x-csrf-token": token},
        json={"filename": "not-a-pdf.txt"},
    )

    assert method.status_code == 405
    assert method.json()["error"]["code"] == "method_not_allowed"
    assert query.status_code == 400
    assert query.json()["error"]["code"] == "invalid_request"
    assert body.status_code == 422
    assert body.json()["error"]["code"] == "request_validation_failed"


def test_mutation_requires_csrf_and_visible_idempotency_key_before_manager(
    transport_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = transport_client.get("/api/v2/collections?limit=1").headers[
        "x-csrf-token"
    ]
    calls: list[str] = []
    response = MutationResponse(
        document=_document_summary(), operation=_operation_summary()
    )

    def fake_retry(*args: object, **kwargs: object) -> MutationResponse:
        calls.append(str(kwargs["idempotency_key"]))
        return response

    monkeypatch.setattr(api.document, "retry_document", fake_retry)
    path = f"/api/v2/documents/{uuid.uuid4()}/retry"

    no_csrf = transport_client.post(
        path,
        headers={"idempotency-key": "retry-key-0001"},
        json={},
    )
    no_key = transport_client.post(path, headers={"x-csrf-token": token}, json={})
    invisible_key = transport_client.post(
        path,
        headers={"x-csrf-token": token, "idempotency-key": "bad key 0001"},
        json={},
    )
    accepted = transport_client.post(
        path,
        headers={"x-csrf-token": token, "idempotency-key": "retry-key-0001"},
        json={},
    )
    unknown_body = transport_client.post(
        path,
        headers={"x-csrf-token": token, "idempotency-key": "retry-key-0002"},
        json={"reason": "change processing"},
    )

    assert no_csrf.status_code == 403
    assert no_key.status_code == 400
    assert invisible_key.status_code == 400
    assert accepted.status_code == 202
    assert accepted.json()["operation"]["operation_type"] == "PREFLIGHT"
    assert unknown_body.status_code == 422
    assert calls == ["retry-key-0001"]


def test_upload_multipart_accepts_exactly_one_file_field(
    transport_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = transport_client.get("/api/v2/collections?limit=1").headers[
        "x-csrf-token"
    ]
    calls: list[dict[str, object]] = []
    result = UploadAcceptedResponse(
        document=_document_summary(), operation=_operation_summary()
    )

    def fake_upload(*args: object, **kwargs: object) -> UploadAcceptedResponse:
        calls.append(kwargs)
        return result

    monkeypatch.setattr(api.document, "upload_document", fake_upload)
    headers = {
        "x-csrf-token": token,
        "idempotency-key": "upload-key-0001",
    }
    path = "/api/v2/collections/customer/documents"
    accepted = transport_client.post(
        path,
        headers=headers,
        files={"file": ("guide.pdf", b"%PDF-1.4\n%%EOF", "application/pdf")},
    )
    extra = transport_client.post(
        path,
        headers=headers,
        data={"collection_key": "other"},
        files={"file": ("guide.pdf", b"%PDF-1.4\n%%EOF", "application/pdf")},
    )
    repeated = transport_client.post(
        path,
        headers=headers,
        files=[
            ("file", ("guide.pdf", b"%PDF-1.4\n%%EOF", "application/pdf")),
            ("file", ("other.pdf", b"%PDF-1.4\n%%EOF", "application/pdf")),
        ],
    )

    assert accepted.status_code == 202
    assert calls[0]["collection_key"] == "customer"
    assert calls[0]["idempotency_key"] == "upload-key-0001"
    assert extra.status_code == 400
    assert repeated.status_code == 400
    assert len(calls) == 1


def test_name_check_rejects_path_like_filename_as_client_input(
    transport_client: TestClient,
) -> None:
    token = transport_client.get("/api/v2/collections?limit=1").headers[
        "x-csrf-token"
    ]

    response = transport_client.post(
        "/api/v2/collections/customer/name-check",
        headers={"x-csrf-token": token},
        json={"filename": "guide／secret.pdf"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_filename"


def test_exact_duplicate_conflict_returns_only_the_existing_document_id(
    transport_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = transport_client.get("/api/v2/collections?limit=1").headers[
        "x-csrf-token"
    ]
    existing_id = uuid.uuid4()

    def duplicate(*_args: object, **_kwargs: object) -> None:
        raise LifecycleError(
            "exact_duplicate",
            "The same PDF bytes are already retained in this collection.",
            extra={"existing_document_id": str(existing_id), "unsafe": "not public"},
        )

    monkeypatch.setattr(api.document, "upload_document", duplicate)
    response = transport_client.post(
        "/api/v2/collections/customer/documents",
        headers={
            "x-csrf-token": token,
            "idempotency-key": "duplicate-upload-0001",
        },
        files={"file": ("guide.pdf", b"%PDF-1.4\n%%EOF", "application/pdf")},
    )

    assert response.status_code == 409
    assert response.json()["error"]["existing_document_id"] == str(existing_id)
    assert "unsafe" not in response.json()["error"]


def test_unknown_collection_documents_and_history_are_not_visible(
    transport_client: TestClient,
    engine: Engine,
) -> None:
    hidden = _document(state=DocumentState.DELETED)
    hidden.collection_key = "retired"
    hidden.storage_key = None
    hidden.terminal_disposition = TerminalDisposition.DELETED
    hidden.deleted_at = utc_now()
    hidden_tombstone = Tombstone(
        document=hidden,
        collection_key="retired",
        disposition=TerminalDisposition.DELETED,
        source_sha256=hidden.sha256,
        actor_type="operator",
        actor_id=ACTOR_ID,
    )
    with Session(engine) as session:
        session.add_all([hidden, hidden_tombstone])
        session.commit()
        hidden_id = hidden.id

    detail = transport_client.get(f"/api/v2/documents/{hidden_id}")
    history = transport_client.get("/api/v2/history")
    filtered = transport_client.get("/api/v2/history?collection_key=retired")

    assert detail.status_code == 404
    assert detail.json()["error"]["code"] == "document_not_found"
    assert history.status_code == 200
    assert history.json()["items"] == []
    assert filtered.status_code == 404
    assert filtered.json()["error"]["code"] == "collection_not_found"


def test_operation_metrics_report_visible_queue_depth_phase_and_age(
    transport_client: TestClient,
    engine: Engine,
) -> None:
    now = utc_now()
    owner = _document(state=DocumentState.PREFLIGHTING)
    operation = WorkOperation(
        document=owner,
        operation_type=OperationType.PREFLIGHT,
        priority=int(OperationPriority.NORMAL),
        state=OperationState.QUEUED,
        phase=OperationPhase.CHECKING_ELIGIBILITY,
        attempt=1,
        created_at=now - timedelta(seconds=90),
        phase_started_at=now - timedelta(seconds=30),
        updated_at=now - timedelta(seconds=2),
    )
    with Session(engine) as session:
        session.add_all([owner, operation])
        session.commit()
        metrics = catalog_manager.operation_metrics(
            session,
            definitions=(_definition(),),
            now=now,
        )

    response = transport_client.get("/api/v2/operations/metrics")

    assert metrics.total == 1
    assert metrics.queued == 1
    assert metrics.running == 0
    assert metrics.oldest_queued_age_seconds == pytest.approx(90)
    assert metrics.buckets[0].phase is OperationPhase.CHECKING_ELIGIBILITY
    assert metrics.buckets[0].oldest_phase_age_seconds == pytest.approx(30)
    assert response.status_code == 200
    assert response.json()["queued"] == 1


def test_upload_manager_commits_strict_snapshot_and_replays_without_notify(
    engine: Engine,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path / "storage")
    notifier = Notifier()
    transition_lock = RLock()

    def scanner(path: Path) -> ScanResult:
        assert path.is_file()
        return ScanResult(
            state=ScanState.CLEAN,
            engine="test-clamd",
            scanned_at=utc_now(),
        )

    with Session(engine) as session:
        first = document_manager.upload_document(
            session,
            settings=settings,
            scanner=scanner,
            transition_lock=transition_lock,
            worker=notifier,
            file=BytesIO(b"%PDF-1.4\n%%EOF"),
            filename="guide.pdf",
            content_type="application/pdf",
            collection_key="customer",
            idempotency_key="upload-key-0001",
            actor_type="operator",
            actor_id=ACTOR_ID,
        )
        replay = document_manager.upload_document(
            session,
            settings=settings,
            scanner=scanner,
            transition_lock=transition_lock,
            worker=notifier,
            file=BytesIO(b"%PDF-1.4\n%%EOF"),
            filename="guide.pdf",
            content_type="application/pdf",
            collection_key="customer",
            idempotency_key="upload-key-0001",
            actor_type="operator",
            actor_id=ACTOR_ID,
        )

        record = session.scalar(
            select(IdempotencyRecord).where(IdempotencyRecord.key == "upload-key-0001")
        )
        assert record is not None
        assert record.response_body == first.model_dump(mode="json")
        assert record.response_body["operation"]["operation_type"] == "PREFLIGHT"
        assert "type" not in record.response_body["operation"]
        assert replay.document.id == first.document.id
        assert replay.idempotent_replay is True
        assert notifier.notifications == 1
        assert session.scalar(select(func.count()).select_from(Document)) == 1


def test_upload_manager_preserves_typed_filename_rejection(
    engine: Engine,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path / "storage")

    def scanner(_path: Path) -> ScanResult:
        raise AssertionError("invalid filenames must fail before staging or scanning")

    with Session(engine) as session:
        with pytest.raises(InvalidFilenameError):
            document_manager.upload_document(
                session,
                settings=settings,
                scanner=scanner,
                transition_lock=RLock(),
                worker=Notifier(),
                file=BytesIO(b"%PDF-1.4\n%%EOF"),
                filename="guide／secret.pdf",
                content_type="application/pdf",
                collection_key="customer",
                idempotency_key="upload-invalid-filename-0001",
                actor_type="operator",
                actor_id=ACTOR_ID,
            )

        assert session.scalar(select(func.count()).select_from(Document)) == 0


def test_decision_and_retry_managers_seal_v2_replay_material(
    engine: Engine,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path / "storage")
    notifier = Notifier()
    transition_lock = RLock()

    with Session(engine) as session:
        review = _document(state=DocumentState.REVIEW_REQUIRED)
        revision = PreparedRevision(
            document=review,
            revision_number=1,
            status=RevisionStatus.SEALED,
            active_qdrant_collection="customer-product-pdfs",
            content_profile_id=PROFILE_A,
            index_profile_id=PROFILE_B,
            preflight_policy_id=PROFILE_C,
            formatter_model_id="formatter-v1",
            dense_model_id="all-mpnet-base-v2",
            dense_dimension=768,
            sparse_model_id="Qdrant/bm25",
            native_text_eligible=True,
            formatter_complete=True,
            vector_complete=True,
            candidate_discovery_complete=True,
            advisory_complete=True,
            clear_for_publication=False,
            incomplete_reasons=[],
            page_count=0,
            chunk_count=0,
            expected_point_count=0,
            markdown_sha256=HASH_A,
            manifest_sha256=HASH_B,
            sealed_at=utc_now(),
        )
        failed = _document(
            state=DocumentState.PREFLIGHT_FAILED,
            sha256="c" * 64,
            failure_retryable=True,
        )
        failed_operation = WorkOperation(
            document=failed,
            operation_type=OperationType.PREFLIGHT,
            priority=int(OperationPriority.NORMAL),
            state=OperationState.FAILED,
            phase=OperationPhase.EXTRACTING,
            attempt=1,
            retryable=True,
            failure_code="provider_unavailable",
            failure_message="The processing provider was unavailable.",
        )
        session.add_all([review, revision, failed, failed_operation])
        session.commit()

        decision = document_manager.decide_document(
            session,
            settings=settings,
            transition_lock=transition_lock,
            worker=notifier,
            document_id=review.id,
            request=DecisionRequest(
                prepared_revision_id=revision.id,
                action=DecisionAction.KEEP,
            ),
            idempotency_key="decision-key-0001",
            actor_type="operator",
            actor_id=ACTOR_ID,
        )
        decision_replay = document_manager.decide_document(
            session,
            settings=settings,
            transition_lock=transition_lock,
            worker=notifier,
            document_id=review.id,
            request=DecisionRequest(
                prepared_revision_id=revision.id,
                action=DecisionAction.KEEP,
            ),
            idempotency_key="decision-key-0001",
            actor_type="operator",
            actor_id=ACTOR_ID,
        )
        retry = document_manager.retry_document(
            session,
            transition_lock=transition_lock,
            worker=notifier,
            document_id=failed.id,
            idempotency_key="retry-key-0001",
            actor_type="operator",
            actor_id=ACTOR_ID,
        )
        retry_replay = document_manager.retry_document(
            session,
            transition_lock=transition_lock,
            worker=notifier,
            document_id=failed.id,
            idempotency_key="retry-key-0001",
            actor_type="operator",
            actor_id=ACTOR_ID,
        )

        assert decision.document.state is DocumentState.PUBLISHING
        assert decision.operation.operation_type is OperationType.PUBLISH
        assert decision_replay.idempotent_replay is True
        assert retry.document.state is DocumentState.PREFLIGHTING
        assert retry.operation.attempt == 2
        assert retry_replay.idempotent_replay is True
        assert notifier.notifications == 2
        records = session.scalars(
            select(IdempotencyRecord).where(
                IdempotencyRecord.key.in_(["decision-key-0001", "retry-key-0001"])
            )
        ).all()
        assert all(record.response_status == 202 for record in records)
        assert all("operation" in (record.response_body or {}) for record in records)


def test_delete_manager_uses_fixed_target_and_replays_one_operation(
    engine: Engine,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path / "storage")
    notifier = Notifier()
    transition_lock = RLock()

    with Session(engine) as session:
        existing = _document(state=DocumentState.PREFLIGHT_FAILED)
        session.add(existing)
        session.commit()

        first = document_manager.delete_document(
            session,
            settings=settings,
            transition_lock=transition_lock,
            worker=notifier,
            document_id=existing.id,
            idempotency_key="delete-key-0001",
            actor_type="operator",
            actor_id=ACTOR_ID,
        )
        replay = document_manager.delete_document(
            session,
            settings=settings,
            transition_lock=transition_lock,
            worker=notifier,
            document_id=existing.id,
            idempotency_key="delete-key-0001",
            actor_type="operator",
            actor_id=ACTOR_ID,
        )

        session.refresh(existing)
        assert existing.state is DocumentState.DELETING
        assert existing.deletion_progress is not None
        assert (
            existing.deletion_progress.active_qdrant_collection
            == "customer-product-pdfs"
        )
        assert first.operation.priority is OperationPriorityName.HIGH
        assert replay.idempotent_replay is True
        assert replay.operation.id == first.operation.id
        assert notifier.notifications == 1
        assert session.scalar(select(func.count()).select_from(WorkOperation)) == 1


def test_disabled_worker_closes_every_mutation_before_state_changes(
    engine: Engine,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path / "storage")
    transition_lock = RLock()

    with Session(engine) as session:
        existing = _document(state=DocumentState.PREFLIGHT_FAILED)
        session.add(existing)
        session.commit()

        with pytest.raises(LifecycleError) as raised:
            document_manager.delete_document(
                session,
                settings=settings,
                transition_lock=transition_lock,
                worker=None,
                document_id=existing.id,
                idempotency_key="maintenance-delete-0001",
                actor_type="operator",
                actor_id=ACTOR_ID,
            )

        assert raised.value.code == "worker_unavailable"
        assert raised.value.status == 503
        assert raised.value.retryable is True
        session.refresh(existing)
        assert existing.state is DocumentState.PREFLIGHT_FAILED
        assert session.scalar(select(func.count()).select_from(WorkOperation)) == 0
