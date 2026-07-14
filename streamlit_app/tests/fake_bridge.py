"""Deterministic in-memory API v2 backend for Streamlit acceptance tests."""

from __future__ import annotations

from copy import deepcopy
from io import BytesIO
from typing import Any

TIMESTAMP = "2026-07-13T15:00:00Z"
LATER_TIMESTAMP = "2026-07-13T15:02:00Z"
SOURCE_SHA = "a" * 64
MANIFEST_SHA = "b" * 64
MARKDOWN_SHA = "c" * 64
TEXT_SHA = "d" * 64
PROJECTION_SHA = "e" * 64

ALL_STATES = (
    "PREFLIGHTING",
    "PREFLIGHT_FAILED",
    "REVIEW_REQUIRED",
    "PUBLISHING",
    "PUBLISH_FAILED",
    "READY",
    "DELETING",
    "DELETE_FAILED",
    "REJECTED",
    "CANCELLED",
    "DELETED",
)
TERMINAL_STATES = {"REJECTED", "CANCELLED", "DELETED"}


def _operation(
    document_id: str,
    *,
    operation_type: str,
    state: str,
    phase: str,
    attempt: int = 1,
    failure: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": f"op::{document_id}::{operation_type.lower()}::{attempt}",
        "document_id": document_id,
        "prepared_revision_id": f"revision::{document_id}",
        "replacement_target_document_id": None,
        "operation_type": operation_type,
        "state": state,
        "phase": phase,
        "priority": "HIGH" if operation_type == "DELETE" else "NORMAL",
        "attempt": attempt,
        "retryable": state == "FAILED",
        "queue_position": 1 if state == "QUEUED" else None,
        "queue_age_seconds": 3.0,
        "phase_age_seconds": 1.0,
        "created_at": TIMESTAMP,
        "updated_at": TIMESTAMP,
        "started_at": TIMESTAMP if state != "QUEUED" else None,
        "completed_at": LATER_TIMESTAMP if state in {"SUCCEEDED", "FAILED"} else None,
        "failure": failure,
    }


def _revision(document_id: str) -> dict[str, Any]:
    completeness = {
        "native_text_eligible": True,
        "formatter_complete": True,
        "vector_complete": True,
        "candidate_discovery_complete": True,
        "advisory_complete": True,
        "clear_for_publication": True,
        "incomplete_reasons": [],
    }
    return {
        "id": f"revision::{document_id}",
        "revision_number": 1,
        "status": "SEALED",
        "active_qdrant_collection": "pdf-customer-v2",
        "content_profile_id": "content-profile-test",
        "index_profile_id": "index-profile-test",
        "preflight_policy_id": "preflight-policy-test",
        "formatter_model_id": "formatter-test",
        "dense_model_id": "dense-test",
        "dense_dimension": 768,
        "sparse_model_id": "sparse-test",
        "language_code": "en",
        "completeness": completeness,
        "page_count": 2,
        "chunk_count": 2,
        "expected_point_count": 2,
        "markdown_sha256": MARKDOWN_SHA,
        "manifest_sha256": MANIFEST_SHA,
        "failure": None,
        "created_at": TIMESTAMP,
        "sealed_at": LATER_TIMESTAMP,
    }


def _document(
    document_id: str,
    filename: str,
    state: str,
    *,
    allowed_actions: list[str],
) -> dict[str, Any]:
    failure = None
    current_operation = None
    if state == "PREFLIGHT_FAILED":
        failure = {
            "code": "formatter_unavailable",
            "message": "The formatter was temporarily unavailable.",
            "retryable": True,
        }
        current_operation = _operation(
            document_id,
            operation_type="PREFLIGHT",
            state="FAILED",
            phase="FORMATTING_MARKDOWN",
            failure=failure,
        )
    elif state == "PUBLISHING":
        current_operation = _operation(
            document_id,
            operation_type="PUBLISH",
            state="RUNNING",
            phase="UPSERT_ACTIVE_POINTS",
        )

    return {
        "id": document_id,
        "collection_key": "customer",
        "original_filename": filename,
        "content_type": "application/pdf",
        "size_bytes": 2048,
        "sha256": SOURCE_SHA,
        "created_by": "operator@example.test",
        "state": state,
        "created_at": TIMESTAMP,
        "updated_at": TIMESTAMP,
        "ready_at": TIMESTAMP if state == "READY" else None,
        "failure": failure,
        "allowed_actions": allowed_actions,
        "source": {
            "original_filename": filename,
            "content_type": "application/pdf",
            "size_bytes": 2048,
            "sha256": SOURCE_SHA,
            "created_by": "operator@example.test",
            "created_at": TIMESTAMP,
            "scan_state": "CLEAN",
            "scan_engine": "clamav-test",
            "scanned_at": TIMESTAMP,
            "available": state not in {"DELETING", *TERMINAL_STATES},
        },
        "terminal_disposition": None,
        "current_operation": current_operation,
        "prepared_revision": _revision(document_id),
        "publication": (
            {
                "id": f"publication::{document_id}",
                "prepared_revision_id": f"revision::{document_id}",
                "active_qdrant_collection": "pdf-customer-v2",
                "status": "VERIFIED",
                "expected_points": 2,
                "verified_points": 2,
                "payload_revision_verified": True,
                "vector_schema_verified": True,
                "screening_zero_verified": True,
                "failure": None,
                "created_at": TIMESTAMP,
                "updated_at": LATER_TIMESTAMP,
                "verified_at": LATER_TIMESTAMP,
            }
            if state == "READY"
            else None
        ),
        "deletion": None,
        "replacement": None,
        "decision": None,
    }


class StatefulBridgeClient:
    """Stateful v2 facade whose transitions emulate durable worker progress."""

    base_url = "https://bridge.test"
    csrf_token = "test-token"
    identity_header_name = None
    identity = None

    def __init__(self) -> None:
        self.documents_by_id = {
            "doc-review-keep": _document(
                "doc-review-keep",
                "keep-candidate.pdf",
                "REVIEW_REQUIRED",
                allowed_actions=["KEEP", "REPLACE", "CANCEL", "DELETE"],
            ),
            "doc-review-replace": _document(
                "doc-review-replace",
                "replacement-candidate.pdf",
                "REVIEW_REQUIRED",
                allowed_actions=["KEEP", "REPLACE", "CANCEL", "DELETE"],
            ),
            "doc-review-cancel": _document(
                "doc-review-cancel",
                "cancel-candidate.pdf",
                "REVIEW_REQUIRED",
                allowed_actions=["KEEP", "REPLACE", "CANCEL", "DELETE"],
            ),
            "doc-retry": _document(
                "doc-retry",
                "retry-me.pdf",
                "PREFLIGHT_FAILED",
                allowed_actions=["RETRY", "DELETE"],
            ),
            "doc-ready": _document(
                "doc-ready",
                "operator-handbook.pdf",
                "READY",
                allowed_actions=["DELETE"],
            ),
            "doc-delete": _document(
                "doc-delete",
                "delete-me.pdf",
                "READY",
                allowed_actions=["DELETE"],
            ),
            "doc-replacement-target": _document(
                "doc-replacement-target",
                "existing-handbook.pdf",
                "READY",
                allowed_actions=["DELETE"],
            ),
            "doc-recovery": _document(
                "doc-recovery",
                "recovery-run.pdf",
                "PUBLISHING",
                allowed_actions=["DELETE"],
            ),
        }
        self.operations_by_id = {
            operation["id"]: operation
            for document in self.documents_by_id.values()
            if (operation := document.get("current_operation")) is not None
        }
        self.tombstones: list[dict[str, Any]] = []
        self.uploads: list[dict[str, Any]] = []
        self.decisions: list[dict[str, Any]] = []
        self.retries: list[dict[str, Any]] = []
        self.deletions: list[dict[str, Any]] = []
        self.document_reads: dict[str, int] = {}

    def close(self) -> None:
        raise AssertionError("the stable fake session should not be closed")

    def health(self, probe: str) -> dict[str, Any]:
        if probe == "live":
            return {"status": "OK", "checks": []}
        return {
            "status": "OK",
            "checks": [
                {"component": "catalog", "status": "READY"},
                {"component": "qdrant", "status": "READY"},
            ],
        }

    def collections(self, *, cursor: str | None = None, limit: int = 50) -> dict[str, Any]:
        if cursor is not None:
            raise AssertionError("the fake exposes one collection page")
        counts = {state: 0 for state in ALL_STATES}
        for document in self.documents_by_id.values():
            if document["state"] not in TERMINAL_STATES:
                counts[document["state"]] += 1
        return {
            "items": [
                {
                    "key": "customer",
                    "display_name": "Customer",
                    "description": "Customer-facing documentation",
                    "audience": "customer",
                    "enabled": True,
                    "counts": {"total": sum(counts.values()), "by_state": counts},
                }
            ],
            "limit": limit,
            "next_cursor": None,
            "has_more": False,
        }

    def collection(self, collection_key: str) -> dict[str, Any]:
        if collection_key != "customer":
            raise AssertionError(f"unexpected collection {collection_key!r}")
        result = self.collections(limit=1)["items"][0]
        return result | {
            "target": {
                "schema_version": 2,
                "schema_compatible": True,
                "qdrant_collection_name": "pdf-customer-v2",
                "failure": None,
            }
        }

    def documents(
        self,
        collection_key: str,
        *,
        state: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        if collection_key != "customer" or cursor is not None:
            raise AssertionError("the fake exposes one customer document page")
        items = [
            deepcopy(document)
            for document in self.documents_by_id.values()
            if document["state"] not in TERMINAL_STATES
            and (state is None or document["state"] == state)
        ][:limit]
        return {
            "items": items,
            "limit": limit,
            "next_cursor": None,
            "has_more": False,
        }

    def name_check(self, collection_key: str, *, filename: str) -> dict[str, Any]:
        if collection_key != "customer":
            raise AssertionError(f"unexpected collection {collection_key!r}")
        return {
            "collection_key": collection_key,
            "normalized_filename": filename.casefold(),
            "matches": [
                {
                    "kind": "FILENAME_FAMILY",
                    "document_id": "doc-replacement-target",
                    "original_filename": "existing-handbook.pdf",
                    "state": "READY",
                    "similarity": 0.82,
                }
            ],
        }

    def upload(
        self,
        collection_key: str,
        *,
        filename: str,
        content: bytes | BytesIO,
        idempotency_key: str,
    ) -> dict[str, Any]:
        payload = content if isinstance(content, bytes) else content.read()
        document_id = f"doc-upload-{len(self.uploads) + 1}"
        document = _document(
            document_id,
            filename,
            "PREFLIGHTING",
            allowed_actions=["DELETE"],
        )
        document["prepared_revision"] = None
        operation = _operation(
            document_id,
            operation_type="PREFLIGHT",
            state="RUNNING",
            phase="EXTRACTING",
        )
        document["current_operation"] = operation
        self.documents_by_id[document_id] = document
        self.operations_by_id[operation["id"]] = operation
        self.uploads.append(
            {
                "document_id": document_id,
                "filename": filename,
                "content": payload,
                "collection_key": collection_key,
                "idempotency_key": idempotency_key,
            }
        )
        return {
            "document": deepcopy(document),
            "operation": deepcopy(operation),
            "idempotent_replay": False,
        }

    def document(self, document_id: str) -> dict[str, Any]:
        self.document_reads[document_id] = self.document_reads.get(document_id, 0) + 1
        return deepcopy(self.documents_by_id[document_id])

    def source(self, document_id: str) -> tuple[bytes, str, str]:
        document = self.documents_by_id[document_id]
        return b"%PDF-1.7 test source", document["original_filename"], "application/pdf"

    def markdown(self, document_id: str) -> dict[str, Any]:
        return {
            "document_id": document_id,
            "prepared_revision_id": f"revision::{document_id}",
            "markdown_sha256": MARKDOWN_SHA,
            "markdown": (
                "# Installation\n\nUse the verified package.\n\n## Windows\n\nRun setup.exe."
            ),
            "pages": [
                {
                    "page_number": 1,
                    "markdown": "# Installation\n\nUse the verified package.",
                    "markdown_sha256": "1" * 64,
                    "source_projection_sha256": PROJECTION_SHA,
                    "markdown_projection_sha256": "f" * 64,
                    "slice_count": 1,
                },
                {
                    "page_number": 2,
                    "markdown": "## Windows\n\nRun setup.exe.",
                    "markdown_sha256": "2" * 64,
                    "source_projection_sha256": PROJECTION_SHA,
                    "markdown_projection_sha256": "0" * 64,
                    "slice_count": 2,
                },
            ],
        }

    def chunks(
        self,
        document_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        chunks = [
            {
                "id": "chunk-installation",
                "prepared_revision_id": f"revision::{document_id}",
                "chunk_index": 0,
                "page_start": 1,
                "page_end": 1,
                "heading_path": ["Installation"],
                "token_count": 17,
                "text_sha256": TEXT_SHA,
                "markdown": "# Installation\n\nUse the verified package.",
            },
            {
                "id": "chunk-windows",
                "prepared_revision_id": f"revision::{document_id}",
                "chunk_index": 1,
                "page_start": 2,
                "page_end": 2,
                "heading_path": ["Installation", "Windows"],
                "token_count": 9,
                "text_sha256": "9" * 64,
                "markdown": "## Windows\n\nRun setup.exe.",
            },
        ]
        index = 1 if cursor == "chunk-page-2" else 0
        if cursor not in {None, "chunk-page-2"}:
            raise AssertionError(f"unexpected chunk cursor {cursor!r}")
        return {
            "document_id": document_id,
            "prepared_revision_id": f"revision::{document_id}",
            "items": [chunks[index]],
            "limit": limit,
            "next_cursor": "chunk-page-2" if index == 0 else None,
            "has_more": index == 0,
        }

    def preflight(
        self,
        document_id: str,
        *,
        cursor: str | None = None,
        limit: int = 25,
    ) -> dict[str, Any]:
        if cursor is not None:
            raise AssertionError("the fake exposes one candidate page")
        revision = _revision(document_id)
        candidate = {
            "id": "candidate::replacement-target",
            "document": {
                "id": "doc-replacement-target",
                "collection_key": "customer",
                "original_filename": "existing-handbook.pdf",
                "state": "READY",
                "sha256": SOURCE_SHA,
            },
            "source": "HYBRID",
            "rank": 1,
            "reasons": ["strong_semantic_match"],
            "max_cosine": 0.94,
            "bm25_score": 8.4,
            "fused_score": 0.82,
            "matched_chunk_pair_count": 2,
            "replacement_eligible": True,
            "evidence": [
                {
                    "id": "evidence::1",
                    "kind": "DOCUMENT_IDENTITY",
                    "model_id": "advisory-test",
                    "valid": True,
                    "label": "Likely replacement",
                    "summary": "The content covers the same installation procedure.",
                    "failure_code": None,
                    "evidence_sha256": "7" * 64,
                    "created_at": TIMESTAMP,
                    "citations": [
                        {
                            "document_id": "doc-replacement-target",
                            "chunk_id": "chunk-installation",
                            "page_start": 1,
                            "page_end": 1,
                            "excerpt": "Use the verified package.",
                        }
                    ],
                }
            ],
        }
        return {
            "document_id": document_id,
            "prepared_revision": revision,
            "completeness": revision["completeness"],
            "candidate_count": 1,
            "candidates": {
                "items": [candidate],
                "limit": limit,
                "next_cursor": None,
                "has_more": False,
            },
        }

    def decide(
        self,
        document_id: str,
        *,
        prepared_revision_id: str,
        action: str,
        target_document_id: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        document = self.documents_by_id[document_id]
        self.decisions.append(
            {
                "document_id": document_id,
                "prepared_revision_id": prepared_revision_id,
                "action": action,
                "target_document_id": target_document_id,
                "idempotency_key": idempotency_key,
            }
        )
        document["decision"] = {
            "id": f"decision::{document_id}",
            "prepared_revision_id": prepared_revision_id,
            "prepared_manifest_sha256": MANIFEST_SHA,
            "action": action,
            "target_document_id": target_document_id,
            "actor_type": "OPERATOR",
            "actor_id": "operator@example.test",
            "created_at": TIMESTAMP,
        }
        if action == "CANCEL":
            operation_type = "DELETE"
            document["state"] = "DELETING"
            document["terminal_disposition"] = "CANCELLED"
            document["source"]["available"] = False
            document["deletion"] = self._deletion_summary("CANCELLED")
        else:
            operation_type = "PUBLISH"
            document["state"] = "PUBLISHING"
            if action == "REPLACE":
                document["replacement"] = {
                    "decision_id": f"decision::{document_id}",
                    "old_document_id": target_document_id,
                    "new_document_id": document_id,
                    "old_document_state": "READY",
                    "new_document_state": "PUBLISHING",
                    "operation_id": f"op::{document_id}::publish::1",
                    "phase": "DELETE_ACTIVE_POINTS",
                    "completed_at": None,
                }
        operation = _operation(
            document_id,
            operation_type=operation_type,
            state="RUNNING",
            phase="PURGE_STORAGE" if action == "CANCEL" else "DELETE_ACTIVE_POINTS",
        )
        document["current_operation"] = operation
        document["allowed_actions"] = []
        document["updated_at"] = LATER_TIMESTAMP
        self.operations_by_id[operation["id"]] = operation
        return {
            "document": deepcopy(document),
            "operation": deepcopy(operation),
            "idempotent_replay": False,
        }

    def retry(self, document_id: str, *, idempotency_key: str) -> dict[str, Any]:
        document = self.documents_by_id[document_id]
        previous = document.get("current_operation") or {}
        attempt = int(previous.get("attempt", 1)) + 1
        operation = _operation(
            document_id,
            operation_type="PREFLIGHT",
            state="RUNNING",
            phase="FORMATTING_MARKDOWN",
            attempt=attempt,
        )
        document["state"] = "PREFLIGHTING"
        document["failure"] = None
        document["current_operation"] = operation
        document["allowed_actions"] = ["DELETE"]
        self.operations_by_id[operation["id"]] = operation
        self.retries.append(
            {
                "document_id": document_id,
                "idempotency_key": idempotency_key,
                "attempt": attempt,
            }
        )
        return {
            "document": deepcopy(document),
            "operation": deepcopy(operation),
            "idempotent_replay": False,
        }

    def delete(self, document_id: str, *, idempotency_key: str) -> dict[str, Any]:
        document = self.documents_by_id[document_id]
        operation = _operation(
            document_id,
            operation_type="DELETE",
            state="RUNNING",
            phase="DELETE_ACTIVE_POINTS",
        )
        document["state"] = "DELETING"
        document["source"]["available"] = False
        document["terminal_disposition"] = "DELETED"
        document["current_operation"] = operation
        document["deletion"] = self._deletion_summary("DELETED")
        document["allowed_actions"] = []
        self.operations_by_id[operation["id"]] = operation
        self.deletions.append({"document_id": document_id, "idempotency_key": idempotency_key})
        return {
            "document": deepcopy(document),
            "operation": deepcopy(operation),
            "idempotent_replay": False,
        }

    def events(
        self,
        document_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        if cursor is not None:
            raise AssertionError("the fake exposes one event page")
        return {
            "document_id": document_id,
            "items": [
                {
                    "id": 1,
                    "document_id": document_id,
                    "operation_id": None,
                    "event_type": "DOCUMENT_ADMITTED",
                    "actor_type": "OPERATOR",
                    "actor_id": "operator@example.test",
                    "occurred_at": TIMESTAMP,
                    "attributes": {"collection_key": "customer"},
                }
            ],
            "limit": limit,
            "next_cursor": None,
            "has_more": False,
        }

    def operation(self, operation_id: str) -> dict[str, Any]:
        return deepcopy(self.operations_by_id[operation_id])

    def operation_metrics(self) -> dict[str, Any]:
        buckets: list[dict[str, Any]] = []
        for operation in self.operations_by_id.values():
            if operation["state"] not in {"QUEUED", "RUNNING", "FAILED"}:
                continue
            buckets.append(
                {
                    "operation_type": operation["operation_type"],
                    "state": operation["state"],
                    "phase": operation["phase"],
                    "count": 1,
                    "oldest_operation_age_seconds": 30.0,
                    "oldest_phase_age_seconds": 10.0,
                }
            )
        queued = sum(bucket["state"] == "QUEUED" for bucket in buckets)
        return {
            "generated_at": LATER_TIMESTAMP,
            "total": len(buckets),
            "queued": queued,
            "running": sum(bucket["state"] == "RUNNING" for bucket in buckets),
            "failed": sum(bucket["state"] == "FAILED" for bucket in buckets),
            "oldest_queued_age_seconds": 30.0 if queued else None,
            "buckets": buckets,
        }

    def history(
        self,
        *,
        collection_key: str | None = None,
        disposition: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        if cursor is not None:
            raise AssertionError("the fake exposes one history page")
        items = [
            deepcopy(item)
            for item in self.tombstones
            if (collection_key is None or item["collection_key"] == collection_key)
            and (disposition is None or item["disposition"] == disposition)
        ][:limit]
        return {
            "items": items,
            "limit": limit,
            "next_cursor": None,
            "has_more": False,
        }

    def advance(self, document_id: str) -> None:
        """Advance one durable fake workflow exactly one externally visible state."""

        document = self.documents_by_id[document_id]
        state = document["state"]
        operation = document.get("current_operation")
        if state == "PREFLIGHTING":
            document["state"] = "REVIEW_REQUIRED"
            document["prepared_revision"] = _revision(document_id)
            document["allowed_actions"] = ["KEEP", "REPLACE", "CANCEL", "DELETE"]
        elif state == "PUBLISHING":
            document["state"] = "READY"
            document["ready_at"] = LATER_TIMESTAMP
            document["allowed_actions"] = ["DELETE"]
            document["publication"] = _document(
                document_id, document["original_filename"], "READY", allowed_actions=["DELETE"]
            )["publication"]
            if document.get("replacement"):
                document["replacement"]["new_document_state"] = "READY"
                document["replacement"]["phase"] = "COMPLETE"
                document["replacement"]["completed_at"] = LATER_TIMESTAMP
        elif state == "DELETING":
            disposition = str(document.get("terminal_disposition") or "DELETED")
            document["state"] = disposition
            deletion = document["deletion"]
            deletion.update(
                {
                    "phase": "COMPLETE",
                    "active_zero_verified_at": LATER_TIMESTAMP,
                    "screening_zero_verified_at": LATER_TIMESTAMP,
                    "storage_purged_at": LATER_TIMESTAMP,
                    "tombstoned_at": LATER_TIMESTAMP,
                    "updated_at": LATER_TIMESTAMP,
                }
            )
            self.tombstones.append(
                {
                    "id": f"tombstone::{document_id}",
                    "document_id": document_id,
                    "collection_key": document["collection_key"],
                    "disposition": disposition,
                    "source_sha256": document["sha256"],
                    "manifest_sha256": MANIFEST_SHA,
                    "reason_code": None,
                    "actor_type": "OPERATOR",
                    "actor_id": "operator@example.test",
                    "occurred_at": LATER_TIMESTAMP,
                }
            )
        else:
            raise AssertionError(f"document {document_id!r} cannot advance from {state}")

        if operation is not None:
            operation["state"] = "SUCCEEDED"
            operation["phase"] = "COMPLETE"
            operation["retryable"] = False
            operation["updated_at"] = LATER_TIMESTAMP
            operation["completed_at"] = LATER_TIMESTAMP
        document["updated_at"] = LATER_TIMESTAMP

    @staticmethod
    def _deletion_summary(disposition: str) -> dict[str, Any]:
        return {
            "terminal_disposition": disposition,
            "phase": "PURGE_STORAGE",
            "active_qdrant_collection": "pdf-customer-v2",
            "screening_qdrant_collection": "pdf-customer-screening-v2",
            "attempts": 1,
            "active_zero_verified_at": TIMESTAMP,
            "screening_zero_verified_at": TIMESTAMP,
            "storage_purged_at": None,
            "tombstoned_at": None,
            "failure": None,
            "updated_at": TIMESTAMP,
        }
