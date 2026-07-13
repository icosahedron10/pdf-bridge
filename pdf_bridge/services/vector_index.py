"""Qdrant collection layout, deterministic point construction, and queries.

PDF Bridge owns two kinds of collections on the pinned Qdrant 1.18 server:

- one versioned active physical collection per logical collection
  (``pdf-bridge-{key}-v{epoch}``), exposed to the external retrieval service
  through the stable ``collection_key`` alias; and
- one private screening collection holding non-retrievable analysis vectors
  for pending documents.

Every point carries named ``content_dense`` and ``content_bm25`` vectors and
a strict payload schema. Point IDs are deterministic UUIDv5 values so retries
are idempotent. Callers must treat ``VectorIndexUnavailableError`` as a
retryable outage, never as an empty result.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass

from qdrant_client import QdrantClient, models

from pdf_bridge.services.bm25 import SparseVectorData
from pdf_bridge.services.candidates import CandidateSource, ChunkHit

INDEX_SCHEMA_VERSION = 1
SCREENING_COLLECTION = "pdf-bridge-screening-v1"
DENSE_VECTOR_NAME = "content_dense"
SPARSE_VECTOR_NAME = "content_bm25"
MAX_POINT_TEXT_CHARS = 3_500

_POINT_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://pdf-bridge.internal/points/v1")


class VectorIndexError(RuntimeError):
    """A Qdrant operation failed in a way that must not be treated as success."""


class VectorIndexUnavailableError(VectorIndexError):
    """Qdrant could not be reached or refused the operation; retry later."""


def physical_collection_name(collection_key: str, epoch: int) -> str:
    """Name the versioned physical collection behind a stable alias."""

    return f"pdf-bridge-{collection_key}-v{epoch}"


def point_id(document_id: uuid.UUID, analysis_id: uuid.UUID, chunk_index: int) -> str:
    """Derive the deterministic UUIDv5 point ID for one chunk."""

    return str(uuid.uuid5(_POINT_NAMESPACE, f"{document_id}:{analysis_id}:{chunk_index}"))


@dataclass(frozen=True, slots=True)
class ChunkPoint:
    """Everything needed to write one chunk point to either collection."""

    document_id: uuid.UUID
    analysis_id: uuid.UUID
    chunk_index: int
    collection_key: str
    page_start: int
    page_end: int
    text_hash: str
    text: str
    dense: tuple[float, ...]
    sparse: SparseVectorData


def _wrap(exc: Exception) -> VectorIndexUnavailableError:
    return VectorIndexUnavailableError(f"Qdrant operation failed: {exc}")


def _point_struct(point: ChunkPoint, *, published: bool, screening: bool) -> models.PointStruct:
    return models.PointStruct(
        id=point_id(point.document_id, point.analysis_id, point.chunk_index),
        vector={
            DENSE_VECTOR_NAME: list(point.dense),
            SPARSE_VECTOR_NAME: models.SparseVector(
                indices=list(point.sparse.indices),
                values=list(point.sparse.values),
            ),
        },
        payload={
            "schema_version": INDEX_SCHEMA_VERSION,
            "document_id": str(point.document_id),
            "analysis_id": str(point.analysis_id),
            "chunk_id": point_id(point.document_id, point.analysis_id, point.chunk_index),
            "chunk_index": point.chunk_index,
            "collection_key": point.collection_key,
            "page_start": point.page_start,
            "page_end": point.page_end,
            "text_hash": point.text_hash,
            "text": point.text[:MAX_POINT_TEXT_CHARS],
            "published": published,
            "screening": screening,
        },
    )


def _document_filter(
    document_id: uuid.UUID,
    *,
    published: bool | None = None,
    screening: bool | None = None,
    schema_version: int | None = None,
) -> models.Filter:
    must: list[models.Condition] = [
        models.FieldCondition(key="document_id", match=models.MatchValue(value=str(document_id)))
    ]
    if published is not None:
        must.append(
            models.FieldCondition(key="published", match=models.MatchValue(value=published))
        )
    if screening is not None:
        must.append(
            models.FieldCondition(key="screening", match=models.MatchValue(value=screening))
        )
    if schema_version is not None:
        must.append(
            models.FieldCondition(
                key="schema_version", match=models.MatchValue(value=schema_version)
            )
        )
    return models.Filter(must=must)


def _ensure_collection(client: QdrantClient, name: str, dimension: int) -> None:
    try:
        if not client.collection_exists(name):
            client.create_collection(
                name,
                vectors_config={
                    DENSE_VECTOR_NAME: models.VectorParams(
                        size=dimension, distance=models.Distance.COSINE
                    )
                },
                sparse_vectors_config={
                    SPARSE_VECTOR_NAME: models.SparseVectorParams(modifier=models.Modifier.IDF)
                },
            )
            for field, schema in (
                ("document_id", models.PayloadSchemaType.KEYWORD),
                ("collection_key", models.PayloadSchemaType.KEYWORD),
                ("published", models.PayloadSchemaType.BOOL),
                ("schema_version", models.PayloadSchemaType.INTEGER),
            ):
                client.create_payload_index(name, field_name=field, field_schema=schema)
    except VectorIndexError:
        raise
    except Exception as exc:
        raise _wrap(exc) from exc


def ensure_screening_collection(client: QdrantClient, *, dimension: int) -> None:
    """Create the private screening collection if it does not exist."""

    _ensure_collection(client, SCREENING_COLLECTION, dimension)


def ensure_active_collection(
    client: QdrantClient, *, collection_key: str, epoch: int, dimension: int
) -> str:
    """Create the epoch's physical collection and point the stable alias at it."""

    name = physical_collection_name(collection_key, epoch)
    _ensure_collection(client, name, dimension)
    try:
        client.update_collection_aliases(
            change_aliases_operations=[
                models.DeleteAliasOperation(
                    delete_alias=models.DeleteAlias(alias_name=collection_key)
                ),
                models.CreateAliasOperation(
                    create_alias=models.CreateAlias(collection_name=name, alias_name=collection_key)
                ),
            ]
        )
    except Exception:
        # The alias may not exist yet; create-only is the common first path.
        try:
            client.update_collection_aliases(
                change_aliases_operations=[
                    models.CreateAliasOperation(
                        create_alias=models.CreateAlias(
                            collection_name=name, alias_name=collection_key
                        )
                    )
                ]
            )
        except Exception as exc:
            raise _wrap(exc) from exc
    return name


def upsert_chunk_points(
    client: QdrantClient,
    collection: str,
    points: list[ChunkPoint],
    *,
    published: bool,
    screening: bool | None = None,
) -> None:
    """Write chunk points with ``wait=true`` so success means durably applied."""

    if not points:
        return
    is_screening = not published if screening is None else screening
    structs = [
        _point_struct(point, published=published, screening=is_screening) for point in points
    ]
    try:
        client.upsert(
            collection,
            points=structs,
            wait=True,
            ordering=models.WriteOrdering.STRONG,
        )
    except Exception as exc:
        raise _wrap(exc) from exc


def count_document_points(
    client: QdrantClient,
    collection: str,
    document_id: uuid.UUID,
    *,
    published: bool | None = None,
    screening: bool | None = None,
    schema_version: int | None = None,
) -> int:
    """Exactly count a document's points in one collection."""

    try:
        return client.count(
            collection,
            count_filter=_document_filter(
                document_id,
                published=published,
                screening=screening,
                schema_version=schema_version,
            ),
            exact=True,
        ).count
    except Exception as exc:
        raise _wrap(exc) from exc


def verify_document_point_count(
    client: QdrantClient,
    collection: str,
    document_id: uuid.UUID,
    *,
    expected: int,
    published: bool | None = None,
    screening: bool | None = None,
    schema_version: int | None = None,
) -> None:
    """Fail loudly when the exact point count does not match expectations."""

    observed = count_document_points(
        client,
        collection,
        document_id,
        published=published,
        screening=screening,
        schema_version=schema_version,
    )
    if observed != expected:
        raise VectorIndexError(
            f"collection {collection!r} holds {observed} points for document "
            f"{document_id}, expected exactly {expected}"
        )


def delete_document_points(client: QdrantClient, collection: str, document_id: uuid.UUID) -> None:
    """Delete every point of a document and verify none remain."""

    try:
        client.delete(
            collection,
            points_selector=models.FilterSelector(filter=_document_filter(document_id)),
            wait=True,
            ordering=models.WriteOrdering.STRONG,
        )
    except Exception as exc:
        raise _wrap(exc) from exc
    verify_document_point_count(client, collection, document_id, expected=0)


def delete_document_points_if_collection_exists(
    client: QdrantClient, collection: str, document_id: uuid.UUID
) -> None:
    """Delete and verify points when a historical collection still exists.

    Old physical epochs can be retired independently of SQL outbox history.
    Their absence is therefore an already-clean result, while every provider
    error and every non-zero post-delete count remains a hard failure.
    """

    try:
        exists = client.collection_exists(collection)
    except Exception as exc:
        raise _wrap(exc) from exc
    if not exists:
        return
    try:
        delete_document_points(client, collection, document_id)
    except VectorIndexError:
        # A retired epoch can disappear between the existence check and the
        # delete. Confirm that race explicitly; never turn a live-collection
        # provider failure into apparent success.
        try:
            still_exists = client.collection_exists(collection)
        except Exception as exc:
            raise _wrap(exc) from exc
        if not still_exists:
            return
        raise


def publish_document_points(
    client: QdrantClient,
    collection: str,
    document_id: uuid.UUID,
    *,
    expected: int,
) -> None:
    """Atomically expose a fully prepared document and verify every point.

    Active points are first written with ``published=false``. Publication is a
    distinct durable outbox step, and SQL closes the intake row only after the
    visibility flip is verified. A crash can delay availability but can never
    expose a pending document.
    """

    try:
        client.set_payload(
            collection,
            payload={"published": True, "screening": False},
            points=models.FilterSelector(filter=_document_filter(document_id)),
            wait=True,
            ordering=models.WriteOrdering.STRONG,
        )
    except Exception as exc:
        raise _wrap(exc) from exc
    verify_document_point_count(
        client,
        collection,
        document_id,
        expected=expected,
        published=True,
        screening=False,
        schema_version=INDEX_SCHEMA_VERSION,
    )


def query_dense(
    client: QdrantClient,
    collection: str,
    *,
    vector: list[float],
    top_k: int,
    exclude_document_id: uuid.UUID | None = None,
    collection_key: str | None = None,
) -> list[ChunkHit]:
    """Rank stored chunks by cosine similarity for one incoming chunk."""

    return _query(
        client,
        collection,
        query=vector,
        using=DENSE_VECTOR_NAME,
        top_k=top_k,
        exclude_document_id=exclude_document_id,
        collection_key=collection_key,
    )


def query_bm25(
    client: QdrantClient,
    collection: str,
    *,
    sparse: SparseVectorData,
    top_k: int,
    exclude_document_id: uuid.UUID | None = None,
    collection_key: str | None = None,
) -> list[ChunkHit]:
    """Rank stored chunks by BM25 for one incoming chunk."""

    if not sparse.indices:
        return []
    return _query(
        client,
        collection,
        query=models.SparseVector(indices=list(sparse.indices), values=list(sparse.values)),
        using=SPARSE_VECTOR_NAME,
        top_k=top_k,
        exclude_document_id=exclude_document_id,
        collection_key=collection_key,
    )


def _query(
    client: QdrantClient,
    collection: str,
    *,
    query: object,
    using: str,
    top_k: int,
    exclude_document_id: uuid.UUID | None,
    collection_key: str | None,
) -> list[ChunkHit]:
    must: list[models.Condition] = [
        models.FieldCondition(
            key="schema_version", match=models.MatchValue(value=INDEX_SCHEMA_VERSION)
        )
    ]
    is_screening = collection == SCREENING_COLLECTION
    if is_screening:
        must.append(models.FieldCondition(key="screening", match=models.MatchValue(value=True)))
        must.append(models.FieldCondition(key="published", match=models.MatchValue(value=False)))
    else:
        must.append(models.FieldCondition(key="published", match=models.MatchValue(value=True)))
        must.append(models.FieldCondition(key="screening", match=models.MatchValue(value=False)))
    if collection_key is not None:
        must.append(
            models.FieldCondition(
                key="collection_key", match=models.MatchValue(value=collection_key)
            )
        )
    must_not: list[models.Condition] = []
    if exclude_document_id is not None:
        must_not.append(
            models.FieldCondition(
                key="document_id", match=models.MatchValue(value=str(exclude_document_id))
            )
        )
    try:
        response = client.query_points(
            collection,
            query=query,
            using=using,
            limit=top_k,
            with_payload=True,
            query_filter=models.Filter(must=must, must_not=must_not or None),
        )
    except Exception as exc:
        raise _wrap(exc) from exc

    hits: list[ChunkHit] = []
    source: CandidateSource = "screening" if is_screening else "active"
    for rank, point in enumerate(response.points, start=1):
        payload = point.payload
        if not isinstance(payload, dict):
            raise VectorIndexError(
                f"collection {collection!r} returned a point with an invalid payload"
            )
        try:
            document_id = uuid.UUID(str(payload["document_id"]))
            chunk_id = str(uuid.UUID(str(payload["chunk_id"])))
        except (KeyError, TypeError, ValueError) as exc:
            raise VectorIndexError(
                f"collection {collection!r} returned a point with an invalid payload"
            ) from exc
        expected_published = not is_screening
        schema_version = payload.get("schema_version")
        if (
            isinstance(schema_version, bool)
            or schema_version != INDEX_SCHEMA_VERSION
            or payload.get("published") is not expected_published
            or payload.get("screening") is not is_screening
            or (collection_key is not None and payload.get("collection_key") != collection_key)
        ):
            raise VectorIndexError(
                f"collection {collection!r} returned a point outside its visibility boundary"
            )
        score = point.score
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(score)
        ):
            raise VectorIndexError(
                f"collection {collection!r} returned a point with an invalid score"
            )
        hits.append(
            ChunkHit(
                document_id=document_id,
                source=source,
                chunk_id=chunk_id,
                score=float(score),
                rank=rank,
            )
        )
    return hits
