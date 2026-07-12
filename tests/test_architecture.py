from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPOSITORY_ROOT / "pdf_bridge"

EXPECTED_MODULES = (
    "__init__.py",
    "app.py",
    "contracts/__init__.py",
    "contracts/job_contracts.py",
    "contracts/schemas.py",
    "core/__init__.py",
    "core/config.py",
    "core/logging_config.py",
    "controllers/__init__.py",
    "controllers/admin_cli.py",
    "controllers/api.py",
    "controllers/job_cli.py",
    "controllers/jobs.py",
    "controllers/web.py",
    "http/__init__.py",
    "http/middleware.py",
    "http/problems.py",
    "http/security.py",
    "managers/__init__.py",
    "managers/batch.py",
    "managers/catalog.py",
    "managers/document.py",
    "managers/health.py",
    "managers/importing.py",
    "managers/job_client.py",
    "managers/search.py",
    "managers/web.py",
    "persistence/__init__.py",
    "persistence/db.py",
    "persistence/models.py",
    "presentation/__init__.py",
    "presentation/api_serializers.py",
    "presentation/theme.py",
    "presentation/view_models.py",
    "services/__init__.py",
    "services/catalog.py",
    "services/document.py",
    "services/errors.py",
    "services/health.py",
    "services/historical_import.py",
    "services/job_batch.py",
    "services/job_http.py",
    "services/job_staging.py",
    "services/lifecycle.py",
    "services/scanner.py",
    "services/search.py",
    "services/storage.py",
    "services/web_page.py",
)

OBSOLETE_ROOT_MODULES = (
    "admin_cli.py",
    "api.py",
    "job_cli.py",
    "jobs.py",
    "lifecycle.py",
    "scanner.py",
    "search.py",
    "storage.py",
    "theme.py",
    "view_models.py",
    "web.py",
)

OBSOLETE_FOUNDATION_MODULES = (
    "config.py",
    "db.py",
    "job_contracts.py",
    "logging_config.py",
    "middleware.py",
    "models.py",
    "problems.py",
    "schemas.py",
    "security.py",
)

OBSOLETE_IMPORT_PREFIXES = tuple(
    f"pdf_bridge.{Path(module).stem}"
    for module in (*OBSOLETE_ROOT_MODULES, *OBSOLETE_FOUNDATION_MODULES)
) + tuple(f"pdf_bridge.{package}" for package in ("utils",))

LAYER_DEPENDENCIES = {
    "core": frozenset(),
    "persistence": frozenset({"core"}),
    "contracts": frozenset({"core", "persistence"}),
    "presentation": frozenset({"core", "contracts", "persistence"}),
    "http": frozenset({"contracts", "core"}),
    "services": frozenset({"core", "contracts", "persistence", "presentation"}),
    "managers": frozenset(
        {"core", "contracts", "persistence", "presentation", "services"}
    ),
    "controllers": frozenset(
        {
            "core",
            "contracts",
            "http",
            "managers",
            "persistence",
            "presentation",
            "services",
        }
    ),
}


def _python_files(layer: str) -> list[Path]:
    return sorted((PACKAGE_ROOT / layer).rglob("*.py"))


def _current_package(path: Path) -> str:
    relative = path.relative_to(REPOSITORY_ROOT)
    if path.name == "__init__.py":
        return ".".join(relative.parent.parts)
    return ".".join(relative.with_suffix("").parts[:-1])


def _resolved_from_module(path: Path, node: ast.ImportFrom) -> str:
    module = node.module or ""
    if not node.level:
        return module
    relative_name = "." * node.level + module
    return importlib.util.resolve_name(relative_name, _current_package(path))


def _import_targets(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    targets: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = _resolved_from_module(path, node)
            if base:
                targets.append((node.lineno, base))
                targets.extend(
                    (node.lineno, f"{base}.{alias.name}")
                    for alias in node.names
                    if alias.name != "*"
                )
    return targets


def _matches_prefix(module: str, prefix: str) -> bool:
    return module == prefix or module.startswith(f"{prefix}.")


def _forbidden_imports(layer: str, prefixes: tuple[str, ...]) -> list[str]:
    violations: set[str] = set()
    for path in _python_files(layer):
        for line, module in _import_targets(path):
            if any(_matches_prefix(module, prefix) for prefix in prefixes):
                relative = path.relative_to(REPOSITORY_ROOT)
                violations.add(f"{relative}:{line} imports {module}")
    return sorted(violations)


def _application_layer(module: str) -> str | None:
    parts = module.split(".")
    if len(parts) >= 2 and parts[0] == "pdf_bridge" and parts[1] in LAYER_DEPENDENCIES:
        return parts[1]
    return None


def _repository_python_files() -> list[Path]:
    return sorted(
        path
        for root in (PACKAGE_ROOT, REPOSITORY_ROOT / "tests", REPOSITORY_ROOT / "migrations")
        for path in root.rglob("*.py")
    )


def _has_redundant_layer_suffix(module: str) -> bool:
    parts = module.split(".")
    if len(parts) < 3 or parts[0] != "pdf_bridge":
        return False
    suffix = {
        "controllers": "_controller",
        "managers": "_manager",
        "services": "_service",
    }.get(parts[1])
    return suffix is not None and parts[2].endswith(suffix)


def test_internal_architecture_module_set_is_exact() -> None:
    expected = set(EXPECTED_MODULES)
    actual = {
        path.relative_to(PACKAGE_ROOT).as_posix() for path in PACKAGE_ROOT.rglob("*.py")
    }
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    message_lines = [
        "Architecture module mismatch",
        "Missing:",
        *(missing or ["(none)"]),
        "Unexpected:",
        *(unexpected or ["(none)"]),
    ]
    assert not missing and not unexpected, "\n".join(message_lines)


def test_package_root_contains_only_init_and_composition_root() -> None:
    root_modules = sorted(path.name for path in PACKAGE_ROOT.glob("*.py"))
    assert root_modules == ["__init__.py", "app.py"]


def test_obsolete_root_forwarding_modules_do_not_exist() -> None:
    remaining = [name for name in OBSOLETE_ROOT_MODULES if (PACKAGE_ROOT / name).exists()]
    assert not remaining, "Obsolete root forwarding modules remain:\n" + "\n".join(remaining)


def test_repository_imports_only_canonical_modules() -> None:
    offenders: set[str] = set()
    for path in _repository_python_files():
        for line, module in _import_targets(path):
            if any(_matches_prefix(module, prefix) for prefix in OBSOLETE_IMPORT_PREFIXES) or (
                _has_redundant_layer_suffix(module)
            ):
                relative = path.relative_to(REPOSITORY_ROOT)
                offenders.add(f"{relative}:{line} imports {module}")
    assert not offenders, "Obsolete module imports remain:\n" + "\n".join(sorted(offenders))


def test_layer_module_names_have_no_redundant_suffixes() -> None:
    offenders = [
        path.relative_to(REPOSITORY_ROOT).as_posix()
        for layer, suffix in (
            ("controllers", "_controller.py"),
            ("managers", "_manager.py"),
            ("services", "_service.py"),
        )
        for path in (PACKAGE_ROOT / layer).glob(f"*{suffix}")
    ]
    assert not offenders, "Redundant layer suffixes remain:\n" + "\n".join(offenders)


def test_package_initializers_do_not_reexport_implementations() -> None:
    offenders = [
        path.relative_to(REPOSITORY_ROOT).as_posix()
        for path in PACKAGE_ROOT.rglob("__init__.py")
        if _import_targets(path)
    ]
    assert not offenders, "Package initializers must not re-export modules:\n" + "\n".join(
        offenders
    )


def test_internal_layer_dependencies_flow_in_one_direction() -> None:
    violations: set[str] = set()
    for layer, allowed_dependencies in LAYER_DEPENDENCIES.items():
        for path in _python_files(layer):
            for line, module in _import_targets(path):
                target_layer = _application_layer(module)
                imports_composition_root = _matches_prefix(module, "pdf_bridge.app")
                if imports_composition_root or (
                    target_layer is not None
                    and target_layer != layer
                    and target_layer not in allowed_dependencies
                ):
                    relative = path.relative_to(REPOSITORY_ROOT)
                    violations.add(f"{relative}:{line} imports {module}")
    assert not violations, "Layer dependency violations:\n" + "\n".join(sorted(violations))


def test_services_do_not_depend_on_transport_or_upper_layers() -> None:
    violations = _forbidden_imports(
        "services",
        (
            "litestar",
            "pdf_bridge.controllers",
            "pdf_bridge.http",
            "pdf_bridge.managers",
        ),
    )
    assert not violations, "Service dependency violations:\n" + "\n".join(violations)


def test_contracts_do_not_depend_on_transport_frameworks() -> None:
    violations = _forbidden_imports("contracts", ("litestar", "pdf_bridge.http"))
    assert not violations, "Contract dependency violations:\n" + "\n".join(violations)


def test_managers_do_not_depend_on_transport_or_controllers() -> None:
    violations = _forbidden_imports(
        "managers",
        ("litestar", "pdf_bridge.controllers", "pdf_bridge.http"),
    )
    assert not violations, "Manager dependency violations:\n" + "\n".join(violations)


def test_transactions_are_not_owned_by_controllers_or_services() -> None:
    offenders: list[str] = []
    for layer in ("controllers", "services"):
        for path in _python_files(layer):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"commit", "rollback"}
                ):
                    relative = path.relative_to(REPOSITORY_ROOT)
                    offenders.append(f"{relative}:{node.lineno} calls {node.func.attr}()")
    assert not offenders, "Transaction boundary violations:\n" + "\n".join(offenders)


def test_presentation_is_stateless_and_transport_independent() -> None:
    violations = _forbidden_imports(
        "presentation",
        (
            "litestar",
            "sqlalchemy",
            "pdf_bridge.controllers",
            "pdf_bridge.http",
            "pdf_bridge.managers",
            "pdf_bridge.services",
        ),
    )
    assert not violations, "Presentation dependency violations:\n" + "\n".join(violations)


def test_controllers_only_import_sqlalchemy_session_for_typing() -> None:
    violations: list[str] = []
    for path in _python_files("controllers"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                sqlalchemy_names = [
                    alias.name
                    for alias in node.names
                    if _matches_prefix(alias.name, "sqlalchemy")
                ]
                if sqlalchemy_names:
                    relative = path.relative_to(REPOSITORY_ROOT)
                    violations.append(
                        f"{relative}:{node.lineno} imports {', '.join(sqlalchemy_names)}"
                    )
            elif isinstance(node, ast.ImportFrom):
                module = _resolved_from_module(path, node)
                if not _matches_prefix(module, "sqlalchemy"):
                    continue
                imported_names = {alias.name for alias in node.names}
                if module == "sqlalchemy.orm" and imported_names == {"Session"}:
                    continue
                relative = path.relative_to(REPOSITORY_ROOT)
                violations.append(
                    f"{relative}:{node.lineno} imports {module}: "
                    f"{', '.join(sorted(imported_names))}"
                )
    assert not violations, "Controllers must not construct SQL queries:\n" + "\n".join(
        violations
    )
