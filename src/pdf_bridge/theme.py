"""Deployment brand colors rendered as semantic CSS custom properties."""

from __future__ import annotations

from pdf_bridge.config import Settings

MINIMUM_TEXT_CONTRAST = 4.5
FOREGROUND_CANDIDATES = ("#000000", "#ffffff")


def _relative_luminance(color: str) -> float:
    channels = [int(color[offset : offset + 2], 16) / 255 for offset in (1, 3, 5)]
    linear = [
        channel / 12.92
        if channel <= 0.04045
        else ((channel + 0.055) / 1.055) ** 2.4
        for channel in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast_ratio(first: str, second: str) -> float:
    """Return the WCAG contrast ratio between two validated opaque RGB colors."""

    lighter, darker = sorted(
        (_relative_luminance(first), _relative_luminance(second)), reverse=True
    )
    return (lighter + 0.05) / (darker + 0.05)


def accessible_foreground(background: str) -> str:
    """Choose the black or white foreground with the strongest WCAG contrast."""

    foreground = max(
        FOREGROUND_CANDIDATES,
        key=lambda candidate: contrast_ratio(background, candidate),
    )
    if contrast_ratio(background, foreground) < MINIMUM_TEXT_CONTRAST:
        raise ValueError(f"no accessible foreground is available for brand color {background}")
    return foreground


def render_theme_css(settings: Settings) -> str:
    """Render the validated deployment palette as semantic CSS tokens."""

    colors = {
        "action": settings.brand_primary_1,
        "action-hover": settings.brand_primary_2,
        "focus": settings.brand_secondary_1,
        "accent": settings.brand_secondary_2,
    }
    declarations = [
        *(f"  --color-{name}: {color};" for name, color in colors.items()),
        *(
            f"  --color-on-{name}: {accessible_foreground(color)};"
            for name, color in colors.items()
        ),
    ]
    return ":root {\n" + "\n".join(declarations) + "\n}\n"
