"""Region preferences and multi-level collection (offline fixtures only)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from career_radar import core, profile, regions, settings
from career_radar.collect import run_collection

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


def raw(**overrides):
    row = {
        "source_id": "fixture", "source": "Fixture board", "source_type": "fixture",
        "source_job_id": "1", "company": "Acme", "title": "Software Engineer",
        "location": "Austin, TX", "url": "https://example.com/jobs/1",
        "description": "Build backend services.",
    }
    row.update(overrides)
    return row


# --- region normalization and matching ---------------------------------------------------

def test_default_region_is_canada_and_validates_country_codes():
    assert settings.clean_region(None)["id"] == "ca"
    assert regions.normalize_region({"id": "us"}) == {
        "id": "us", "label": "United States", "terms": [], "include_remote": True}
    assert regions.normalize_region({"id": "gb"})["label"] == "United Kingdom"
    assert regions.normalize_region({"id": "global"})["id"] == "global"
    assert regions.normalize_region({"id": "custom", "label": "Europe", "terms": ["EMEA"]})["label"] == "Europe"


@pytest.mark.parametrize("location,region,expected", [
    ("Toronto, ON", "ca", True),
    ("Austin, TX", "us", True),
    ("New York, NY", "us", True),
    ("Berlin, Germany", "de", True),
    ("Munich", "de", True),
    ("London, England", "gb", True),
    ("Bengaluru, India", "in", True),
    ("Remote - Canada", "ca", True),
    ("Remote - United States", "ca", False),
    ("Remote - US", "us", True),
    ("Remote - UK", "gb", True),
    ("Remote - Canada", "us", False),
    ("Remote - not available in the United States", "us", False),
    ("Remote - outside the United States", "us", False),
    ("Remote - not available outside the United States", "us", True),
    ("Remote", "us", True),
    ("Tokyo, Japan", "us", False),
    ("Anywhere", "global", True),
])
def test_region_location_matching(location, region, expected):
    assert regions.matches(location, {"id": region}) is expected


def test_remote_can_be_excluded_and_custom_terms_work():
    assert regions.matches("Remote", {"id": "us", "include_remote": False}) is False
    custom = {"id": "custom", "label": "Europe", "terms": ["EMEA", "Europe"]}
    assert regions.matches("Remote - EMEA", custom) is True
    assert regions.matches("Berlin, Germany", custom) is False
    assert regions.matches("Berlin, Germany", custom | {"terms": ["Europe", "Germany"]}) is True


def test_region_catalog_starts_with_global_and_common_countries():
    catalog = regions.catalog()
    assert catalog[0] == {"id": "global", "label": "Anywhere (global)"}
    assert {"id": "ca", "label": "Canada"} in catalog
    assert {"id": "us", "label": "United States"} in catalog
    assert any(item["id"] == "custom" for item in catalog)


# --- levels ------------------------------------------------------------------------------

def test_profile_accepts_mid_senior_and_any_level():
    assert profile.normalize_profile({"levels": ["mid_level"]})["levels"] == ["mid_level"]
    assert profile.normalize_profile({"levels": ["senior_level", "internship"]})["levels"] == [
        "internship", "senior_level"]
    assert profile.normalize_profile({"levels": ["any"]})["levels"] == list(profile.ALL_LEVELS)
    # Invalid or missing selections keep the early-career default.
    assert profile.normalize_profile({"levels": ["bogus"]})["levels"] == list(profile.LEVELS)
    assert profile.normalize_profile({})["levels"] == list(profile.LEVELS)


def test_normalize_job_keeps_senior_roles_only_when_requested():
    senior = raw(title="Senior Software Engineer", description="5+ years of software engineering experience.")
    assert core.normalize_job(senior, NOW) is None
    job = core.normalize_job(senior, NOW, region={"id": "us"}, levels=profile.ALL_LEVELS)
    assert job and job["level"] == "senior_level"
    assert job["location"] == "Austin, TX"
    assert job["priority_reasons"] == ["United States opportunity"]


def test_normalize_job_keeps_mid_level_roles_and_respects_region():
    mid = raw(title="Software Developer", description="Build services.")
    job = core.normalize_job(mid, NOW, region={"id": "us"}, levels=profile.ALL_LEVELS)
    assert job and job["level"] == "mid_level"
    assert core.normalize_job({**mid, "location": "Toronto, ON"}, NOW,
                              region={"id": "us"}, levels=profile.ALL_LEVELS) is None


def test_normalize_job_global_region_accepts_any_location():
    job = core.normalize_job(raw(location="Sydney, Australia"), NOW,
                             region={"id": "global"}, levels=profile.ALL_LEVELS)
    assert job and job["location"] == "Sydney, Australia"


def test_collection_uses_region_and_level_preferences(tmp_path):
    db = tmp_path / "career.db"
    sources = [{"id": "fixture", "name": "Fixture board", "type": "fixture", "url": "https://example.com/board"}]

    def fetch(_source):
        return [raw(title="Senior Software Engineer",
                    description="6+ years of software engineering experience.")]

    early = run_collection(db, registry=sources, fetcher=fetch,
                           region={"id": "us"}, levels=profile.LEVELS)
    assert early["matched_rows"] == 0 and core.stats(db)["total"] == 0

    every = run_collection(db, registry=sources, fetcher=fetch,
                           region={"id": "us"}, levels=profile.ALL_LEVELS)
    assert every["matched_rows"] == 1
    stored = core.all_jobs(db)[0]
    assert stored["level"] == "senior_level" and stored["location"] == "Austin, TX"


def test_list_jobs_filters_by_region_and_level(tmp_path):
    db = tmp_path / "career.db"
    ca = raw(source_job_id="ca", title="Junior Software Engineer", location="Toronto, ON")
    us = raw(source_job_id="us", title="Senior Software Engineer",
             description="5+ years of software engineering experience.")
    core.upsert_jobs(db, [ca], region={"id": "ca"})
    core.upsert_jobs(db, [us], region={"id": "us"}, levels=profile.ALL_LEVELS)

    assert core.list_jobs(db, region={"id": "ca"})["total"] == 1
    assert core.list_jobs(db, region={"id": "us"}, levels=profile.ALL_LEVELS)["total"] == 1
    assert core.list_jobs(db, region={"id": "us"}, levels=profile.LEVELS)["total"] == 0


def test_settings_region_round_trip_and_rejects_unknown_ids(tmp_path):
    saved = settings.save({"region": {"id": "us", "terms": ["Bay Area"], "include_remote": False}}, tmp_path)
    assert saved["region"] == {"id": "us", "label": "United States", "terms": ["Bay Area"],
                               "include_remote": False}
    assert settings.load(tmp_path)["region"]["id"] == "us"
    with pytest.raises(settings.SettingsError):
        settings.save({"region": {"id": "not-a-country"}}, tmp_path)
