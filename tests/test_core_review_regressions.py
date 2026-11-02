"""Durable fresh-review regressions; fixture state only, no real network."""
from datetime import datetime, timezone
from pathlib import Path
import json
import socket
import pytest
from career_radar import core
NOW = datetime(2026, 10, 31, 14, tzinfo=timezone.utc)
FIXTURE = Path(__file__).parent / "fixtures" / "legacy_lossy_ref_identity.json"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Core regression tests must not use a network")
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


def raw(**changes):
    return dict(source_id="fixture-employer", source_job_id="R1", source="Fixture employer",
                source_type="greenhouse", company="Fixture Aerospace", title="Junior Firmware Developer",
                location="Toronto, ON, Canada", url="https://jobs.example.org/view?ref=R1",
                description="Computer science. 0-2 years of experience.", verification="direct") | changes


@pytest.mark.parametrize("location", [
    "Remote — can work from anywhere", "Remote — Work can be performed worldwide",
    "Remote — Can work remotely", "Remote — not open to residents of Canada",
    "Remote — Canada residents are not eligible", "Remote — Canadian residents are not eligible",
])
def test_ordinary_can_and_resident_exclusions_do_not_establish_canada(location):
    assert core.normalize_job(raw(location=location), NOW) is None


@pytest.mark.parametrize("location", [
    "Remote — CAN", "Remote — Canada", "Remote — Canadian residents eligible",
    "Remote — Canada residents are eligible", "Toronto, ON", "Jordan, Ontario",
])
def test_canadian_positive_tokens_remain_eligible(location):
    assert core.normalize_job(raw(location=location), NOW) is not None


@pytest.mark.parametrize("location", [
    "Remote — not open to residents of Canada", "Remote — Canada residents are not eligible",
    "Remote — Canadian residents are not eligible",
])
def test_resident_exclusion_cannot_be_overridden_by_country_hint(location):
    assert core.normalize_job(raw(location=location, country_hint="CA"), NOW) is None


@pytest.mark.parametrize("description", [
    "This role requires a master's degree.", "This role requires a PhD.",
    "Candidates must be citizens of the United States.", "Only nationals of the United States may apply.",
    "A PhD is required and you will mentor bachelor's students in Computer Science.",
])
def test_mandatory_credential_grammar(description):
    assert core.normalize_job(raw(description=description), NOW) is None


@pytest.mark.parametrize("description", [
    "Minimum qualifications: bachelor's degree in Computer Science or master's degree in Computer Science.",
    "Minimum qualifications: master's degree in Computer Science or bachelor's degree in Software Engineering.",
    "This role does not require a master's degree.",
    "An undergraduate degree in Computer Science is required.",
    "This role requires an undergraduate degree in Computer Science.",
    "This role requires a bachelor's degree in Computer Science or a master's degree in Engineering.",
    "Bachelor's, Master's or PhD in Computer Science. Python experience.",
])
def test_local_qualified_degree_alternatives_and_negation(description):
    assert core.normalize_job(raw(description=description), NOW) is not None


CLOCKS = [
    ("Application deadline: 2026-11-06 at 4 PM.", "missing_timezone"),
    ("Application deadline: 2026-11-06 at 4 PM FOO.", "unknown_timezone"),
    ("Application deadline: 2026-11-06T21:14:59.1234567Z.", "invalid_clock"),
    ("Application deadline: 2026-11-01 at 1:30 AM ET.", "ambiguous_local_time"),
    ("Application deadline: 2027-03-14 at 2:30 AM ET.", "nonexistent_local_time"),
]


@pytest.mark.parametrize("prose,uncertainty", CLOCKS)
def test_uncertainty_does_not_change_priority_reason_type(prose, uncertainty):
    job = core.normalize_job(raw(description=prose), NOW)
    assert job["deadline_uncertainty"] == uncertainty
    assert isinstance(job["priority_reasons"], list)
    assert all(isinstance(reason, str) for reason in job["priority_reasons"])
    assert "Canadian early-career opportunity" in job["priority_reasons"]
    assert {"aerospace", "embedded"} <= set(job["tags"])
    assert any("confirm the exact cut-off" in note for note in job["eligibility_notes"])


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("prose,uncertainty", CLOCKS)
def test_deadline_enrichment_is_one_semantic_unit(tmp_path, reverse, prose, uncertainty):
    db = tmp_path / "enriched.db"
    employer = raw(description="Controlled goods screening. Computer science.")
    curated = raw(source_id="fixture-curated", source_job_id="C1", verification="curated",
                  description=prose + " Enrolled in a co-op program.")
    rows = [employer, curated]
    core.upsert_jobs(db, list(reversed(rows)) if reverse else rows, NOW)
    job, = core.all_jobs(db)
    assert job["deadline_uncertainty"] == uncertainty
    assert job["deadline_verification"] == "curated"
    assert job["deadline_evidence"] == prose
    assert any("confirm the exact cut-off" in note for note in job["eligibility_notes"])
    assert any("Security/export-control" in note for note in job["eligibility_notes"])
    assert not any("Check co-op" in note for note in job["eligibility_notes"])
    assert isinstance(job["priority_reasons"], list)


RBC = ("Application Deadline: 2026-11-08. Note: Applications will be accepted until 11:59 PM "
       "on the day prior to the application deadline date above.")


@pytest.mark.parametrize("value", ["2026-11-08", "2026-11-08T23:59:00+00:00"])
def test_published_day_prior_clause_beats_generic_metadata(value):
    job = core.normalize_job(raw(description=RBC, deadline=value,
                                 deadline_evidence="validThrough: " + value), NOW)
    assert job["deadline_kind"] == "date"
    assert job["deadline_date"] == "2026-11-07"
    assert job["deadline_uncertainty"] == "missing_timezone"
    assert job["deadline_evidence"] == RBC


def test_consistent_structured_time_can_refine_date_only_prose():
    job = core.normalize_job(raw(description="Application deadline: 2026-11-06.",
        deadline="2026-11-06T21:14:59.123456Z", deadline_evidence="validThrough: 2026-11-06T21:14:59.123456Z"), NOW)
    assert job["deadline_kind"] == "datetime"
    assert job["deadline"] == "2026-11-06T21:14:59.123456+00:00"


def install_historical_fixture(db, status, marked):
    fixture = json.loads(FIXTURE.read_text())
    with core.connection(db) as conn:
        for table in ("jobs", "aliases", "observations", "meta"):
            for record in fixture["tables"][table]:
                columns = ",".join(record)
                placeholders = ",".join("?" for _ in record)
                conn.execute("INSERT INTO " + table + "(" + columns + ") VALUES(" + placeholders + ")", tuple(record.values()))
        stored = json.loads(conn.execute("SELECT data FROM jobs").fetchone()[0])
        stored["status"] = status
        if not marked:
            stored.pop("identity_policy", None)
            conn.execute("DELETE FROM meta WHERE key='identity_policy_version'")
        conn.execute("UPDATE jobs SET data=?", (json.dumps(stored),))
    return fixture["input_rows"], stored["id"]


@pytest.mark.parametrize("marked", [False, True])
def test_lossy_historical_new_groups_split_without_certifying_old_urls(tmp_path, marked):
    db = tmp_path / "legacy.db"
    rows, original = install_historical_fixture(db, "new", marked)
    core.upsert_jobs(db, [], NOW)
    assert len(core.all_jobs(db)) == 2
    assert core.get_job(db, original) is not None
    core.upsert_jobs(db, rows, NOW)
    jobs = core.all_jobs(db)
    assert len(jobs) == 2
    assert {j["url"] for j in jobs} == {row["url"] for row in rows}
    assert all(j["status"] == "new" for j in jobs)
    before = core.all_jobs(db)
    core.upsert_jobs(db, [], NOW)
    assert core.all_jobs(db) == before


@pytest.mark.parametrize("marked", [False, True])
@pytest.mark.parametrize("status", ["saved", "applied", "dismissed"])
def test_lossy_historical_user_state_fails_closed_atomically(tmp_path, marked, status):
    db = tmp_path / "legacy-state.db"
    rows, original = install_historical_fixture(db, status, marked)
    before = core.all_jobs(db)
    marker = core.get_meta(db, "identity_policy_version")
    with pytest.raises(ValueError, match="manual identity review"):
        core.upsert_jobs(db, rows, NOW)
    assert core.all_jobs(db) == before
    assert core.get_job(db, original)["status"] == status
    assert core.get_meta(db, "identity_policy_version") == marker


@pytest.mark.parametrize("metadata", ["pending_digest", "pending_reminders", "digest_announced_ids"])
def test_lossy_historical_notification_identity_needs_review(tmp_path, metadata):
    db = tmp_path / "legacy-notification.db"
    rows, original = install_historical_fixture(db, "new", True)
    state = [{"jobs": [{"id": original}], "new_ids": [original]}]
    core.set_meta(db, metadata, state)
    before = core.all_jobs(db)
    with pytest.raises(ValueError, match="manual identity review"):
        core.upsert_jobs(db, rows, NOW)
    assert core.all_jobs(db) == before
    assert core.get_meta(db, metadata) == state


def test_unproven_single_legacy_url_cannot_lend_saved_state(tmp_path):
    db = tmp_path / "legacy-single.db"
    rows, original = install_historical_fixture(db, "saved", True)
    with core.connection(db) as conn:
        conn.execute("DELETE FROM aliases WHERE source_id=?", (rows[1]["source_id"],))
        conn.execute("DELETE FROM observations WHERE source_id=?", (rows[1]["source_id"],))
    generic = raw(source_id="fixture-new", source_job_id="generic", url="https://jobs.example.org/view")
    result = core.upsert_jobs(db, [generic], NOW)
    assert result["new"] == 1
    assert len(core.all_jobs(db)) == 2
    assert core.get_job(db, original)["status"] == "saved"
    assert core.get_job(db, result["new_ids"][0])["status"] == "new"


def test_proven_current_cross_source_group_survives_missing_meta_marker(tmp_path):
    db = tmp_path / "proven-current.db"
    job_id = core.upsert_jobs(db, [raw(), raw(source_id="fixture-curated", source_job_id="C1", verification="curated")], NOW)["new_ids"][0]
    core.set_status(db, job_id, "applied")
    with core.connection(db) as conn:
        conn.execute("DELETE FROM meta WHERE key='identity_policy_version'")
        data = json.loads(conn.execute("SELECT data FROM jobs WHERE id=?", (job_id,)).fetchone()[0])
        data.pop("identity_policy", None)
        conn.execute("UPDATE jobs SET data=? WHERE id=?", (json.dumps(data), job_id))
    core.upsert_jobs(db, [], NOW)
    job, = core.all_jobs(db)
    assert job["id"] == job_id and job["status"] == "applied"
