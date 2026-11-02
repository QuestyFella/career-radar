"""Synthetic fixtures ONLY. None of these records belong in live data."""
import gzip
import json
import socket
import threading
import zlib
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from career_radar import extras
_HTTPX_STREAM = httpx.Client.stream


def source(kind, **kw):
    return {"id": "fixture-" + kind, "name": "Fixture " + kind,
            "type": kind, "url": "https://fixture.invalid/list", **kw}


@pytest.fixture(autouse=True)
def no_live_network(monkeypatch):
    """Even a broken SSRF guard must never reach real DNS or a socket."""
    def forbidden(*args, **kwargs):
        raise AssertionError("Live network access is forbidden in adapter tests")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    getattr(extras, "_DNS_CACHE", {}).clear()
    yield
    getattr(extras, "_DNS_CACHE", {}).clear()


def mock_http(monkeypatch, payloads):
    """Keep real HTTPX streaming/decoding; replace only the socket boundary."""
    calls = []
    queue = list(payloads)

    def stream(self, method, url, **kwargs):
        calls.append((method, url, kwargs))
        return _HTTPX_STREAM(self, method, url, **kwargs)

    def buffered_request(*args, **kwargs):
        pytest.fail("Adapters must stream responses, never use Client.request()")

    def handle(self, request):
        assert queue, ("unexpected request", request.method, str(request.url))
        payload = queue.pop(0)
        if isinstance(payload, Exception):
            raise payload
        status, data = payload if isinstance(payload, tuple) else (200, payload)
        is_json = isinstance(data, (list, dict))
        content = (json.dumps(data) if is_json else data).encode("utf-8")
        headers = {"Content-Type": "application/json" if is_json else "text/html; charset=utf-8",
                   "Content-Length": str(len(content))}
        return httpx.Response(status, headers=headers, stream=httpx.ByteStream(content))

    mock_dns(monkeypatch)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)
    monkeypatch.setattr(httpx.Client, "stream", stream)
    monkeypatch.setattr(httpx.Client, "request", buffered_request)
    monkeypatch.setattr(extras, "_pace", lambda url: None)
    return calls


def assert_contract(job):
    required = {"source_id", "source", "source_type", "source_job_id", "company",
                "title", "location", "url", "description", "posted_at", "deadline",
                "deadline_evidence", "deadline_source_url", "verification", "is_closed"}
    assert required <= job.keys()
    assert job["verification"] in {"direct", "curated", "public_listing"}
    assert isinstance(job["is_closed"], bool)
    assert job["url"].startswith("https://")
    if job["deadline"] is None:
        assert job["deadline_evidence"] is None
        assert job["deadline_source_url"] is None


def test_simplify_keeps_terms_closed_status_and_index_date_provenance(monkeypatch):
    rows = [{"id": "fixture-intern-1", "company_name": "Fixture Space", "title": "Firmware Intern",
             "locations": ["Toronto, ON, Canada", "New York, NY"], "url": "https://employer.invalid/jobs/1",
             "active": True, "is_visible": True, "terms": ["Fall 2026", "Winter 2027"],
             "date_posted": 1793318400, "date_updated": 1793404800, "degrees": ["Bachelor's"]},
            {"id": "fixture-closed", "company_name": "Fixture Space", "title": "Software Engineer",
             "locations": ["Ottawa, ON, Canada"], "url": "https://employer.invalid/jobs/2", "active": False}]
    mock_http(monkeypatch, [rows])
    jobs = extras.fetch_source(source("simplify", level_hint="internship", include_closed=True, canada_only=True))
    assert len(jobs) == 2
    assert jobs[0]["verification"] == "curated"
    assert jobs[0]["posted_at"] is None  # Simplify discovery is not employer datePosted.
    assert jobs[0]["index_posted_at"].startswith("2026-")
    assert jobs[0]["terms"] == ["Fall 2026", "Winter 2027"]
    assert jobs[0]["country_hint"] == "CA"
    assert jobs[1]["is_closed"] is True
    for job in jobs:
        assert_contract(job)


def test_simplify_newgrad_filters_hidden_closed_and_noncanada(monkeypatch):
    base = {"company_name": "Fixture", "title": "Engineer I", "active": True, "id": "fixture-a",
            "locations": ["Toronto, ON, Canada"], "url": "https://employer.invalid/jobs/a"}
    mock_http(monkeypatch, [[base, {**base, "id": "fixture-b", "is_visible": False},
                              {**base, "id": "fixture-c", "active": False},
                              {**base, "id": "fixture-d", "locations": ["Remote"]}]])
    jobs = extras.fetch_source(source("simplify", level_hint="new_grad", canada_only=True))
    assert len(jobs) == 1 and jobs[0]["level_hint"] == "new_grad"


def test_frogg_real_field_names_and_no_deadline_inferred_from_term(monkeypatch):
    mock_http(monkeypatch, [[{"company": "Fixture", "role": "Embedded Co-op", "location": "Ottawa, ON, Canada",
                              "link": "https://employer.invalid/job/2", "term": "Winter 2027", "status": "open",
                              "date_posted": "2026-10-30", "date_found": "2026-10-31", "date_closed": None}]])
    jobs = extras.fetch_source(source("frogg", canada_only=True))
    assert jobs[0]["posted_at"] == "2026-10-30"
    assert jobs[0]["level_hint"] == "internship"
    assert jobs[0]["deadline"] is None
    assert "Winter 2027" in jobs[0]["description"]
    assert_contract(jobs[0])


LINKEDIN_CARD = '''<li><div class="base-search-card" data-entity-urn="urn:li:jobPosting:{id}">
<a class="base-card__full-link" href="https://ca.linkedin.com/jobs/view/fixture-{id}?trackingId=fixture">Apply</a>
<h3 class="base-search-card__title">Junior Software Developer</h3>
<h4 class="base-search-card__subtitle">Fixture Ltd</h4>
<span class="job-search-card__location">Toronto, Ontario, Canada</span>
<time datetime="2026-10-30">Yesterday</time></div></li>'''


def test_linkedin_real_cards_and_actual_page_size_not_assumed_25(monkeypatch):
    calls = mock_http(monkeypatch, [LINKEDIN_CARD.format(id=1), LINKEDIN_CARD.format(id=2), ""])
    jobs = extras.fetch_source(source("linkedin", url="https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search",
                                     keywords="software", max_pages=3, detail_limit=0))
    assert len(jobs) == 2
    assert [c[2]["params"]["start"] for c in calls] == [0, 1, 2]
    assert calls[0][2]["params"]["f_E"] == "1,2"
    assert calls[0][2]["params"]["location"] == "Canada"
    assert jobs[0]["url"] == "https://ca.linkedin.com/jobs/view/fixture-1"
    assert jobs[0]["posted_at"] == "2026-10-30"
    assert jobs[0]["verification"] == "public_listing"
    assert_contract(jobs[0])


def test_linkedin_anchor_wrapped_card_is_supported(monkeypatch):
    card = LINKEDIN_CARD.format(id=7).replace('<div class="base-search-card"', '<a href="https://ca.linkedin.com/jobs/view/fixture-7" class="base-search-card"')
    card = card.replace('<a class="base-card__full-link" href="https://ca.linkedin.com/jobs/view/fixture-7?trackingId=fixture">Apply</a>', '')
    card = card.replace('</div></li>', '</a></li>')
    mock_http(monkeypatch, [card, ""])
    jobs = extras.fetch_source(source("linkedin", max_pages=2))
    assert len(jobs) == 1 and jobs[0]["source_job_id"] == "7"


def test_linkedin_bounded_coverage_raises_with_real_partial_records(monkeypatch):
    mock_http(monkeypatch, [LINKEDIN_CARD.format(id=1)])
    with pytest.raises(extras.SourceTruncated, match="LinkedIn") as error:
        extras.fetch_source(source("linkedin", max_pages=1, detail_limit=0))
    assert len(error.value.partial_jobs) == 1
    assert error.value.status == "partial"


@pytest.mark.parametrize("html", ["<title>Security Verification</title><p>captcha</p>",
                                    '<html><body><form action="/checkpoint/challenge">Sign in</form></body></html>',
                                    "<html><h1>Something changed</h1></html>", ""])
def test_linkedin_never_turns_first_page_challenge_or_unknown_schema_into_success(monkeypatch, html):
    mock_http(monkeypatch, [html])
    with pytest.raises(extras.SourceError):
        extras.fetch_source(source("linkedin", max_pages=1))


def test_linkedin_detail_description_and_seniority(monkeypatch):
    detail = '''<div class="show-more-less-html__markup"><p>Build flight software. 0–2 years of experience.</p></div>
    <ul><li class="description__job-criteria-item"><h3>Seniority level</h3><span class="description__job-criteria-text">Entry level</span></li></ul>'''
    mock_http(monkeypatch, [LINKEDIN_CARD.format(id=1), "", detail])
    jobs = extras.fetch_source(source("linkedin", max_pages=2, detail_limit=1))
    assert "Build flight software" in jobs[0]["description"]
    assert jobs[0]["details_fetched"] is True


def test_http_rate_limit_is_a_visible_error_and_no_cookies(monkeypatch):
    calls = mock_http(monkeypatch, [(429, "Too Many Requests")])
    with pytest.raises(extras.SourceError, match="429"):
        extras.fetch_source(source("remotive"))
    assert "cookies" not in calls[0][2]
    assert "Cookie" not in calls[0][2].get("headers", {})
    assert calls[0][2]["follow_redirects"] is False


def muse_job(job_id, location):
    return {"id": job_id, "name": "Junior Data Analyst", "company": {"name": "Fixture"},
            "locations": [{"name": location}], "levels": [{"name": "Entry Level"}],
            "contents": "<p>SQL and Python</p>", "publication_date": "2026-10-30T12:00:00Z",
            "refs": {"landing_page": "https://www.themuse.com/jobs/fixture/" + str(job_id)}}


def test_muse_pagination_and_does_not_trust_search_geography_for_remote(monkeypatch):
    calls = mock_http(monkeypatch, [{"page": 0, "page_count": 2, "total": 2, "timed_out": False,
                                    "results": [muse_job(1, "Flexible / Remote")]},
                                   {"page": 1, "page_count": 2, "total": 2, "timed_out": False,
                                    "results": [muse_job(2, "Toronto, Canada")]}])
    jobs = extras.fetch_source(source("themuse", canada_only=True, max_pages=4, locations=["Toronto, Canada"]))
    assert len(jobs) == 1 and jobs[0]["source_job_id"] == "2"
    assert calls[1][2]["params"]["page"] == 1
    assert jobs[0]["verification"] == "public_listing"
    assert_contract(jobs[0])


def test_muse_declared_total_mismatch_is_not_success(monkeypatch):
    mock_http(monkeypatch, [{"page": 0, "page_count": 1, "total": 2, "results": [muse_job(1, "Toronto, Canada")]}])
    with pytest.raises(extras.SourceError, match="total"):
        extras.fetch_source(source("themuse"))


def test_remotive_explicit_canada_and_not_worldwide_by_assumption(monkeypatch):
    common = {"title": "Junior Developer", "company_name": "Fixture", "description": "<p>Build software.</p>",
              "publication_date": "2026-10-30T10:30:00", "job_type": "full_time"}
    mock_http(monkeypatch, [{"job-count": 2, "total-job-count": 2, "jobs": [
        {**common, "id": 1, "url": "https://remotive.com/remote-jobs/fixture-1", "candidate_required_location": "Canada, USA"},
        {**common, "id": 2, "url": "https://remotive.com/remote-jobs/fixture-2", "candidate_required_location": "Worldwide"}]}])
    jobs = extras.fetch_source(source("remotive", canada_only=True))
    assert len(jobs) == 1
    assert jobs[0]["url"].startswith("https://remotive.com/")  # Required attribution link.
    assert jobs[0]["deadline"] is None
    assert_contract(jobs[0])


def test_remoteok_skips_metadata_not_actual_jobs(monkeypatch):
    mock_http(monkeypatch, [[{"legal": "Fixture attribution terms", "last_updated": 1793404800},
        {"id": "fixture-1", "position": "Junior Engineer", "company": "Fixture", "location": "Canada",
         "description": "<p>Work remotely.</p>", "date": "2026-10-30T00:00:00+00:00",
         "url": "https://remoteok.com/remote-jobs/fixture-1"}]])
    jobs = extras.fetch_source(source("remoteok", canada_only=True))
    assert len(jobs) == 1 and jobs[0]["source_job_id"] == "fixture-1"
    assert_contract(jobs[0])


def test_arbeitnow_follows_next_and_rejects_cross_origin_pagination(monkeypatch):
    row = {"slug": "fixture-1", "title": "Junior Developer", "company_name": "Fixture", "location": "Toronto, Canada",
           "description": "&lt;p&gt;Python&lt;/p&gt;", "created_at": 1793318400,
           "url": "https://www.arbeitnow.com/jobs/fixture-1"}
    mock_http(monkeypatch, [{"data": [row], "links": {"next": "https://www.arbeitnow.com/api/job-board-api?page=2"}},
                            {"data": [], "links": {"next": None}}])
    jobs = extras.fetch_source(source("arbeitnow", url="https://www.arbeitnow.com/api/job-board-api"))
    assert jobs[0]["description"] == "Python"
    assert_contract(jobs[0])
    mock_http(monkeypatch, [{"data": [row], "links": {"next": "http://127.0.0.1/internal"}}])
    with pytest.raises(extras.SourceError, match="pagination"):
        extras.fetch_source(source("arbeitnow", url="https://www.arbeitnow.com/api/job-board-api"))


JOBPOSTING = {"@context": "https://schema.org", "@type": "JobPosting", "title": "Junior Flight Software Engineer",
              "identifier": {"value": "fixture-flight-1"}, "hiringOrganization": {"name": "Fixture Space"},
              "jobLocation": {"address": {"addressLocality": "Ottawa", "addressRegion": "ON", "addressCountry": "CA"}},
              "description": "<p>Write C++.</p>", "datePosted": "2026-10-30", "validThrough": "2026-11-14T23:59:00-05:00",
              "employmentType": "FULL_TIME"}


def test_jsonld_graph_has_real_deadline_evidence(monkeypatch):
    html = '<script type="application/ld+json">' + json.dumps({"@graph": [JOBPOSTING]}) + '</script>'
    mock_http(monkeypatch, [html])
    jobs = extras.fetch_source(source("jsonld", url="https://employer.invalid/jobs/flight", company="Fixture Space"))
    assert jobs[0]["deadline"] == "2026-11-14T23:59:00-05:00"
    assert "validThrough" in jobs[0]["deadline_evidence"]
    assert jobs[0]["deadline_source_url"] == "https://employer.invalid/jobs/flight"
    assert jobs[0]["country_hint"] == "CA"
    assert jobs[0]["verification"] == "direct"
    assert_contract(jobs[0])


def test_jsonld_unknown_deadline_and_invalid_calendar_dates(monkeypatch):
    data = deepcopy(JOBPOSTING)
    data.pop("validThrough")
    data["datePosted"] = "2026-02-30"
    mock_http(monkeypatch, ['<script type="application/ld+json">' + json.dumps(data) + '</script>'])
    job = extras.fetch_source(source("jsonld"))[0]
    assert job["deadline"] is None and job["posted_at"] is None
    assert_contract(job)


def test_jsonld_non_job_is_schema_error(monkeypatch):
    mock_http(monkeypatch, ['<script type="application/ld+json">{"@type":"Organization"}</script>'])
    with pytest.raises(extras.SourceError, match="JobPosting"):
        extras.fetch_source(source("jsonld"))


def test_workday_discovers_canada_facet_details_and_pagination(monkeypatch):
    def page(n, total=2):
        return {"total": total, "jobPostings": [{"title": "Software Co-op", "externalPath": "/job/Toronto/Fixture_" + str(n),
                 "locationsText": "Toronto, ON", "postedOn": "Posted Yesterday", "bulletFields": [str(n)]}],
                "facets": [{"facetParameter": "Country", "descriptor": "Country",
                            "values": [{"id": "fixture-canada-id", "descriptor": "Canada", "count": 2}]}]}
    def detail(n):
        return {"jobPostingInfo": {"id": "fixture-wd-" + str(n), "title": "Software Co-op",
                "jobDescription": "<p>Current student. Application Deadline: 2026-11-14</p>",
                "startDate": "2026-10-30", "location": "Toronto, ON", "jobReqId": str(n),
                "country": {"descriptor": "Canada"}, "timeType": "Full time"}}
    calls = mock_http(monkeypatch, [page(0, 3), page(1), page(2, 0), detail(1), detail(2)])
    jobs = extras.fetch_source(source("workday", url="https://fixture.wd3.myworkdayjobs.com/Campus",
                                     tenant="fixture", site="Campus", company="Fixture", country="Canada", page_size=1))
    assert len(jobs) == 2
    assert calls[1][2]["json"]["appliedFacets"] == {"Country": ["fixture-canada-id"]}
    assert calls[2][2]["json"]["offset"] == 1
    assert jobs[0]["posted_at"] == "2026-10-30"
    assert jobs[0]["deadline"] == "2026-11-14"
    assert jobs[0]["verification"] == "direct"
    assert jobs[0]["country_hint"] == "CA"
    assert jobs[0]["url"] == "https://fixture.wd3.myworkdayjobs.com/en-US/Campus/job/Toronto/Fixture_1"
    assert_contract(jobs[0])


def test_workday_relative_posted_on_is_not_an_invented_date(monkeypatch):
    mock_http(monkeypatch, [{"total": 1, "jobPostings": [{"title": "Engineer I", "externalPath": "/job/Fixture_1",
        "locationsText": "Toronto, ON", "postedOn": "Posted 30+ Days Ago"}], "facets": []},
        {"jobPostingInfo": {"id": "fixture-wd", "title": "Engineer I", "jobDescription": "Build software"}}])
    job = extras.fetch_source(source("workday", url="https://fixture.wd3.myworkdayjobs.com/Campus",
                                    tenant="fixture", site="Campus", company="Fixture"))[0]
    assert job["posted_at"] is None
    assert job["deadline"] is None


@pytest.mark.parametrize("metadata", [None, "2026-11-08", "2026-11-08T23:59:00Z"])
@pytest.mark.parametrize("published,expected,uncertainty", [
    ("Application Deadline: 2026-11-08. Note: Applications will be accepted until 11:59 PM on the day prior to the application deadline date above.",
     "2026-11-07", "missing_timezone"),
    ("Application Deadline: 2026-11-08 at 5 PM ET. Note: Applications will be accepted until 11:59 PM on the day prior to the application deadline date above.",
     "2026-11-07", "missing_timezone"),
    ("Formal Application Deadline: November 6, 2026 at 4 PM ET.", "2026-11-06T16:00:00-05:00", None),
    ("Application Deadline: 2026-11-06 at 4 PM.", "2026-11-06", "missing_timezone"),
    ("Application Deadline: 2026-11-06.", "2026-11-06", None),
])
def test_workday_metadata_does_not_overwrite_published_prose(monkeypatch, metadata, published, expected, uncertainty):
    from career_radar.deadlines import parse_published_deadline

    calls = mock_http(monkeypatch, [
        {"total": 1, "jobPostings": [{"title": "Software Co-op", "externalPath": "/job/Fixture_1",
                                    "locationsText": "Toronto, ON, Canada"}]},
        {"jobPostingInfo": {"id": "fixture-deadline", "title": "Software Co-op", "startDate": "2026-10-30",
                            "jobDescription": f"<p>{published}</p>", "validThrough": metadata}},
    ])
    rows = extras.fetch_source(source("workday", url="https://fixture.wd3.myworkdayjobs.com/Campus",
                                      tenant="fixture", site="Campus", company="Fixture"))
    assert len(rows) == 1 and [call[0] for call in calls] == ["POST", "GET"]
    row = json.loads(json.dumps(rows[0]))
    assert row["deadline"] == expected
    assert row["deadline_evidence"] == published
    assert row["deadline_source_url"] == row["url"]
    assert row["posted_at"] == "2026-10-30"
    parsed = parse_published_deadline(row["deadline_evidence"])
    assert parsed is not None and (parsed.value, parsed.uncertainty) == (expected, uncertainty)
    assert_contract(row)


@pytest.mark.parametrize("description", ["Work term: November 2026.", "Application Deadline: 2026-02-30."])
@pytest.mark.parametrize("metadata", ["2026-11-08", "2026-11-08T21:14:59-00:00", "2026-11-08T21:14:59.1234567Z"])
def test_workday_structured_deadline_is_fallback_without_valid_prose(monkeypatch, description, metadata):
    mock_http(monkeypatch, [
        {"total": 1, "jobPostings": [{"title": "Software Co-op", "externalPath": "/job/Fixture_1",
                                    "locationsText": "Toronto, ON, Canada"}]},
        {"jobPostingInfo": {"id": "fixture-fallback", "title": "Software Co-op",
                            "jobDescription": f"<p>{description}</p>", "validThrough": metadata}},
    ])
    rows = extras.fetch_source(source("workday", url="https://fixture.wd3.myworkdayjobs.com/Campus",
                                      tenant="fixture", site="Campus", company="Fixture"))
    assert len(rows) == 1
    row = json.loads(json.dumps(rows[0]))
    assert row["deadline"] == metadata
    assert row["deadline_evidence"] == f"Workday validThrough: {metadata}"
    assert row["deadline_source_url"] == row["url"]
    assert_contract(row)


@pytest.mark.parametrize("displayed", ["2026-11-15", "2026-11-15 at 5 PM ET."])
def test_explicit_rbc_prior_day_deadline_note_is_respected(displayed):
    text = f"Application Deadline: {displayed} Note: Applications will be accepted until 11:59 PM on the day prior to the application deadline date above."
    deadline, evidence = extras.explicit_deadline(text)
    assert deadline == "2026-11-14"
    assert evidence == text
    assert "day prior" in evidence
    assert "2026-11-15" in evidence


JOBBANK_CARD = '''<meta name="description" content="View 1 job postings near Canada">
<div class="results-jobs"><article id="article-123"><a class="resultJobItem" href="/jobsearch/jobposting/123;jsessionid=fixture?source=searchresults">
<span class="noctitle">software developer</span><li class="date">October 30, 2026</li>
<li class="business">Fixture Ltd</li><li class="location">Location Toronto (ON)</li></a></article></div>'''


def test_jobbank_public_details_and_advertised_until(monkeypatch):
    mock_http(monkeypatch, [JOBBANK_CARD, '<main><h1>software developer</h1><p>Experience: 7 months to less than 1 year.</p><h3>Advertised until</h3><p>2026-11-14</p></main>'])
    job = extras.fetch_source(source("jobbank", url="https://www.jobbank.gc.ca/jobsearch/jobsearch", keywords="software"))[0]
    assert job["url"] == "https://www.jobbank.gc.ca/jobsearch/jobposting/123"
    assert job["posted_at"] == "2026-10-30"
    assert job["deadline"] == "2026-11-14"
    assert "Advertised until" in job["deadline_evidence"]
    assert "7 months" in job["description"]
    assert job["verification"] == "public_listing"
    assert_contract(job)


def test_jobbank_count_cap_is_explicit(monkeypatch):
    mock_http(monkeypatch, [JOBBANK_CARD.replace("View 1 job", "View 200 job")])
    with pytest.raises(extras.SourceTruncated) as error:
        extras.fetch_source(source("jobbank", url="https://www.jobbank.gc.ca/jobsearch/jobsearch", max_pages=1, detail_limit=0))
    assert len(error.value.partial_jobs) == 1


@pytest.mark.parametrize("kind,payload", [("simplify", {}), ("frogg", {"oops": []}),
                                         ("themuse", {"results": "bad"}), ("remotive", {"jobs": None}),
                                         ("arbeitnow", {"data": 42}), ("remoteok", {})])
def test_invalid_feed_schema_is_not_empty_success(monkeypatch, kind, payload):
    mock_http(monkeypatch, [payload])
    with pytest.raises(extras.SourceError):
        extras.fetch_source(source(kind))


def test_employer_jsonld_discovers_open_company_jobs_without_curated_verification_upgrade(monkeypatch):
    candidate = {"company": "Fixture Aero", "role": "Software Intern", "location": "Toronto, Canada",
                 "link": "https://employer.invalid/job/open", "status": "open", "term": "Winter 2027"}
    other = {**candidate, "company": "Other", "link": "https://other.invalid/job/2"}
    html = '<script type="application/ld+json">' + json.dumps(JOBPOSTING) + '</script>'
    calls = mock_http(monkeypatch, [[candidate, other], html])
    jobs = extras.fetch_source(source("employer_jsonld", company_filter="Fixture Aero", employer_host="employer.invalid",
                                     feed_type="frogg", detail_limit=5))
    assert len(jobs) == 1
    assert len(calls) == 2
    assert jobs[0]["verification"] == "direct"
    assert jobs[0]["discovery_verification"] == "curated"
    assert jobs[0]["deadline"] == "2026-11-14T23:59:00-05:00"
    assert_contract(jobs[0])


def test_employer_jsonld_missing_schema_is_not_faked_direct_coverage(monkeypatch):
    candidate = {"company": "Fixture Aero", "role": "Software Intern", "location": "Toronto, Canada",
                 "link": "https://employer.invalid/job/open", "status": "open"}
    mock_http(monkeypatch, [[candidate], "<html>JavaScript required</html>"])
    with pytest.raises(extras.SourceError, match="JobPosting"):
        extras.fetch_source(source("employer_jsonld", company_filter="Fixture Aero", employer_host="employer.invalid",
                                   feed_type="frogg", detail_limit=5))


def test_sitemap_employer_microdata_has_real_posted_date_not_sitemap_lastmod(monkeypatch):
    sitemap = '''<urlset xmlns="http://www.google.com/schemas/sitemap/0.9"><url>
      <loc>https://employer.invalid/job/Toronto-Intern-Software-Developer/123/</loc><lastmod>2026-10-31</lastmod>
      </url><url><loc>https://employer.invalid/job/Senior-Software-Engineer/456/</loc></url></urlset>'''
    detail = '''<span itemprop="title">Intern, Software Developer (Winter 2027)</span>
      <div itemscope itemtype="http://schema.org/JobPosting">Posting Start Date: 10/30/26 Requisition ID: 123
      <span lang="en-US">Toronto, ON, CAN, M1A 1A1</span>
      <span itemprop="description">Write C++. Students only, not new graduates.</span></div>'''
    calls = mock_http(monkeypatch, [sitemap, detail])
    jobs = extras.fetch_source(source("sitemap_jobposting", url="https://employer.invalid/sitemap.xml",
                                     employer_host="employer.invalid", company="Fixture Aero"))
    assert len(calls) == 2 and len(jobs) == 1
    assert jobs[0]["source_job_id"] == "123"
    assert jobs[0]["posted_at"] == "2026-10-30"
    assert jobs[0]["deadline"] is None
    assert jobs[0]["verification"] == "direct"
    assert "Students only" in jobs[0]["description"]
    assert_contract(jobs[0])


def test_sitemap_selects_public_jobposting_jsonld_and_never_uses_lastmod_as_deadline(monkeypatch):
    sitemap = '''<urlset><url><loc>https://employer.invalid/job/Graduate-Software-Engineer/9/</loc>
      <lastmod>2026-10-31</lastmod></url></urlset>'''
    item = deepcopy(JOBPOSTING)
    item.pop("validThrough")
    item.pop("datePosted")
    detail = '<script type="application/ld+json">' + json.dumps(item) + '</script>'
    mock_http(monkeypatch, [sitemap, detail])
    job = extras.fetch_source(source("sitemap_jobposting", url="https://employer.invalid/sitemap.xml",
                                    employer_host="employer.invalid", company="Fixture Aero"))[0]
    assert job["deadline"] is None and job["posted_at"] is None
    assert job["discovery_verification"] == "direct"


def test_unknown_source_type_is_explicit():
    with pytest.raises(extras.SourceError, match="Unsupported"):
        extras.fetch_source(source("not-implemented"))


@pytest.mark.parametrize("location", ["Ontario, CA, United States", "Remote, on-site in USA"])
def test_country_hint_does_not_turn_california_or_lowercase_on_into_canada(monkeypatch, location):
    mock_http(monkeypatch, [[{"company": "Fixture", "role": "Software Intern", "location": location,
                              "link": "https://employer.invalid/job/us", "status": "open"}]])
    assert extras.fetch_source(source("frogg", canada_only=True)) == []


def test_jsonld_remote_state_ca_is_not_canada(monkeypatch):
    item = deepcopy(JOBPOSTING)
    item.pop("jobLocation")
    item["jobLocationType"] = "TELECOMMUTE"
    item["applicantLocationRequirements"] = {"@type": "State", "name": "CA"}
    mock_http(monkeypatch, ['<script type="application/ld+json">' + json.dumps(item) + '</script>'])
    assert extras.fetch_source(source("jsonld", canada_only=True)) == []


def test_unsafe_job_url_is_rejected_not_passed_to_application(monkeypatch):
    mock_http(monkeypatch, [[{"company": "Fixture", "role": "Software Intern", "location": "Canada",
                              "link": "javascript:alert(1)", "status": "open"}]])
    with pytest.raises(extras.SourceError, match="URL"):
        extras.fetch_source(source("frogg"))


@pytest.mark.parametrize("url", [
    "http://127.1/job", "http://2130706433/job", "http://0x7f000001/job",
    "http://0177.0.0.1/job", "http://0177.1/job", "http://0x7f.0.0.1/job",
    "http://127.0.0.01/job", "http://127.1./job", "http://8.8/job",
    "http://localhost./job", "http://jobs.localhost/job", "http://router/job",
    "http://router.local/job", "http://router.lan/job", "http://router.home.arpa/job",
    "http://10.0.0.1/job", "http://169.254.169.254/job", "http://100.64.0.1/job",
    "http://[::1]/job", "http://[::ffff:127.0.0.1]/job", "http://[fe80::1]/job",
    "http://[fd00::1]/job", "http://[fe80::1%25eth0]/job", "http://[ff02::1]/job",
    "http://[64:ff9b::7f00:1]/job", "http://224.0.0.1/job",
    "http://%31%32%37.1/job", "http://１２７.０.０.１/job",
    "http://public.example:invalid/job", "http://public.example\\@127.1/job",
])
def test_safe_url_rejects_local_or_ambiguous_destinations(url):
    with pytest.raises(extras.SourceError):
        extras._safe_url(url)


@pytest.mark.parametrize("url", [
    "https://public.example/jobs?a=1", "https://public.example./jobs",
    "http://8.8.8.8/jobs", "https://[2606:4700:4700::1111]/jobs",
])
def test_safe_url_preserves_valid_public_url_syntax(url):
    assert extras._safe_url(url) == url


def mock_dns(monkeypatch, records=None):
    """Public IPs below are synthetic answers only; socket access is forbidden."""
    calls = []
    records = records or {}

    def resolve(host, port, *args, **kwargs):
        calls.append((host, port))
        addresses = records.get(host, ["8.8.8.8"])
        if isinstance(addresses, Exception):
            raise addresses
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET,
                 socket.SOCK_STREAM, socket.IPPROTO_TCP, "",
                 (ip, port, 0, 0) if ":" in ip else (ip, port)) for ip in addresses]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    return calls


class TrackedStream(httpx.SyncByteStream):
    """Synthetic wire chunks with observable consumption and cleanup."""
    def __init__(self, chunks):
        self.chunks = chunks
        self.consumed = []
        self.close_calls = 0

    def __iter__(self):
        for index, chunk in enumerate(self.chunks):
            self.consumed.append(index)
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    def close(self):
        self.close_calls += 1


def mock_transport(monkeypatch, payloads):
    """Exercise the real request/redirect boundary, stopping before sockets."""
    calls, queue = [], list(payloads)

    def handle(self, request):
        request.read()  # A real transport consumes the outgoing request stream.
        calls.append((request, type(self._pool).__name__))
        assert queue, ("unexpected transport request", str(request.url))
        status, headers, body = queue.pop(0)
        stream = body if isinstance(body, httpx.SyncByteStream) else httpx.ByteStream(
            body.encode("utf-8") if isinstance(body, str) else body)
        return httpx.Response(status, headers=headers, stream=stream)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)
    monkeypatch.setattr(extras, "_pace", lambda url: None)
    return calls


@pytest.mark.parametrize("target", [
    "http://127.1/private", "http://2130706433/private", "http://0x7f000001/private",
    "http://0177.1/private", "http://localhost./private", "http://[::1]/private",
    "http://[fd00::1]/private", "http://private.example/private",
])
def test_request_rejects_private_redirect_before_any_internal_http(monkeypatch, target):
    mock_dns(monkeypatch, {"private.example": ["127.0.0.1"]})
    calls = mock_transport(monkeypatch, [(302, {"Location": target}, ""), (200, {}, "unsafe")])
    with pytest.raises(extras.SourceError):
        extras._request("GET", "https://public.example/jobs")
    assert len(calls) == 1  # The only HTTP request is the public first hop.


@pytest.mark.parametrize("addresses", [
    ["127.0.0.1"], ["10.0.0.1"], ["169.254.169.254"], ["::1"], ["fd00::1"],
    ["::ffff:127.0.0.1"], ["8.8.8.8", "192.168.1.1"], ["8.8.8.8", "fe80::1"],
])
def test_request_rejects_all_local_dns_answers_including_mixed_public_sets(monkeypatch, addresses):
    mock_dns(monkeypatch, {"public.example": addresses})
    calls = mock_transport(monkeypatch, [(200, {}, "unsafe")])
    with pytest.raises(extras.SourceError):
        extras._request("GET", "https://public.example/jobs")
    assert calls == []


def test_request_validates_dns_on_later_redirect_hops(monkeypatch):
    mock_dns(monkeypatch, {"private.example": ["192.168.1.1"]})
    calls = mock_transport(monkeypatch, [
        (301, {"Location": "https://second.example/jobs"}, ""),
        (307, {"Location": "https://private.example/jobs"}, ""),
        (200, {}, "unsafe"),
    ])
    with pytest.raises(extras.SourceError):
        extras._request("GET", "https://public.example/jobs")
    assert len(calls) == 2


def test_request_pins_public_ip_keeps_host_tls_identity_and_disables_proxy(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://private.example:8080")
    monkeypatch.setenv("NO_PROXY", "")
    dns_calls = mock_dns(monkeypatch)
    calls = mock_transport(monkeypatch, [(200, {}, "public fixture")])
    response = extras._request("GET", "https://public.example:8443/jobs", params={"page": 2})
    assert response.text == "public fixture"
    request, pool = calls[0]
    assert str(request.url) == "https://8.8.8.8:8443/jobs?page=2"
    assert request.headers["Host"] == "public.example:8443"
    assert request.extensions["sni_hostname"] == "public.example"
    assert pool == "ConnectionPool"  # No environment HTTP proxy bypasses validation.
    assert dns_calls == [("public.example", 8443)]
    assert str(response.url) == "https://public.example:8443/jobs?page=2"


def test_request_public_relative_redirect_is_stateless_and_bounded(monkeypatch):
    dns_calls = mock_dns(monkeypatch)
    calls = mock_transport(monkeypatch, [
        (302, {"Location": "/final?chosen=1", "Set-Cookie": "fixture=not-forwarded"}, ""),
        (200, {}, "public fixture"),
    ])
    response = extras._request("GET", "https://public.example/start", params={"initial": 1})
    assert response.text == "public fixture"
    assert str(response.url) == "https://public.example/final?chosen=1"
    assert "Cookie" not in calls[1][0].headers
    assert calls[1][0].url.host == "8.8.8.8"
    assert dns_calls == [("public.example", 443)]  # Bounded successful DNS cache.
    calls = mock_transport(monkeypatch, [(302, {"Location": f"/hop/{i}"}, "") for i in range(4)])
    with pytest.raises(extras.SourceError, match="Too many"):
        extras._request("GET", "https://public.example/start")
    assert len(calls) == 4


def test_request_dns_failure_is_visible_without_http(monkeypatch):
    mock_dns(monkeypatch, {"public.example": socket.gaierror("synthetic DNS failure")})
    calls = mock_transport(monkeypatch, [(200, {}, "unsafe")])
    with pytest.raises(extras.SourceError, match="DNS"):
        extras._request("GET", "https://public.example/jobs")
    assert calls == []


def test_request_dns_timeout_is_bounded_without_http(monkeypatch):
    release, finished = threading.Event(), threading.Event()

    def stalled_dns(*args, **kwargs):
        try:
            release.wait(1)
            raise socket.gaierror("synthetic resolver released")
        finally:
            finished.set()

    monkeypatch.setattr(socket, "getaddrinfo", stalled_dns)
    monkeypatch.setattr(extras, "_DNS_TIMEOUT", 0.01, raising=False)
    calls = mock_transport(monkeypatch, [(200, {}, "unsafe")])
    try:
        with pytest.raises(extras.SourceError, match="DNS.*timed out"):
            extras._request("GET", "https://public.example/jobs")
        assert calls == []
    finally:
        release.set()
        # No resolver may outlive monkeypatch teardown, even on a RED failure.
        if getattr(extras, "_DNS_LOCK", None) is not None:
            assert finished.wait(1)


def test_request_dns_cache_has_entry_and_time_bounds(monkeypatch):
    monkeypatch.setattr(extras, "_DNS_CACHE_MAX", 2, raising=False)
    dns_calls = mock_dns(monkeypatch)
    mock_transport(monkeypatch, [(200, {}, "fixture")] * 5)
    for host in ("one.example", "one.example", "two.example", "three.example"):
        extras._request("GET", f"https://{host}/jobs")
    assert len(dns_calls) == 3
    assert len(extras._DNS_CACHE) <= 2
    monkeypatch.setattr(extras, "_DNS_TTL", 0, raising=False)
    extras._request("GET", "https://three.example/jobs")
    assert len(dns_calls) == 4


@pytest.mark.parametrize("url,host,address,port", [
    ("https://public.example/jobs", "public.example", "8.8.8.8", 443),
    ("http://public.example/jobs", "public.example", "8.8.8.8", 80),
    ("https://8.8.8.8/jobs", "8.8.8.8", "8.8.8.8", 443),
    ("https://[2606:4700:4700::1111]/jobs", "2606:4700:4700::1111", "2606:4700:4700::1111", 443),
])
def test_request_real_http_stack_uses_pinned_socket_and_verified_tls_identity(monkeypatch, url, host, address, port):
    import httpcore
    import ssl

    dns_calls = mock_dns(monkeypatch)
    connected, tls_hosts, written = [], [], []

    class SyntheticStream(httpcore.MockStream):
        def write(self, buffer, timeout=None):
            written.append(buffer)

        def start_tls(self, ssl_context, server_hostname=None, timeout=None):
            assert ssl_context.verify_mode == ssl.CERT_REQUIRED
            assert ssl_context.check_hostname is True
            tls_hosts.append(server_hostname)
            return super().start_tls(ssl_context, server_hostname, timeout)

    def connect(self, host, port, **kwargs):
        connected.append((host, port))
        return SyntheticStream([b"HTTP/1.1 200 OK\r\nContent-Length: 7\r\nConnection: close\r\n\r\nfixture"])

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", connect)
    response = extras._request("GET", url)
    assert response.text == "fixture" and str(response.url) == url
    assert connected == [(address, port)]
    assert tls_hosts == ([host] if url.startswith("https:") else [])
    assert b"Host: " + httpx.URL(url).netloc + b"\r\n" in b"".join(written)
    assert dns_calls == ([(host, port)] if host == "public.example" else [])


def test_request_retries_only_validated_public_addresses_on_connect_failure(monkeypatch):
    import httpcore

    mock_dns(monkeypatch, {"public.example": ["8.8.8.8", "9.9.9.9"]})
    connected = []

    def connect(self, host, port, **kwargs):
        connected.append(host)
        if host == "8.8.8.8":
            raise httpcore.ConnectError("synthetic first address unavailable")
        return httpcore.MockStream([b"HTTP/1.1 200 OK\r\nContent-Length: 7\r\n\r\nfixture"])

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", connect)
    assert extras._request("GET", "https://public.example/jobs").text == "fixture"
    assert connected == ["8.8.8.8", "9.9.9.9"]


def test_request_exhausted_dns_capacity_fails_closed_without_queueing(monkeypatch):
    slots = threading.BoundedSemaphore(1)
    slots.acquire()
    monkeypatch.setattr(extras, "_DNS_SLOTS", slots)
    dns_calls = mock_dns(monkeypatch)
    calls = mock_transport(monkeypatch, [(200, {}, "unsafe")])
    try:
        with pytest.raises(extras.SourceError, match="DNS resolver capacity"):
            extras._request("GET", "https://public.example/jobs")
    finally:
        slots.release()
    assert dns_calls == [] and calls == []


@pytest.mark.parametrize("published,expected", [
    ("Application Deadline: November 5, 2026 at 5:00 PM ET.", "2026-11-05T17:00:00-05:00"),
    ("Application Deadline: July 5, 2026 at 5:00 PM ET.", "2026-07-05T17:00:00-04:00"),
    ("Application Deadline: November 5, 2026 at 5:00 pm et.", "2026-11-05T17:00:00-05:00"),
    ("Closing date: November 5, 2026 at 5:00 PM EDT.", "2026-11-05T17:00:00-04:00"),
    ("Closing date: 2026-11-05 at 17:00 UTC.", "2026-11-05T17:00:00+00:00"),
    ("Application Deadline: 2026-11-05T17:00:00.123456Z.", "2026-11-05T17:00:00.123456+00:00"),
    ("Application Deadline: November 5, 2026 at 5:00 PM (America/Toronto).", "2026-11-05T17:00:00-05:00"),
    ("Application Deadline: 2026-11-05T17:00:00-05:00.", "2026-11-05T17:00:00-05:00"),
    ("Application Deadline: November 5, 2026 at 5:00 PM UTC-05:00.", "2026-11-05T17:00:00-05:00"),
    ("Application Deadline: November 5, 2026 at 5 PM ET.", "2026-11-05T17:00:00-05:00"),
    ("Application Deadline: July 5, 2026 at 5 PM ET.", "2026-07-05T17:00:00-04:00"),
    ("Application Deadline: November 5, 2026 at 5 pm et.", "2026-11-05T17:00:00-05:00"),
    ("Closing date: November 5, 2026 at 5 PM EDT.", "2026-11-05T17:00:00-04:00"),
    ("Closing date: 2026-11-05 at 12 AM UTC.", "2026-11-05T00:00:00+00:00"),
    ("Closing date: 2026-11-05 at 12 PM UTC.", "2026-11-05T12:00:00+00:00"),
    ("Application Deadline: November 5, 2026 at 5 PM (America/Toronto).", "2026-11-05T17:00:00-05:00"),
    ("Application Deadline: November 5, 2026 at 5 PM UTC-05:00.", "2026-11-05T17:00:00-05:00"),
    ("Application Deadline: November 5, 2026 at 5PM -0530.", "2026-11-05T17:00:00-05:30"),
])
def test_explicit_deadline_preserves_published_time_zone_and_full_evidence(published, expected):
    deadline, evidence = extras.explicit_deadline(published)
    assert deadline == expected
    assert datetime.fromisoformat(deadline).tzinfo is not None
    assert evidence == published


@pytest.mark.parametrize("published,expected_date", [
    ("Application Deadline: November 5, 2026 at 5:00 PM.", "2026-11-05"),
    ("Application Deadline: November 5, 2026 at 5:00 PM XYZ.", "2026-11-05"),
    ("Application Deadline: November 1, 2026 at 1:30 AM ET.", "2026-11-01"),
    ("Application Deadline: March 8, 2026 at 2:30 AM ET.", "2026-03-08"),
    ("Application Deadline: November 5, 2026 at 5 PM.", "2026-11-05"),
    ("Application Deadline: November 5, 2026 at 5 PM XYZ.", "2026-11-05"),
    ("Application Deadline: November 1, 2026 at 1 AM ET.", "2026-11-01"),
    ("Application Deadline: March 8, 2026 at 2 AM ET.", "2026-03-08"),
    ("Application Deadline: November 5, 2026 at 13 PM ET.", "2026-11-05"),
    ("Application Deadline: November 5, 2026 at 0 AM ET.", "2026-11-05"),
])
def test_explicit_deadline_without_unambiguous_timezone_stays_date_only(published, expected_date):
    deadline, evidence = extras.explicit_deadline(published)
    assert deadline == expected_date
    assert evidence == published  # Keep the published clock time even if it cannot define an instant.


@pytest.mark.parametrize("clock", ["5:00 PM", "5 PM"])
def test_workday_published_time_survives_adapter_json_round_trip(monkeypatch, clock):
    from career_radar import core
    def no_database(*args, **kwargs):
        pytest.fail("Adapter/core normalization must not open any database")

    monkeypatch.setattr(core.sqlite3, "connect", no_database)
    published = f"Application Deadline: November 5, 2026 at {clock} ET."
    mock_http(monkeypatch, [
        {"total": 1, "jobPostings": [{"title": "Software Co-op", "externalPath": "/job/Fixture_1",
                                    "locationsText": "Toronto, ON, Canada"}]},
        {"jobPostingInfo": {"id": "fixture-time", "title": "Software Co-op", "jobDescription": f"<p>{published}</p>"}},
    ])
    job = extras.fetch_source(source("workday", url="https://fixture.wd3.myworkdayjobs.com/Campus",
                                    tenant="fixture", site="Campus", company="Fixture"))[0]
    restored = json.loads(json.dumps(job))
    assert restored["deadline"] == "2026-11-05T17:00:00-05:00"
    assert datetime.fromisoformat(restored["deadline"]).astimezone(timezone.utc).isoformat() == "2026-11-05T22:00:00+00:00"
    assert restored["deadline_evidence"] == published
    assert restored["deadline_source_url"] == job["url"]
    now = datetime(2026, 10, 31, 14, tzinfo=timezone.utc)
    normalized = core.normalize_job(restored, now)
    direct = core.normalize_job({**restored, "deadline": None, "deadline_evidence": None}, now)
    assert normalized is not None and direct is not None
    assert normalized["deadline_kind"] == direct["deadline_kind"] == "datetime"
    assert normalized["deadline"] == direct["deadline"] == "2026-11-05T22:00:00+00:00"
    assert normalized["deadline_evidence"] == published
    assert normalized["deadline_source_url"] == job["url"]


@pytest.mark.parametrize("source_id", [
    "simplify-new-graduates", "simplify-internships-all-terms", "frogg-canadian-internships",
])
def test_configured_curated_feeds_output_open_then_closed_for_reconciliation(monkeypatch, source_id):
    configs = json.loads((Path(__file__).resolve().parents[1] / "career_radar" / "boards" / "extra_sources.json").read_text())
    config = next(s for s in configs if s["id"] == source_id)
    if config["type"] == "simplify":
        opened = {"id": "fixture-lifecycle", "company_name": "Fixture", "title": "Software Intern",
                  "locations": ["Toronto, ON, Canada"], "url": "https://employer.invalid/job/lifecycle",
                  "active": True, "is_visible": True}
        closed = {**opened, "active": False}
        foreign = {**closed, "id": "fixture-us", "locations": ["New York, NY, USA"],
                   "url": "https://employer.invalid/job/us"}
    else:
        opened = {"company": "Fixture", "role": "Software Intern", "location": "Toronto, ON, Canada",
                  "link": "https://employer.invalid/job/lifecycle", "status": "open", "date_closed": None}
        closed = {**opened, "status": "closed", "date_closed": "2026-11-01"}
        foreign = {**closed, "location": "New York, NY, USA", "link": "https://employer.invalid/job/us"}
    mock_http(monkeypatch, [[opened, foreign], [closed, foreign]])
    before = extras.fetch_source(config)
    after = extras.fetch_source(config)
    assert len(before) == 1 and before[0]["is_closed"] is False
    assert len(after) == 1, "An explicit closure must reach the parent's reconciliation layer"
    assert after[0]["is_closed"] is True
    assert (after[0]["source_job_id"], after[0]["url"]) == (before[0]["source_job_id"], before[0]["url"])
    assert after[0]["country_hint"] == "CA"
    assert [j for j in after if not j["is_closed"]] == []
    assert_contract(after[0])


def test_remotive_registry_sets_six_hour_collection_cooldown():
    configs = json.loads((Path(__file__).resolve().parents[1] / "career_radar" / "boards" / "extra_sources.json").read_text())
    config = next(s for s in configs if s["id"] == "remotive-canada")
    assert config.get("enabled", True) is True
    assert config.get("min_interval_seconds") == 21600
    assert config["canada_only"] is True


def test_simplify_preserves_explicit_closure_even_when_curator_hides_record(monkeypatch):
    hidden = {"id": "fixture-hidden", "company_name": "Fixture", "title": "Software Intern",
              "locations": ["Toronto, ON, Canada"], "url": "https://employer.invalid/job/hidden",
              "active": True, "is_visible": False}
    mock_http(monkeypatch, [[hidden], [{**hidden, "active": False}]])
    config = source("simplify", canada_only=True, include_closed=True)
    assert extras.fetch_source(config) == []  # Hidden active entries are never advertised as open.
    jobs = extras.fetch_source(config)
    assert len(jobs) == 1 and jobs[0]["is_closed"] is True


@pytest.mark.parametrize("headers", [{}, {"Content-Length": "10"}])
def test_streamed_response_cap_stops_at_first_overflow(monkeypatch, headers):
    mock_dns(monkeypatch)
    stream = TrackedStream([b"a" * 10, b"b" * 10, b"do-not-consume"])
    mock_transport(monkeypatch, [(200, headers, stream)])
    monkeypatch.setattr(extras, "MAX_RESPONSE_BYTES", 16)
    with pytest.raises(extras.SourceError, match="byte limit"):
        extras._request("GET", "https://public.example/jobs")
    assert stream.consumed == [0, 1]
    assert stream.close_calls == 1


@pytest.mark.parametrize("length,message", [
    ("17", "byte limit"), ("-1", "Content-Length"), ("invalid", "Content-Length"),
])
def test_streamed_response_rejects_bad_length_before_reading(monkeypatch, length, message):
    mock_dns(monkeypatch)
    stream = TrackedStream([b"do-not-consume"])
    mock_transport(monkeypatch, [(200, {"Content-Length": length}, stream)])
    monkeypatch.setattr(extras, "MAX_RESPONSE_BYTES", 16)
    with pytest.raises(extras.SourceError, match=message):
        extras._request("GET", "https://public.example/jobs")
    assert stream.consumed == []
    assert stream.close_calls == 1


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308, 403, 429, 500])
def test_streamed_response_redirects_and_errors_close_without_reading(monkeypatch, status):
    mock_dns(monkeypatch)
    stream = TrackedStream([b"do-not-consume"])
    headers = {"Location": "/final?chosen=1", "Content-Length": "1000000",
               "Set-Cookie": "fixture=not-forwarded"}
    calls = mock_transport(monkeypatch, [(status, headers, stream), (200, {}, "final")])
    monkeypatch.setattr(extras, "MAX_RESPONSE_BYTES", 16)
    if status < 400:
        response = extras._request("POST", "https://public.example/start", params={"initial": 1}, json={"fixture": True})
        assert response.text == "final"
        assert str(response.url) == "https://public.example/final?chosen=1"
        final = calls[1][0]
        assert "Cookie" not in final.headers
        assert final.method == ("GET" if status == 303 else "POST")
        assert final.content == (b"" if status == 303 else b'{"fixture":true}')
    else:
        with pytest.raises(extras.SourceError, match=str(status)):
            extras._request("GET", "https://public.example/start")
        assert len(calls) == 1
    assert stream.consumed == []
    assert stream.close_calls == 1


@pytest.mark.parametrize("target", ["http://127.1/private", "https://private.example/jobs", "/login", "/start", None])
def test_streamed_response_rejected_redirect_closes_without_reading(monkeypatch, target):
    mock_dns(monkeypatch, {"private.example": ["127.0.0.1"]})
    stream = TrackedStream([b"do-not-consume"])
    calls = mock_transport(monkeypatch, [(302, {"Location": target} if target else {}, stream)])
    with pytest.raises(extras.SourceError):
        extras._request("GET", "https://public.example/start")
    assert len(calls) == 1
    assert stream.consumed == []
    assert stream.close_calls == 1


@pytest.mark.parametrize("kind", ["json", "html"])
@pytest.mark.parametrize("coding", ["identity", "gzip", "deflate", "raw-deflate"])
def test_streamed_response_success_preserves_decoding_and_callers(monkeypatch, kind, coding):
    mock_dns(monkeypatch)
    data = {"jobs": [{"title": "Développeur à Montréal"}]}
    text = json.dumps(data, ensure_ascii=False) if kind == "json" else "<p>Développeur à Montréal</p>"
    charset = "utf-8" if kind == "json" else "iso-8859-1"
    body = text.encode(charset)
    wire = (gzip.compress(body) if coding == "gzip" else
            zlib.compress(body, wbits=-zlib.MAX_WBITS if coding == "raw-deflate" else zlib.MAX_WBITS)
            if coding in {"deflate", "raw-deflate"} else body)
    headers = {"Content-Type": f"{'application/json' if kind == 'json' else 'text/html'}; charset={charset}",
               "Content-Length": str(len(wire)), "Content-Encoding": "deflate" if coding == "raw-deflate" else coding}
    stream = TrackedStream([wire[:10], wire[10:]])
    calls = mock_transport(monkeypatch, [(200, headers, stream)])
    monkeypatch.setattr(extras, "MAX_RESPONSE_BYTES", max(len(wire), len(body)))
    if kind == "json":
        assert extras._json_response("GET", "https://public.example/jobs") == data
    else:
        response = extras._request("GET", "https://public.example/jobs")
        assert response.content == body
        assert response.text == text and response.encoding == charset
        assert response.headers["Content-Encoding"] == headers["Content-Encoding"]
        assert str(response.url) == "https://public.example/jobs"
        assert response.is_closed
    assert calls[0][0].headers["Accept-Encoding"] == "gzip, deflate"
    assert stream.consumed == [0, 1]
    assert stream.close_calls == 1


@pytest.mark.parametrize("coding", ["gzip", "deflate"])
def test_streamed_response_caps_decompression_before_allocating_full_output(monkeypatch, coding):
    mock_dns(monkeypatch)
    wire = gzip.compress(b"a" * 256) if coding == "gzip" else zlib.compress(b"a" * 256)
    assert len(wire) < 64
    stream = TrackedStream([wire, b"do-not-consume"])
    mock_transport(monkeypatch, [(200, {"Content-Length": str(len(wire)), "Content-Encoding": coding}, stream)])
    monkeypatch.setattr(extras, "MAX_RESPONSE_BYTES", 64)
    original_decoder, output_limits = zlib.decompressobj, []

    class RecordingDecoder:
        def __init__(self, *args, **kwargs):
            self.decoder = original_decoder(*args, **kwargs)

        def decompress(self, data, max_length=0):
            output_limits.append(max_length)
            return self.decoder.decompress(data, max_length)

        def __getattr__(self, name):
            return getattr(self.decoder, name)

    monkeypatch.setattr(zlib, "decompressobj", RecordingDecoder)
    with pytest.raises(extras.SourceError, match="byte limit"):
        extras._request("GET", "https://public.example/jobs")
    assert output_limits and all(0 < limit <= 65 for limit in output_limits)
    assert stream.consumed == [0]
    assert stream.close_calls == 1


def test_streamed_response_also_caps_compressed_wire_bytes(monkeypatch):
    mock_dns(monkeypatch)
    wire = gzip.compress(b"ok")
    assert len(wire) > 16
    stream = TrackedStream([wire, b"do-not-consume"])
    mock_transport(monkeypatch, [(200, {"Content-Encoding": "gzip"}, stream)])
    monkeypatch.setattr(extras, "MAX_RESPONSE_BYTES", 16)
    with pytest.raises(extras.SourceError, match="byte limit"):
        extras._request("GET", "https://public.example/jobs")
    assert stream.consumed == [0]
    assert stream.close_calls == 1


@pytest.mark.parametrize("coding", ["br", "zstd", "gzip, deflate", "unknown"])
def test_streamed_response_unsupported_compression_fails_closed_before_reading(monkeypatch, coding):
    mock_dns(monkeypatch)
    stream = TrackedStream([b"do-not-consume"])
    mock_transport(monkeypatch, [(200, {"Content-Encoding": coding}, stream)])
    with pytest.raises(extras.SourceError, match="Content-Encoding"):
        extras._request("GET", "https://public.example/jobs")
    assert stream.consumed == []
    assert stream.close_calls == 1


@pytest.mark.parametrize("damage", ["truncated", "trailing"])
def test_streamed_response_invalid_compression_is_not_partial_success(monkeypatch, damage):
    mock_dns(monkeypatch)
    wire = gzip.compress(b"fixture")
    wire = wire[:-4] if damage == "truncated" else wire + gzip.compress(b"unexpected second member")
    stream = TrackedStream([wire])
    mock_transport(monkeypatch, [(200, {"Content-Encoding": "gzip"}, stream)])
    with pytest.raises(extras.SourceError, match="compressed"):
        extras._request("GET", "https://public.example/jobs")
    assert stream.close_calls == 1


def test_streamed_response_read_failure_closes_and_reports_source_error(monkeypatch):
    mock_dns(monkeypatch)
    stream = TrackedStream([b"part", httpx.ReadError("synthetic reset"), b"do-not-consume"])
    mock_transport(monkeypatch, [(200, {}, stream)])
    with pytest.raises(extras.SourceError, match="Network error.*ReadError"):
        extras._request("GET", "https://public.example/jobs")
    assert stream.consumed == [0, 1]
    assert stream.close_calls == 1


@pytest.mark.parametrize("closed_at", [29.0, 30.0, 31.0])
def test_success_deadline_includes_client_transport_teardown(monkeypatch, closed_at):
    clock, calls, closes = [0.0], [], []
    stream = TrackedStream([b"{}"])
    monkeypatch.setattr(extras.time, "monotonic", lambda: clock[0])

    class ClosingTransport(httpx.MockTransport):
        def close(self):
            super().close()
            clock[0] = closed_at
            closes.append(closed_at)

    def handle(request):
        calls.append(request)
        assert "cookie" not in request.headers
        return httpx.Response(200, stream=stream)

    monkeypatch.setattr(extras, "_PublicTransport", lambda **kwargs: ClosingTransport(handle))
    if closed_at >= 30.0:
        with pytest.raises(extras.SourceError, match="deadline"):
            extras._request("GET", "https://public.example/jobs")
    else:
        response = extras._request("GET", "https://public.example/jobs")
        assert response.status_code == 200 and response.json() == {} and response.is_closed
    assert len(calls) == 1 and closes == [closed_at] and clock[0] == closed_at
    assert stream.consumed == [0] and stream.close_calls == 1

