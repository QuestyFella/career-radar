"""Synthetic fixtures for rules only; never production job records."""
from datetime import datetime, timezone, timedelta
import pytest
from career_radar import core
NOW = datetime(2026, 10, 31, 14, 0, tzinfo=timezone.utc)

def raw(**changes):
    item = dict(source_id="test-gh", source="Fixture employer", source_type="greenhouse",
                source_job_id="1", company="Fixture Company", title="Junior Software Developer",
                location="Toronto, Ontario, Canada", url="https://example.com/careers/1",
                description="Bachelor's in computer science. Python and software development. 0-2 years of experience.",
                verification="direct", posted_at="2026-10-30", is_closed=False)
    return item | changes

@pytest.mark.parametrize("location,expected", [
    ("Toronto, CAN", True), ("Ottawa, Ontario", True), ("Vancouver, BC", True),
    ("Remote - Canada", True), ("Montreal, Quebec, Canada", True), ("Kitchener, ON", True),
    ("St. John's, Newfoundland and Labrador", True), ("Waterloo, Ontario", True),
    ("Remote - US", False), ("Remote", False), ("North America", False),
    ("San Francisco, CA", False), ("Ontario, CA, United States", False),
    ("Vancouver, WA", False), ("London, UK", False), ("London, ON", True),
])
def test_canadian_location(location, expected):
    assert core.canadian_location(location) is expected

def test_country_hint_does_not_override_explicit_us_location():
    assert core.normalize_job(raw(location="Remote - United States", country_hint="CA"), NOW) is None

@pytest.mark.parametrize("title,description,expected", [
    ("Software Engineering Intern", "CS undergraduates", "internship"),
    ("Developer Co-op", "Enrolled in a co-op program", "co_op"),
    ("Software Engineer, New Grad 2027", "Graduating this year", "new_grad"),
    ("Junior Data Analyst", "Bachelor's degree in CS", "entry_level"),
    ("Technical Support Specialist I", "0-2 years experience", "entry_level"),
    ("Associate Product Manager", "New graduate role", "new_grad"),
    ("Software Developer", "Requires 1-2 years of experience", "entry_level"),
    ("Senior Software Engineer", "10 years", None),
    ("Staff Software Engineer", "8 years", None),
    ("Software Engineer II", "Minimum 4 years of experience", None),
    ("Software Engineer", "Minimum 5 years of experience required", None),
    ("Research Scientist", "PhD required", None),
    ("Registered Nurse", "Nursing license required", None),
])
def test_role_scope(title, description, expected):
    result = core.normalize_job(raw(title=title, description=description), NOW)
    assert (result or {}).get("level") == expected

def test_topic_tags_are_descriptive_and_base_score_is_neutral():
    plain = core.normalize_job(raw(), NOW)
    embedded = core.normalize_job(raw(title="Firmware Engineering Intern", company="Kepler Communications"), NOW)
    assert plain is not None and embedded is not None
    # Personal ranking comes from the user's profile at request time (ranking.Matcher).
    assert embedded["priority_score"] == plain["priority_score"] == 40
    assert {"embedded", "aerospace"} <= set(embedded["tags"])

def test_unknown_seniority_is_not_silently_assumed_junior():
    assert core.normalize_job(raw(title="Software Engineer", description="Build distributed systems."), NOW) is None

def test_coop_and_work_authorization_are_caveats_not_assumptions():
    j = core.normalize_job(raw(title="Software Developer Co-op", description="Must be enrolled in an accredited co-op program. Must be legally authorized to work in Canada."), NOW)
    assert any("co-op" in n.lower() for n in j["eligibility_notes"])
    assert any("authorization" in n.lower() for n in j["eligibility_notes"])

def test_us_citizen_only_role_rejected_even_if_canadian_office():
    assert core.normalize_job(raw(description="Applicants must be US citizens."), NOW) is None

def test_canadian_citizenship_requirement_flagged():
    j = core.normalize_job(raw(description="Must be a Canadian citizen and eligible for security clearance."), NOW)
    assert any("citizenship" in n.lower() for n in j["eligibility_notes"])

def test_actual_explicit_deadline_not_application_posted_date():
    j = core.normalize_job(raw(description="Posted October 24, 2026. Apply by November 5, 2026."), NOW)
    assert core.parsed_date(j["deadline"]).astimezone(core.TORONTO).date().isoformat() == "2026-11-05"
    assert j["deadline_kind"] == "date"
    assert "Apply by" in j["deadline_evidence"]
    assert j["deadline_source_url"] == j["url"]

def test_no_invented_rolling_deadline():
    j = core.normalize_job(raw(description="Applications reviewed on a rolling basis. Start January 2027."), NOW)
    assert j["deadline"] is None
    assert j["deadline_evidence"] is None

def test_structured_deadline_requires_evidence():
    j = core.normalize_job(raw(deadline="2026-11-06", deadline_evidence="JobPosting.validThrough: 2026-11-06"), NOW)
    assert core.parsed_date(j["deadline"]).astimezone(core.TORONTO).date().isoformat() == "2026-11-06"
    assert core.normalize_job(raw(deadline="2026-11-06"), NOW)["deadline"] is None

def test_expired_or_source_closed_job_is_not_active():
    assert core.normalize_job(raw(is_closed=True), NOW)["active"] is False
    assert core.normalize_job(raw(deadline="2026-10-30", deadline_evidence="Closing date: 2026-10-30"), NOW)["active"] is False

def test_deadline_date_inclusive_until_toronto_end_of_day():
    now = datetime(2026, 11, 5, 23, 0, tzinfo=timezone.utc)
    j = core.normalize_job(raw(deadline="2026-11-05", deadline_evidence="Apply by 2026-11-05"), now)
    assert j["active"] is True

def test_safe_urls_and_meaningful_parameters():
    # Unknown query keys can be requisition IDs, even when their value says tracking.
    assert core.canonical_url("https://example.com/job?id=17&utm_source=x&ref=tracking") == "https://example.com/job?id=17&ref=tracking"
    assert core.canonical_url("https://boards.greenhouse.io/example?gh_jid=123&utm_medium=x").endswith("?gh_jid=123")
    for url in ("javascript:alert(1)", "file:///etc/passwd", "http://127.0.0.1/x", "http://10.1.2.3/x", "https://user:pass@example.com/job"):
        assert core.normalize_job(raw(url=url), NOW) is None

def test_dedup_preserves_application_state_and_all_sources(tmp_path):
    db = tmp_path / "test.db"
    first = core.upsert_jobs(db, [raw()], now=NOW)
    assert first["new"] == 1
    item = core.list_jobs(db)["items"][0]
    core.set_status(db, item["id"], "saved")
    result = core.upsert_jobs(db, [raw(source_id="test-li", source="LinkedIn", source_type="linkedin", url="https://example.com/careers/1?utm_source=linkedin", verification="public_listing")], now=NOW + timedelta(hours=12))
    assert result["new"] == 0
    assert core.list_jobs(db)["total"] == 1
    item = core.get_job(db, item["id"])
    assert item["status"] == "saved"
    assert len(item["sources"]) == 2
    assert item["url"].startswith("https://example.com/")

def test_eligibility_changes_disable_an_existing_listing(tmp_path):
    db = tmp_path / "test.db"
    core.upsert_jobs(db, [raw()], now=NOW)
    core.upsert_jobs(db, [raw(title="Senior Software Developer")], now=NOW)
    assert core.list_jobs(db)["total"] == 0

def test_pagination_and_search_have_correct_total(tmp_path):
    db = tmp_path / "test.db"
    rows = [raw(source_job_id=str(i), title=f"Junior Developer Project {i}", url=f"https://example.com/job/{i}") for i in range(65)]
    core.upsert_jobs(db, rows, now=NOW)
    result = core.list_jobs(db, page=2, page_size=30)
    assert result["total"] == 65 and len(result["items"]) == 30 and result["pages"] == 3
    assert core.list_jobs(db, q="Project 64")["total"] == 1
    assert core.list_jobs(db, q="' OR 1=1 --")["total"] == 0

def test_failed_source_does_not_close_jobs(tmp_path):
    db = tmp_path / "test.db"
    core.upsert_jobs(db, [raw()], now=NOW)
    core.record_source(db, {"id":"test-gh", "name":"Fixture employer", "type":"greenhouse", "url":"https://example.com"}, status="error", error="timeout", now=NOW)
    assert core.list_jobs(db)["total"] == 1

def test_status_validation_and_reminder_exclusions(tmp_path):
    db = tmp_path / "test.db"
    core.upsert_jobs(db, [raw(deadline="2026-11-02", deadline_evidence="Closing 2026-11-02")], now=NOW)
    job = core.list_jobs(db)["items"][0]
    with pytest.raises(ValueError):
        core.set_status(db, job["id"], "bad")
    assert len(core.due_reminders(db, now=NOW)) == 1
    core.set_status(db, job["id"], "applied")
    assert core.due_reminders(db, now=NOW) == []

def test_reminders_dedupe_by_band_and_deadline(tmp_path):
    db = tmp_path / "test.db"
    core.upsert_jobs(db, [raw(deadline="2026-11-06", deadline_evidence="Closing 2026-11-06")], now=NOW)
    due = core.due_reminders(db, now=NOW)
    assert len(due) == 1 and due[0]["reminder_band"] == "7d"
    core.mark_reminders(db, due, now=NOW)
    assert core.due_reminders(db, now=NOW) == []
    assert core.due_reminders(db, now=NOW + timedelta(days=4))[0]["reminder_band"] == "3d"

def test_no_deadline_no_reminder_and_no_fake_timestamp(tmp_path):
    db = tmp_path / "test.db"
    core.upsert_jobs(db, [raw(posted_at=None)], now=NOW)
    assert core.due_reminders(db, now=NOW) == []
    assert core.list_jobs(db)["items"][0]["posted_at"] is None

def test_stale_curated_roles_are_hidden_by_default(tmp_path):
    db = tmp_path / "test.db"
    core.upsert_jobs(db, [raw(verification="curated")], now=NOW - timedelta(days=40))
    core.expire_stale(db, now=NOW)
    assert core.list_jobs(db)["total"] == 0


def test_multi_country_offer_still_includes_a_canadian_option():
    assert core.canadian_location("Remote - Canada, United States") is True
    assert core.canadian_location("Remote - US only, not Canada") is False


def test_french_technical_interns_are_not_dropped():
    job = core.normalize_job(raw(title="Développeur logiciel stagiaire", description="Étudiant en informatique, français requis."), NOW)
    assert job is not None and job["level"] == "internship"


def test_published_deadline_time_and_timezone_are_preserved():
    job = core.normalize_job(raw(description="Apply by November 5, 2026 at 5:00 PM ET."), NOW)
    assert job["deadline_kind"] == "datetime"
    assert job["deadline"] == "2026-11-05T22:00:00+00:00"
    assert "5:00 PM ET" in job["deadline_evidence"]


def test_published_time_without_timezone_is_not_presented_as_exact():
    job = core.normalize_job(raw(description="Apply by November 5, 2026 at 5:00 PM."), NOW)
    assert job["deadline_kind"] == "date"
    assert any("timezone" in note.lower() for note in job["eligibility_notes"])


def test_date_only_reminder_uses_calendar_days_not_assumed_time(tmp_path):
    db = tmp_path / "test.db"
    core.upsert_jobs(db, [raw(deadline="2026-11-02", deadline_evidence="Closing November 2, 2026")], now=NOW)
    next_day_morning = datetime(2026, 11, 1, 14, 0, tzinfo=timezone.utc)
    assert core.due_reminders(db, now=next_day_morning)[0]["reminder_band"] == "1d"


def test_date_only_deadline_keeps_explicit_calendar_field_for_ui():
    job = core.normalize_job(raw(deadline="2026-11-06", deadline_evidence="Closing date: 2026-11-06"), NOW)
    assert job["deadline_date"] == "2026-11-06"


def test_structured_unzoned_deadline_is_not_assumed_toronto_time():
    job = core.normalize_job(raw(deadline="2026-11-06T17:00:00", deadline_evidence="validThrough: 2026-11-06T17:00:00"), NOW)
    assert job["deadline_kind"] == "date"
    assert job["deadline_date"] == "2026-11-06"
    assert any("timezone" in note for note in job["eligibility_notes"])


def test_foreign_board_cannot_match_bare_ambiguous_city():
    assert core.normalize_job(raw(location="United Kingdom; London; Cambridge; Cambridge, United Kingdom"), NOW) is None
    assert core.normalize_job(raw(location="Victoria, Australia"), NOW) is None


def test_only_canadian_location_options_are_displayed():
    job = core.normalize_job(raw(location="San Francisco, CA; Toronto, Ontario, Canada; Remote in Canada; Remote in USA"), NOW)
    assert "Toronto" in job["location"] and "Remote in Canada" in job["location"]
    assert "USA" not in job["location"] and "Francisco" not in job["location"]


def test_remote_multi_country_is_displayed_as_remote_canada():
    job = core.normalize_job(raw(location="Remote (United States | Canada)"), NOW)
    assert "Canada" in job["location"] and "Remote" in job["location"]
    assert "United States" not in job["location"]


@pytest.mark.parametrize("title,description,degrees", [
    ("Machine Learning Intern (PhD)", "Undergraduate opportunities also exist elsewhere in our company.", []),
    ("Research Intern", "Curated listing; listed degrees: PhD", ["PhD"]),
    ("Data Science Intern (Masters)", "Intern role", []),
])
def test_advanced_degree_only_roles_are_excluded(title, description, degrees):
    assert core.normalize_job(raw(title=title, description=description, degrees=degrees), NOW) is None


@pytest.mark.parametrize("description", [
    "Minimum 5 years of experience. Python preferred.",
    "At least 5 years’ experience",
    "5 or more years of experience required",
    "U.S. citizens only.",
])
def test_disqualifying_requirement_cannot_be_cancelled_by_other_clause(description):
    assert core.normalize_job(raw(description=description), NOW) is None


def test_negated_canada_hint_and_lowercase_on_are_rejected():
    assert core.normalize_job(raw(location="Remote - excluding Canada", country_hint="CA"), NOW) is None
    assert core.normalize_job(raw(location="Portland, on-site"), NOW) is None
    assert core.normalize_job(raw(location="Vancouver, Washington"), NOW) is None


@pytest.mark.parametrize("url", ["http://127.1/", "http://2130706433/", "http://0x7f000001/", "http://0177.0.0.1/", "http://[::1]/"])
def test_abbreviated_or_numeric_private_urls_are_rejected(url):
    assert core.canonical_url(url) is None

