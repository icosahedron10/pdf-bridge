from __future__ import annotations

from litestar import get
from litestar.testing import TestClient


def _assert_framework_error(response, status_code: int) -> dict:
    assert response.status_code == status_code
    assert response.headers["content-type"].startswith("application/json")
    payload = response.json()
    assert payload["status_code"] == status_code
    assert isinstance(payload["detail"], str) and payload["detail"]
    assert "code" not in payload
    return payload


def test_framework_validation_uses_litestar_error_contract(
    client: TestClient, csrf_headers: dict[str, str]
) -> None:
    response = client.post(
        "/api/v1/uploads/preflight",
        headers=csrf_headers,
        json={"filename": "invalid.pdf", "size_bytes": 0},
    )

    problem = _assert_framework_error(response, 400)
    assert isinstance(problem["extra"], list)
    assert any(error.get("key") == "size_bytes" for error in problem["extra"])


def test_openapi_advertises_problem_json_for_deliberate_failures(
    client: TestClient,
) -> None:
    schema_response = client.get("/api/openapi.json")
    assert schema_response.status_code == 200
    assert schema_response.headers["content-type"].startswith("application/json")
    schema = schema_response.json()
    response = schema["paths"]["/api/v1/uploads/preflight"]["post"]["responses"]["422"]
    problem_content = response["content"]["application/problem+json"]
    assert problem_content["schema"]["$ref"] == "#/components/schemas/ProblemDetail"
    assert "application/json" not in response["content"]


def test_openapi_preserves_optional_body_and_pdf_download_media_type(
    client: TestClient,
) -> None:
    schema = client.get("/api/openapi.json").json()

    deletion = schema["paths"][
        "/api/v1/documents/{document_id}/deletion"
    ]["post"]
    assert deletion["requestBody"].get("required", False) is False
    assert "application/json" in deletion["requestBody"]["content"]

    download = schema["paths"][
        "/api/v1/jobs/batches/{batch_id}/operations/{operation_id}/content"
    ]["get"]
    assert "application/pdf" in download["responses"]["200"]["content"]


def test_unexpected_errors_use_safe_litestar_json(app) -> None:
    @get("/test-only/unexpected", sync_to_thread=False)
    def fail_deliberately() -> None:
        raise RuntimeError("sensitive implementation detail")

    app.register(fail_deliberately)
    with TestClient(
        app,
        base_url="http://testserver.local",
        raise_server_exceptions=False,
    ) as test_client:
        response = test_client.get("/test-only/unexpected")

    problem = _assert_framework_error(response, 500)
    assert problem["detail"] == "Internal Server Error"
    assert response.headers["x-request-id"]
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "sensitive" not in response.text


def test_framework_routing_errors_use_litestar_json_and_security_headers(
    client: TestClient,
) -> None:
    responses = (
        client.get("/api/v1/does-not-exist"),
        client.post("/api/v1/health/live"),
        client.get("/api/v1/documents/not-a-uuid"),
        client.get("/api/does-not-exist"),
    )

    for response, expected_status in zip(responses, (404, 405, 404, 404), strict=True):
        _assert_framework_error(response, expected_status)
        assert response.headers["x-request-id"]
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "same-origin"
