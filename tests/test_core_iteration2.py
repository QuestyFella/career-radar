"""Final corrective regressions: synthetic historical rows and mocked source I/O."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import socket
import sqlite3

import httpx
import pytest
from career_radar import adapters
from career_radar import core
from career_radar import extras
NOW = datetime(2026, 10, 31, 14, tzinfo=timezone.utc)
URL = "https://jobs.example.org/view"
FIXTURE = Path(__file__).parent / "fixtures" / "legacy_identity_iteration2.json"
TABLES = ("jobs", "aliases", "observations", "source_rejections", "sources", "reminders", "meta")
FALLBACK_CASES = ("legacy_fallback", "legacy_url_native", "iteration1_fallback", "iteration1_promoted_fallback")
NEGATIVE_CASES = ("legacy_negative_native", "legacy_negative_fallback",
                  "iteration1_negative_native", "iteration1_negative_fallback")
STATES = ("new", "saved", "applied", "dismissed", "pending_digest", "pending_reminders",
          "digest_announced_ids", "reminder")
PATHS = ("greenhouse", "lever", "ashby", "jsonld", "workday")
REAL_CLIENT = httpx.Client


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Iteration-2 core fixtures forbid live network/DNS")
    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(core, "utcnow", lambda: NOW)


def raw(**changes):
    return dict(source_id="historical-fixture", source_job_id="PROVEN-NATIVE",
                source="Synthetic employer", source_type="fixture", company="Synthetic Aerospace",
                title="Junior Firmware Developer", location="Toronto, ON, Canada", url=URL,
                description="Computer science. No prior experience required.",
                verification="direct", is_closed=False) | changes


def snapshot(db):
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        return {
            "schema": [dict(r) for r in conn.execute(
                "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name")],
            "tables": {table: [dict(r) for r in conn.execute("SELECT * FROM " + table + " ORDER BY 1,2")]
                       for table in TABLES},
        }


def install_fixture(db, case, marker=True):
    fixture = json.loads(FIXTURE.read_text())
    stored = fixture["cases"][case]
    # Use the historical five-column negative schema, not today's initializer.
    with sqlite3.connect(db) as conn:
        for sql in fixture["schema_sql"]:
            conn.execute(sql)
        for table, records in stored["tables"].items():
            for row in records:
                conn.execute("INSERT INTO " + table + "(" + ",".join(row) + ") VALUES(" +
                             ",".join("?" for _ in row) + ")", tuple(row.values()))
        if not marker:
            conn.execute("DELETE FROM meta WHERE key='identity_policy_version'")
            for row in conn.execute("SELECT id,data FROM jobs").fetchall():
                job = json.loads(row[1])
                job.pop("identity_policy", None)
                conn.execute("UPDATE jobs SET data=? WHERE id=?", (json.dumps(job), row[0]))
    return stored


def add_state(db, state="new"):
    jobs = core.all_jobs(db)
    job_id = jobs[0]["id"] if jobs else "unrelated-synthetic-id"
    core.set_meta(db, "unrelated_metadata", {"keep": ["source state", "notification state"]})
    core.record_source(db, {"id": "unrelated-source", "name": "Synthetic metadata"},
                       status="partial", raw_count=2, matched_count=1, error="synthetic", now=NOW)
    if state in core.STATES and jobs:
        core.set_status(db, job_id, state)
    elif state == "reminder":
        core.mark_reminders(db, [{"id": job_id, "deadline": "2026-11-03", "reminder_band": "3d"}], NOW)
    elif state == "digest_announced_ids":
        core.set_meta(db, state, [job_id])
    elif state in ("pending_digest", "pending_reminders"):
        core.set_meta(db, state, [{"jobs": [{"id": job_id}], "new_ids": [job_id], "batch_id": "fixture-batch"}])
    return job_id


def assert_atomic_review(db, call):
    before = snapshot(db)
    with pytest.raises(ValueError, match="manual identity review"):
        call()
    assert snapshot(db) == before, "Manual-review refusal must roll back schema and every data table"


def test_durable_fixture_preserves_actual_generators_and_lossy_inputs():
    fixture = json.loads(FIXTURE.read_text())
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == "e0f99220f0fb41bd66642d8dd047e9a3df2e0d30e48771a1efe5047b0d160318"
    assert fixture["generator_source_sha256"] == "126be0fdf88c7df21bd0e9429d5430feb6af37d33e47c8e8927124b568c24632"
    assert fixture["iteration1_source_sha256"] == "1cc42e4c75888a0dac4081674d8420048dd6c2718bb0aea14e9264d72b7285be"
    assert len(fixture["cases"]) == 10
    for name, case in fixture["cases"].items():
        original, = case["calls"][0]["input_rows"]
        assert original["url"] == URL + "?ref=OLD-REQUISITION"
        if "negative" in name:
            negative, = case["tables"]["source_rejections"]
            assert negative["url"] == URL and len(negative) == 5
        else:
            job = json.loads(case["tables"]["jobs"][0]["data"])
            assert job["url"] == URL
        if name.startswith("iteration1"):
            assert case["tables"]["meta"] == [{"key": "identity_policy_version", "value": "2"}]
    promoted = fixture["cases"]["iteration1_promoted_fallback"]
    assert promoted["outcomes"][1]["updated"] == 1 and promoted["outcomes"][1]["new"] == 0
    assert json.loads(promoted["tables"]["observations"][0]["data"])["url_identity_policy"] == "exact-v2"


@pytest.mark.parametrize("case", FALLBACK_CASES)
@pytest.mark.parametrize("marker", [True, False], ids=["marker", "no-marker"])
@pytest.mark.parametrize("state", STATES)
def test_old_url_shaped_source_key_never_lends_identity_even_after_iteration1(tmp_path, case, marker, state):
    db = tmp_path / "legacy-fallback.db"
    install_fixture(db, case, marker)
    original = add_state(db, state)
    assert core.get_job(db, original)["source_job_id"] == URL
    assert_atomic_review(db, lambda: core.upsert_jobs(db, [
        raw(source_id="unrelated-new-source", source_job_id="OTHER", url="https://jobs.example.org/other"),
        raw(source_job_id=None, description="A different posting at the bare URL."),
    ], NOW + timedelta(hours=2)))


@pytest.mark.parametrize("case", FALLBACK_CASES + NEGATIVE_CASES)
@pytest.mark.parametrize("operation", ["empty-upsert", "reconcile"])
def test_ambiguous_history_cannot_be_certified_by_empty_refresh_or_reconciliation(tmp_path, case, operation):
    db = tmp_path / "no-silent-upgrade.db"
    install_fixture(db, case)
    add_state(db, "reminder")
    call = (lambda: core.upsert_jobs(db, [], NOW)) if operation == "empty-upsert" else (
        lambda: core.reconcile_source(db, "historical-fixture", set(), NOW))
    assert_atomic_review(db, call)


@pytest.mark.parametrize("case", ["legacy_native", "iteration1_native"])
@pytest.mark.parametrize("state", ["saved", "applied", "dismissed"])
def test_genuine_native_legacy_id_keeps_state_when_url_changes(tmp_path, case, state):
    db = tmp_path / "native.db"
    install_fixture(db, case)
    original = add_state(db, state)
    core.mark_reminders(db, [{"id": original, "deadline": "2026-11-03", "reminder_band": "3d"}], NOW)
    before = snapshot(db)
    previous = core.get_job(db, original)
    result = core.upsert_jobs(db, [raw(url=URL + "?ref=CHANGED")], NOW + timedelta(hours=2))
    current, = core.all_jobs(db)
    assert result["new"] == 0 and result["updated"] == 1
    assert current["id"] == original and current["status"] == state
    assert current["first_seen"] == previous["first_seen"] and current["url"] == URL + "?ref=CHANGED"
    after = snapshot(db)
    assert after["tables"]["sources"] == before["tables"]["sources"]
    assert after["tables"]["reminders"] == before["tables"]["reminders"]
    assert core.get_meta(db, "unrelated_metadata") == {"keep": ["source state", "notification state"]}
    core.upsert_jobs(db, [], NOW + timedelta(hours=3))
    assert snapshot(db) == after


@pytest.mark.parametrize("state", ["saved", "applied", "dismissed"])
@pytest.mark.parametrize("remove_markers", [False, True])
def test_current_exact_fallback_updates_normally_and_does_not_lend_state_to_other_url(tmp_path, state, remove_markers):
    db = tmp_path / "current-fallback.db"
    row = raw(source_job_id=None, url=URL + "?ref=EXACT")
    original = core.upsert_jobs(db, [row], NOW)["new_ids"][0]
    core.set_status(db, original, state)
    if remove_markers:
        with core.connection(db) as conn:
            conn.execute("DELETE FROM meta WHERE key='identity_policy_version'")
            job = core.get_job(db, original)
            job.pop("identity_policy", None)
            conn.execute("UPDATE jobs SET data=? WHERE id=?", (json.dumps(job), original))
    same = core.upsert_jobs(db, [row | {"url": row["url"] + "&utm_source=fixture"}], NOW)
    assert same["updated"] == 1 and same["new"] == 0
    other = core.upsert_jobs(db, [raw(source_job_id=None)], NOW)["new_ids"][0]
    assert other != original and core.get_job(db, other)["status"] == "new"
    assert core.get_job(db, original)["status"] == state
    core.reconcile_source(db, "historical-fixture", {row["url"], URL}, NOW)
    assert all(job["active"] for job in core.all_jobs(db))


def test_newly_observed_native_url_shaped_id_has_its_own_provenance(tmp_path):
    db = tmp_path / "current-url-native.db"
    original = core.upsert_jobs(db, [raw(source_job_id="https://ids.example.org/requisition/A")], NOW)["new_ids"][0]
    core.set_status(db, original, "applied")
    core.upsert_jobs(db, [raw(source_job_id="https://ids.example.org/requisition/A", url=URL + "?ref=NEW")], NOW)
    current, = core.all_jobs(db)
    assert current["id"] == original and current["status"] == "applied"


@pytest.mark.parametrize("field", ["source_identity_policy", "source_id", "source_job_id"])
def test_source_key_provenance_is_bound_to_the_alias_not_a_global_version(tmp_path, field):
    db = tmp_path / "bound-proof.db"
    core.upsert_jobs(db, [raw(source_job_id=None)], NOW)
    with core.connection(db) as conn:
        item = json.loads(conn.execute("SELECT data FROM observations").fetchone()[0])
        item.pop(field, None)
        conn.execute("UPDATE observations SET data=?", (json.dumps(item),))
    assert_atomic_review(db, lambda: core.upsert_jobs(db, [raw(source_job_id=None)], NOW))


@pytest.mark.parametrize("case", NEGATIVE_CASES)
@pytest.mark.parametrize("marker", [True, False], ids=["marker", "no-marker"])
@pytest.mark.parametrize("verification", ["curated", "direct"])
def test_historical_negative_urls_cannot_archive_unrelated_exact_postings(tmp_path, case, marker, verification):
    db = tmp_path / "negative-history.db"
    install_fixture(db, case, marker)
    add_state(db, "pending_digest")
    assert_atomic_review(db, lambda: core.upsert_jobs(db, [raw(
        source_id="current-positive", source_job_id="FRESH-NATIVE", verification=verification)], NOW))
    assert len(snapshot(db)["tables"]["source_rejections"]) == 1


@pytest.mark.parametrize("native_id", [None, "NEG-NATIVE"])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("separate_batches", [False, True])
@pytest.mark.parametrize("verification", ["curated", "direct"])
def test_proven_current_negatives_keep_both_orders_and_positive_recovery(tmp_path, native_id, reverse, separate_batches, verification):
    db = tmp_path / "current-negative.db"
    positive = raw(source_id="positive-copy", verification=verification)
    negative = raw(source_job_id=native_id, title="Senior Firmware Developer")
    rows = [negative, positive] if reverse else [positive, negative]
    for batch in ([row] for row in rows) if separate_batches else [rows]:
        core.upsert_jobs(db, batch, NOW)
    job, = core.all_jobs(db)
    assert not job["active"]
    assert len(snapshot(db)["tables"]["source_rejections"]) == 1
    core.set_status(db, job["id"], "saved")
    core.upsert_jobs(db, [negative | {"title": "Junior Firmware Developer"}], NOW + timedelta(hours=1))
    recovered, = core.all_jobs(db)
    assert recovered["active"] and recovered["status"] == "saved" and recovered["id"] == job["id"]
    assert snapshot(db)["tables"]["source_rejections"] == []


@pytest.mark.parametrize("policy", [None, "exact-v1"])
def test_unknown_negative_row_provenance_is_not_repaired_by_current_meta(tmp_path, policy):
    db = tmp_path / "negative-proof.db"
    core.upsert_jobs(db, [raw(title="Senior Firmware Developer")], NOW)
    with core.connection(db) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(source_rejections)")}
        if "url_identity_policy" in columns:
            conn.execute("UPDATE source_rejections SET url_identity_policy=?", (policy,))
    assert_atomic_review(db, lambda: core.upsert_jobs(db, [raw(source_id="copy")], NOW))


def test_exact_negative_cannot_use_an_unproven_historical_positive_url(tmp_path):
    db = tmp_path / "reverse-lossy-url.db"
    install_fixture(db, "iteration1_native")
    original = add_state(db, "applied")
    assert core.get_job(db, original)["url"] == URL
    assert_atomic_review(db, lambda: core.upsert_jobs(db, [raw(
        source_id="new-authority", source_job_id="DIFFERENT", title="Senior Firmware Developer")], NOW))


def test_empty_old_negative_schema_is_upgraded_without_inventing_provenance(tmp_path):
    db = tmp_path / "old-empty-ledger.db"
    install_fixture(db, "legacy_native")
    with sqlite3.connect(db) as conn:
        assert len(conn.execute("PRAGMA table_info(source_rejections)").fetchall()) == 5
    core.upsert_jobs(db, [raw(url=URL + "?ref=CHANGED"), raw(
        source_id="other-authority", source_job_id="NEG", url="https://jobs.example.org/unrelated",
        title="Senior Firmware Developer")], NOW)
    negative, = snapshot(db)["tables"]["source_rejections"]
    assert negative.get("url_identity_policy") == core.IDENTITY_POLICY
    after = snapshot(db)
    core.upsert_jobs(db, [], NOW)
    assert snapshot(db) == after


EXCLUSIONS = (
    "Remote — not open to Canadian residents",
    "Remote — not eligible for Canadian applicants",
    "Remote — not available to Canadian candidates",
    "Remote — not currently authorized for Canadian residents",
    "Remote — not supported for applicants in Canada",
    "Remote — not hiring residents of Canada",
    "Remote — excluding Canadian residents",
    "Remote — Canadian candidates are not currently eligible",
)


@pytest.mark.parametrize("location", EXCLUSIONS)
@pytest.mark.parametrize("hint", [None, "CA"])
def test_canadian_exclusion_person_and_preposition_ordering(location, hint):
    assert core.canadian_options(location) == []
    assert not core.canadian_location(location)
    assert core.normalize_job(raw(location=location, country_hint=hint), NOW) is None


@pytest.mark.parametrize("location,expected", [
    ("Remote — Canadian residents eligible", True), ("Remote — open to Canadian residents", True),
    ("Remote — available to applicants in Canada", True), ("Remote — CAN", True),
    ("Toronto, ON", True), ("Jordan, Ontario", True), ("Peru, ON", True),
    ("Remote — can work anywhere", False), ("Remote — Can work from home", False),
    ("Vancouver, Washington", False), ("Waterloo, Belgium", False), ("North America — Remote", False),
])
def test_geography_controls_remain_unchanged(location, expected):
    assert (core.normalize_job(raw(location=location), NOW) is not None) is expected


DEGREE_ALTERNATIVES = (
    "Minimum qualifications: bachelor's degree in Electrical and Computer Engineering or master's degree in Computer Science.",
    "Minimum qualifications: bachelor's degree in Computer Science or a related field, or master's degree in Computer Science.",
    "A bachelor's degree, master's degree, or doctoral degree is required.",
    "Required: master's degree in Electrical and Computer Engineering or bachelor's degree in Computer Science.",
    "Minimum education: bachelor's degree in Computer Science or Software Engineering, or master's degree in Computer Science.",
    "Required: an undergraduate degree in Electrical and Computer Engineering or a master's degree in Computer Science.",
    "A bachelor's degree in Computer Science and Engineering / master's degree in Computer Science is required.",
)

JOINT_REQUIREMENTS = (
    "A bachelor's degree and a master's degree are required.",
    "A bachelor's degree in Computer Science and a master's degree in Engineering are required.",
    "Minimum qualifications: bachelor's degree in Electrical and Computer Engineering and master's degree in Computer Science.",
    "A bachelor's degree, master's degree, and doctoral degree are required.",
    "A bachelor's degree in Computer Science or Software Engineering and a master's degree are required.",
    "A bachelor's degree in Computer Science is encouraged, or a master's degree is required.",
    "A bachelor's degree in Computer Science is required; a master's degree is also required.",
    "A bachelor's degree in Computer Science is preferred. This role requires a PhD.",
    "A PhD is required and you will mentor bachelor's students in Computer Science.",
    "We mentor bachelor's students in Computer Science or Electrical Engineering; a master's degree is mandatory.",
)


@pytest.mark.parametrize("description", DEGREE_ALTERNATIVES)
def test_local_degree_alternatives_allow_subject_conjunctions_and_oxford_or(description):
    assert not core.advanced_degree_required(description)
    assert core.normalize_job(raw(description=description), NOW) is not None


@pytest.mark.parametrize("description", JOINT_REQUIREMENTS)
def test_degree_subject_grammar_cannot_swallow_joint_requirements_or_mentorship(description):
    assert core.advanced_degree_required(description)
    assert core.normalize_job(raw(description=description), NOW) is None


class Wire(httpx.SyncByteStream):
    def __init__(self, body):
        self.body = body

    def __iter__(self):
        yield self.body


def source_row(monkeypatch, path, *, location="Toronto, ON, Canada", description=None):
    description = raw()["description"] if description is None else description
    source = {"id": "fixture-" + path, "name": "Synthetic Aerospace", "type": path, "board": "fixture"}
    common = {"id": "17", "title": raw()["title"]}
    if path == "greenhouse":
        payload = {"jobs": [common | {"absolute_url": URL, "content": description, "location": {"name": location}}],
                   "meta": {"total": 1}}
    elif path == "lever":
        payload = [{"id": "17", "text": raw()["title"], "hostedUrl": URL,
                    "descriptionPlain": description, "categories": {"location": location}}]
    elif path == "ashby":
        payload = {"jobs": [common | {"jobUrl": URL, "descriptionPlain": description,
                                      "location": location, "isListed": True}]}
    elif path == "jsonld":
        source["url"] = URL
        payload = {"@type": "JobPosting", "identifier": {"value": "17"}, "title": raw()["title"],
                   "description": description, "url": URL, "jobLocation": {"address": location},
                   "hiringOrganization": {"name": source["name"]}}
    else:
        assert path == "workday"
        source.update(url="https://fixture.wd3.myworkdayjobs.com/Campus", tenant="fixture", site="Campus")
        index = {"total": 1, "jobPostings": [{"title": raw()["title"], "externalPath": "/job/Fixture_17",
                                              "locationsText": location, "bulletFields": ["17"]}]}
        payload = {"jobPostingInfo": common | {"jobDescription": description, "location": location}}
    calls = []

    def handle(request):
        calls.append(str(request.url))
        value = index if path == "workday" and request.method == "POST" else payload
        body = json.dumps(value)
        if path == "jsonld":
            body = '<script type="application/ld+json">' + body + '</script>'
        return httpx.Response(200, stream=Wire(body.encode()))

    transport = httpx.MockTransport(handle)
    if path in ("greenhouse", "lever", "ashby"):
        monkeypatch.setattr(adapters.httpx, "Client", lambda **kwargs: REAL_CLIENT(**(kwargs | {"transport": transport})))
        rows = adapters.fetch_source(source)
    else:
        monkeypatch.setattr(extras, "_PublicTransport", lambda **kwargs: transport)
        rows = extras.fetch_source(source)
    assert len(rows) == 1 and len(calls) == (2 if path == "workday" else 1)
    return json.loads(json.dumps(rows[0]))


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("location", EXCLUSIONS[:3])
def test_actual_source_json_path_respects_canadian_person_exclusions(monkeypatch, path, location):
    assert core.normalize_job(source_row(monkeypatch, path, location=location), NOW) is None


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("description", DEGREE_ALTERNATIVES[:3])
def test_actual_source_json_path_preserves_local_degree_alternatives(monkeypatch, path, description):
    assert core.normalize_job(source_row(monkeypatch, path, description=description), NOW) is not None


@pytest.mark.parametrize("path", PATHS)
def test_actual_source_paths_retain_positive_geography_and_joint_degree_control(monkeypatch, path):
    row = source_row(monkeypatch, path, location="Jordan, Ontario")
    assert core.normalize_job(row, NOW) is not None
    joint = source_row(monkeypatch, path, description=JOINT_REQUIREMENTS[2])
    assert core.normalize_job(joint, NOW) is None
