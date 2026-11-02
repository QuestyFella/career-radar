"""Collection integration tests: fixtures only, no network or live database."""
from career_radar import core
from career_radar.collect import run_collection


def test_collection_records_true_counts_and_failure_without_data_loss(tmp_path):
    db = tmp_path / "collect.db"
    sources = [{"id":"ok", "name":"Fixture board", "type":"fixture", "url":"https://example.com/board"},
               {"id":"blocked", "name":"Restricted fixture", "type":"fixture", "url":"https://example.net"}]
    def fetch(source):
        if source["id"] == "blocked":
            raise RuntimeError("HTTP 403 login required")
        return [{"source_id":"ok", "source":"Fixture board", "source_job_id":"1", "source_type":"fixture",
                 "company":"Fixture Corp", "title":"Junior Developer", "location":"Toronto, ON",
                 "url":"https://example.com/jobs/1", "description":"0-2 years experience"}]
    report = run_collection(db, registry=sources, fetcher=fetch)
    assert report["sources_total"] == 2
    assert report["sources_ok"] == 1
    assert report["sources_failed"] == 1
    assert report["new_jobs"] == 1
    assert core.stats(db)["total"] == 1
    assert core.stats(db)["collection_running"] is False
    assert core.sources(db)[0]["status"] == "blocked"
    second = run_collection(db, registry=sources, fetcher=fetch)
    assert second["new_jobs"] == 0
    assert core.stats(db)["total"] == 1


def test_collection_enforces_unique_registry_ids(tmp_path):
    import pytest
    src = {"id":"dup", "name":"Fixture", "type":"fixture", "url":"https://example.com"}
    with pytest.raises(ValueError, match="Duplicate"):
        run_collection(tmp_path / "dup.db", registry=[src,src], fetcher=lambda _: [])


def test_source_malformed_payload_is_not_silent_success(tmp_path):
    db = tmp_path / "bad.db"
    src = {"id":"bad", "name":"Fixture", "type":"fixture", "url":"https://example.com"}
    report = run_collection(db, registry=[src], fetcher=lambda _: {"error":"unavailable"})
    assert report["sources_failed"] == 1
    assert core.sources(db)[0]["status"] == "error"


def test_exclusive_lock_leaves_existing_running_collection_untouched(tmp_path):
    import fcntl
    db = tmp_path / "locked.db"
    lock = db.with_suffix(".collect.lock")
    with lock.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        report = run_collection(db, registry=[], fetcher=lambda _: [])
        assert report["skipped"] is True


def test_bounded_sources_keep_real_rows_and_partial_status(tmp_path):
    class Partial(RuntimeError):
        status = "partial"
        partial_jobs = [{"source_id":"partial", "source":"Partial fixture", "source_job_id":"1", "source_type":"fixture",
                         "company":"Fixture Co", "title":"Junior Developer", "location":"Toronto, Canada",
                         "url":"https://example.com/partial/1", "description":"0-2 years experience"}]
    def fetch(_):
        raise Partial("cap reached: first page only")
    db = tmp_path / "partial.db"
    src = {"id":"partial", "name":"Partial fixture", "type":"fixture", "url":"https://example.com", "coverage_note":"LIMITED first page"}
    report = run_collection(db, registry=[src], fetcher=fetch)
    assert report["new_jobs"] == 1
    assert core.list_jobs(db)["total"] == 1
    source = core.sources(db)[0]
    assert source["status"] == "partial" and source["raw_count"] == 1
    assert "cap reached" in source["error"]
    assert source["coverage"] == "LIMITED first page"


def test_disabled_source_is_not_claimed_as_blocked(tmp_path):
    db = tmp_path / "disabled.db"
    src = {"id":"disabled", "name":"Disabled fixture", "type":"fixture", "url":"https://example.com", "enabled":False, "disabled_reason":"No useful Canadian coverage"}
    report = run_collection(db, registry=[src], fetcher=lambda _: [])
    assert report["sources_failed"] == 0
    assert report["sources_disabled"] == 1
    assert core.sources(db)[0]["status"] == "disabled"


def test_only_complete_ats_snapshots_close_missing_jobs(tmp_path):
    db = tmp_path / "closure.db"
    source = {"id":"complete", "name":"Complete ATS", "type":"greenhouse", "url":"https://example.com"}
    row = {"source_job_id":"1", "company":"Fixture", "title":"Junior Developer", "location":"Toronto, ON",
           "url":"https://example.com/one", "verification":"direct"}
    run_collection(db, registry=[source], fetcher=lambda _: [dict(row)])
    class Partial(RuntimeError):
        status = "partial"
        partial_jobs = []
    def partial(_):
        raise Partial("bounded incomplete page")
    run_collection(db, registry=[source], fetcher=partial)
    assert core.list_jobs(db)["total"] == 1
    run_collection(db, registry=[source], fetcher=lambda _: [])
    assert core.list_jobs(db)["total"] == 0


def test_new_job_count_does_not_include_imported_closed_history(tmp_path):
    db = tmp_path / "history.db"
    source = {"id":"curated", "name":"Curated", "type":"simplify", "url":"https://example.com"}
    row = {"source_job_id":"1", "company":"Fixture", "title":"Junior Developer", "location":"Toronto, ON",
           "url":"https://example.com/one", "verification":"curated", "is_closed":True}
    report = run_collection(db, registry=[source], fetcher=lambda _: [row])
    assert report["new_jobs"] == 0
    assert core.list_jobs(db)["total"] == 0


def test_public_feed_cooldown_avoids_refetch_without_faking_freshness(tmp_path):
    db = tmp_path / "cooldown.db"
    source = {"id":"paced", "name":"Rate limited", "type":"remotive", "url":"https://example.com", "min_interval_seconds":21600}
    calls = []
    def fetch(_):
        calls.append(1)
        return []
    run_collection(db, registry=[source], fetcher=fetch)
    prior = core.sources(db)[0]["last_checked"]
    report = run_collection(db, registry=[source], fetcher=fetch)
    assert len(calls) == 1
    assert core.sources(db)[0]["last_checked"] == prior
    assert report["sources_cached"] == 1


def _rotation_source(n):
    return {"id": f"s{n}", "name": f"Board {n}", "type": "fixture", "url": f"https://example.com/{n}"}


def _rotation_rows(source):
    import time
    time.sleep(0.05)
    return [{"source_job_id": source["id"], "company": source["name"], "title": "Junior Developer",
             "location": "Toronto, ON", "url": f"{source['url']}/job", "description": "0-2 years experience"}]


def test_time_budget_defers_unstarted_sources_without_touching_their_jobs(tmp_path):
    db = tmp_path / "budget.db"
    sources = [_rotation_source(n) for n in range(6)]
    run_collection(db, registry=sources, fetcher=_rotation_rows)
    assert core.stats(db)["total"] == 6
    calls = []
    report = run_collection(db, registry=sources, fetcher=lambda s: calls.append(s["id"]) or _rotation_rows(s),
                            workers=1, max_seconds=0)
    assert calls == [] and report["sources_deferred"] == 6 and report["sources_failed"] == 0
    assert {s["status"] for s in core.sources(db)} == {"ok"}  # health of deferred boards is unchanged
    assert core.stats(db)["total"] == 6


def test_time_budget_rotates_stalest_sources_first(tmp_path):
    db = tmp_path / "rotate.db"
    sources = [_rotation_source(n) for n in range(4)]
    run_collection(db, registry=sources[:2], fetcher=_rotation_rows)  # s0, s1 already checked
    order = []
    run_collection(db, registry=sources, fetcher=lambda s: order.append(s["id"]) or _rotation_rows(s), workers=1)
    assert order == ["s0", "s1", "s2", "s3"]  # no budget: registry order
    order.clear()
    run_collection(db, registry=sources, fetcher=lambda s: order.append(s["id"]) or _rotation_rows(s),
                   workers=1, max_seconds=60)
    assert order == ["s0", "s1", "s2", "s3"]  # equal staleness keeps registry order
    order.clear()
    run_collection(db, registry=sources[2:], fetcher=_rotation_rows)  # s2, s3 now fresher
    run_collection(db, registry=sources, fetcher=lambda s: order.append(s["id"]) or _rotation_rows(s),
                   workers=1, max_seconds=60)
    assert order[:2] == ["s0", "s1"]


def test_full_collection_retires_health_of_removed_sources(tmp_path, monkeypatch):
    from career_radar import collect
    db = tmp_path / "retire.db"
    sources = [_rotation_source(n) for n in range(3)]
    run_collection(db, registry=sources, fetcher=_rotation_rows)
    # A subset run (tests, targeted re-collections) never retires anything.
    report = run_collection(db, registry=sources[:1], fetcher=_rotation_rows)
    assert report["sources_retired"] == [] and len(core.sources(db)) == 3
    monkeypatch.setattr(collect, "load_registry", lambda: sources[:2])  # s2 dropped from the registry
    report = run_collection(db, fetcher=_rotation_rows)
    assert report["sources_retired"] == ["s2"]
    assert sorted(s["id"] for s in core.sources(db)) == ["s0", "s1"]
    assert core.stats(db)["sources_total"] == 2
    assert core.stats(db)["total"] == 3  # its job stays until it expires unseen

