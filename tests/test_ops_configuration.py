from __future__ import annotations

from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_jenkins_example_uses_clean_workspace_and_released_wheel() -> None:
    pipeline = (REPOSITORY_ROOT / "Jenkinsfile.example").read_text(encoding="utf-8")

    assert "parameters {" not in pipeline
    assert "skipDefaultCheckout(true)" in pipeline
    assert pipeline.count("deleteDir()") >= 2
    assert 'rm -f "$PULL_RESULT" "$PIPELINE_REPORT"' in pipeline
    assert "--only-binary=:all:" in pipeline
    assert '"pdf-bridge==$PDF_BRIDGE_CLIENT_VERSION"' in pipeline
    assert "pip install --disable-pip-version-check ." not in pipeline
    assert "PDF_BRIDGE_JOB_ALLOWED_HOST = 'pdf-bridge.internal'" in pipeline
    assert '--allowed-host "$PDF_BRIDGE_JOB_ALLOWED_HOST"' in pipeline
    assert '--pull-result "$PULL_RESULT"' in pipeline


def test_compose_health_uses_readiness_and_larger_ephemeral_tmp() -> None:
    compose = (REPOSITORY_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "/api/v1/health/ready" in compose
    assert "/api/v1/health/live" not in compose
    assert "/tmp:size=256m,mode=1777" in compose


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
