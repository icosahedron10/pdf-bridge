from __future__ import annotations

import tomllib
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")


def test_runtime_has_one_v2_surface_and_no_integrated_web_bundle() -> None:
    runtime_files = sorted((REPOSITORY_ROOT / "pdf_bridge").rglob("*.py"))
    runtime_text = "\n".join(path.read_text(encoding="utf-8") for path in runtime_files)

    assert "/api/v1" not in runtime_text
    assert "jinja" not in runtime_text.casefold()
    assert "TemplateResponse" not in runtime_text
    assert not (REPOSITORY_ROOT / "pdf_bridge" / "templates").exists()
    assert not (REPOSITORY_ROOT / "pdf_bridge" / "static").exists()


def test_target_model_dependencies_are_exactly_pinned() -> None:
    project = tomllib.loads(_read("pyproject.toml"))["project"]
    dependencies = set(project["dependencies"])
    streamlit_dependencies = set(project["optional-dependencies"]["streamlit"])

    assert "sentence-transformers==5.5.0" in dependencies
    assert "fastembed==0.8.0" in dependencies
    assert "lingua-language-detector==2.2.0" in dependencies
    assert "streamlit==1.59.2" in streamlit_dependencies
    assert not any(dependency.casefold().startswith("jinja2") for dependency in dependencies)


def test_compose_uses_private_fixed_qdrant_and_offline_model_cache() -> None:
    compose = _read("docker-compose.yml")
    qdrant_environment = compose.split("  qdrant:", 1)[1].split("  app:", 1)[0]
    app_environment = compose.split("  app:", 1)[1].split("  streamlit:", 1)[0]

    assert "/api/v2/health/ready" in compose
    assert "/api/v1" not in compose
    assert 'QDRANT__SERVICE__JWT_RBAC: "true"' in compose
    assert "qdrant_private:" in compose
    assert "internal: true" in compose
    assert "PDF_BRIDGE_QDRANT_SCREENING_COLLECTION_NAME" in compose
    assert "PDF_BRIDGE_FORMATTER_TOKENIZER_CLASS" in compose
    assert 'PDF_BRIDGE_MODEL_LOCAL_FILES_ONLY: "true"' in compose
    assert "/var/cache/pdf-bridge-models:ro" in compose
    assert "PDF_BRIDGE_EMBEDDING_API_URL" not in compose
    assert "PDF_BRIDGE_QDRANT_ADMIN_API_KEY" in qdrant_environment
    assert "PDF_BRIDGE_QDRANT_API_KEY" in app_environment
    assert "PDF_BRIDGE_QDRANT_ADMIN_API_KEY" not in app_environment
    assert "PDF_BRIDGE_QDRANT_API_KEY" not in qdrant_environment


def test_compose_runs_streamlit_as_a_hardened_api_only_client() -> None:
    compose = _read("docker-compose.yml")
    streamlit_service = compose.split("  streamlit:", 1)[1].split("\nnetworks:", 1)[0]
    dockerfile = _read("streamlit.Dockerfile")

    assert "dockerfile: streamlit.Dockerfile" in streamlit_service
    assert "PDF_BRIDGE_URL: http://app:8000" in streamlit_service
    assert "condition: service_healthy" in streamlit_service
    assert "operator_private" in streamlit_service
    assert "qdrant_private" not in streamlit_service
    assert "/var/lib/pdf-bridge" not in streamlit_service
    assert "/var/cache/pdf-bridge-models" not in streamlit_service
    assert "read_only: true" in streamlit_service
    assert "no-new-privileges:true" in streamlit_service
    assert "_stcore/health" in streamlit_service
    assert '["localhost","127.0.0.1","app"]' in compose
    assert "clamav_private:" in compose
    assert "operator_private:" in compose

    assert '"httpx==0.28.1"' in dockerfile
    assert '"streamlit==1.59.2"' in dockerfile
    assert "COPY pdf_bridge" not in dockerfile
    assert "sentence-transformers" not in dockerfile


def test_entrypoint_fails_closed_on_missing_or_shared_credentials() -> None:
    entrypoint = _read("docker-entrypoint.sh")

    for credential in (
        "PDF_BRIDGE_SESSION_SECRET",
        "PDF_BRIDGE_QDRANT_API_KEY",
        "PDF_BRIDGE_FORMATTER_API_TOKEN",
        "PDF_BRIDGE_LLM_API_TOKEN",
    ):
        assert credential in entrypoint
    assert "PDF_BRIDGE_QDRANT_ADMIN_API_KEY must never be injected" in entrypoint
    assert "must all be different" in entrypoint
    assert "alembic upgrade head" in entrypoint
    assert "set -eu" in entrypoint
