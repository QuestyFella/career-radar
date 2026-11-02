"""Second-review regressions, isolated fixtures and no live network/data writes."""
from datetime import datetime, timezone
import pytest
from career_radar import core
NOW = datetime(2026, 10, 31, 14, tzinfo=timezone.utc)


def raw(**changes):
    return dict(source_id="employer", source_job_id="A", source="Fixture employer", source_type="greenhouse",
                company="Fixture", title="Junior Software Developer", location="Toronto, ON, Canada",
                url="https://example.com/jobs/A", verification="direct",
                description="Computer science. 0-2 years of experience.", is_closed=False) | changes


@pytest.mark.parametrize("url", ["http://１２７.０.０.１/review", "http://127。0.0.1/review", "http://ｌｏｃａｌｈｏｓｔ/review"])
def test_browser_idna_meaning_is_validated_before_acceptance(url):
    assert core.normalize_job(raw(url=url), NOW) is None


@pytest.mark.parametrize("location,hint", [
    ("Ottawa, Kansas", None), ("Waterloo, Iowa", None), ("Victoria, Minnesota", None),
    ("Toronto, South Dakota", None), ("Remote - not eligible in Canada", "CA"),
    ("Remote - Canada is excluded", "CA"), ("Remote - Canada not eligible", "CA"),
])
def test_explicit_us_states_and_negative_eligibility_override_mentions(location, hint):
    assert core.normalize_job(raw(location=location, country_hint=hint), NOW) is None


@pytest.mark.parametrize("description", [
    "A Ph.D. is required.",
    "PhD required. You will mentor undergraduate interns in computer science.",
    "Only U.S. citizens may apply.",
    "Applicants must hold United States citizenship.",
    "Currently pursuing a Masters or PhD degree at a university in Canada (required).",
    "What we're looking for: An ongoing or recently completed PhD in Computer Science, Mathematics, AI or a related field.",
    "We are looking for current PhD students and recent PhD graduates for paid research internships.",
])
def test_advanced_only_or_us_citizen_requirements_are_not_cancelled_by_boilerplate(description):
    assert core.normalize_job(raw(title="Machine Learning Intern", description=description), NOW) is None


@pytest.mark.parametrize("description", [
    "Currently pursuing a Bachelor's or Master's degree in Computer Science.",
    "Bachelor's, Master's or PhD in Computer Science. Python experience.",
    "Bachelor's degree required. Master's degree preferred.",
    "Computer science undergraduates welcome. A PhD is not required.",
])
def test_bachelor_alternatives_and_preferred_degrees_remain_eligible(description):
    assert core.normalize_job(raw(title="Machine Learning Intern", description=description), NOW) is not None


@pytest.mark.parametrize("reverse", [False, True])
def test_direct_disqualification_retained_before_or_after_curated_copy(tmp_path, reverse):
    db = tmp_path / "negative.db"
    direct = raw(description="Minimum 5 years of professional software development experience required.")
    curated = raw(source_id="curated", source_job_id="A-index", verification="curated", description="Curated junior listing.")
    core.upsert_jobs(db, [direct, curated] if reverse else [curated, direct], NOW)
    assert not any(j["active"] for j in core.all_jobs(db))
    # A later genuine employer correction can restore eligibility; no permanent tombstone.
    core.upsert_jobs(db, [raw()], NOW)
    assert any(j["active"] for j in core.all_jobs(db))


def test_curated_requisition_b_cannot_lend_applied_state_to_distinct_a(tmp_path):
    db = tmp_path / "identity.db"
    curated_b = raw(source_id="curated", source_job_id="B-index", verification="curated", url="https://example.com/jobs/B")
    bid = core.upsert_jobs(db, [curated_b], NOW)["new_ids"][0]
    core.set_status(db, bid, "applied")
    core.upsert_jobs(db, [raw(), raw(source_job_id="B", url="https://example.com/jobs/B")], NOW)
    roles = {j["source_job_id"]: j for j in core.all_jobs(db)}
    assert len(roles) == 2
    assert roles["A"]["status"] == "new"
    assert roles["B"]["status"] == "applied"
    assert roles["B"]["id"] == bid


def test_fingerprint_only_matches_are_not_application_identity(tmp_path):
    db = tmp_path / "ambiguous.db"
    core.upsert_jobs(db, [raw()], NOW)
    result = core.upsert_jobs(db, [raw(source_id="public", source_job_id="other", verification="public_listing", url="https://other.example.com/posting")], NOW)
    assert result["new"] == 1
    assert len(core.all_jobs(db)) == 2


def legacy_group(db, status="new"):
    import json
    first = core.upsert_jobs(db, [raw()], NOW)["new_ids"][0]
    other = raw(source_id="curated", source_job_id="B-index", verification="curated", url="https://example.com/jobs/B")
    observed = core.normalize_job(other, NOW)
    with core.connection(db) as conn:
        legacy = json.loads(conn.execute("SELECT data FROM jobs WHERE id=?", (first,)).fetchone()[0])
        conn.execute("DELETE FROM meta WHERE key='identity_policy_version'")
        legacy.pop("identity_policy", None)
        conn.execute("UPDATE jobs SET data=? WHERE id=?", (json.dumps(legacy), first))
        conn.execute("INSERT INTO aliases VALUES(?,?,?,?,?,?)", ("curated", "B-index", first, other["url"], 1, core.iso(NOW)))
        conn.execute("INSERT INTO observations VALUES(?,?,?,?)", ("curated", "B-index", first, json.dumps(observed)))
    core.set_status(db, first, status)
    return first, other


def test_prelaunch_legacy_fingerprint_group_splits_on_refresh(tmp_path):
    db = tmp_path / "legacy.db"
    first, other = legacy_group(db)
    core.upsert_jobs(db, [other], NOW)
    jobs = core.all_jobs(db)
    assert len(jobs) == 2
    assert {j["url"] for j in jobs} == {"https://example.com/jobs/A", "https://example.com/jobs/B"}
    assert core.get_job(db, first)["url"] == "https://example.com/jobs/A"


def test_ambiguous_legacy_application_state_requires_manual_review(tmp_path):
    db = tmp_path / "legacy-state.db"
    first, other = legacy_group(db, "applied")
    before = core.all_jobs(db)
    with pytest.raises(ValueError, match="Legacy"):
        core.upsert_jobs(db, [other], NOW)
    assert core.all_jobs(db) == before
    assert core.get_job(db, first)["status"] == "applied"


def test_proven_alias_identity_survives_employer_url_change(tmp_path):
    db = tmp_path / "verified-alias.db"
    curated = raw(source_id="curated", source_job_id="A-index", verification="curated")
    first = core.upsert_jobs(db, [raw(), curated], NOW)["new_ids"][0]
    core.set_status(db, first, "applied")
    core.upsert_jobs(db, [raw(url="https://example.com/new-link/A"), curated], NOW)
    assert len(core.all_jobs(db)) == 1
    assert core.get_job(db, first)["status"] == "applied"
    assert core.get_job(db, first)["url"] == "https://example.com/new-link/A"
