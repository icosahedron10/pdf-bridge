"""Opt-in live proof that Bridge's Qdrant token cannot cross its RBAC boundary."""

from __future__ import annotations

import json
import os
import uuid

import httpx
import pytest


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        pytest.fail(f"{name} is required for the live Qdrant RBAC gate")
    return value


def _configured_active_collection() -> str:
    raw = _required_environment("PDF_BRIDGE_COLLECTIONS")
    try:
        definitions = json.loads(raw)
        names = [
            item["qdrant_collection_name"]
            for item in definitions
            if item.get("enabled") is True
        ]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        pytest.fail(f"PDF_BRIDGE_COLLECTIONS is invalid: {exc}")
    if not names or not all(isinstance(name, str) and name for name in names):
        pytest.fail("PDF_BRIDGE_COLLECTIONS must contain an enabled physical collection")
    return names[0]


def _assert_forbidden(response: httpx.Response, action: str) -> None:
    assert response.status_code == 403, (
        f"Bridge token unexpectedly allowed {action}; status={response.status_code}"
    )


@pytest.mark.qdrant_rbac
def test_live_bridge_token_cannot_manage_or_access_unrelated_collections() -> None:
    """Create only disposable collections and prove the deployed token's negative rights."""

    if os.environ.get("RUN_QDRANT_RBAC_LIVE_TEST") != "1":
        pytest.skip("set RUN_QDRANT_RBAC_LIVE_TEST=1 to run the live Qdrant RBAC gate")

    qdrant_url = _required_environment("PDF_BRIDGE_QDRANT_URL").rstrip("/")
    bridge_token = _required_environment("PDF_BRIDGE_QDRANT_API_KEY")
    admin_key = _required_environment("PDF_BRIDGE_QDRANT_ADMIN_API_KEY")
    active_collection = _configured_active_collection()
    assert bridge_token != admin_key, "Bridge token must differ from Qdrant's admin signing key"

    ca_file = os.environ.get("QDRANT_RBAC_TEST_CA_FILE", "").strip()
    verify: bool | str = ca_file or True
    nonce = uuid.uuid4().hex
    unrelated = f"pdf-bridge-rbac-unrelated-{nonce}"
    denied_topology = f"pdf-bridge-rbac-denied-{nonce}"
    disposable_names = (unrelated, denied_topology)
    vector_schema = {"vectors": {"size": 1, "distance": "Cosine"}}

    with (
        httpx.Client(
            base_url=qdrant_url,
            headers={"api-key": admin_key},
            timeout=10,
            verify=verify,
        ) as admin,
        httpx.Client(
            base_url=qdrant_url,
            headers={"api-key": bridge_token},
            timeout=10,
            verify=verify,
        ) as bridge,
    ):
        admin.put(f"/collections/{unrelated}", json=vector_schema).raise_for_status()
        try:
            bridge.get(f"/collections/{active_collection}").raise_for_status()

            listed = bridge.get("/collections")
            listed.raise_for_status()
            visible_names = {
                item["name"] for item in listed.json()["result"]["collections"]
            }
            assert unrelated not in visible_names

            _assert_forbidden(
                bridge.get(f"/collections/{unrelated}"),
                "unrelated collection metadata access",
            )
            _assert_forbidden(
                bridge.put(
                    f"/collections/{unrelated}/points",
                    params={"wait": "true"},
                    json={"points": [{"id": 1, "vector": [1.0]}]},
                ),
                "an unrelated point write",
            )
            _assert_forbidden(
                bridge.put(f"/collections/{denied_topology}", json=vector_schema),
                "collection creation",
            )
        finally:
            for collection in disposable_names:
                response = admin.delete(f"/collections/{collection}")
                if response.status_code != 404:
                    response.raise_for_status()
