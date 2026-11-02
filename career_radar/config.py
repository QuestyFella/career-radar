"""Runtime paths and settings for a self-hosted Career Radar.

Everything mutable lives under one data directory (``CAREER_RADAR_DATA``, default ``./data``;
``/data`` in the Docker image), so a single volume holds the database, the board registries,
discovery state, the resume profile, settings and secrets.

Registries:
  * built-in curated boards ship read-only in ``career_radar/boards/`` (``sources.json`` and
    ``extra_sources.json``); user changes to them (enabled/disabled) are overrides, never edits;
  * ``<data>/discovered_sources.json`` holds boards found by discovery; it is seeded on first run
    from the bundled ``seed_discovered_sources.json`` and then maintained by discovery;
  * ``<data>/user_sources.json`` holds boards and searches the user added;
  * ``<data>/source_overrides.json`` holds per-source enable flags and removals.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent
BUILTIN_REGISTRY = PACKAGE / "boards"
STATIC = PACKAGE / "static"
SEED_DISCOVERED = BUILTIN_REGISTRY / "seed_discovered_sources.json"

DISCOVERED_FILE = "discovered_sources.json"
USER_SOURCES_FILE = "user_sources.json"
OVERRIDES_FILE = "source_overrides.json"
SETTINGS_FILE = "settings.json"
SECRETS_FILE = "secrets.json"
PROFILE_FILE = "profile.json"

_lock = threading.RLock()


def data_dir() -> Path:
    path = Path(os.environ.get("CAREER_RADAR_DATA") or os.environ.get("CAREER_DATA") or Path.cwd() / "data")
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    explicit = os.environ.get("CAREER_DB")
    return Path(explicit) if explicit else data_dir() / "career.db"


def discovery_dir(root: Path | None = None) -> Path:
    return Path(root or data_dir()) / "discovery"


def leads_dir(root: Path | None = None) -> Path:
    return Path(root or data_dir()) / "leads"


def read_json(path: Path, default):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default
    except (OSError, ValueError):
        return default


def write_json(path: Path, value, *, private: bool = False) -> None:
    """Atomic replace; ``private`` files (secrets) are created 0600."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            if private:
                os.chmod(tmp, 0o600)
            with os.fdopen(fd, "w") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=1)
                handle.write("\n")
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


def seed_data_dir(root: Path | None = None) -> None:
    """First run: copy the bundled discovered-board seed so discovery starts from a known set."""
    root = Path(root or data_dir())
    target = root / DISCOVERED_FILE
    if not target.exists() and SEED_DISCOVERED.is_file() and os.environ.get("CAREER_RADAR_SEED", "1") != "0":
        root.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SEED_DISCOVERED, target)
