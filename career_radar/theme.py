"""Accent-color palette for the web UI.

The saved appearance is a single ``#rrggbb`` accent. The server derives a small
set of readable CSS custom properties (hover, soft fill, sidebar tint and so on)
instead of letting the browser generate arbitrary low-contrast combinations.
Light picks are deepened enough for white text and accent links on paper.
"""
from __future__ import annotations

import colorsys
import re

DEFAULT_ACCENT = "#236b53"
_HEX = re.compile(r"^#[0-9a-f]{6}$")


def clean_accent(value) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if not _HEX.fullmatch(text):
        raise ValueError("accent: use a #rrggbb color")
    return text


def _rgb(accent: str):
    return tuple(int(accent[index:index + 2], 16) / 255 for index in (1, 3, 5))


def _hsl(hue: float, lightness: float, saturation: float) -> str:
    return f"hsl({round(hue * 360, 1)} {round(max(0.0, min(1.0, saturation)) * 100, 1)}% {round(max(0.0, min(1.0, lightness)) * 100, 1)}%)"


def _luminance(rgb) -> float:
    def channel(value):
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4
    red, green, blue = (channel(value) for value in rgb)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def palette(accent: str) -> dict[str, str]:
    """CSS custom properties for an accent color; empty for the built-in theme."""
    accent = clean_accent(accent)
    if not accent:
        return {}
    hue, lightness, saturation = colorsys.rgb_to_hls(*_rgb(accent))
    lightness = min(lightness, 0.38)
    saturation = min(saturation, 0.82)
    # Keep white button text and accent-colored links readable, even for bright picks.
    while lightness > 0.08 and _luminance(colorsys.hls_to_rgb(hue, lightness, saturation)) > 0.18:
        lightness -= 0.01
    return {
        "--accent": _hsl(hue, lightness, saturation),
        "--accent-hover": _hsl(hue, max(0.08, lightness - 0.09), saturation),
        "--accent-soft": _hsl(hue, 0.94, saturation * 0.45),
        "--accent-border": _hsl(hue, 0.76, saturation * 0.45),
        "--accent-contrast": "#fffef5",
        "--sidebar": _hsl(hue, 0.16, min(saturation, 0.36)),
        "--sidebar-line": _hsl(hue, 0.28, min(saturation, 0.28)),
        "--sidebar-text": _hsl(hue, 0.92, min(saturation, 0.12)),
        "--sidebar-muted": _hsl(hue, 0.66, min(saturation, 0.25)),
        "--sidebar-hover": _hsl(hue, 0.24, min(saturation, 0.32)),
        "--nav-text": _hsl(hue, 0.82, min(saturation, 0.10)),
        "--nav-active-bg": _hsl(hue, 0.88, min(saturation, 0.45)),
        "--nav-active-ink": _hsl(hue, 0.22, min(saturation, 0.40)),
    }


def css(accent: str) -> str:
    properties = palette(accent)
    if not properties:
        return "/* Career Radar default theme */\n"
    body = "\n".join(f"  {name}: {value};" for name, value in properties.items())
    return f":root {{\n{body}\n}}\n"
