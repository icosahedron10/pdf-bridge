"""Point-only Qdrant access for fixed, platform-owned collections.

This module deliberately contains no collection, alias, or payload-index
mutation.  Readiness describes and validates the platform-provisioned schema;
runtime code can only search, count, retrieve, upsert, update point payloads,
and delete points.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from qdrant_client import models

from pdf_bridge.services.local_embeddings import SparseVector

INDEX_SCHEMA_VERSION = 2
DENSE_VECTOR_NAME = "dense"
SPARSE_VECTOR_NAME = "bm25"
DENSE_DIMENSION = 768

REQUIRED_PAYLOAD_INDEXES = {
    "document_id": models.PayloadSchemaType.KEYWORD,
    "collection_key": models.PayloadSchemaType.KEYWORD,
    "prepared_revision_id": models.PayloadSchemaType.KEYWORD,
    "schema_version": models.PayloadSchemaType.INTEGER,
    "published": models.PayloadSchemaType.BOOL,
    "visibility": models.PayloadSchemaType.KEYWORD,
}


class VectorIndexError(RuntimeError):
    """Base class for a Qdrant failure that can never be treated as empty data."""


class VectorIndexUnavailableError(VectorIndexError):
    """The configured Qdrant service did not complete an operation."""


class VectorIndexSchemaError(VectorIndexError):
    """A fixed collection exists with a schema outside the target contract."""


class VectorIndexConsistencyError(VectorIndexError):
    """Persisted points do not exactly match the prepared revision."""


class QdrantPointClient(Protocol):
    """Methods Bridge credentials are allowed to use."""

    def get_collection(self, collection_name: str, **kwargs: Any) -> Any: ...

    def get_collection_aliases(self, collection_name: str, **kwargs: Any) -> Any: ...

    def upsert(self, collection_name: str, points: Any, **kwargs: Any) -> Any: ...

    def count(self, collection_name: str, **kwargs: Any) -> Any: ...

    def retrieve(self, collection_name: str, ids: Any, **kwargs: Any) -> Any: ...

    def set_payload(
        self, collection_name: str, payload: Any, points: Any, **kwargs: Any
    ) -> Any: ...

    def delete(self, collection_name: str, points_selector: Any, **kwargs: Any) -> Any: ...

    def query_points(self, collection_name: str, **kwargs: Any) -> Any: ...


@dataclass(frozen=True, slots=True)
class ChunkPoint:
    """One immutable prepared chunk with both target vector encodings."""

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    prepared_revision_id: uuid.UUID
    collection_key: str
    active_qdrant_collection: str
    chunk_index: int
    page_start: int
    page_end: int
    heading_path: tuple[str, ...]
    text_sha256: str
    markdown: str
    content_profile_id: str
    index_profile_id: str
    dense: tuple[float, ...]
    sparse: SparseVector


@dataclass(frozen=True, slots=True)
class CandidateHit:
    """Bounded point correlation returned by one candidate query."""

    document_id: uuid.UUID
    prepared_revision_id: uuid.UUID
    chunk_id: uuid.UUID
    score: float
    rank: int
    source: str


def _unavailable(action: str, exc: Exception) -> VectorIndexUnavailableError:
    return VectorIndexUnavailableError(f"Qdrant could not {action}")


def _schema_value(value: Any) -> Any:
    return getattr(value, "value", value)


def validate_collection_schema(client: QdrantPointClient, collection_name: str) -> None:
    """Describe one fixed collection and reject every material schema drift."""

    try:
        info = client.get_collection(collection_name)
        aliases_response = client.get_collection_aliases(collection_name)
    except Exception as exc:
        raise _unavailable(f"describe fixed collection {collection_name!r}", exc) from exc

    issues: list[str] = []
    aliases = getattr(aliases_response, "aliases", None)
    if not isinstance(aliases, list):
        issues.append("collection aliases could not be attested")
    elif aliases:
        issues.append("fixed physical collections must not participate in aliases")
    status = _schema_value(getattr(info, "status", None))
    if status != _schema_value(models.CollectionStatus.GREEN):
        issues.append(f"status is {status!r}, expected green")

    params = getattr(getattr(info, "config", None), "params", None)
    vectors = getattr(params, "vectors", None)
    if not isinstance(vectors, dict) or set(vectors) != {DENSE_VECTOR_NAME}:
        issues.append("named dense vectors must contain only 'dense'")
    else:
        dense = vectors[DENSE_VECTOR_NAME]
        if getattr(dense, "size", None) != DENSE_DIMENSION:
            issues.append("dense vector size must be 768")
        if _schema_value(getattr(dense, "distance", None)) != _schema_value(
            models.Distance.COSINE
        ):
            issues.append("dense vector distance must be Cosine")

    sparse_vectors = getattr(params, "sparse_vectors", None)
    if not isinstance(sparse_vectors, dict) or set(sparse_vectors) != {SPARSE_VECTOR_NAME}:
        issues.append("named sparse vectors must contain only 'bm25'")
    else:
        sparse = sparse_vectors[SPARSE_VECTOR_NAME]
        if getattr(sparse, "index", None) is None:
            issues.append("bm25 sparse indexing must be enabled")
        if _schema_value(getattr(sparse, "modifier", None)) != _schema_value(
            models.Modifier.IDF
        ):
            issues.append("bm25 sparse modifier must be IDF")

    payload_schema = getattr(info, "payload_schema", {})
    for name, expected in REQUIRED_PAYLOAD_INDEXES.items():
        actual = payload_schema.get(name) if isinstance(payload_schema, dict) else None
        actual_type = _schema_value(getattr(actual, "data_type", None))
        if actual_type != _schema_value(expected):
            issues.append(f"payload index {name!r} must use {expected.value}")

    if issues:
        joined = "; ".join(issues)
        raise VectorIndexSchemaError(f"fixed collection {collection_name!r}: {joined}")


def validate_fixed_collections(
    client: QdrantPointClient,
    *,
    active_collections: list[str],
    screening_collection: str,
) -> None:
    """Validate every distinct configured collection without mutating topology."""

    names = [*active_collections, screening_collection]
    if any(not name.strip() for name in names):
        raise VectorIndexSchemaError("fixed collection names cannot be blank")
    if len(set(names)) != len(names):
        raise VectorIndexSchemaError("active and screening collection names must be unique")
    for name in names:
        validate_collection_schema(client, name)


def _document_filter(
    document_id: uuid.UUID,
    *,
    prepared_revision_id: uuid.UUID | None = None,
    collection_key: str | None = None,
    published: bool | None = None,
    visibility: str | None = None,
    exclude_revision_id: uuid.UUID | None = None,
) -> models.Filter:
    must: list[models.Condition] = [
        models.FieldCondition(
            key="document_id", match=models.MatchValue(value=str(document_id))
        )
    ]
    if prepared_revision_id is not None:
        must.append(
            models.FieldCondition(
                key="prepared_revision_id",
                match=models.MatchValue(value=str(prepared_revision_id)),
            )
        )
    if collection_key is not None:
        must.append(
            models.FieldCondition(
                key="collection_key", match=models.MatchValue(value=collection_key)
            )
        )
    if published is not None:
        must.append(
            models.FieldCondition(key="published", match=models.MatchValue(value=published))
        )
    if visibility is not None:
        must.append(
            models.FieldCondition(
                key="visibility", match=models.MatchValue(value=visibility)
            )
        )
    must_not: list[models.Condition] = []
    if exclude_revision_id is not None:
        must_not.append(
            models.FieldCondition(
                key="prepared_revision_id",
                match=models.MatchValue(value=str(exclude_revision_id)),
            )
        )
    return models.Filter(must=must, must_not=must_not or None)


def collection_filter(
    collection_key: str,
    *,
    published: bool,
    visibility: str,
    exclude_revision_id: uuid.UUID | None = None,
) -> models.Filter:
    """Build the mandatory logical-collection candidate boundary."""

    must: list[models.Condition] = [
        models.FieldCondition(
            key="collection_key", match=models.MatchValue(value=collection_key)
        ),
        models.FieldCondition(key="published", match=models.MatchValue(value=published)),
        models.FieldCondition(
            key="visibility", match=models.MatchValue(value=visibility)
        ),
        models.FieldCondition(
            key="schema_version", match=models.MatchValue(value=INDEX_SCHEMA_VERSION)
        ),
    ]
    must_not: list[models.Condition] = []
    if exclude_revision_id is not None:
        must_not.append(
            models.FieldCondition(
                key="prepared_revision_id",
                match=models.MatchValue(value=str(exclude_revision_id)),
            )
        )
    return models.Filter(must=must, must_not=must_not or None)


def _validate_point(point: ChunkPoint) -> None:
    if len(point.dense) != DENSE_DIMENSION or any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        for value in point.dense
    ):
        raise VectorIndexConsistencyError(
            "prepared dense vector must be finite and 768-dimensional"
        )
    if (
        not point.sparse.indices
        or len(point.sparse.indices) != len(point.sparse.values)
        or len(set(point.sparse.indices)) != len(point.sparse.indices)
        or any(
            isinstance(index, bool) or not isinstance(index, int) or index < 0
            for index in point.sparse.indices
        )
        or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            for value in point.sparse.values
        )
    ):
        raise VectorIndexConsistencyError("prepared BM25 vector is empty or uncorrelated")
    if not point.markdown.strip():
        raise VectorIndexConsistencyError("prepared point Markdown cannot be empty")


def _point_payload(
    point: ChunkPoint,
    *,
    published: bool,
    visibility: str,
) -> dict[str, Any]:
    return {
        "schema_version": INDEX_SCHEMA_VERSION,
        "document_id": str(point.document_id),
        "prepared_revision_id": str(point.prepared_revision_id),
        "collection_key": point.collection_key,
        "active_qdrant_collection": point.active_qdrant_collection,
        "chunk_id": str(point.chunk_id),
        "chunk_index": point.chunk_index,
        "page_start": point.page_start,
        "page_end": point.page_end,
        "heading_path": list(point.heading_path),
        "text_sha256": point.text_sha256,
        "markdown": point.markdown,
        "content_profile_id": point.content_profile_id,
        "index_profile_id": point.index_profile_id,
        "published": published,
        "visibility": visibility,
    }


def _point_struct(
    point: ChunkPoint,
    *,
    published: bool,
    visibility: str,
) -> models.PointStruct:
    _validate_point(point)
    return models.PointStruct(
        id=point.chunk_id,
        vector={
            DENSE_VECTOR_NAME: list(point.dense),
            SPARSE_VECTOR_NAME: models.SparseVector(
                indices=list(point.sparse.indices),
                values=list(point.sparse.values),
            ),
        },
        payload=_point_payload(point, published=published, visibility=visibility),
    )


def _exact_payload_matches(actual: object, expected: object) -> bool:
    """Compare JSON payload values without Python's bool/int equivalence."""

    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return set(actual) == set(expected) and all(
            _exact_payload_matches(actual[key], value) for key, value in expected.items()
        )
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(
            _exact_payload_matches(actual_value, expected_value)
            for actual_value, expected_value in zip(actual, expected, strict=True)
        )
    return actual == expected


def _finite_numeric_sequence(value: object, *, length: int) -> tuple[float, ...] | None:
    if not isinstance(value, list) or len(value) != length:
        return None
    if any(
        isinstance(item, bool)
        or not isinstance(item, (int, float))
        or not math.isfinite(item)
        for item in value
    ):
        return None
    return tuple(float(item) for item in value)


def _vectors_match(actual: object, expected: ChunkPoint) -> bool:
    if not isinstance(actual, dict) or set(actual) != {
        DENSE_VECTOR_NAME,
        SPARSE_VECTOR_NAME,
    }:
        return False

    dense = _finite_numeric_sequence(actual[DENSE_VECTOR_NAME], length=DENSE_DIMENSION)
    if dense is None or dense != tuple(float(value) for value in expected.dense):
        return False

    sparse = actual[SPARSE_VECTOR_NAME]
    indices = getattr(sparse, "indices", None)
    values = getattr(sparse, "values", None)
    if not isinstance(indices, list) or any(
        isinstance(index, bool) or not isinstance(index, int) or index < 0
        for index in indices
    ):
        return False
    sparse_values = _finite_numeric_sequence(values, length=len(expected.sparse.values))
    return (
        tuple(indices) == expected.sparse.indices
        and sparse_values is not None
        and sparse_values == tuple(float(value) for value in expected.sparse.values)
    )


def upsert_screening_points(
    client: QdrantPointClient,
    *,
    screening_collection: str,
    points: list[ChunkPoint],
) -> None:
    """Upsert one complete prepared revision as private screening points."""

    if not points:
        raise VectorIndexConsistencyError("a prepared revision must contain at least one point")
    try:
        client.upsert(
            screening_collection,
            points=[
                _point_struct(point, published=False, visibility="screening")
                for point in points
            ],
            wait=True,
        )
    except VectorIndexError:
        raise
    except Exception as exc:
        raise _unavailable("upsert screening points", exc) from exc


def stage_active_points(
    client: QdrantPointClient,
    *,
    active_collection: str,
    points: list[ChunkPoint],
) -> None:
    """Write a complete revision behind a non-retrievable publication gate."""

    if not points:
        raise VectorIndexConsistencyError("publication cannot stage an empty revision")
    try:
        client.upsert(
            active_collection,
            points=[
                _point_struct(point, published=False, visibility="publishing")
                for point in points
            ],
            wait=True,
        )
    except VectorIndexError:
        raise
    except Exception as exc:
        raise _unavailable("stage active points", exc) from exc


def count_document_points(
    client: QdrantPointClient,
    *,
    collection: str,
    document_id: uuid.UUID,
    prepared_revision_id: uuid.UUID | None = None,
) -> int:
    """Return an exact point count or fail; outages are never interpreted as zero."""

    try:
        result = client.count(
            collection,
            count_filter=_document_filter(
                document_id, prepared_revision_id=prepared_revision_id
            ),
            exact=True,
        )
    except Exception as exc:
        raise _unavailable("count document points", exc) from exc
    count = getattr(result, "count", None)
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise VectorIndexConsistencyError("Qdrant returned an invalid exact point count")
    return count


def verify_prepared_points(
    client: QdrantPointClient,
    *,
    collection: str,
    points: list[ChunkPoint],
    published: bool,
    visibility: str,
) -> None:
    """Verify every stored point is an exact copy of its prepared chunk."""

    if not points:
        raise VectorIndexConsistencyError("cannot verify an empty prepared revision")
    document_id = points[0].document_id
    revision_id = points[0].prepared_revision_id
    if any(
        point.document_id != document_id or point.prepared_revision_id != revision_id
        for point in points
    ):
        raise VectorIndexConsistencyError("point batch mixes document or revision identities")
    expected_by_id: dict[str, ChunkPoint] = {}
    for point in points:
        _validate_point(point)
        point_id = str(point.chunk_id)
        if point_id in expected_by_id:
            raise VectorIndexConsistencyError("point batch contains a duplicate chunk identity")
        expected_by_id[point_id] = point
    total = count_document_points(
        client, collection=collection, document_id=document_id
    )
    if total != len(points):
        raise VectorIndexConsistencyError(
            f"expected {len(points)} document points in {collection!r}, found {total}"
        )
    try:
        records = client.retrieve(
            collection,
            ids=[point.chunk_id for point in points],
            with_payload=True,
            with_vectors=[DENSE_VECTOR_NAME, SPARSE_VECTOR_NAME],
        )
    except Exception as exc:
        raise _unavailable("retrieve points for verification", exc) from exc
    if not isinstance(records, list) or len(records) != len(expected_by_id):
        raise VectorIndexConsistencyError("Qdrant did not return every deterministic point ID")
    seen_ids: set[str] = set()
    for record in records:
        record_id = str(getattr(record, "id", ""))
        payload = getattr(record, "payload", None)
        vectors = getattr(record, "vector", None)
        expected = expected_by_id.get(record_id)
        if expected is None or record_id in seen_ids:
            raise VectorIndexConsistencyError("Qdrant returned a foreign or duplicate point")
        seen_ids.add(record_id)
        expected_payload = _point_payload(
            expected,
            published=published,
            visibility=visibility,
        )
        if not _exact_payload_matches(payload, expected_payload):
            raise VectorIndexConsistencyError(
                "Qdrant point payload does not exactly match its prepared chunk"
            )
        if not _vectors_match(vectors, expected):
            raise VectorIndexConsistencyError(
                "Qdrant point vectors do not exactly match their prepared values"
            )
    if seen_ids != set(expected_by_id):
        raise VectorIndexConsistencyError("Qdrant point identities were incomplete")


def activate_prepared_points(
    client: QdrantPointClient,
    *,
    collection: str,
    document_id: uuid.UUID,
    prepared_revision_id: uuid.UUID,
) -> None:
    """Open the publication visibility gate only for the verified revision."""

    try:
        client.set_payload(
            collection,
            payload={"published": True, "visibility": "active"},
            points=_document_filter(
                document_id,
                prepared_revision_id=prepared_revision_id,
                published=False,
                visibility="publishing",
            ),
            wait=True,
        )
    except Exception as exc:
        raise _unavailable("activate prepared points", exc) from exc


def delete_document_points(
    client: QdrantPointClient,
    *,
    collection: str,
    document_id: uuid.UUID,
) -> None:
    """Submit a point-filter delete with wait-for-apply."""

    try:
        client.delete(
            collection,
            points_selector=_document_filter(document_id),
            wait=True,
        )
    except Exception as exc:
        raise _unavailable("delete document points", exc) from exc


def verify_document_zero(
    client: QdrantPointClient,
    *,
    collection: str,
    document_id: uuid.UUID,
) -> None:
    """Require an exact durable zero before storage cleanup may proceed."""

    count = count_document_points(
        client, collection=collection, document_id=document_id
    )
    if count != 0:
        raise VectorIndexConsistencyError(
            f"document {document_id} still has {count} points in {collection!r}"
        )


def query_candidates(
    client: QdrantPointClient,
    *,
    collection: str,
    query: tuple[float, ...] | SparseVector,
    using: str,
    collection_key: str,
    published: bool,
    visibility: str,
    source: str,
    exclude_revision_id: uuid.UUID,
    limit: int,
) -> list[CandidateHit]:
    """Query one vector family inside the mandatory logical visibility boundary."""

    if using not in {DENSE_VECTOR_NAME, SPARSE_VECTOR_NAME}:
        raise ValueError("candidate queries must use the dense or bm25 vector")
    if not 1 <= limit <= 100:
        raise ValueError("candidate query limit must be between 1 and 100")
    vector: list[float] | models.SparseVector
    if isinstance(query, SparseVector):
        vector = models.SparseVector(indices=list(query.indices), values=list(query.values))
    else:
        vector = list(query)
    try:
        response = client.query_points(
            collection,
            query=vector,
            using=using,
            query_filter=collection_filter(
                collection_key,
                published=published,
                visibility=visibility,
                exclude_revision_id=exclude_revision_id,
            ),
            limit=limit,
            with_payload=["document_id", "prepared_revision_id", "chunk_id"],
            with_vectors=False,
        )
    except Exception as exc:
        raise _unavailable("query candidate points", exc) from exc
    hits: list[CandidateHit] = []
    for rank, point in enumerate(getattr(response, "points", []), start=1):
        payload = getattr(point, "payload", {})
        score = getattr(point, "score", None)
        try:
            document_id = uuid.UUID(str(payload["document_id"]))
            revision_id = uuid.UUID(str(payload["prepared_revision_id"]))
            chunk_id = uuid.UUID(str(payload["chunk_id"]))
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise VectorIndexConsistencyError(
                "candidate point has invalid identity payload"
            ) from exc
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(score)
        ):
            raise VectorIndexConsistencyError("candidate point has an invalid score")
        hits.append(
            CandidateHit(
                document_id=document_id,
                prepared_revision_id=revision_id,
                chunk_id=chunk_id,
                score=float(score),
                rank=rank,
                source=source,
            )
        )
    return hits
