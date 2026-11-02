"""Synthetic fixtures for the source expansion: SmartRecruiters, Workable, Recruitee and
BambooHR adapters, Workday country-facet discovery, title prefiltering and the optional
discovered registry. The imported autouse guard forbids real DNS and sockets."""
from __future__ import annotations

import json

import pytest

from career_radar import collect
from career_radar import config
from career_radar import core
from career_radar import extras
from test_extras import assert_contract, mock_http, no_live_network  # noqa: F401 - autouse network guard


def src(kind, **kw):
    return {"id": f"fixture-{kind}", "name": "Fixture Employer", "type": kind,
            "url": "https://fixture.invalid/careers", "canada_only": True, **kw}


# --- core title gate -------------------------------------------------------------------

TITLES = [
    "Software Developer Intern", "Senior Software Engineer", "Software Engineering Manager",
    "Engineering Team Lead", "Lead Software Engineer Intern", "Graduate Analyst", "Associate",
    "Research Intern", "PhD Research Intern", "Master's Thesis Student, Robotics",
    "Marketing Coordinator", "Data Analyst", "Embedded Firmware Co-op", "Trainee",
    "Branch Manager Trainee", "Principal Architect", "Junior QA Tester", "Stagiaire développeur",
    "Analyste de données", "Technical Support Specialist", "Accountant", "Developer III",
    "Business Analyst, New Grad", "Research Assistant", "Junk Removal Specialist",
    "Civil Engineering Intern", "Business Development Associate", "Store Associate", "AI Trainer",
    "Environmental Software Developer", "Linux Intern", "Data Annotator", "Mechanical Engineering Co-op",
]
DESCRIPTIONS = [
    "", "Pursuing a computer science degree. 0-2 years of experience.",
    "Entry level role for recent graduates in software engineering.",
    "Minimum of 5 years of experience required.",
]


def test_title_gate_never_rejects_a_title_infer_level_would_accept():
    for title in TITLES:
        for description in DESCRIPTIONS:
            for hint in (None, "internship", "entry_level"):
                if core.infer_level(title, description, hint) is not None:
                    assert core.title_could_match(title), (title, description, hint)


@pytest.mark.parametrize("title,expected", [
    ("Software Developer Intern", True), ("Senior Software Engineer", False),
    ("Software Engineering Manager", False), ("Engineering Intern", True),
    ("Graduate Analyst", True), ("Marketing Coordinator", False), ("PhD Research Intern", False),
    ("Research Intern", True), ("Junk Removal Specialist", False), ("Embedded Firmware Co-op", True),
    # Other disciplines/occupations unless the title also names computing or aerospace work.
    ("Civil Engineering Intern", False), ("Junior Structural Engineer", False), ("Data Entry Clerk", False),
    ("AI Tutor - Freelance", False), ("Security Guard - Student", False), ("Business Development Representative", False),
    ("Early Talent Recruiter", False), ("Ingénieure ou Ingénieur en géotechnique", False),
    ("Aerospace Structural Engineer Intern", True), ("Environmental Software Developer", True),
    ("Geomatics Engineer Intern - GNSS Algorithms", True), ("IT Security Officer", True),
    ("Marketing Data Analyst", True), ("Construction Innovation Systems Analyst", True),
    # Short tokens are word-bounded ("UI" in Guide, "product" in Production, "test" in Contest).
    ("Guide", False), ("Production Worker", False), ("Contest Coordinator", False), ("Mobile Crane Operator", False),
    ("UI Developer", True), ("Production Support Analyst", True), ("Junior QA Tester", True),
    ("Linux Intern", True), ("Circuit Analysis Co-op", True), ("ASIC Design Engineer", True),
])
def test_title_gate_examples(title, expected):
    assert core.title_could_match(title) is expected


@pytest.mark.parametrize("location,canadian", [
    ("Hoofddorp HQ, NL", False), ("Delft, NL", False), ("Recife, PE", False), ("Bratislava, SK", False),
    ("NZ-CAN-CHRISTCHURCH-115-1 ~ 115 Orchard Rd ~ V2500 WORKSHOP", False),
    ("San Luis Obispo, Ontario, Laguna Hills, San Diego, Portland, Folsom", False),
    ("Mount Pearl, NL, Canada", True), ("St. John's, NL; Canada", True), ("Charlottetown, PE", True),
    ("Saskatoon, SK", True), ("CA - Esterhazy, SK; Canada", True), ("CAN-Alberta-Acheson; Canada", True),
    ("Volante - CAN; Canada", True), ("Toronto, ON, CAN; Canada", True), ("Ottawa, Ontario", True),
    ("San Francisco, Palo Alto, Los Angeles, Toronto", True), ("Waterloo, Ontario; Canada", True),
])
def test_shared_province_codes_and_lookalike_places(location, canadian):
    assert bool(core.canadian_options(location)) is canadian


@pytest.mark.parametrize("title,expected", [
    ("Software Developer Intern", True), ("Associate", False), ("Store Associate", False),
    ("Graduate Analyst", False), ("Data Analyst", True), ("Research Intern", False), ("Senior Data Engineer", False),
])
def test_technical_title_requires_a_technical_word(title, expected):
    assert core.technical_title(title) is expected


# --- Workday ---------------------------------------------------------------------------

def wd_detail(n, title):
    return {"jobPostingInfo": {"id": f"wd-{n}", "title": title, "jobDescription": f"<p>{title} work.</p>",
                               "startDate": "2026-10-30", "location": "Toronto, ON", "jobReqId": f"R{n}",
                               "country": {"descriptor": "Canada"}, "timeType": "Full time"}}


def wd_row(n, title):
    return {"title": title, "externalPath": f"/job/Toronto/Fixture_{n}", "locationsText": "Toronto, ON",
            "bulletFields": [f"R{n}"]}


NESTED_FACETS = [
    {"facetParameter": "timeType", "descriptor": "Full Time or Part Time",
     "values": [{"descriptor": "Full time", "id": "ft", "count": 3}]},
    {"facetParameter": "locationMainGroup", "values": [
        {"facetParameter": "locationCountry", "descriptor": "Location Country",
         "values": [{"descriptor": "Japan", "id": "jp-id", "count": 1}, {"descriptor": "Canada", "id": "ca-id", "count": 2}]},
        {"facetParameter": "locations", "descriptor": "Locations",
         "values": [{"descriptor": "Toronto", "id": "to-id", "count": 2}]}]},
]


def test_workday_discovers_nested_location_country_facet(monkeypatch):
    first = {"total": 3, "jobPostings": [wd_row(9, "Software Co-op")], "facets": NESTED_FACETS}
    page = {"total": 1, "jobPostings": [wd_row(1, "Software Developer Co-op")], "facets": []}
    calls = mock_http(monkeypatch, [first, page, wd_detail(1, "Software Developer Co-op")])
    jobs = extras.fetch_source(src("workday", url="https://fixture.wd3.myworkdayjobs.com/External",
                                   tenant="fixture", site="External", company="Fixture", country="Canada"))
    assert calls[1][2]["json"]["appliedFacets"] == {"locationCountry": ["ca-id"]}
    assert len(jobs) == 1 and jobs[0]["country_hint"] == "CA" and jobs[0]["details_fetched"] is True


@pytest.mark.parametrize("descriptor", ["Country", "Country/Region", "Countries", "Location Country"])
def test_workday_country_facet_aliases(monkeypatch, descriptor):
    facets = [{"facetParameter": "cf", "descriptor": descriptor,
               "values": [{"descriptor": "Canada", "id": "ca", "count": 1}]}]
    first = {"total": 1, "jobPostings": [wd_row(1, "Data Analyst")], "facets": facets}
    calls = mock_http(monkeypatch, [first, {"total": 1, "jobPostings": [wd_row(1, "Data Analyst")]},
                                    wd_detail(1, "Data Analyst")])
    extras.fetch_source(src("workday", url="https://fixture.wd3.myworkdayjobs.com/External",
                            tenant="fixture", site="External", company="Fixture", country="Canada"))
    assert calls[1][2]["json"]["appliedFacets"] == {"cf": ["ca"]}


def test_workday_without_country_facet_still_refuses_unfiltered_query(monkeypatch):
    first = {"total": 1, "jobPostings": [wd_row(1, "Data Analyst")],
             "facets": [{"facetParameter": "timeType", "descriptor": "Time Type", "values": []}]}
    mock_http(monkeypatch, [first])
    with pytest.raises(extras.SourceError, match="facet"):
        extras.fetch_source(src("workday", url="https://fixture.wd3.myworkdayjobs.com/External",
                                tenant="fixture", site="External", company="Fixture", country="Canada"))


def _wd_fallback(monkeypatch, facets):
    first = {"total": 2, "jobPostings": [wd_row(1, "Data Analyst")], "facets": facets}
    calls = mock_http(monkeypatch, [first, {"total": 1, "jobPostings": [wd_row(1, "Data Analyst")]},
                                    wd_detail(1, "Data Analyst")])
    jobs = extras.fetch_source(src("workday", url="https://fixture.wd3.myworkdayjobs.com/External",
                                   tenant="fixture", site="External", company="Fixture", country="Canada"))
    assert len(jobs) == 1 and jobs[0]["country_hint"] == "CA"
    return calls[1][2]["json"]["appliedFacets"]


def test_workday_uses_a_nested_country_list_without_a_country_facet(monkeypatch):
    facets = [{"facetParameter": "locationMainGroup", "values": [
        {"facetParameter": "locationHierarchy1", "descriptor": "Locations",
         "values": [{"descriptor": "Brazil", "id": "br"}, {"descriptor": "Canada", "id": "ca"}]},
        {"facetParameter": "locations", "descriptor": "Sites", "values": [{"descriptor": "Canada, Toronto", "id": "to"}]}]}]
    assert _wd_fallback(monkeypatch, facets) == {"locationHierarchy1": ["ca"]}


def test_workday_falls_back_to_every_canadian_location_value(monkeypatch):
    places = [("Canada, Toronto", "to"), ("Calgary", "yyc"), ("Canada - Ottawa (Bill Leathem)", "ott"),
              ("CA-QC-LONGUEUIL-1000 Marie-Victorin", "lgl"), ("London", "lon"), ("Ontario, CA", "ont-ca"),
              ("US-CA-San Jose", "sj"), ("Germany, Munich", "muc"), ("Victoria", "vic")]
    facets = [{"facetParameter": "jobFamilyGroup", "descriptor": "Job Category", "values": [{"descriptor": "Canada", "id": "x"}]},
              {"facetParameter": "locationMainGroup", "values": [
                  {"facetParameter": "locations", "descriptor": "Locations",
                   "values": [{"descriptor": d, "id": i} for d, i in places]}]}]
    assert _wd_fallback(monkeypatch, facets) == {"locations": ["to", "yyc", "ott", "lgl"]}


def test_workday_location_facet_without_canadian_values_still_refuses(monkeypatch):
    facets = [{"facetParameter": "locations", "descriptor": "Locations",
               "values": [{"descriptor": "London", "id": "lon"}, {"descriptor": "Germany, Munich", "id": "muc"}]}]
    mock_http(monkeypatch, [{"total": 1, "jobPostings": [wd_row(1, "Data Analyst")], "facets": facets}])
    with pytest.raises(extras.SourceError, match="unavailable"):
        extras.fetch_source(src("workday", url="https://fixture.wd3.myworkdayjobs.com/External",
                                tenant="fixture", site="External", company="Fixture", country="Canada"))


def test_workday_details_only_titles_that_could_match_with_early_career_first(monkeypatch):
    rows = [wd_row(1, "Senior Manager, Finance"), wd_row(2, "Data Analyst"), wd_row(3, "Software Developer Intern")]
    first = {"total": 3, "jobPostings": rows}
    calls = mock_http(monkeypatch, [first, wd_detail(3, "Software Developer Intern")])
    with pytest.raises(extras.SourceTruncated) as caught:
        extras.fetch_source(src("workday", url="https://fixture.wd3.myworkdayjobs.com/External",
                                tenant="fixture", site="External", company="Fixture", detail_limit=1))
    assert [c[1].rsplit("/", 1)[-1] for c in calls[1:]] == ["Fixture_3"]
    partial = {j["title"]: j for j in caught.value.partial_jobs}
    assert len(partial) == 3 and partial["Software Developer Intern"]["details_fetched"] is True
    assert partial["Data Analyst"]["details_fetched"] is False
    assert partial["Senior Manager, Finance"]["details_fetched"] is False


def test_workday_irrelevant_titles_are_kept_without_detail_requests(monkeypatch):
    rows = [wd_row(1, "Senior Manager, Finance"), wd_row(2, "Data Analyst")]
    calls = mock_http(monkeypatch, [{"total": 2, "jobPostings": rows}, wd_detail(2, "Data Analyst")])
    jobs = extras.fetch_source(src("workday", url="https://fixture.wd3.myworkdayjobs.com/External",
                                   tenant="fixture", site="External", company="Fixture", detail_limit=5))
    assert len(calls) == 2 and len(jobs) == 2
    for job in jobs:
        assert_contract(job)


# --- SmartRecruiters -------------------------------------------------------------------

SR_API = "https://api.smartrecruiters.com/v1/companies/FixtureCo/postings"


def sr_posting(n, title="Software Developer Intern", country="ca"):
    city, place = ("Toronto", "Toronto, Ontario, Canada") if country == "ca" else ("Austin", "Austin, TX, United States")
    return {"id": str(7000 + n), "name": title, "company": {"identifier": "FixtureCo", "name": "Fixture Co"},
            "releasedDate": "2026-10-30T17:20:17.366Z", "typeOfEmployment": {"label": "Intern"},
            "location": {"city": city, "country": country, "remote": False, "fullLocation": place}}


def sr_detail(n, title="Software Developer Intern"):
    return {"id": str(7000 + n), "name": title,
            "postingUrl": f"https://jobs.smartrecruiters.com/FixtureCo/{7000 + n}-software-developer-intern",
            "jobAd": {"sections": {
                "companyDescription": {"text": "<p>About us: we hire across the world.</p>"},
                "jobDescription": {"text": "<p>Build flight software in C++.</p>"},
                "qualifications": {"text": "<ul><li>Enrolled in computer science.</li></ul>"},
                "additionalInformation": {"text": "<p>Application Deadline: 2026-11-20</p>"}}}}


def test_smartrecruiters_paginates_canadian_postings_and_fetches_relevant_details(monkeypatch):
    page1 = {"offset": 0, "limit": 2, "totalFound": 3,
             "content": [sr_posting(1), sr_posting(2, "Senior Director, Finance")]}
    page2 = {"offset": 2, "limit": 2, "totalFound": 3, "content": [sr_posting(3, "Junior Embedded Developer")]}
    calls = mock_http(monkeypatch, [page1, page2, sr_detail(1), sr_detail(3, "Junior Embedded Developer")])
    jobs = extras.fetch_source(src("smartrecruiters", company_id="FixtureCo", company="Fixture Co", page_size=2))
    assert [c[1] for c in calls] == [SR_API, SR_API, SR_API + "/7001", SR_API + "/7003"]
    assert calls[0][2]["params"] == {"limit": 2, "offset": 0, "country": "ca"}
    assert calls[1][2]["params"]["offset"] == 2
    assert len(jobs) == 3
    intern = next(j for j in jobs if j["source_job_id"] == "7001")
    assert intern["url"] == "https://jobs.smartrecruiters.com/FixtureCo/7001-software-developer-intern"
    assert intern["company"] == "Fixture Co" and intern["country_hint"] == "CA"
    assert intern["verification"] == "direct" and intern["details_fetched"] is True
    assert "flight software" in intern["description"] and "About us" not in intern["description"]
    assert intern["deadline"] == "2026-11-20"
    assert intern["posted_at"] == "2026-10-30T17:20:17.366Z"
    senior = next(j for j in jobs if j["source_job_id"] == "7002")
    assert senior["details_fetched"] is False and senior["url"] == "https://jobs.smartrecruiters.com/FixtureCo/7002"
    for job in jobs:
        assert_contract(job)


def test_smartrecruiters_short_snapshot_is_partial_not_complete(monkeypatch):
    page = {"offset": 0, "limit": 2, "totalFound": 5, "content": [sr_posting(1)]}
    mock_http(monkeypatch, [page, {"offset": 1, "limit": 2, "totalFound": 5, "content": []}])
    with pytest.raises(extras.SourceTruncated) as caught:
        extras.fetch_source(src("smartrecruiters", company_id="FixtureCo", page_size=2, detail_limit=0))
    assert len(caught.value.partial_jobs) == 1


def test_smartrecruiters_empty_board_and_invalid_identifier(monkeypatch):
    mock_http(monkeypatch, [{"offset": 0, "limit": 100, "totalFound": 0, "content": []}])
    assert extras.fetch_source(src("smartrecruiters", company_id="FixtureCo")) == []
    with pytest.raises(extras.SourceError, match="identifier"):
        extras.fetch_source(src("smartrecruiters", company_id="../admin"))


def test_smartrecruiters_blank_ad_keeps_the_board_but_missing_schema_fails(monkeypatch):
    """Live case: an employer published an ad whose sections are all empty strings. That
    posting is kept without a description; the rest of the board still imports."""
    blank = sr_detail(2, "Junior Data Developer")
    blank["jobAd"]["sections"] = {k: {"title": k, "text": ""} for k in
                                  ("companyDescription", "jobDescription", "qualifications", "additionalInformation")}
    page = {"offset": 0, "limit": 100, "totalFound": 2,
            "content": [sr_posting(1), sr_posting(2, "Junior Data Developer")]}
    mock_http(monkeypatch, [page, sr_detail(1), blank])
    jobs = extras.fetch_source(src("smartrecruiters", company_id="FixtureCo", company="Fixture Co"))
    by_id = {j["source_job_id"]: j for j in jobs}
    assert "flight software" in by_id["7001"]["description"]
    assert by_id["7002"]["description"] == "" and by_id["7002"]["details_fetched"] is True
    page = {"offset": 0, "limit": 100, "totalFound": 1, "content": [sr_posting(1)]}
    mock_http(monkeypatch, [page, {"id": "7001", "name": "Software Developer Intern"}])
    with pytest.raises(extras.SourceError, match="missing jobAd sections") as caught:
        extras.fetch_source(src("smartrecruiters", company_id="FixtureCo"))
    assert len(caught.value.partial_jobs) == 1  # fetched rows survive the schema failure


# --- Workable --------------------------------------------------------------------------

def test_workable_widget_rows_keep_canadian_postings(monkeypatch):
    payload = {"name": "Fixture Co", "description": "", "jobs": [
        {"title": "Firmware Engineering Co-op", "shortcode": "ABC123", "url": "https://apply.workable.com/j/ABC123",
         "published_on": "2026-10-20", "country": "Canada", "city": "Waterloo", "state": "Ontario",
         "telecommuting": False, "employment_type": "Internship",
         "locations": [{"country": "Canada", "countryCode": "CA", "city": "Waterloo", "region": "Ontario", "hidden": False}],
         "description": "<p>Write RTOS firmware.</p><p>Apply by November 20, 2026.</p>"},
        {"title": "Software Engineer", "shortcode": "US999", "url": "https://apply.workable.com/j/US999",
         "published_on": "2026-10-21", "country": "United States", "city": "Austin", "state": "Texas", "telecommuting": True,
         "locations": [{"country": "United States", "countryCode": "US", "city": "Austin", "region": "Texas", "hidden": False}],
         "description": "<p>US role.</p>"}]}
    calls = mock_http(monkeypatch, [payload])
    jobs = extras.fetch_source(src("workable", board="fixtureco"))
    assert calls[0][1] == "https://apply.workable.com/api/v1/widget/accounts/fixtureco"
    assert calls[0][2]["params"] == {"details": "true"}
    assert len(jobs) == 1
    job = jobs[0]
    assert (job["company"], job["location"], job["url"]) == ("Fixture Co", "Waterloo, Ontario, Canada", "https://apply.workable.com/j/ABC123")
    assert job["source_job_id"] == "ABC123" and job["posted_at"] == "2026-10-20"
    assert job["deadline"] == "2026-11-20" and job["country_hint"] == "CA" and job["verification"] == "direct"
    assert_contract(job)


def test_workable_rejects_unexpected_schema(monkeypatch):
    mock_http(monkeypatch, [{"name": "Fixture"}])
    with pytest.raises(extras.SourceError):
        extras.fetch_source(src("workable", board="fixtureco"))


# --- Recruitee -------------------------------------------------------------------------

def rc_offer(n, **kw):
    offer = {"id": n, "title": "Embedded Software Intern", "status": "published",
             "careers_url": f"https://fixtureco.recruitee.com/o/embedded-intern-{n}",
             "published_at": "2026-10-27 17:42:41 UTC", "close_at": "2026-11-30 23:59:00 UTC",
             "company_name": "Fixture Co", "description": "<p>Build drivers.</p>",
             "requirements": "<ul><li>Studying computer engineering.</li></ul>", "remote": False,
             "locations": [{"city": "Ottawa", "state": "Ontario", "country": "Canada", "country_code": "CA"}]}
    offer.update(kw)
    return offer


def test_recruitee_offers_use_published_rows_and_close_at_fallback(monkeypatch):
    us = rc_offer(3, locations=[{"city": "Austin", "state": "Texas", "country": "United States", "country_code": "US"}])
    calls = mock_http(monkeypatch, [{"offers": [rc_offer(1), rc_offer(2, status="draft"), us]}])
    jobs = extras.fetch_source(src("recruitee", board="fixtureco"))
    assert calls[0][1] == "https://fixtureco.recruitee.com/api/offers/"
    assert len(jobs) == 1
    job = jobs[0]
    assert job["location"] == "Ottawa, Ontario, Canada" and job["country_hint"] == "CA"
    assert job["posted_at"] == "2026-10-27T17:42:41+00:00"
    assert job["deadline"] == "2026-11-30T23:59:00+00:00"
    assert job["deadline_evidence"] == "Recruitee close_at: 2026-11-30 23:59:00 UTC"
    assert "Studying computer engineering" in job["description"] and job["company"] == "Fixture Co"
    assert_contract(job)


def test_recruitee_published_prose_deadline_wins_over_close_at(monkeypatch):
    offer = rc_offer(1, description="<p>Application Deadline: 2026-11-20</p>")
    mock_http(monkeypatch, [{"offers": [offer]}])
    job = extras.fetch_source(src("recruitee", board="fixtureco"))[0]
    assert job["deadline"] == "2026-11-20" and job["deadline_evidence"].startswith("Application Deadline")


def test_recruitee_rejects_invalid_subdomain():
    with pytest.raises(extras.SourceError, match="identifier"):
        extras.fetch_source(src("recruitee", board="evil.example.com/x"))


# --- BambooHR --------------------------------------------------------------------------

def test_bamboohr_fetches_details_only_for_relevant_canadian_rows(monkeypatch):
    listing = {"meta": {}, "result": [
        {"id": "41", "jobOpeningName": "Robotics Software Co-op", "location": {"city": "Kitchener", "state": "Ontario"},
         "atsLocation": {"country": "Canada", "state": "Ontario", "province": None, "city": "Kitchener"},
         "employmentStatusLabel": "Co-op"},
        {"id": "42", "jobOpeningName": "Technical Support Specialist", "location": {"city": "Cali", "state": "Colombia"},
         "atsLocation": {"country": None, "state": None, "province": None, "city": None}},
        {"id": "43", "jobOpeningName": "VP, Sales", "location": {"city": None, "state": None},
         "atsLocation": {"country": "Canada", "state": "Ontario", "province": None, "city": "Remote"}}]}
    detail = {"result": {"jobOpening": {
        "jobOpeningShareUrl": "https://fixtureco.bamboohr.com/careers/41", "jobOpeningName": "Robotics Software Co-op",
        "description": "<p>ROS 2 work. Application Deadline: 2026-11-25</p>", "datePosted": "2026-10-15",
        "location": {"city": "Kitchener", "state": "Ontario", "addressCountry": "Canada"},
        "employmentStatusLabel": "Co-op"}}}
    calls = mock_http(monkeypatch, [listing, detail])
    jobs = extras.fetch_source(src("bamboohr", board="fixtureco", company="Fixture Co"))
    assert [c[1] for c in calls] == ["https://fixtureco.bamboohr.com/careers/list",
                                     "https://fixtureco.bamboohr.com/careers/41/detail"]
    by_id = {j["source_job_id"]: j for j in jobs}
    assert set(by_id) == {"41", "43"}
    coop = by_id["41"]
    assert coop["url"] == "https://fixtureco.bamboohr.com/careers/41" and coop["company"] == "Fixture Co"
    assert coop["location"] == "Kitchener, Ontario, Canada" and coop["country_hint"] == "CA"
    assert coop["posted_at"] == "2026-10-15" and coop["deadline"] == "2026-11-25" and coop["details_fetched"] is True
    assert by_id["43"]["details_fetched"] is False
    for job in jobs:
        assert_contract(job)


# --- optional discovered registry ------------------------------------------------------

def write(path, value):
    path.write_text(json.dumps(value))


def test_registry_adds_valid_discovered_sources_without_duplicates(tmp_path, monkeypatch):
    write(tmp_path / "sources.json", [{"id": "greenhouse-a", "name": "A", "type": "greenhouse", "board": "a",
                                       "url": "https://job-boards.greenhouse.io/a"}])
    write(tmp_path / "extra_sources.json", [{"id": "workday-x", "name": "X", "type": "workday", "tenant": "x", "site": "Ext",
                                             "url": "https://x.wd3.myworkdayjobs.com/Ext"}])
    write(tmp_path / "discovered_sources.json", [
        {"id": "greenhouse-a", "name": "Duplicate id", "type": "greenhouse", "board": "zzz", "url": "https://job-boards.greenhouse.io/zzz"},
        {"id": "greenhouse-a-upper", "name": "Same board", "type": "greenhouse", "board": "A", "url": "https://job-boards.greenhouse.io/A"},
        {"id": "workday-x-ext", "name": "Same site", "type": "workday", "tenant": "x", "site": "ext", "url": "https://x.wd3.myworkdayjobs.com/ext"},
        {"id": "greenhouse-new", "name": "New", "type": "greenhouse", "board": "new", "url": "https://job-boards.greenhouse.io/new"},
        {"id": "broken", "name": "Missing URL", "type": "lever", "board": "b"},
        "not-an-object"])
    monkeypatch.setattr(config, "BUILTIN_REGISTRY", tmp_path)
    monkeypatch.setenv("CAREER_RADAR_DATA", str(tmp_path))
    assert [s["id"] for s in collect.load_registry()] == ["greenhouse-a", "workday-x", "greenhouse-new"]


def test_registry_without_or_with_corrupt_discovered_file_keeps_curated_sources(tmp_path, monkeypatch):
    write(tmp_path / "sources.json", [{"id": "a", "name": "A", "type": "greenhouse", "board": "a", "url": "https://job-boards.greenhouse.io/a"}])
    write(tmp_path / "extra_sources.json", [])
    monkeypatch.setattr(config, "BUILTIN_REGISTRY", tmp_path)
    monkeypatch.setenv("CAREER_RADAR_DATA", str(tmp_path))
    assert [s["id"] for s in collect.load_registry()] == ["a"]
    (tmp_path / "discovered_sources.json").write_text("{not json")
    assert [s["id"] for s in collect.load_registry()] == ["a"]
