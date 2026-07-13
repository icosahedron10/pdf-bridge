from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from pdf_bridge.managers.importing import run_manifest_import
from pdf_bridge.managers.worker import AnalysisWorker, WorkerProviders
from pdf_bridge.persistence.db import session_scope
from pdf_bridge.persistence.models import (
    AnalysisStatus,
    AuditEvent,
    DecisionAction,
    Document,
    DocumentAnalysis,
    DocumentState,
    IndexAction,
    IndexOutboxEntry,
    IndexTarget,
    IntakeDecision,
    OutboxState,
    ScanState,
    utc_now,
)
from pdf_bridge.services import historical_import
from pdf_bridge.services.scanner import ScanResult
from pdf_bridge.services.storage import (
    StorageLayout,
    resolve_storage_key,
    storage_key_for,
)

PDF_ONE = b"%PDF-1.4\n% historical one\n%%EOF\n"
PDF_TWO = b"%PDF-1.4\n% historical two\n%%EOF\n"


@pytest.mark.parametrize("tombstone", [DocumentState.CANCELLED, DocumentState.DELETED])
def test_content_purge_retains_decision_and_outbox_snapshots(
    settings,
    session_factory: sessionmaker[Session],
    tombstone: DocumentState,
) -> None:
    document_id = uuid.uuid4()
    analysis_id = uuid.uuid4()
    decision_id = uuid.uuid4()
    layout = StorageLayout.from_root(settings.storage_root)
    storage_key = storage_key_for(document_id)
    canonical_path = resolve_storage_key(layout, storage_key)
    canonical_path.parent.mkdir(parents=True, exist_ok=True)
    canonical_path.write_bytes(PDF_ONE)

    with session_factory() as session:
        document = Document(
            id=document_id,
            original_filename="retained.pdf",
            normalized_filename="retained.pdf",
            storage_key=storage_key,
            size_bytes=len(PDF_ONE),
            sha256="a" * 64,
            idempotency_key=f"upload-{document_id}",
            state=DocumentState.INGESTED,
            collection_key="customer",
            scan_state=ScanState.CLEAN,
            scan_engine="test-clamd",
            scanned_at=utc_now(),
            uploader_identity="operator@example.test",
            analysis_revision=1,
            text_sha256="b" * 64,
        )
        analysis = DocumentAnalysis(
            id=analysis_id,
            document=document,
            revision=1,
            status=AnalysisStatus.COMPLETE,
            pipeline_fingerprint="pl1-test",
            collection_epoch=1,
            completed_at=utc_now(),
        )
        decision = IntakeDecision(
            id=decision_id,
            document=document,
            analysis_id=analysis_id,
            analysis_revision=1,
            action=DecisionAction.KEEP,
            idempotency_key=f"decision-{document_id}",
            advisory_override=True,
            actor_type="session",
            actor_id="operator@example.test",
        )
        outbox = IndexOutboxEntry(
            document_id=document_id,
            analysis_id=analysis_id,
            collection_key="customer",
            collection_epoch=1,
            target=IndexTarget.ACTIVE,
            action=IndexAction.UPSERT,
            expected_points=0,
            state=OutboxState.DONE,
            attempts=1,
            completed_at=utc_now(),
        )
        audit = AuditEvent(
            document=document,
            event_type="decision_keep",
            actor_type="session",
            actor_id="operator@example.test",
            details={"analysis_revision": 1},
        )
        session.add_all([document, analysis, decision, outbox, audit])
        session.commit()

    worker = AnalysisWorker(
        settings=settings,
        session_factory=session_factory,
        providers=WorkerProviders(),
    )
    with session_factory() as session:
        document = session.get(Document, document_id)
        assert document is not None
        worker._purge_document_content(session, document, tombstone=tombstone)
        session.commit()

    with session_factory() as session:
        document = session.get(Document, document_id)
        decision = session.get(IntakeDecision, decision_id)
        outbox = session.scalar(
            select(IndexOutboxEntry).where(IndexOutboxEntry.document_id == document_id)
        )
        assert document is not None
        assert document.state == tombstone
        assert document.storage_key is None
        assert document.analysis_manifest_hash is not None
        assert session.get(DocumentAnalysis, analysis_id) is None
        assert decision is not None and decision.analysis_id == analysis_id
        assert outbox is not None and outbox.analysis_id == analysis_id
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.document_id == document_id)
            )
            == 2
        )
    assert not canonical_path.exists()


def _write_two_item_manifest(tmp_path: Path) -> tuple[Path, Path]:
    source_root = tmp_path / "historical-sources"
    source_root.mkdir()
    (source_root / "one.pdf").write_bytes(PDF_ONE)
    (source_root / "two.pdf").write_bytes(PDF_TWO)
    manifest_path = tmp_path / "historical-v3.json"
    manifest_path.write_text(
        json.dumps(
            {
                "version": 3,
                "documents": [
                    {
                        "path": "one.pdf",
                        "filename": "One.pdf",
                        "collection_key": "customer",
                    },
                    {
                        "path": "two.pdf",
                        "filename": "Two.pdf",
                        "collection_key": "internal",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return manifest_path, source_root


def _fail_second_scan():
    calls = 0

    def scanner(_path: Path) -> ScanResult:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated later-entry scan failure")
        return ScanResult(state=ScanState.CLEAN, engine="test-clamd", scanned_at=utc_now())

    return scanner


def test_historical_import_compensates_earlier_promotions_on_later_failure(
    tmp_path: Path,
    settings,
    session_factory: sessionmaker[Session],
) -> None:
    manifest_path, source_root = _write_two_item_manifest(tmp_path)

    with pytest.raises(RuntimeError, match="later-entry scan failure"):
        run_manifest_import(
            manifest_path=manifest_path,
            source_root=source_root,
            dry_run=False,
            actor_id="import-regression-test",
            settings_provider=lambda: settings,
            scanner_factory=lambda _settings: _fail_second_scan(),
            session_scope_factory=lambda: session_scope(session_factory),
        )

    layout = StorageLayout.from_root(settings.storage_root)
    assert list(layout.objects.rglob("*.pdf")) == []
    assert list(layout.temporary.iterdir()) == []
    with session_factory() as session:
        assert session.scalars(select(Document)).all() == []


def test_historical_import_surfaces_failed_mid_import_compensation(
    tmp_path: Path,
    settings,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, source_root = _write_two_item_manifest(tmp_path)
    attempted_keys: list[str] = []

    def refusing_remove(
        _layout: StorageLayout, storage_key: str, *, missing_ok: bool = False
    ) -> None:
        attempted_keys.append(storage_key)
        raise OSError("canonical storage unavailable")

    monkeypatch.setattr(historical_import, "remove_storage_key", refusing_remove)
    with pytest.raises(historical_import.HistoricalImportCleanupError) as failure:
        run_manifest_import(
            manifest_path=manifest_path,
            source_root=source_root,
            dry_run=False,
            actor_id="import-regression-test",
            settings_provider=lambda: settings,
            scanner_factory=lambda _settings: _fail_second_scan(),
            session_scope_factory=lambda: session_scope(session_factory),
        )

    assert len(attempted_keys) == 1
    assert attempted_keys[0] in failure.value.failed_storage_keys
    assert isinstance(failure.value.__cause__, RuntimeError)
    assert "later-entry scan failure" in str(failure.value.__cause__)
    layout = StorageLayout.from_root(settings.storage_root)
    assert len(list(layout.objects.rglob("*.pdf"))) == 1
    assert list(layout.temporary.iterdir()) == []
