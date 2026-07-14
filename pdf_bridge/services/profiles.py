"""Deterministic identities for immutable preparation and policy profiles."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass

PROFILE_SCHEMA_VERSION = 1

_PROFILE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_QDRANT_COLLECTION_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")


@dataclass(frozen=True, slots=True)
class ProfileIdentity:
    """One profile's persisted identifier and exact canonical source JSON."""

    profile_id: str
    canonical_json: str


@dataclass(frozen=True, slots=True)
class PipelineProfiles:
    """Correlated content, index, and preflight-policy profile identities."""

    content: ProfileIdentity
    index: ProfileIdentity
    preflight_policy: ProfileIdentity


def _canonical_value(value: object, *, location: str) -> object:
    if value is None or isinstance(value, (bool, str)):
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ValueError(f"{location} contains invalid Unicode") from exc
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{location} contains a non-finite float")
        return 0.0 if value == 0.0 else value
    if isinstance(value, Mapping):
        normalized: dict[str, object] = {}
        for key, nested in value.items():
            if not isinstance(key, str) or not key or any(ord(character) < 32 for character in key):
                raise TypeError(f"{location} mapping keys must be non-empty control-free strings")
            normalized[key] = _canonical_value(nested, location=f"{location}.{key}")
        return normalized
    if isinstance(value, (list, tuple)):
        return [
            _canonical_value(item, location=f"{location}[{index}]")
            for index, item in enumerate(value)
        ]
    raise TypeError(
        f"{location} contains unsupported profile value type {type(value).__name__}; "
        "convert it explicitly to JSON data"
    )


def canonical_profile_json(material: Mapping[str, object]) -> str:
    """Return stable compact JSON, rejecting implicit or lossy coercions."""

    normalized = _canonical_value(material, location="profile")
    if not isinstance(normalized, dict):  # Defensive: the public annotation is runtime-enforced.
        raise TypeError("profile material must be a mapping")
    return json.dumps(
        normalized,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _identity(material: Mapping[str, object]) -> ProfileIdentity:
    canonical = canonical_profile_json(material)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return ProfileIdentity(profile_id=f"sha256:{digest}", canonical_json=canonical)


def _validated_inputs(profile_name: str, inputs: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(inputs, Mapping):
        raise TypeError(f"{profile_name} inputs must be a mapping")
    if not inputs:
        raise ValueError(f"{profile_name} inputs cannot be empty")
    normalized = _canonical_value(inputs, location=f"{profile_name}.inputs")
    if not isinstance(normalized, dict):
        raise TypeError(f"{profile_name} inputs must be a mapping")
    return normalized


def _validate_profile_id(field_name: str, value: str) -> None:
    if not _PROFILE_ID.fullmatch(value):
        raise ValueError(f"{field_name} must be a lowercase sha256 profile identifier")


def build_content_profile(inputs: Mapping[str, object]) -> ProfileIdentity:
    """Hash parser, formatter, serializer, tokenizer, and chunker inputs."""

    material = {
        "profile_schema_version": PROFILE_SCHEMA_VERSION,
        "profile_kind": "content",
        "inputs": _validated_inputs("content", inputs),
    }
    return _identity(material)


def build_index_profile(
    inputs: Mapping[str, object],
    *,
    content_profile_id: str,
    active_qdrant_collection: str,
) -> ProfileIdentity:
    """Hash vector/point inputs and the resolved immutable active target."""

    _validate_profile_id("content_profile_id", content_profile_id)
    if not _QDRANT_COLLECTION_NAME.fullmatch(active_qdrant_collection):
        raise ValueError("active_qdrant_collection must be a safe resolved physical name")
    material = {
        "profile_schema_version": PROFILE_SCHEMA_VERSION,
        "profile_kind": "index",
        "content_profile_id": content_profile_id,
        "active_qdrant_collection": active_qdrant_collection,
        "inputs": _validated_inputs("index", inputs),
    }
    return _identity(material)


def build_preflight_policy_profile(
    inputs: Mapping[str, object],
    *,
    index_profile_id: str,
) -> ProfileIdentity:
    """Hash candidate, evidence, classifier, verifier, and policy inputs."""

    _validate_profile_id("index_profile_id", index_profile_id)
    material = {
        "profile_schema_version": PROFILE_SCHEMA_VERSION,
        "profile_kind": "preflight_policy",
        "index_profile_id": index_profile_id,
        "inputs": _validated_inputs("preflight_policy", inputs),
    }
    return _identity(material)


def build_pipeline_profiles(
    *,
    content_inputs: Mapping[str, object],
    index_inputs: Mapping[str, object],
    preflight_policy_inputs: Mapping[str, object],
    active_qdrant_collection: str,
) -> PipelineProfiles:
    """Build the three independently useful, cryptographically chained profiles."""

    content = build_content_profile(content_inputs)
    index = build_index_profile(
        index_inputs,
        content_profile_id=content.profile_id,
        active_qdrant_collection=active_qdrant_collection,
    )
    preflight_policy = build_preflight_policy_profile(
        preflight_policy_inputs,
        index_profile_id=index.profile_id,
    )
    return PipelineProfiles(
        content=content,
        index=index,
        preflight_policy=preflight_policy,
    )
