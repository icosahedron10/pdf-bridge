from __future__ import annotations

from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TARGET_DOCUMENTS = (
    "README.md",
    "docs/service-contract.md",
    "docs/architecture.md",
    "docs/contracts/intake-api.md",
    "docs/contracts/chunks-qdrant.md",
    "docs/configuration.md",
    "docs/runbook.md",
    "docs/security.md",
    "streamlit_app/README.md",
)


def read(relative_path: str) -> str:
    return (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")


def test_authoritative_documents_are_labeled_as_current() -> None:
    for relative_path in TARGET_DOCUMENTS:
        opening = "\n".join(read(relative_path).splitlines()[:8])
        assert "Status: Current" in opening, relative_path


def test_refactor_records_have_final_statuses() -> None:
    assert "Status: Implemented" in "\n".join(
        read("docs/refactor-plan.md").splitlines()[:8]
    )
    assert "Status: Historical" in "\n".join(
        read("docs/refactor-gap.md").splitlines()[:8]
    )


def test_target_contract_records_the_refactor_decisions() -> None:
    target_text = "\n".join(read(relative_path) for relative_path in TARGET_DOCUMENTS)

    for required in (
        "sentence-transformers/all-mpnet-base-v2",
        "Qdrant/bm25",
        "named dense vector `dense`",
        "named sparse vector `bm25`",
        "fixed pre-provisioned",
        "Streamlit",
        "Qdrant-first",
        "REVIEW_REQUIRED",
        "/api/v2",
    ):
        assert required in target_text, required

    assert "content_dense" not in target_text
    assert "content_bm25" not in target_text


def test_duplicate_documentation_surfaces_are_retired() -> None:
    notice = read("interactive-docs/app/retirement-notice.tsx")
    interactive_sources = REPOSITORY_ROOT / "interactive-docs" / "app" / "_docs"

    assert "Status: Retired" in notice
    assert "no longer an authoritative source" in notice
    assert not any(interactive_sources.glob("*.tsx"))
    assert "Status: Superseded" in read("docs/importing.md")
    assert "Jenkinsfile.example" not in read(".dockerignore")
