"""Isolated regressions from the independent review; no production records."""
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from threading import Event, Thread
import json
import pytest
from career_radar import core
NOW = datetime(2026, 10, 31, 14, tzinfo=timezone.utc)


def record(**changes):
    return dict(source_id="employer", source_job_id="first", source="Employer", source_type="greenhouse",
                title="Junior Software Developer", company="Fixture Inc", location="Toronto, ON",
                url="https://example.com/first", verification="direct", is_closed=False,
                description="Computer science undergraduate. Canadian citizenship and security clearance required.",
                deadline="2026-11-02", deadline_evidence="Application deadline: 2026-11-02") | changes


def other(**changes):
    # An exact canonical application URL establishes identity; matching titles do not.
    return record(source_id="curated", source_job_id="curated-first", source="Curated list",
                  source_type="simplify", url="https://example.com/first?utm_source=curated", verification="curated",
                  description="Early-career software opportunity.", deadline="2026-11-20",
                  deadline_evidence="Application deadline: 2026-11-20") | changes


@pytest.mark.parametrize("reverse", [False, True])
def test_direct_open_and_its_deadline_and_warnings_win_in_both_orders(tmp_path, reverse):
    db = tmp_path / "state.db"
    rows = [record(), other(is_closed=True)]
    for row in reversed(rows) if reverse else rows:
        core.upsert_jobs(db, [row], NOW)
    jobs = core.all_jobs(db)
    assert len(jobs) == 1
    job = jobs[0]
    assert job["active"] is True
    assert job["verification"] == "direct"
    assert job["deadline_date"] == "2026-11-02"
    assert any("Citizenship" in n for n in job["eligibility_notes"])
    assert any("screening" in n for n in job["eligibility_notes"])
    assert len(job["sources"]) == 2


@pytest.mark.parametrize("reverse", [False, True])
def test_direct_closed_cannot_be_reopened_by_curated_copy(tmp_path, reverse):
    db = tmp_path / "state.db"
    rows = [record(is_closed=True), other()]
    for row in reversed(rows) if reverse else rows:
        core.upsert_jobs(db, [row], NOW)
    assert len(core.all_jobs(db)) == 1
    assert core.all_jobs(db)[0]["active"] is False


def test_distinct_requisitions_do_not_share_application_status(tmp_path):
    db = tmp_path / "state.db"
    first = core.upsert_jobs(db, [record()], NOW)["new_ids"][0]
    core.set_status(db, first, "applied")
    result = core.upsert_jobs(db, [record(source_job_id="second", url="https://example.com/second")], NOW)
    assert result["new"] == 1
    assert len(core.all_jobs(db)) == 2
    assert core.get_job(db, first)["status"] == "applied"
    assert core.get_job(db, result["new_ids"][0])["status"] == "new"


def test_two_direct_boards_do_not_collapse_different_requisitions(tmp_path):
    db = tmp_path / "state.db"
    core.upsert_jobs(db, [record()], NOW)
    result = core.upsert_jobs(db, [record(source_id="employer-campus", source_job_id="second", url="https://example.com/second")], NOW)
    assert result["new"] == 1


def test_authoritative_qualification_change_disables_curated_copy(tmp_path):
    db = tmp_path / "state.db"
    core.upsert_jobs(db, [record(), other()], NOW)
    core.upsert_jobs(db, [record(title="Senior Software Developer")], NOW)
    assert core.all_jobs(db)[0]["active"] is False


def test_complete_snapshot_closes_missing_alias_but_not_other_requisition(tmp_path):
    db = tmp_path / "state.db"
    core.upsert_jobs(db, [record(), record(source_job_id="second", url="https://example.com/second")], NOW)
    core.reconcile_source(db, "employer", {"second"}, now=NOW + timedelta(hours=12))
    jobs = core.all_jobs(db)
    assert len(jobs) == 2
    assert {j["source_job_id"] for j in jobs if j["active"]} == {"second"}


def test_expiry_cannot_lose_concurrent_user_save(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    job_id = core.upsert_jobs(db, [record()], NOW)["new_ids"][0]
    original = core.connection
    snapshot, attempted, saved = Event(), Event(), Event()
    errors = []

    class Cursor:
        def __init__(self, cursor):
            self.cursor = cursor
        def fetchall(self):
            rows = self.cursor.fetchall()
            snapshot.set()
            assert attempted.wait(2)
            saved.wait(0.25)  # A proper write transaction blocks this writer until expiry commits.
            return rows

    class Connection:
        def __init__(self, conn):
            self.conn = conn
        def execute(self, statement, params=()):
            cursor = self.conn.execute(statement, params)
            return Cursor(cursor) if statement == "SELECT id,data FROM jobs" else cursor
        def __getattr__(self, name):
            return getattr(self.conn, name)

    @contextmanager
    def intercepted(path=core.DEFAULT_DB):
        with original(path) as conn:
            yield Connection(conn)

    monkeypatch.setattr(core, "connection", intercepted)
    def save():
        try:
            assert snapshot.wait(2)
            attempted.set()
            core.set_status(db, job_id, "saved")
            saved.set()
        except BaseException as exc:
            errors.append(exc)
    thread = Thread(target=save)
    thread.start()
    core.expire_stale(db, now=NOW + timedelta(days=5))
    thread.join(3)
    assert not thread.is_alive() and not errors
    assert core.get_job(db, job_id)["status"] == "saved"
    assert core.get_job(db, job_id)["active"] is False
