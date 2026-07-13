from __future__ import annotations

import re
from pathlib import Path

import pytest
from litestar.testing import TestClient
from pydantic import SecretStr, ValidationError

from pdf_bridge.app import create_app
from pdf_bridge.core.config import Settings
from tests.conftest import clean_scanner

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
APP_CSS = REPOSITORY_ROOT / "pdf_bridge" / "static" / "app.css"
STATIC_ROOT = APP_CSS.parent
TEMPLATE_ROOT = REPOSITORY_ROOT / "pdf_bridge" / "templates"

BRAND_FIELDS = (
    "brand_primary_1",
    "brand_primary_2",
    "brand_secondary_1",
    "brand_secondary_2",
)
THEME_ENVIRONMENT_VARIABLES = tuple(
    f"PDF_BRIDGE_{field_name.upper()}" for field_name in BRAND_FIELDS
) + ("PDF_BRIDGE_THEME_DEFAULT",)
HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
CSS_VARIABLE = re.compile(
    r"(?P<name>--color-[a-z0-9-]+)\s*:\s*(?P<value>#[0-9a-fA-F]{6})\s*;"
)
CSS_DECLARATION = re.compile(
    r"(?m)^[ \t]*(?P<property>-{0,2}[a-zA-Z][\w-]*)[ \t]*:[ \t]*"
    r"(?P<value>[^;{}]+);"
)
RAW_COLOR_LITERAL = re.compile(
    r"#[0-9a-fA-F]{3}(?:[0-9a-fA-F]?|[0-9a-fA-F]{3}|[0-9a-fA-F]{5})?"
    r"(?![0-9a-fA-F])"
    r"|(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch)\(\s*[+-]?(?:\d|\.)",
    re.IGNORECASE,
)


@pytest.fixture(autouse=True)
def clear_theme_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in THEME_ENVIRONMENT_VARIABLES:
        monkeypatch.delenv(variable, raising=False)


@pytest.fixture
def settings_kwargs(tmp_path: Path) -> dict[str, object]:
    return {
        "app_env": "test",
        "auth_mode": "anonymous-poc",
        "storage_root": tmp_path / "theme-data",
        "database_url": "sqlite+pysqlite:///:memory:",
        "session_secret": SecretStr("theme-test-session-secret-at-least-32-characters"),
        "job_token": SecretStr("theme-test-job-token-different-at-least-32-characters"),
        "collections": [
            {
                "key": "customer",
                "display_name": "Customer Product",
                "description": "Approved customer-facing product content.",
                "audience": "customer",
            }
        ],
    }


def _relative_luminance(color: str) -> float:
    channels = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [
        channel / 12.92
        if channel <= 0.04045
        else ((channel + 0.055) / 1.055) ** 2.4
        for channel in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast_ratio(first: str, second: str) -> float:
    lighter, darker = sorted(
        (_relative_luminance(first), _relative_luminance(second)), reverse=True
    )
    return (lighter + 0.05) / (darker + 0.05)


def test_theme_defaults_match_the_existing_client_palette(
    settings_kwargs: dict[str, object],
) -> None:
    settings = Settings(**settings_kwargs)

    assert settings.brand_primary_1 == "#173f34"
    assert settings.brand_primary_2 == "#0f3028"
    assert settings.brand_secondary_1 == "#d5a846"
    assert settings.brand_secondary_2 == "#d9c78f"
    assert settings.theme_default == "system"


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("brand_primary_1", "#0A1b2C"),
        ("brand_primary_2", "#ABCDEF"),
        ("brand_secondary_1", "#7A4b00"),
        ("brand_secondary_2", "#FfEeDd"),
    ),
)
def test_theme_colors_accept_exactly_six_hexadecimal_digits(
    settings_kwargs: dict[str, object], field_name: str, value: str
) -> None:
    settings = Settings(**settings_kwargs, **{field_name: value})

    assert HEX_COLOR.fullmatch(getattr(settings, field_name))
    assert getattr(settings, field_name).casefold() == value.casefold()


@pytest.mark.parametrize("field_name", BRAND_FIELDS)
@pytest.mark.parametrize(
    "value",
    (
        "173f34",
        "#123",
        "#1234",
        "#12345",
        "#1234567",
        "#12345678",
        "#gg0000",
        " #123456",
        "#123456 ",
        "#123456; body { color: red; }",
    ),
)
def test_theme_colors_reject_every_non_contract_value(
    settings_kwargs: dict[str, object], field_name: str, value: str
) -> None:
    storage_root = settings_kwargs["storage_root"]
    assert isinstance(storage_root, Path)

    with pytest.raises(ValidationError):
        Settings(**settings_kwargs, **{field_name: value})

    assert not storage_root.exists()


def test_theme_default_rejects_an_unknown_mode(settings_kwargs: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Settings(**settings_kwargs, theme_default="sepia")


def test_theme_settings_load_from_deployment_environment(
    settings_kwargs: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = {
        "brand_primary_1": "#102030",
        "brand_primary_2": "#204060",
        "brand_secondary_1": "#7a4b00",
        "brand_secondary_2": "#f0e0c0",
        "theme_default": "dark",
    }
    for field_name, value in expected.items():
        monkeypatch.setenv(f"PDF_BRIDGE_{field_name.upper()}", value)

    settings = Settings(**settings_kwargs)

    for field_name, value in expected.items():
        assert getattr(settings, field_name) == value


def test_theme_css_contains_configured_tokens_and_accessible_foregrounds(
    settings_kwargs: dict[str, object],
) -> None:
    configured = {
        "brand_primary_1": "#123456",
        "brand_primary_2": "#f0e0d0",
        "brand_secondary_1": "#805500",
        "brand_secondary_2": "#ffeeaa",
    }
    settings = Settings(**settings_kwargs, **configured)
    application = create_app(settings, scanner=clean_scanner)

    with TestClient(
        application,
        base_url="http://testserver",
        raise_server_exceptions=True,
    ) as client:
        response = client.get("/theme.css")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/css")
    variables = {match["name"]: match["value"] for match in CSS_VARIABLE.finditer(response.text)}
    assert variables["--color-action"].casefold() == configured["brand_primary_1"]
    assert variables["--color-action-hover"].casefold() == configured["brand_primary_2"]
    assert variables["--color-focus"].casefold() == configured["brand_secondary_1"]
    assert variables["--color-accent"].casefold() == configured["brand_secondary_2"]

    token_pairs = (
        ("--color-action", "--color-on-action"),
        ("--color-action-hover", "--color-on-action-hover"),
        ("--color-focus", "--color-on-focus"),
        ("--color-accent", "--color-on-accent"),
    )
    for surface_token, foreground_token in token_pairs:
        assert foreground_token in variables
        assert _contrast_ratio(variables[surface_token], variables[foreground_token]) >= 4.5
    assert variables["--color-on-action"] == "#ffffff"
    assert variables["--color-on-action-hover"] == "#000000"


@pytest.mark.parametrize(
    ("path", "expected_status"),
    (
        ("/library", 200),
        ("/library/customer", 200),
        ("/queue", 200),
        ("/upload", 200),
        ("/library/not-configured", 404),
    ),
)
def test_every_html_surface_renders_theme_metadata_and_toggle(
    client: TestClient, path: str, expected_status: int
) -> None:
    response = client.get(path)

    assert response.status_code == expected_status
    assert '<meta name="color-scheme" content="light dark">' in response.text
    assert re.search(r"<html\b[^>]*\bdata-theme-default=\"system\"", response.text)
    assert 'href="/theme.css"' in response.text
    assert 'data-theme-toggle' in response.text

    theme_script = re.search(r"<script\b[^>]*src=\"/static/theme\.js\"[^>]*>", response.text)
    assert theme_script
    assert "defer" not in theme_script.group(0)
    assert "async" not in theme_script.group(0)
    assert theme_script.start() < response.text.index('href="/theme.css"')
    assert theme_script.start() < response.text.index('href="/static/app.css"')

    policy = response.headers["content-security-policy"]
    assert "script-src 'self'" in policy
    assert "style-src 'self'" in policy


def test_all_page_templates_inherit_the_shared_theme_shell() -> None:
    page_templates = sorted(TEMPLATE_ROOT.glob("*.html"))
    assert page_templates

    for template in page_templates:
        if template.name == "base.html":
            continue
        first_line = template.read_text(encoding="utf-8").splitlines()[0]
        assert first_line == '{% extends "base.html" %}', template


def test_component_assets_do_not_contain_literal_colors_outside_semantic_tokens() -> None:
    css = APP_CSS.read_text(encoding="utf-8")
    token_layer_end = css.index("\n*,\n")
    violations: list[str] = []
    for declaration in CSS_DECLARATION.finditer(css):
        if not RAW_COLOR_LITERAL.search(declaration["value"]):
            continue
        if (
            declaration.start() < token_layer_end
            and declaration["property"].startswith("--color-")
        ):
            continue
        line = css.count("\n", 0, declaration.start()) + 1
        violations.append(f"{APP_CSS.relative_to(REPOSITORY_ROOT)}:{line}")

    component_assets = [
        *sorted(STATIC_ROOT.glob("*.js")),
        *sorted(TEMPLATE_ROOT.rglob("*.html")),
    ]
    for asset in component_assets:
        text = asset.read_text(encoding="utf-8")
        for match in RAW_COLOR_LITERAL.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            violations.append(f"{asset.relative_to(REPOSITORY_ROOT)}:{line}")

    assert violations == [], "literal component colors found:\n" + "\n".join(violations)


def test_neutral_surface_text_does_not_depend_on_arbitrary_brand_contrast() -> None:
    css = APP_CSS.read_text(encoding="utf-8")

    assert css.count("--color-interactive-text: var(--color-text);") == 1
    assert css.count("--color-interactive-text-hover: var(--color-text);") == 1
    assert "--color-interactive-text: var(--color-action);" not in css
    assert "--color-interactive-text: var(--color-accent);" not in css


def test_static_svg_color_exceptions_are_explicit_and_cannot_grow() -> None:
    allowed_literals = {
        "favicon.svg": {"#173f34", "#fffdf8", "#d5a846"},
    }

    for asset in sorted(STATIC_ROOT.glob("*.svg")):
        literals = {
            match.group(0).casefold()
            for match in re.finditer(
                r"#[0-9a-fA-F]{6}(?![0-9a-fA-F])", asset.read_text(encoding="utf-8")
            )
        }
        assert literals == allowed_literals.get(asset.name, set()), asset
