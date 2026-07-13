from __future__ import annotations

from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_jenkins_artifacts_are_removed() -> None:
    for relative_path in ("Jenkinsfile.example", "docs/jenkins.md"):
        assert not (REPOSITORY_ROOT / relative_path).exists(), relative_path


def test_container_runs_exactly_one_uvicorn_process() -> None:
    dockerfile = (REPOSITORY_ROOT / "Dockerfile").read_text(encoding="utf-8")
    entrypoint = (REPOSITORY_ROOT / "docker-entrypoint.sh").read_text(encoding="utf-8")

    assert '"--workers", "1"' in dockerfile
    assert "PDF_BRIDGE_JOB_TOKEN" not in entrypoint
    assert "Jenkins" not in entrypoint


def test_compose_health_uses_readiness_and_larger_ephemeral_tmp() -> None:
    compose = (REPOSITORY_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "/api/v1/health/ready" in compose
    assert "/api/v1/health/live" not in compose
    assert "/tmp:size=256m,mode=1777" in compose


def test_compose_pins_and_isolates_authenticated_qdrant() -> None:
    compose = (REPOSITORY_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "qdrant/qdrant:v1.18.1" in compose
    assert 'QDRANT__SERVICE__JWT_RBAC: "true"' in compose
    assert "QDRANT__SERVICE__API_KEY:" in compose
    assert "PDF_BRIDGE_QDRANT_API_KEY:" in compose
    assert "PDF_BRIDGE_QDRANT_URL: http://qdrant:6333" in compose
    assert "qdrant_private:" in compose
    assert "internal: true" in compose
    assert "6333:6333" not in compose


def test_compose_forwards_every_documented_operator_tunable() -> None:
    compose = (REPOSITORY_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    env_example = (REPOSITORY_ROOT / ".env.example").read_text(encoding="utf-8")

    for variable in (
        "PDF_BRIDGE_MAX_UPLOAD_BYTES",
        "PDF_BRIDGE_MAX_UPLOAD_FILES",
        "PDF_BRIDGE_UPLOAD_CHUNK_BYTES",
        "PDF_BRIDGE_CLAMD_TIMEOUT",
        "PDF_BRIDGE_CLAMD_STREAM_MAX_BYTES",
        "PDF_BRIDGE_WORKER_ENABLED",
        "PDF_BRIDGE_WORKER_POLL_SECONDS",
        "PDF_BRIDGE_WORKER_LEASE_SECONDS",
        "PDF_BRIDGE_WORKER_HEARTBEAT_SECONDS",
        "PDF_BRIDGE_PARSE_WALL_CLOCK_SECONDS",
        "PDF_BRIDGE_PARSE_CPU_SECONDS",
        "PDF_BRIDGE_PARSE_MEMORY_BYTES",
        "PDF_BRIDGE_ANALYSIS_MAX_PAGES",
        "PDF_BRIDGE_ANALYSIS_MAX_CHARACTERS",
        "PDF_BRIDGE_ANALYSIS_MAX_CHUNKS",
        "PDF_BRIDGE_EMBEDDING_API_URL",
        "PDF_BRIDGE_EMBEDDING_API_TOKEN",
        "PDF_BRIDGE_EMBEDDING_MODEL_ID",
        "PDF_BRIDGE_EMBEDDING_DIMENSION",
        "PDF_BRIDGE_EMBEDDING_TIMEOUT",
        "PDF_BRIDGE_LLM_API_URL",
        "PDF_BRIDGE_LLM_API_TOKEN",
        "PDF_BRIDGE_LLM_CLASSIFIER_MODEL",
        "PDF_BRIDGE_LLM_VERIFIER_MODEL",
        "PDF_BRIDGE_LLM_TIMEOUT",
        "PDF_BRIDGE_QDRANT_API_KEY",
        "PDF_BRIDGE_QDRANT_TIMEOUT",
        "PDF_BRIDGE_SEARCH_API_URL",
        "PDF_BRIDGE_SEARCH_API_TOKEN",
        "PDF_BRIDGE_SEARCH_API_TIMEOUT",
    ):
        assert f"{variable}:" in compose, variable
        assert f"${{{variable}" in compose, variable
        assert f"{variable}=" in env_example, variable

    assert "PDF_BRIDGE_JOB_TOKEN" not in compose
    assert "PDF_BRIDGE_CLAIM_LEASE_MINUTES" not in compose
    assert "PDF_BRIDGE_JOB_TOKEN" not in env_example
    assert "PDF_BRIDGE_CLAIM_LEASE_MINUTES" not in env_example


def test_compose_forwards_deployment_theme_settings() -> None:
    compose = (REPOSITORY_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    env_example = (REPOSITORY_ROOT / ".env.example").read_text(encoding="utf-8")

    for variable in (
        "PDF_BRIDGE_BRAND_PRIMARY_1",
        "PDF_BRIDGE_BRAND_PRIMARY_2",
        "PDF_BRIDGE_BRAND_SECONDARY_1",
        "PDF_BRIDGE_BRAND_SECONDARY_2",
        "PDF_BRIDGE_THEME_DEFAULT",
    ):
        assert f"{variable}:" in compose
        assert f"{variable}=" in env_example
        assert f"${{{variable}:-" not in compose
        assert f"${{{variable}-" in compose
