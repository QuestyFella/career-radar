"""Canada/qualification regressions for the approved code-only repair."""
from datetime import datetime, timezone
import socket

import pytest
from career_radar import core
NOW = datetime(2026, 10, 31, 14, tzinfo=timezone.utc)


def raw(**changes):
    return dict(source_id="fixture", source_job_id="fixture-A", source_type="greenhouse",
                company="Synthetic fixture", title="Machine Learning Intern",
                location="Toronto, ON, Canada", url="https://employer.example/jobs/A",
                description="Computer science students welcome.", verification="direct") | changes


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Eligibility checks must never use network access")
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


@pytest.mark.parametrize("location", [
    "Waterloo, Belgium", "Victoria, Malta", "Toronto, Italy", "Vancouver, Chile",
    "Waterloo, Democratic Republic of the Congo", "Cambridge, South Africa",
    "Toronto, Ontario, Belgium", "Waterloo, Netherlands", "Toronto, OR", "Waterloo, OR",
])
@pytest.mark.parametrize("hint", [None, "CA"])
def test_explicit_foreign_country_wins_over_city_or_hint(location, hint):
    assert core.normalize_job(raw(location=location, country_hint=hint), NOW) is None


@pytest.mark.parametrize("country", [country for country in core.pycountry.countries if country.alpha_2 != "CA"], ids=lambda country: country.alpha_2)
def test_every_iso_foreign_country_overrides_a_city_homonym_and_hint(country):
    assert core.normalize_job(raw(location=f"Waterloo, {country.name}", country_hint="CA"), NOW) is None


@pytest.mark.parametrize("location", [
    "Remote outside Canada", "Remote - Canada (not eligible)",
    "Remote - Canada [not available]", "Remote - outside of Canada",
    "Remote - not available to candidates in Canada", "Remote - Canada excluded",
    "Remote - anywhere except Canada", "Remote - excluding Canada",
])
def test_negated_canada_never_becomes_an_eligible_option(location):
    assert core.normalize_job(raw(location=location, country_hint="CA"), NOW) is None


@pytest.mark.parametrize("location,hint", [
    ("Toronto, ON", None), ("Toronto", None), ("Remote - Canada", None),
    ("Kamloops, BC, Canada", None), ("St. John's, Newfoundland and Labrador", None),
    ("Toronto, ON, Canada; Waterloo, Belgium", None), ("Canada / United States", None),
    ("Quebec City", "CA"), ("Victoria, BC", None), ("Cambridge, Ontario", None),
    ("Jordan, Ontario", None), ("Peru, ON", None),
])
def test_clear_canadian_options_stay_in_scope(location, hint):
    job = core.normalize_job(raw(location=location, country_hint=hint), NOW)
    assert job is not None
    assert "Belgium" not in job["location"]
    assert "United States" not in job["location"]


@pytest.mark.parametrize("description", [
    "A Masters degree is mandatory.",
    "Minimum qualifications: PhD in Computer Science.",
    "Required qualifications: Master's degree in Computer Science.",
    "A doctoral degree is essential.",
    "Applicants must possess U.S. citizenship.",
    "Only United States citizens may apply.",
    "U.S. citizenship is mandatory.",
    "Applicants are required to hold United States citizenship.",
    "Non-US citizens are not eligible.",
    "Minimum three years of professional software development experience.",
    "At least four years of software engineering experience.",
    "Five or more years of experience required.",
    "Minimum three (3) years of professional experience.",
    "Minimum three to five years of software development experience.",
])
def test_mandatory_requirements_are_not_softened_by_intern_title(description):
    assert core.normalize_job(raw(description=description), NOW) is None


@pytest.mark.parametrize("description", [
    "Minimum qualifications: Bachelor's or Master's degree in Computer Science.",
    "Bachelor's, Master's or PhD in Computer Science.",
    "Bachelor's degree required. Master's degree preferred.",
    "Preferred qualifications: a Master's degree in Computer Science.",
    "A Masters degree is optional. Undergraduates are welcome.",
    "Must collaborate with PhD researchers. Computer science undergraduates welcome.",
    "U.S. citizenship is not required.",
    "No United States citizenship required.",
    "Must possess U.S. citizenship or Canadian permanent residency.",
    "Canadian or U.S. citizenship is required.",
    "Cyprus citizenship is required. Verify citizenship eligibility.",
    "Non-US citizenship required. Verify citizenship eligibility.",
    "Our company has five years of industry experience. Computer science undergraduates welcome.",
    "We have 20 years of industry experience. Computer science undergraduates welcome.",
    "Three years of experience preferred.",
    "Three years of experience is not required.",
    "Zero to two years of professional experience.",
    "Up to two years of software development experience.",
])
def test_alternatives_preferences_and_nonrequirements_remain_matches(description):
    assert core.normalize_job(raw(description=description), NOW) is not None
