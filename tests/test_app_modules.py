"""Registry layering, user sources, overrides, settings, scheduler and scout (all offline)."""
from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from career_radar import config, registry, scheduler, scout, settings

TZ = ZoneInfo("America/Toronto")


def discovered_file(data, *sources):
    config.write_json(data / config.DISCOVERED_FILE, {"schema_version": 1, "sources": list(sources)})


def board(ident, slug, kind="greenhouse", **extra):
    return {"id": ident, "name": slug.title(), "type": kind, "url": f"https://job-boards.greenhouse.io/{slug}",
            "board": slug, **extra}


# --- registry --------------------------------------------------------------------------

def test_layers_are_tagged_and_builtin_wins_duplicates(tmp_path):
    builtin = registry.builtin_sources()
    first = next(s for s in builtin if s.get("type") == "greenhouse" and s.get("board"))
    discovered_file(tmp_path, board("dup", first["board"]), board("fresh-board", "fresh"))
    rows = registry.load_registry(tmp_path)
    by_id = {s["id"]: s for s in rows}
    assert by_id[first["id"]]["origin"] == "builtin"
    assert "dup" not in by_id  # same board as a built-in entry
    assert by_id["fresh-board"]["origin"] == "discovered"
    assert len(rows) == len(builtin) + 1


def test_invalid_discovered_file_never_disables_builtin(tmp_path):
    (tmp_path / config.DISCOVERED_FILE).write_text("{not json")
    assert len(registry.load_registry(tmp_path)) == len(registry.builtin_sources())


def test_disable_and_reenable_any_source(tmp_path):
    ident = registry.builtin_sources()[0]["id"]
    registry.update_source(ident, {"enabled": False}, tmp_path)
    assert registry.find(ident, tmp_path)["enabled"] is False
    registry.update_source(ident, {"enabled": True}, tmp_path)
    assert registry.find(ident, tmp_path)["enabled"] is True


def test_builtin_cannot_be_removed_or_renamed(tmp_path):
    ident = registry.builtin_sources()[0]["id"]
    with pytest.raises(registry.RegistryError):
        registry.remove_source(ident, tmp_path)
    with pytest.raises(registry.RegistryError):
        registry.update_source(ident, {"name": "Mine"}, tmp_path)


def test_removed_discovered_board_stays_removed(tmp_path):
    discovered_file(tmp_path, board("acme-gh", "acme"))
    registry.remove_source("acme-gh", tmp_path)
    assert registry.find("acme-gh", tmp_path) is None
    from career_radar import discover
    key = discover.registry_key(board("acme-gh", "acme"))
    assert key in registry.removed_keys(tmp_path)
    # Discovery treats removed keys as known, so a new run never re-adds the board.
    discovered_file(tmp_path, board("acme-gh", "acme"))
    assert registry.find("acme-gh", tmp_path) is None


def test_user_search_add_edit_remove(tmp_path):
    source = registry.search_source("linkedin", "  firmware   intern ", None)
    assert source["keywords"] == "firmware intern" and source["canada_only"] is True
    saved = registry.add_user_source(source, tmp_path)
    assert saved["origin"] == "user" and saved["added_by_user"] is True
    with pytest.raises(registry.RegistryError) as clash:
        registry.add_user_source(registry.search_source("linkedin", "Firmware Intern"), tmp_path)
    assert clash.value.status == 409
    updated = registry.update_source(saved["id"], {"keywords": "embedded intern", "name": "Embedded"}, tmp_path)
    assert updated["keywords"] == "embedded intern" and updated["name"] == "Embedded"
    registry.remove_source(saved["id"], tmp_path)
    assert registry.find(saved["id"], tmp_path) is None


@pytest.mark.parametrize("kind,keywords", [("nope", "python"), ("jobbank", "x"), ("jobbank", "y" * 81)])
def test_search_source_validation(kind, keywords):
    with pytest.raises(registry.RegistryError):
        registry.search_source(kind, keywords)


def test_readding_a_removed_board_clears_the_removal(tmp_path):
    from career_radar import boards_check, discover
    discovered_file(tmp_path, board("acme-gh", "acme"))
    registry.remove_source("acme-gh", tmp_path)
    source = boards_check.board_source("https://job-boards.greenhouse.io/acme", "Acme", data=tmp_path)
    registry.add_user_source(source, tmp_path)
    assert discover.registry_key(source) not in registry.removed_keys(tmp_path)


def test_board_source_rejects_unsupported_urls(tmp_path):
    from career_radar import boards_check
    with pytest.raises(registry.RegistryError):
        boards_check.board_source("https://example.com/careers", data=tmp_path)


def test_board_check_uses_injected_fetcher_and_flags_monitored(tmp_path):
    from career_radar import boards_check

    def fetcher(source):
        return [{"title": "Software Developer Intern", "company": "Acme", "location": "Toronto, ON, Canada",
                 "url": "https://job-boards.greenhouse.io/acme/jobs/1", "description": "Summer 2027 internship."}]

    builtin = next(s for s in registry.builtin_sources() if s.get("type") == "greenhouse" and s.get("board"))
    result = boards_check.check(["https://job-boards.greenhouse.io/acme", builtin["url"], "https://example.com/jobs"],
                                data=tmp_path, fetcher=fetcher)
    assert result["unsupported"] == ["https://example.com/jobs"]
    statuses = {c["key"]: c["status"] for c in result["candidates"]}
    assert "already_monitored" in statuses.values()
    acme = next(c for c in result["candidates"] if "acme" in c["key"])
    assert acme["status"] in {"verified", "no_canada_tech"}


# --- settings -----------------------------------------------------------------------------

def test_schedule_validation_and_defaults(tmp_path):
    saved = settings.save({"schedule": {"collect_times": ["18:00", "07:30", "07:30"], "scout_enabled": True}}, tmp_path)
    assert saved["schedule"]["collect_times"] == ["07:30", "18:00"]
    assert saved["schedule"]["scout_enabled"] is True
    assert saved["schedule"]["discovery_time"] == settings.DEFAULT_SCHEDULE["discovery_time"]
    for bad in ({"collect_times": ["25:00"]}, {"discovery_time": "4:30"}, {"scout_themes_per_run": 9},
                {"collection_budget_minutes": 1}, {"collect_times": "08:00"}):
        with pytest.raises(settings.SettingsError):
            settings.save({"schedule": bad}, tmp_path)


def test_corrupt_settings_fall_back_to_defaults(tmp_path):
    (tmp_path / config.SETTINGS_FILE).write_text('{"schedule": {"collect_times": ["99:99"]}}')
    assert settings.load(tmp_path)["schedule"] == settings.DEFAULT_SCHEDULE


def test_keys_are_private_masked_and_env_wins(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    settings.set_api_key("openai", "sk-test-1234567890abcd", tmp_path)
    mode = stat.S_IMODE(os.stat(tmp_path / config.SECRETS_FILE).st_mode)
    assert mode == 0o600
    status = settings.secret_status(tmp_path)["openai"]
    assert status == {"set": True, "source": "file", "hint": "…abcd"}
    assert "1234567890" not in json.dumps(settings.secret_status(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-zzzzzzzzzzzz9999")
    assert settings.api_key("openai", tmp_path) == "sk-env-zzzzzzzzzzzz9999"
    assert settings.secret_status(tmp_path)["openai"]["source"] == "env"
    settings.set_api_key("openai", None, tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY")
    assert settings.api_key("openai", tmp_path) is None


def test_bad_keys_and_providers_are_rejected(tmp_path):
    with pytest.raises(settings.SettingsError):
        settings.set_api_key("openai", "has spaces in it", tmp_path)
    with pytest.raises(settings.SettingsError):
        settings.set_api_key("codex", "anything", tmp_path)
    with pytest.raises(settings.SettingsError):
        settings.save({"llm": {"provider": "not-a-provider"}}, tmp_path)
    with pytest.raises(settings.SettingsError):
        settings.save({"llm": {"provider": "openai-compatible", "base_url": "ftp://x"}}, tmp_path)


# --- scheduler -------------------------------------------------------------------------------

class Clock:
    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now


def test_next_run_handles_dst_and_ordering():
    now = datetime(2026, 11, 1, 5, 0, tzinfo=timezone.utc)  # 01:00 EDT, the DST fall-back day
    upcoming = scheduler.next_run(["08:00", "18:00"], now, TZ)
    assert upcoming.astimezone(TZ).hour == 8 and upcoming.astimezone(TZ).date().isoformat() == "2026-11-01"
    assert scheduler.next_run([], now, TZ) is None
    with pytest.raises(ValueError):
        scheduler.parse_times(["8:00"])


def test_tick_runs_due_jobs_once_and_never_replays_backlog():
    start = datetime(2026, 11, 2, 12, 59, tzinfo=TZ)
    clock = Clock(start)
    calls = []
    sched = scheduler.Scheduler({"collect": lambda: calls.append(clock.now) or (True, "ok")},
                                lambda: {"collect": ["13:00"]}, tz=TZ, clock=clock)
    sched.tick()  # first tick only records the time
    assert calls == []
    clock.now = start + timedelta(minutes=2)
    sched.tick()
    for _ in range(100):
        if not sched.is_running("collect"):
            break
        import time
        time.sleep(0.01)
    assert len(calls) == 1
    clock.now = start + timedelta(days=3)  # long suspend: one run, not three
    sched.tick()
    for _ in range(100):
        if not sched.is_running("collect"):
            break
        import time
        time.sleep(0.01)
    assert len(calls) == 2
    status = sched.status()["jobs"]["collect"]
    assert status["last_ok"] is True and status["last_summary"] == "ok"


def test_failures_are_recorded_and_bad_plans_ignored():
    results = {}
    sched = scheduler.Scheduler({"collect": lambda: 1 / 0, "discovery": lambda: (True, "x")},
                                lambda: {"collect": ["08:00"], "discovery": ["bad"]}, tz=TZ,
                                on_result=lambda name, r: results.setdefault(name, r))
    assert sched.run_now("collect", wait=True) is True
    assert results["collect"]["ok"] is False and "ZeroDivisionError" in results["collect"]["summary"]
    assert sched.status()["jobs"]["discovery"]["times"] == []


def test_run_now_refuses_overlap():
    import threading
    gate = threading.Event()
    sched = scheduler.Scheduler({"collect": lambda: gate.wait(5)}, lambda: {}, tz=TZ)
    assert sched.run_now("collect") is True
    assert sched.run_now("collect") is False
    gate.set()


# --- scout --------------------------------------------------------------------------------------

class FakeLLM:
    def __init__(self, replies):
        self.replies, self.prompts = list(replies), []

    def complete_json(self, system, user, *, max_tokens=2500):
        self.prompts.append(user)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def test_scout_writes_clean_leads_and_survives_a_failed_theme(tmp_path):
    client = FakeLLM([
        {"leads": [{"company": "Orbit Labs", "board_url": "https://jobs.lever.co/orbitlabs", "ats": "lever",
                    "canada_evidence": "Montreal, QC"},
                   {"company": "No URL Inc", "board_url": "javascript:alert(1)"},
                   {"company": "", "board_url": "https://jobs.lever.co/x"},
                   "junk"]},
        RuntimeError("rate limited"),
    ])
    result = scout.run(client, root=tmp_path, themes=2)
    assert result["lead_count"] == 1 and result["suggestions"] == 1
    assert len(result["errors"]) == 1 and "rate limited" in result["errors"][0]
    lead = json.loads((tmp_path / "leads").glob("scout-*.json").__next__().read_text())
    assert lead["leads"][0]["board_url"] == "https://jobs.lever.co/orbitlabs"
    assert "Already known boards" in client.prompts[0]


def test_scout_raises_when_every_theme_fails(tmp_path):
    with pytest.raises(RuntimeError):
        scout.run(FakeLLM([RuntimeError("down")]), root=tmp_path, themes=1)
