"""Accent-colour theming (offline)."""
from __future__ import annotations

import pytest

from career_radar import settings, theme


def test_clean_accent_accepts_empty_and_hex_only():
    assert theme.clean_accent("") == ""
    assert theme.clean_accent(None) == ""
    assert theme.clean_accent("#1F5F9E") == "#1f5f9e"
    for bad in ("blue", "#fff", "#12345", "1f5f9e", "#gggggg"):
        with pytest.raises(ValueError):
            theme.clean_accent(bad)


def test_palette_derives_readable_variables():
    variables = theme.palette("#1f5f9e")
    assert variables["--accent"].startswith("hsl(")
    assert variables["--accent-hover"].startswith("hsl(")
    assert variables["--sidebar"].startswith("hsl(")
    assert variables["--accent-contrast"] == "#fffef5"
    assert variables["--nav-active-bg"].startswith("hsl(")


def test_light_accents_are_deepened():
    light = theme.palette("#fff3a0")["--accent"]
    dark = theme.palette("#1f2937")["--accent"]
    assert light != dark
    # Derived accents never exceed 38% lightness, and bright picks are darkened further
    # until white button text stays readable.
    lightness = float(light.rsplit(" ", 1)[-1].rstrip("%)"))
    assert lightness <= 38


def test_css_contains_overrides_and_default_is_empty():
    assert theme.css("") == "/* Career Radar default theme */\n"
    css = theme.css("#9c3b5b")
    assert ":root {" in css and "--accent:" in css and "--sidebar:" in css


def test_appearance_settings_round_trip_and_validation(tmp_path):
    saved = settings.save({"appearance": {"accent": "#1F5F9E"}}, tmp_path)
    assert saved["appearance"] == {"accent": "#1f5f9e"}
    assert settings.load(tmp_path)["appearance"]["accent"] == "#1f5f9e"
    assert settings.save({"appearance": {"accent": ""}}, tmp_path)["appearance"] == {"accent": ""}
    with pytest.raises(settings.SettingsError):
        settings.save({"appearance": {"accent": "not-a-color"}}, tmp_path)
