from __future__ import annotations

import uuid
from dataclasses import replace

import pytest
from qdrant_client import QdrantClient, models

from pdf_bridge.services.local_embeddings import SparseVector
from pdf_bridge.services.vector_index import (
    ChunkPoint,
    VectorIndexConsistencyError,
    VectorIndexSchemaError,
    activate_prepared_points,
    delete_document_points,
    stage_active_points,
    upsert_screening_points,
    validate_fixed_collections,
    verify_document_zero,
    verify_prepared_points,
)

ACTIVE = "customer-pdfs"
SCREENING = "private-screening"


def provision(client: QdrantClient, name: str, *, dimension: int = 768) -> None:
    client.create_collection(
        name,
        vectors_config={
            "dense": models.VectorParams(size=dimension, distance=models.Distance.COSINE)
        },
        sparse_vectors_config={
            "bm25": models.SparseVectorParams(
                index=models.SparseIndexParams(), modifier=models.Modifier.IDF
            )
        },
    )


class SchemaView:
    """Local Qdrant omits payload indexes, so inject the platform schema description."""

    def __init__(self, client: QdrantClient) -> None:
        self.client = client

    def get_collection(self, collection_name: str):
        info = self.client.get_collection(collection_name)
        kinds = {
            "document_id": models.PayloadSchemaType.KEYWORD,
            "collection_key": models.PayloadSchemaType.KEYWORD,
            "prepared_revision_id": models.PayloadSchemaType.KEYWORD,
            "schema_version": models.PayloadSchemaType.INTEGER,
            "published": models.PayloadSchemaType.BOOL,
            "visibility": models.PayloadSchemaType.KEYWORD,
        }
        payload_schema = {
            name: models.PayloadIndexInfo(data_type=kind, points=0)
            for name, kind in kinds.items()
        }
        return info.model_copy(update={"payload_schema": payload_schema})

    def get_collection_aliases(self, collection_name: str):
        return self.client.get_collection_aliases(collection_name)


def prepared_points() -> list[ChunkPoint]:
    document_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
    revision_id = uuid.UUID("22222222-2222-2222-2222-222222222222")
    points: list[ChunkPoint] = []
    for index in range(2):
        points.append(
            ChunkPoint(
                chunk_id=uuid.uuid5(document_id, f"{revision_id}:{index}"),
                document_id=document_id,
                prepared_revision_id=revision_id,
                collection_key="customer",
                active_qdrant_collection=ACTIVE,
                chunk_index=index,
                page_start=index + 1,
                page_end=index + 1,
                heading_path=("Guide",),
                text_sha256=f"{index:064x}",
                markdown=f"## Guide\n\nChunk {index}",
                content_profile_id="sha256:" + "a" * 64,
                index_profile_id="sha256:" + "b" * 64,
                dense=(1.0, *([0.0] * 767)),
                sparse=SparseVector(indices=(10 + index,), values=(1.0,)),
            )
        )
    return points


def screening_points() -> tuple[QdrantClient, list[ChunkPoint]]:
    client = QdrantClient(":memory:")
    provision(client, SCREENING)
    points = prepared_points()
    upsert_screening_points(client, screening_collection=SCREENING, points=points)
    return client, points


def test_fixed_schema_validation_and_gated_publication_deletion() -> None:
    client = QdrantClient(":memory:")
    provision(client, ACTIVE)
    provision(client, SCREENING)
    points = prepared_points()

    validate_fixed_collections(
        SchemaView(client),  # type: ignore[arg-type]
        active_collections=[ACTIVE],
        screening_collection=SCREENING,
    )
    upsert_screening_points(client, screening_collection=SCREENING, points=points)
    verify_prepared_points(
        client,
        collection=SCREENING,
        points=points,
        published=False,
        visibility="screening",
    )

    stage_active_points(client, active_collection=ACTIVE, points=points)
    verify_prepared_points(
        client,
        collection=ACTIVE,
        points=points,
        published=False,
        visibility="publishing",
    )
    activate_prepared_points(
        client,
        collection=ACTIVE,
        document_id=points[0].document_id,
        prepared_revision_id=points[0].prepared_revision_id,
    )
    verify_prepared_points(
        client,
        collection=ACTIVE,
        points=points,
        published=True,
        visibility="active",
    )

    delete_document_points(
        client,
        collection=ACTIVE,
        document_id=points[0].document_id,
    )
    verify_document_zero(
        client,
        collection=ACTIVE,
        document_id=points[0].document_id,
    )


def test_schema_drift_is_reported_and_never_repaired() -> None:
    client = QdrantClient(":memory:")
    provision(client, ACTIVE, dimension=3)
    provision(client, SCREENING)

    with pytest.raises(VectorIndexSchemaError, match="size must be 768"):
        validate_fixed_collections(
            SchemaView(client),  # type: ignore[arg-type]
            active_collections=[ACTIVE],
            screening_collection=SCREENING,
        )

    assert client.get_collection(ACTIVE).config.params.vectors["dense"].size == 3


def test_duplicate_fixed_names_fail_before_any_describe_call() -> None:
    class DescribeSpy:
        calls = 0

        def get_collection(self, collection_name: str):
            self.calls += 1
            raise AssertionError("duplicate validation must happen first")

    spy = DescribeSpy()
    with pytest.raises(VectorIndexSchemaError, match="must be unique"):
        validate_fixed_collections(
            spy,  # type: ignore[arg-type]
            active_collections=[ACTIVE],
            screening_collection=ACTIVE,
        )
    assert spy.calls == 0


def test_fixed_schema_readiness_rejects_alias_topology() -> None:
    client = QdrantClient(":memory:")
    provision(client, ACTIVE)
    provision(client, SCREENING)
    client.update_collection_aliases(
        change_aliases_operations=[
            models.CreateAliasOperation(
                create_alias=models.CreateAlias(
                    collection_name=ACTIVE,
                    alias_name="mutable-customer-alias",
                )
            )
        ]
    )

    with pytest.raises(VectorIndexSchemaError, match="must not participate in aliases"):
        validate_fixed_collections(
            SchemaView(client),  # type: ignore[arg-type]
            active_collections=[ACTIVE],
            screening_collection=SCREENING,
        )


@pytest.mark.parametrize(
    ("field", "tampered_value"),
    [
        ("schema_version", 3),
        ("document_id", "33333333-3333-3333-3333-333333333333"),
        ("prepared_revision_id", "44444444-4444-4444-4444-444444444444"),
        ("collection_key", "another-customer"),
        ("active_qdrant_collection", "another-physical-target"),
        ("chunk_id", "55555555-5555-5555-5555-555555555555"),
        ("chunk_index", 99),
        ("page_start", 99),
        ("page_end", 99),
        ("heading_path", ["Tampered"]),
        ("text_sha256", "f" * 64),
        ("markdown", "tampered Markdown"),
        ("content_profile_id", "sha256:" + "c" * 64),
        ("index_profile_id", "sha256:" + "d" * 64),
        ("published", 0),
        ("visibility", "active"),
        ("unexpected_payload_field", "not allowed"),
    ],
)
def test_verification_rejects_any_payload_tamper(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    tampered_value: object,
) -> None:
    client, points = screening_points()
    retrieve = client.retrieve

    def retrieve_with_tamper(*args: object, **kwargs: object):
        records = retrieve(*args, **kwargs)
        payload = {**records[0].payload, field: tampered_value}
        records[0] = records[0].model_copy(update={"payload": payload})
        return records

    monkeypatch.setattr(client, "retrieve", retrieve_with_tamper)

    with pytest.raises(VectorIndexConsistencyError, match="payload does not exactly match"):
        verify_prepared_points(
            client,
            collection=SCREENING,
            points=points,
            published=False,
            visibility="screening",
        )


@pytest.mark.parametrize(
    "fault",
    [
        "dense_value",
        "dense_dimension",
        "sparse_index",
        "sparse_value",
        "missing_vector",
        "extra_vector",
    ],
)
def test_verification_rejects_vector_tamper(
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    client, points = screening_points()
    retrieve = client.retrieve

    def retrieve_with_tamper(*args: object, **kwargs: object):
        records = retrieve(*args, **kwargs)
        vectors = dict(records[0].vector)
        if fault == "dense_value":
            dense = list(vectors["dense"])
            dense[0] = 0.5
            vectors["dense"] = dense
        elif fault == "dense_dimension":
            vectors["dense"] = list(vectors["dense"][:-1])
        elif fault == "sparse_index":
            sparse = vectors["bm25"]
            vectors["bm25"] = sparse.model_copy(update={"indices": [999]})
        elif fault == "sparse_value":
            sparse = vectors["bm25"]
            vectors["bm25"] = sparse.model_copy(update={"values": [0.5]})
        elif fault == "missing_vector":
            del vectors["bm25"]
        elif fault == "extra_vector":
            vectors["unexpected"] = [1.0]
        else:  # pragma: no cover - the parameter list is closed above
            raise AssertionError(f"unknown fault {fault}")
        records[0] = records[0].model_copy(update={"vector": vectors})
        return records

    monkeypatch.setattr(client, "retrieve", retrieve_with_tamper)

    with pytest.raises(VectorIndexConsistencyError, match="vectors do not exactly match"):
        verify_prepared_points(
            client,
            collection=SCREENING,
            points=points,
            published=False,
            visibility="screening",
        )


def test_verification_rejects_stale_document_points() -> None:
    client, points = screening_points()
    stale = replace(
        points[0],
        chunk_id=uuid.uuid4(),
        prepared_revision_id=uuid.uuid4(),
    )
    upsert_screening_points(client, screening_collection=SCREENING, points=[stale])

    with pytest.raises(
        VectorIndexConsistencyError, match="expected 2 document points.*found 3"
    ):
        verify_prepared_points(
            client,
            collection=SCREENING,
            points=points,
            published=False,
            visibility="screening",
        )
