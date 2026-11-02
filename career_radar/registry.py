"""Source and board registry: built-in curated boards, discovered boards and user boards.

Layers (first wins when two entries name the same board):
  1. built-in curated boards shipped in ``career_radar/boards/`` (read-only),
  2. boards and keyword searches the user added (``<data>/user_sources.json``),
  3. boards found by discovery (``<data>/discovered_sources.json``).

User choices never edit layer 1 or 3 in place; they are stored as overrides
(``<data>/source_overrides.json``): per-source ``enabled`` flags and ``removed`` boards.
Removed discovered boards are filtered out at load time, dropped from the discovered file on
the next discovery run, and never re-added by discovery.
"""
from __future__ import annotations

import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import config

BUILTIN_FILES = ("sources.json", "extra_sources.json")
SEARCH_TYPES = {
    "linkedin": {"label": "LinkedIn public job search", "url": "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search",
                 "defaults": {"max_pages": 2, "detail_limit": 5},
                 "note": "LinkedIn public guest search for your region and role levels; bounded pages, "
                         "may be rate limited. Added by you."},
    "jobbank": {"label": "Job Bank Canada search", "url": "https://www.jobbank.gc.ca/jobsearch/jobsearch",
                "defaults": {"max_pages": 2, "detail_limit": 50},
                "note": "Government of Canada Job Bank keyword search; bounded pages. Added by you."},
}
BOARD_LABELS = {
    "greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby", "workday": "Workday",
    "smartrecruiters": "SmartRecruiters", "workable": "Workable", "recruitee": "Recruitee", "bamboohr": "BambooHR",
}
OTHER_LABELS = {
    "simplify": "SimplifyJobs list", "frogg": "Frogg", "themuse": "The Muse", "remotive": "Remotive",
    "remoteok": "Remote OK", "arbeitnow": "Arbeitnow", "sitemap_jobposting": "Employer sitemap",
    "employer_jsonld": "Employer page", "jsonld": "Structured-data page",
}
_lock = threading.RLock()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _rows(document):
    rows = document.get("sources", []) if isinstance(document, dict) else document if isinstance(document, list) else []
    return [s for s in rows if isinstance(s, dict)]


def _valid(source):
    return all(source.get(k) for k in ("id", "name", "type", "url"))


def source_identity(source):
    """Board identity used to keep one entry per employer board across registries."""
    import json
    kind = str(source.get("type") or "")
    if kind == "workday":
        return (kind, str(source.get("tenant") or "").lower(), str(source.get("site") or "").lower(),
                str(source.get("keywords") or "").lower(), json.dumps(source.get("facet_filters") or {}, sort_keys=True))
    board = source.get("board") or source.get("company_id") or source.get("board_token")
    if board:
        return (kind, str(board).lower())
    if kind in SEARCH_TYPES:
        return (kind, str(source.get("keywords") or "").strip().lower())
    return (kind, str(source.get("url") or "").rstrip("/").lower())


def builtin_sources():
    """Curated boards shipped with the app. A missing or invalid file is a packaging bug."""
    rows = []
    for filename in BUILTIN_FILES:
        path = Path(config.BUILTIN_REGISTRY) / filename
        if not path.is_file():
            raise RuntimeError(f"Missing source registry: {filename}")
        document = config.read_json(path, None)
        if isinstance(document, dict):
            document = document.get("sources")
        if not isinstance(document, list):
            raise ValueError(f"Invalid registry format: {filename}")
        rows.extend(document)
    return rows


def user_sources(data=None):
    return [s for s in _rows(config.read_json(Path(data or config.data_dir()) / config.USER_SOURCES_FILE, [])) if _valid(s)]


def discovered_sources(data=None):
    """Machine-verified additions; a bad file never disables curated sources."""
    return [s for s in _rows(config.read_json(Path(data or config.data_dir()) / config.DISCOVERED_FILE, {})) if _valid(s)]


def overrides(data=None):
    value = config.read_json(Path(data or config.data_dir()) / config.OVERRIDES_FILE, {})
    value = value if isinstance(value, dict) else {}
    enabled = value.get("enabled") if isinstance(value.get("enabled"), dict) else {}
    removed = value.get("removed") if isinstance(value.get("removed"), dict) else {}
    return {"enabled": {str(k): bool(v) for k, v in enabled.items()}, "removed": {str(k): v for k, v in removed.items() if isinstance(v, dict)}}


def _save_overrides(value, data=None):
    config.write_json(Path(data or config.data_dir()) / config.OVERRIDES_FILE, value)


def removed_keys(data=None):
    """Discovery keys (``registry_key``) of boards the user removed; discovery never re-adds them."""
    return {str(v.get("key")) for v in overrides(data)["removed"].values() if v.get("key")}


def curated_sources(data=None):
    """Boards discovery must treat as already monitored: built-in plus user-added."""
    return [s for s in builtin_sources() if isinstance(s, dict) and s.get("id")] + user_sources(data)


def load_registry(data=None, *, with_origin=True):
    """Every monitored source: built-in, user and discovered, deduplicated, with overrides applied."""
    data = Path(data or config.data_dir())
    state = overrides(data)
    removed = set(state["removed"])
    rows, seen_ids, seen_boards = [], set(), set()
    layers = (("builtin", builtin_sources()), ("user", user_sources(data)), ("discovered", discovered_sources(data)))
    for origin, layer in layers:
        for source in layer:
            if not isinstance(source, dict):
                continue
            if origin != "builtin":
                if not _valid(source) or source["id"] in seen_ids or source["id"] in removed:
                    continue
                identity = source_identity(source)
                if identity in seen_boards:
                    continue
            source = dict(source)
            if with_origin:
                source["origin"] = origin
            if source.get("id") in state["enabled"]:
                source["enabled"] = state["enabled"][source["id"]]
            rows.append(source)
            seen_ids.add(source.get("id"))
            seen_boards.add(source_identity(source))
    return rows


def type_catalog():
    items = [{"type": t, "label": label, "kind": "board"} for t, label in BOARD_LABELS.items()]
    items += [{"type": t, "label": spec["label"], "kind": "search"} for t, spec in SEARCH_TYPES.items()]
    return items


def find(source_id, data=None):
    return next((s for s in load_registry(data) if s.get("id") == source_id), None)


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:60] or "source"


def _unique(base, taken):
    ident, n = base, 2
    while ident in taken:
        ident, n = f"{base}-{n}", n + 1
    return ident


class RegistryError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def add_user_source(source, data=None):
    """Store a user board/search. ``source`` must already be a complete registry entry."""
    data = Path(data or config.data_dir())
    with _lock:
        current = load_registry(data)
        identity = source_identity(source)
        clash = next((s for s in current if source_identity(s) == identity), None)
        if clash:
            raise RegistryError(f"Already monitored as “{clash.get('name')}”.", status=409)
        source = dict(source)
        source["id"] = _unique(source.get("id") or _slug(source["name"]), {s.get("id") for s in current})
        source.update(added_by_user=True, added_at=_now())
        rows = user_sources(data) + [source]
        config.write_json(data / config.USER_SOURCES_FILE, {"schema_version": 1, "sources": rows})
        # A board the user re-adds after removing it is wanted again.
        state = overrides(data)
        key = source.get("discovery_key")
        if key:
            state["removed"] = {k: v for k, v in state["removed"].items() if v.get("key") != key}
            _save_overrides(state, data)
    return dict(source, origin="user")


def search_source(kind, keywords, name=None):
    spec = SEARCH_TYPES.get(kind)
    keywords = re.sub(r"\s+", " ", str(keywords or "")).strip()
    if not spec:
        raise RegistryError("Unsupported search type.")
    if not 2 <= len(keywords) <= 80:
        raise RegistryError("Keywords must be 2–80 characters.")
    label = "LinkedIn public" if kind == "linkedin" else "Job Bank Canada"
    return {"id": f"user-{kind}-{_slug(keywords)}", "name": (str(name or "").strip()[:120] or f"{label} — {keywords}"),
            "type": kind, "url": spec["url"], "keywords": keywords, "canada_only": True,
            "coverage_note": spec["note"], **spec["defaults"]}


def update_source(source_id, changes, data=None):
    data = Path(data or config.data_dir())
    with _lock:
        source = find(source_id, data)
        if not source:
            raise RegistryError("Source not found.", status=404)
        if "enabled" in changes and changes["enabled"] is not None:
            state = overrides(data)
            state["enabled"][source_id] = bool(changes["enabled"])
            _save_overrides(state, data)
        editable = {k: v for k, v in changes.items() if k in {"name", "keywords"} and v is not None}
        if editable:
            if source["origin"] != "user":
                raise RegistryError("Only boards you added can be renamed or edited.")
            rows = user_sources(data)
            for row in rows:
                if row["id"] == source_id:
                    if "name" in editable:
                        name = str(editable["name"]).strip()[:120]
                        if not name:
                            raise RegistryError("Name cannot be empty.")
                        row["name"] = name
                    if "keywords" in editable:
                        if row["type"] not in SEARCH_TYPES:
                            raise RegistryError("Only keyword searches have keywords.")
                        row["keywords"] = search_source(row["type"], editable["keywords"])["keywords"]
            config.write_json(data / config.USER_SOURCES_FILE, {"schema_version": 1, "sources": rows})
    return find(source_id, data)


def remove_source(source_id, data=None):
    from . import discover
    data = Path(data or config.data_dir())
    with _lock:
        source = find(source_id, data)
        if not source:
            raise RegistryError("Source not found.", status=404)
        if source["origin"] == "builtin":
            raise RegistryError("Built-in sources cannot be removed; disable them instead.")
        state = overrides(data)
        state["enabled"].pop(source_id, None)
        if source["origin"] == "user":
            rows = [s for s in user_sources(data) if s["id"] != source_id]
            config.write_json(data / config.USER_SOURCES_FILE, {"schema_version": 1, "sources": rows})
        else:
            state["removed"][source_id] = {"key": discover.registry_key(source), "name": source.get("name"),
                                           "removed_at": _now()}
        _save_overrides(state, data)
    return source_id
