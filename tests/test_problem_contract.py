from __future__ import annotations

from fastapi.testclient import TestClient

from pdf_bridge.schemas import ProblemDetail


def test_runtime_validation_problem_matches_public_schema(
    client: TestClient, csrf_headers: dict[str, str]
) -> None:
    response = client.post(
        "/api/v1/uploads/preflight",
        headers=csrf_headers,
        json={"filename": "invalid.pdf", "size_bytes": 0},
    )
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    problem = ProblemDetail.model_validate(response.json())
    assert problem.code == "validation-error"
    assert problem.request_id
    assert problem.errors[0].location[-1] == "size_bytes"


def test_openapi_advertises_problem_json_for_validation_errors(client: TestClient) -> None:
    schema = client.get("/api/openapi.json").json()
    response = schema["paths"]["/api/v1/uploads/preflight"]["post"]["responses"]["422"]
    problem_content = response["content"]["application/problem+json"]
    assert problem_content["schema"]["$ref"] == "#/components/schemas/ProblemDetail"
    assert "application/json" not in response["content"]


def test_unexpected_errors_are_safe_problem_json(app) -> None:
    def fail_deliberately() -> None:
        raise RuntimeError("sensitive implementation detail")

    app.add_api_route("/test-only/unexpected", fail_deliberately, methods=["GET"])
    with TestClient(app, raise_server_exceptions=False) as test_client:
        response = test_client.get("/test-only/unexpected")
    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/problem+json")
    problem = ProblemDetail.model_validate(response.json())
    assert problem.code == "internal-error"
    assert problem.request_id
    assert response.headers["x-request-id"] == problem.request_id
    assert response.headers["cache-control"] == "no-store"
    assert "sensitive" not in response.text


def test_framework_404_is_also_problem_json(client: TestClient) -> None:
    response = client.get("/api/v1/does-not-exist")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert ProblemDetail.model_validate(response.json()).code == "not-found"
