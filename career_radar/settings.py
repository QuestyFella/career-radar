"""User settings (LLM choice, schedule) and write-only secrets (API keys).

``<data>/settings.json`` holds non-secret choices. ``<data>/secrets.json`` (mode 0600) holds API
keys entered in the web UI. An environment variable (e.g. ``OPENAI_API_KEY``) always wins over a
stored key, so Docker users can keep keys out of the data volume entirely. Keys are never
returned by the API, only whether one is set, where it comes from and its last 4 characters.
"""
from __future__ import annotations

import os
import re
import threading
from pathlib import Path

from . import config, regions

DEFAULT_SCHEDULE = {
    "enabled": True,
    "collect_times": ["08:00", "18:00"],
    "discovery_time": "04:30",
    "scout_enabled": False,
    "scout_time": "01:30",
    "scout_themes_per_run": 3,
    "collection_budget_minutes": 40,
    "discovery_budget_minutes": 15,
}
_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_lock = threading.RLock()


class SettingsError(ValueError):
    pass


def timezone_name():
    return os.environ.get("CAREER_RADAR_TIMEZONE") or os.environ.get("TZ_NAME") or "America/Toronto"


def _path(name, data=None):
    return Path(data or config.data_dir()) / name


def _time(value, field):
    value = str(value or "").strip()
    if not _TIME.match(value):
        raise SettingsError(f"{field}: use 24-hour HH:MM")
    return value


def _int(value, field, low, high):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise SettingsError(f"{field}: must be a whole number") from None
    if not low <= number <= high:
        raise SettingsError(f"{field}: must be between {low} and {high}")
    return number


def clean_schedule(value, base=None):
    schedule = dict(base or DEFAULT_SCHEDULE)
    value = value if isinstance(value, dict) else {}
    for key in ("enabled", "scout_enabled"):
        if key in value:
            schedule[key] = bool(value[key])
    if "collect_times" in value:
        times = value["collect_times"]
        if not isinstance(times, list) or len(times) > 12:
            raise SettingsError("collect_times: a list of at most 12 HH:MM times")
        schedule["collect_times"] = sorted({_time(t, "collect_times") for t in times})
    for key in ("discovery_time", "scout_time"):
        if key in value:
            schedule[key] = _time(value[key], key)
    if "scout_themes_per_run" in value:
        schedule["scout_themes_per_run"] = _int(value["scout_themes_per_run"], "scout_themes_per_run", 1, 6)
    if "collection_budget_minutes" in value:
        schedule["collection_budget_minutes"] = _int(value["collection_budget_minutes"], "collection_budget_minutes", 5, 180)
    if "discovery_budget_minutes" in value:
        schedule["discovery_budget_minutes"] = _int(value["discovery_budget_minutes"], "discovery_budget_minutes", 2, 120)
    return schedule


def clean_region(value, base=None):
    """Validate the search region; a missing value keeps Canada."""
    if value is None:
        return dict(base or regions.DEFAULT_REGION, terms=list((base or regions.DEFAULT_REGION).get("terms") or []))
    if not isinstance(value, dict):
        raise SettingsError("region: send an object with an id")
    cleaned = regions.normalize_region(value)
    if cleaned == regions.default_region() and str(value.get("id") or "").strip().casefold() not in {"", "ca", "can", "canada"}:
        raise SettingsError("region: unknown country or region")
    return cleaned


def load(data=None):
    raw = config.read_json(_path(config.SETTINGS_FILE, data), {})
    raw = raw if isinstance(raw, dict) else {}
    llm = raw.get("llm") if isinstance(raw.get("llm"), dict) else {}
    try:
        schedule = clean_schedule(raw.get("schedule"))
    except SettingsError:
        schedule = dict(DEFAULT_SCHEDULE)
    try:
        region = clean_region(raw.get("region"))
    except SettingsError:
        region = dict(regions.DEFAULT_REGION, terms=[])
    provider = str(llm.get("provider") or os.environ.get("CAREER_RADAR_LLM_PROVIDER") or "")
    model = str(llm.get("model") or os.environ.get("CAREER_RADAR_LLM_MODEL") or "")
    base_url = str(llm.get("base_url") or os.environ.get("CAREER_RADAR_LLM_BASE_URL") or "")
    return {"llm": {"provider": provider, "model": model, "base_url": base_url}, "schedule": schedule,
            "region": region}


def save(changes, data=None):
    from . import llm as llm_module
    with _lock:
        current = load(data)
        if "llm" in changes and changes["llm"] is not None:
            value = changes["llm"] if isinstance(changes["llm"], dict) else {}
            provider = str(value.get("provider", current["llm"]["provider"]) or "")
            if provider and provider not in llm_module.PROVIDERS:
                raise SettingsError("Unknown LLM provider")
            model = str(value.get("model", current["llm"]["model"]) or "").strip()[:200]
            base_url = str(value.get("base_url", current["llm"]["base_url"]) or "").strip()[:300]
            if base_url and not re.match(r"^https?://[^\s/]+", base_url):
                raise SettingsError("base_url: must start with http:// or https://")
            current["llm"] = {"provider": provider, "model": model, "base_url": base_url.rstrip("/")}
        if "schedule" in changes and changes["schedule"] is not None:
            current["schedule"] = clean_schedule(changes["schedule"], current["schedule"])
        if "region" in changes and changes["region"] is not None:
            current["region"] = clean_region(changes["region"], current["region"])
        config.write_json(_path(config.SETTINGS_FILE, data), current)
        return current


# --- secrets ---------------------------------------------------------------------------

def _stored_secrets(data=None):
    value = config.read_json(_path(config.SECRETS_FILE, data), {})
    return value if isinstance(value, dict) else {}


def api_key(provider_id, data=None):
    """The key for a provider: environment variable first, then the stored secret."""
    from . import llm as llm_module
    provider = llm_module.PROVIDERS.get(provider_id)
    if provider and provider.key_env and os.environ.get(provider.key_env):
        return os.environ[provider.key_env].strip()
    stored = _stored_secrets(data).get(provider_id)
    return str(stored).strip() if stored else None


def set_api_key(provider_id, key, data=None):
    from . import llm as llm_module
    provider = llm_module.PROVIDERS.get(provider_id)
    if not provider or provider.auth == "oauth":
        raise SettingsError("This provider does not use an API key")
    with _lock:
        secrets = _stored_secrets(data)
        if key is None or not str(key).strip():
            secrets.pop(provider_id, None)
        else:
            key = str(key).strip()
            if len(key) > 500 or re.search(r"\s", key):
                raise SettingsError("That does not look like an API key")
            secrets[provider_id] = key
        config.write_json(_path(config.SECRETS_FILE, data), secrets, private=True)


def secret_status(data=None):
    from . import llm as llm_module
    stored = _stored_secrets(data)
    status = {}
    for pid, provider in llm_module.PROVIDERS.items():
        if provider.auth == "oauth":
            continue
        env = provider.key_env and os.environ.get(provider.key_env)
        key = env or stored.get(pid)
        status[pid] = {"set": bool(key), "source": "env" if env else "file" if key else None,
                       "hint": ("…" + str(key)[-4:]) if key and len(str(key)) >= 12 else ("set" if key else None)}
    return status
