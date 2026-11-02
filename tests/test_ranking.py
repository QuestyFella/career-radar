"""Offline tests for career_radar.ranking.Matcher (synthetic jobs and profiles only)."""
from __future__ import annotations

import random
import time

import pytest

from career_radar.ranking import BASE_REASON, Matcher


def job(**overrides) -> dict:
    base = {"id": "j1", "title": "Software Developer Intern", "company": "Example Corp", "location": "Toronto, ON",
            "description": "Build internal tools.", "level": "internship", "category": "Software", "tags": [],
            "priority_score": 40, "priority_reasons": [BASE_REASON]}
    return base | overrides


def reasons(result) -> str:
    return " | ".join(result["reasons"])


# --------------------------------------------------------------------------- activity and passthrough

@pytest.mark.parametrize("profile", [None, {}, {"name": "Alex Example", "email": "alex@example.com"},
                                     {"levels": ["internship", "co_op", "new_grad", "entry_level"]},
                                     {"levels": []}, "junk"])
def test_inactive_profiles_pass_jobs_through(profile):
    matcher = Matcher(profile)
    assert matcher.active is False and matcher.terms == []
    original = job(priority_score=47, priority_reasons=["Canadian early-career opportunity", "Custom"])
    applied = matcher.apply(original)
    assert applied is not original and "match" not in original
    assert applied["priority_score"] == 47 and applied["priority_reasons"] == original["priority_reasons"]
    assert applied["match"] == {"score": 47, "reasons": original["priority_reasons"], "terms": [], "priority": False}
    assert matcher.score(original)["priority"] is False


@pytest.mark.parametrize("profile", [{"interests": ["embedded"]}, {"skills": ["Python"]},
                                     {"preferred_locations": ["Ottawa"]}, {"exclude_keywords": ["sales"]},
                                     {"levels": ["new_grad"]}])
def test_any_preference_activates(profile):
    assert Matcher(profile).active is True


def test_terms_are_the_profile_interests():
    assert Matcher({"interests": ["embedded", "aerospace"], "skills": ["C++"]}).terms == ["embedded", "aerospace"]


def test_apply_returns_new_dict_with_score_fields():
    matcher = Matcher({"interests": ["embedded"]})
    original = job(title="Embedded Software Intern")
    applied = matcher.apply(original)
    assert applied is not original and original["priority_score"] == 40 and "match" not in original
    assert applied["priority_score"] == applied["match"]["score"] == 60
    assert applied["priority_reasons"] == applied["match"]["reasons"]
    assert applied["priority_reasons"] is not applied["match"]["reasons"]


# --------------------------------------------------------------------------- levels

def test_level_bonus_and_penalty():
    matcher = Matcher({"levels": ["new_grad"]})
    wanted = matcher.score(job(level="new_grad"))
    assert wanted["score"] == 45
    assert wanted["reasons"] == [BASE_REASON, "Role level you want: New grad"]
    other = matcher.score(job(level="internship"))
    assert other["score"] == 20 and "Not one of your preferred role levels" in other["reasons"]
    labels = {"internship": "Internship", "co_op": "Co-op", "entry_level": "Entry level"}
    for level, label in labels.items():
        assert f"Role level you want: {label}" in Matcher({"levels": [level]}).score(job(level=level))["reasons"]


def test_all_levels_with_other_preferences_give_the_bonus():
    result = Matcher({"skills": ["Python"]}).score(job(level="co_op"))
    assert result["score"] == 45 and "Role level you want: Co-op" in result["reasons"]


# --------------------------------------------------------------------------- interests

def test_interest_in_title_is_strong_and_priority():
    result = Matcher({"interests": ["embedded"]}).score(job(title="Embedded Software Intern"))
    assert result["score"] == 60
    assert "Matches your interest: embedded" in result["reasons"]
    assert result["terms"] == ["embedded"] and result["priority"] is True


def test_interest_synonyms_tags_and_company():
    matcher = Matcher({"interests": ["embedded", "aerospace"]})
    synonym = matcher.score(job(title="Firmware Developer Co-op", level="co_op"))
    assert "Matches your interest: embedded" in synonym["reasons"]
    rtos = matcher.score(job(title="RTOS Test Intern"))
    assert "Matches your interest: embedded" in rtos["reasons"]
    company = matcher.score(job(company="Northwind Satellite Systems"))
    assert "Matches your interest: aerospace" in company["reasons"] and company["priority"]
    tagged = matcher.score(job(title="Software Intern", tags=["aerospace", "ontario"]))
    assert "Matches your interest: aerospace" in tagged["reasons"]
    both = matcher.score(job(title="Embedded Flight Software Intern", company="Example Space"))
    assert both["score"] == 40 + 5 + 30 and both["terms"] == ["embedded", "aerospace"]


def test_interest_cap_is_thirty():
    matcher = Matcher({"interests": ["embedded", "aerospace", "robotics"]})
    result = matcher.score(job(title="Embedded Robotics Intern", company="Example Aerospace"))
    assert result["score"] == 40 + 5 + 30
    assert sum(r.startswith("Matches your interest") for r in result["reasons"]) == 3


def test_interest_in_description_only_is_weak():
    matcher = Matcher({"interests": ["embedded", "aerospace", "robotics", "quantum"]})
    one = matcher.score(job(description="You will write firmware for our test rigs."))
    assert one["score"] == 50 and "Description mentions your interest: embedded" in one["reasons"]
    assert one["priority"] is False and one["terms"] == ["embedded"]
    many = matcher.score(job(description="Firmware for satellites, robots and quantum sensors."))
    assert many["score"] == 40 + 5 + 10   # capped at +10
    assert "Description mentions your interests: embedded, aerospace, robotics" in many["reasons"]


def test_interest_word_boundaries_and_boilerplate():
    matcher = Matcher({"interests": ["aerospace", "health", "security", "games"]})
    boring = job(title="Software Developer Intern", company="Gamestop-ish Example",
                 description="Comfortable office space downtown. Health and dental benefits. Security clearance "
                             "may be required. This is a game-changing role. Workspace provided.")
    result = matcher.score(boring)
    assert result["score"] == 45 and result["terms"] == []
    real = matcher.score(job(description="Our health records platform and space mission software."))
    assert "Description mentions your interests: aerospace, health" in real["reasons"]


def test_free_text_interest_matches_literally():
    matcher = Matcher({"interests": ["climate tech", "satellites"]})
    assert matcher.score(job(title="Climate-Tech Software Intern"))["priority"] is True
    assert matcher.score(job(title="Satellite Operations Intern"))["terms"] == ["satellites"]
    assert matcher.score(job(title="Software Intern"))["terms"] == []


def test_short_interest_needs_capitals():
    matcher = Matcher({"interests": ["ml"]})   # alias of machine learning
    assert matcher.score(job(title="ML Engineer Intern"))["priority"] is True
    assert matcher.score(job(title="Software Intern", description="Add 500 ml of water"))["terms"] == []


# --------------------------------------------------------------------------- skills

def test_skills_points_cap_and_reason_list():
    matcher = Matcher({"skills": ["Python", "C++", "Linux", "Docker", "Git", "SQL", "Rust", "Kubernetes", "AWS"]})
    result = matcher.score(job(description="We use Python, C++, Linux, Docker, Git, SQL, Rust, Kubernetes and AWS."))
    assert result["score"] == 40 + 5 + 21
    assert "9 of your skills: Python, C++, Linux, Docker, Git" in result["reasons"]
    assert result["terms"][:3] == ["Python", "C++", "Linux"]
    small = matcher.score(job(description="Python and Linux experience."))
    assert small["score"] == 40 + 5 + 6 and "2 of your skills: Python, Linux" in small["reasons"]


@pytest.mark.parametrize("text, expected", [
    ("Experience with CAD tools", False),
    ("Present to C-level executives", False),
    ("Report to the C-suite", False),
    ("We closed our Series C last year", False),
    ("Copyright (C) Example Corp", False),
    ("Install it to C:\\Tools", False),
    ("Keep sensors below 40°C", False),
    ("Objective-C and C# developers", False),
    ("Strong C programming skills", True),
    ("Languages: C, Python", True),
    ("Embedded C/C++ firmware", True),
])
def test_c_language_boundaries(text, expected):
    result = Matcher({"skills": ["C"]}).score(job(description=text))
    assert ("C" in result["terms"]) is expected, text


@pytest.mark.parametrize("skill, text, expected", [
    ("C++", "Modern C++17 codebase", True),
    ("C++", "C and Python only", False),
    ("C#", "Unity games in C#.", True),
    ("C#", "C and C++", False),
    (".NET", "Back end in ASP.NET Core", True),
    (".NET", "Visit example.net for details", False),
    ("Node.js", "APIs in Node.js and TypeScript", True),
    ("Node.js", "graph node js-like", False),
    ("Go", "Microservices in Go and Rust", True),
    ("Go", "Our Golang services", True),
    ("Go", "Ready to go the extra mile", False),
    ("Go", "Let's Go! Join us", False),
    ("R", "Statistics in R and Python", True),
    ("R", "Join our R&D team", False),
    ("Java", "JavaScript frontends", False),
    ("Java", "Java backend", True),
    ("CAN bus", "CAN bus diagnostics", True),
    ("CAN bus", "you can bus tables", False),
    ("Excel", "Excel at teamwork", False),
    ("Excel", "Advanced Excel and Power BI", True),
    ("React", "Frontends in React and TypeScript", True),
    ("React", "you react to incidents", False),
    ("Spring", "Spring 2027 co-op term", False),
    ("Spring", "Java and Spring microservices", True),
])
def test_skill_boundaries(skill, text, expected):
    result = Matcher({"skills": [skill]}).score(job(title="Software Intern", description=text))
    assert (skill in result["terms"]) is expected, (skill, text)


def test_skill_in_title_counts_and_description_is_capped_at_6000_chars():
    matcher = Matcher({"skills": ["Rust"]})
    assert matcher.score(job(title="Rust Developer Intern", description=""))["terms"] == ["Rust"]
    far = job(description="x " * 3000 + " Rust")
    assert len(far["description"]) > 6000 and matcher.score(far)["terms"] == []


# --------------------------------------------------------------------------- locations

@pytest.mark.parametrize("preference, location, expected", [
    ("Toronto", "Toronto, ON", True),
    ("toronto", "TORONTO, Ontario, Canada", True),
    ("Montréal", "Montreal, QC", True),
    ("Montreal", "Montréal, Québec", True),
    ("Remote", "Remote (Canada)", True),
    ("remote", "Hybrid / remote-friendly", True),
    ("Ontario", "Waterloo, ON", True),
    ("Ontario", "Ottawa, Ontario", True),
    ("Ontario", "Vancouver, BC", False),
    ("Texas", "Austin, TX", True),
    ("California", "San Francisco, CA", True),
    ("Texas", "Austin, Ontario", False),
    ("Toronto, ON", "Toronto, Ontario", True),
    ("York", "New York, NY", True),
    ("York", "Yorkton, SK", False),
    ("Ottawa", "Toronto, ON", False),
    ("BC", "Burnaby, BC", True),
])
def test_preferred_locations(preference, location, expected):
    result = Matcher({"preferred_locations": [preference]}).score(job(location=location))
    assert (f"In a location you prefer: {preference}" in result["reasons"]) is expected
    assert result["score"] == (55 if expected else 45)


def test_location_bonus_counts_once():
    result = Matcher({"preferred_locations": ["Toronto", "Ontario", "Remote"]}).score(
        job(location="Toronto, ON or Remote"))
    assert result["score"] == 55 and sum(r.startswith("In a location") for r in result["reasons"]) == 1


# --------------------------------------------------------------------------- excludes and priority

def test_excluded_title_word_penalizes_and_blocks_priority():
    matcher = Matcher({"interests": ["embedded"], "exclude_keywords": ["sales", "Senior"]})
    result = matcher.score(job(title="Embedded Sales Engineer Intern"))
    assert result["score"] == 40 + 5 + 15 - 30
    assert "Title contains an excluded word: sales" in result["reasons"]
    assert result["priority"] is False
    assert matcher.score(job(title="Senior Embedded Engineer"))["priority"] is False
    assert "Title contains an excluded word" not in reasons(matcher.score(job(title="Wholesale Embedded Intern")))
    assert matcher.score(job(title="Embedded Intern", description="Sales team nearby"))["priority"] is True


def test_priority_from_high_score_without_title_interest():
    matcher = Matcher({"interests": ["embedded", "aerospace"], "preferred_locations": ["Toronto"],
                       "skills": ["Python", "C++", "Linux", "Git", "Docker", "SQL", "Bash"]})
    result = matcher.score(job(title="Software Developer Intern", description=(
        "Write firmware for satellites using Python, C++, Linux, Git, Docker, SQL and Bash.")))
    assert result["score"] == 40 + 5 + 10 + 21 + 10 and result["score"] >= 75
    assert result["priority"] is True
    assert not any(r.startswith("Matches your interest") for r in result["reasons"])


def test_score_is_clamped():
    matcher = Matcher({"levels": ["new_grad"], "exclude_keywords": ["intern", "software", "developer"]})
    assert matcher.score(job(title="Software Developer Intern"))["score"] == 0
    high = Matcher({"interests": ["embedded", "aerospace"], "preferred_locations": ["Toronto"],
                    "skills": ["Python", "C++", "Linux", "Git", "Docker", "SQL", "Bash", "Rust"]})
    best = high.score(job(title="Embedded Aerospace Intern", description="Python C++ Linux Git Docker SQL Bash Rust"))
    assert best["score"] == 100


def test_missing_or_odd_job_fields_do_not_crash():
    matcher = Matcher({"interests": ["embedded"], "skills": ["C"], "preferred_locations": ["Toronto"],
                       "exclude_keywords": ["sales"], "levels": ["internship"]})
    for odd in ({}, {"title": None, "description": None, "tags": None, "location": None},
                {"title": 123, "tags": "embedded", "level": "bogus"}, {"description": "é" * 7000 + " C"}):
        result = matcher.score(odd)
        assert 0 <= result["score"] <= 100 and result["reasons"][0] == BASE_REASON


def test_non_ascii_text_and_terms():
    matcher = Matcher({"interests": ["énergie"], "skills": ["I2C"], "preferred_locations": ["Québec"]})
    result = matcher.score(job(title="Stagiaire en énergie", description="Capteurs I²C", location="Québec, QC"))
    assert result["terms"] == ["énergie", "I2C"]
    assert "In a location you prefer: Québec" in result["reasons"]


# --------------------------------------------------------------------------- performance

PROSE = ("We are looking for a curious Software Developer Intern to join our platform team in Toronto. You will "
         "design, build and test services that help thousands of customers across Canada manage their accounts. "
         "Our stack includes Python, TypeScript and PostgreSQL running on AWS, plus embedded C++ firmware for our "
         "satellite ground stations. You will collaborate with product managers and designers, write clean, "
         "well-tested code, take part in code reviews and learn how we ship software safely every day. "
         "Requirements: currently enrolled in Computer Science, Software Engineering or a related program; Git; "
         "strong communication skills. Nice to have: Docker, Kubernetes, React, CI/CD, Linux, Series C startup "
         "experience, CAD tools. We offer competitive pay, health and dental benefits, office space downtown and "
         "a hybrid work model. Équipe bilingue à Montréal. ")


def test_scoring_three_thousand_jobs_is_fast():
    rng = random.Random(11)
    titles = ["Software Developer Intern", "Embedded Firmware Co-op", "Data Analyst New Grad", "Sales Intern",
              "Junior Web Developer", "Machine Learning Intern", "Hardware Test Technician", "Cloud Engineer I"]
    words = PROSE.split()
    jobs = []
    for i in range(3000):
        rng.shuffle(words)
        jobs.append(job(id=str(i), title=rng.choice(titles), company=f"Example {i % 50}",
                        location=rng.choice(["Toronto, ON", "Montréal, QC", "Remote (Canada)", "Vancouver, BC"]),
                        level=rng.choice(["internship", "co_op", "new_grad", "entry_level"]),
                        tags=rng.choice([[], ["aerospace"], ["embedded", "ontario"], ["remote"]]),
                        description=(" ".join(words) + " ") * 9))   # ~8,000 chars; 6,000 are scanned
    profile = {"interests": ["embedded", "aerospace", "machine learning", "robotics", "security", "climate tech"],
               "skills": ["Python", "C", "C++", "C#", "Go", "R", "Rust", "JavaScript", "TypeScript", "SQL", "Linux",
                          "Git", "Docker", "Kubernetes", "AWS", "React", "Node.js", ".NET", "PyTorch", "ROS",
                          "FreeRTOS", "STM32", "KiCad", "CAN bus", "SPI", "I2C", "MATLAB", "Bash", "Verilog",
                          "Excel"],
               "preferred_locations": ["Toronto", "Ontario", "Remote"],
               "exclude_keywords": ["sales", "senior"], "levels": ["internship", "co_op", "new_grad"]}
    timings = []
    for _ in range(2):   # best of two: a busy machine should not fail the test
        started = time.perf_counter()
        matcher = Matcher(profile)
        results = [matcher.apply(j) for j in jobs]
        timings.append(time.perf_counter() - started)
    assert len(results) == 3000 and all(0 <= r["priority_score"] <= 100 for r in results)
    assert any(r["match"]["priority"] for r in results) and not all(r["match"]["priority"] for r in results)
    sample = Matcher(profile).score(job(title="Software Developer Intern", description=PROSE))
    assert {"Python", "C++", "AWS", "Linux"} <= set(sample["terms"]) and "C" not in sample["terms"]
    assert min(timings) < 2.0, f"ranking 3,000 jobs took {min(timings):.2f}s"
