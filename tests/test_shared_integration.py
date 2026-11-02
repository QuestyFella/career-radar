"""Exercise actual callers of the consolidated policies, with no live I/O."""
from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import sqlite3

import httpx
import pytest

from career_radar import adapters
from career_radar import bounded_http
from career_radar import core
from career_radar import deadlines
from career_radar import extras

NOW = datetime(2026, 10, 31, 14, tzinfo=timezone.utc)
URL_CASES = json.loads((Path(__file__).parent / "fixtures/url_safety_cases.json").read_text())["cases"]
SOURCE = {"id": "fixture-workday", "name": "Fixture", "type": "workday", "company": "Fixture",
          "url": "https://fixture.wd3.myworkdayjobs.com/Campus", "tenant": "fixture", "site": "Campus"}


@pytest.fixture(autouse=True)
def no_live_io(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Shared-policy tests must not access network or a database")
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)


@pytest.mark.parametrize("case", URL_CASES, ids=lambda case: case["id"])
def test_core_uses_shared_job_link_policy(case):
    assert core.canonical_url(case["value"]) == case["canonical"]


@pytest.mark.parametrize("case", URL_CASES, ids=lambda case: case["id"])
def test_extra_fetcher_uses_shared_public_url_policy(case):
    if case["normalized"] is None:
        with pytest.raises(extras.SourceError):
            extras._safe_url(case["value"])
    else:
        assert extras._safe_url(case["value"]) == case["normalized"]


def transport_response(payload):
    data = json.dumps(payload).encode()
    return httpx.Response(200, headers={"Content-Type": "application/json", "Content-Length": str(len(data))},
                          stream=httpx.ByteStream(data))


PUBLISHED = [
    "Application Deadline: November 5, 2026 at 5 PM ET.",
    "Application Deadline: November 5, 2026 at 17:30:15 UTC.",
    "Application Deadline: November 5, 2026 at 17:30 -05:00.",
    "Application Deadline: November 5, 2026 at 17:30:15.123456 UTC.",
    "Application Deadline: November 1, 2026 at 1:30 AM ET.",
    "Application Deadline: March 8, 2026 at 2:30 AM ET.",
    "Application Deadline: November 5, 2026 at 5 PM.",
    "Application Deadline: November 5, 2026 at 5 PM XYZ.",
    "Application Deadline: November 5, 2026 at 25:30 UTC.",
    "Apply by 5th November 2026 at noon UTC.",
]


@pytest.mark.parametrize("published", PUBLISHED)
@pytest.mark.parametrize("path", ["ats", "workday"])
def test_actual_source_to_core_preserves_shared_deadline_result(monkeypatch, published, path):
    expected = deadlines.parse_published_deadline(published)
    assert expected is not None
    if path == "ats":
        row = adapters._lever({"id": "fixture-lever", "name": "Fixture", "type": "lever", "company": "Fixture"}, {
            "id": "fixture-A", "text": "Software Intern", "hostedUrl": "https://jobs.example/A",
            "categories": {"location": "Toronto, ON, Canada"},
            "descriptionPlain": "Computer science undergraduates welcome. " + published,
        })
    else:
        payloads = iter([
            {"total": 1, "jobPostings": [{"title": "Software Intern", "externalPath": "/job/Fixture_A",
                                         "locationsText": "Toronto, ON, Canada"}]},
            {"jobPostingInfo": {"id": "fixture-A", "title": "Software Intern",
                                "jobDescription": "<p>Computer science undergraduates welcome.</p><p>" + published + "</p>"}},
        ])
        monkeypatch.setattr(extras, "_PublicTransport", lambda **kw: httpx.MockTransport(lambda req: transport_response(next(payloads))))
        row = extras.fetch_source(SOURCE)[0]
    normalized = core.normalize_job(json.loads(json.dumps(row)), NOW)
    assert normalized is not None
    assert normalized["deadline_evidence"] == expected.evidence
    assert normalized.get("deadline_uncertainty") == expected.uncertainty
    if "T" in expected.value:
        actual = datetime.fromisoformat(normalized["deadline"])
        assert normalized["deadline_kind"] == "datetime"
        assert actual == datetime.fromisoformat(expected.value)
    else:
        assert normalized["deadline_kind"] == "date"
        assert normalized["deadline_date"] == expected.value
    if expected.uncertainty:
        assert any("deadline" in note.lower() and "confirm" in note.lower() for note in normalized["eligibility_notes"])


@pytest.mark.parametrize("value,uncertainty", [
    ("2026-11-05T17:30:00-00:00", "unknown_timezone"),
    ("2026-11-05T17:30:00", "missing_timezone"),
    ("2026-11-05T25:30:00+00:00", "invalid_clock"),
])
def test_structured_clock_obeys_same_uncertainty_policy(value, uncertainty):
    row = {"source_id": "fixture", "source_job_id": "A", "company": "Fixture", "title": "Software Intern",
           "location": "Toronto, Canada", "url": "https://jobs.example/A", "description": "Computer science students welcome.",
           "deadline": value, "deadline_evidence": "JobPosting.validThrough: " + value}
    job = core.normalize_job(row, NOW)
    assert job["deadline_kind"] == "date"
    assert job["deadline_date"] == "2026-11-05"
    assert job.get("deadline_uncertainty") == uncertainty
    assert job["deadline_evidence"] == row["deadline_evidence"]


@pytest.mark.parametrize("path", ["ats", "jsonld", "workday"])
@pytest.mark.parametrize("value,uncertainty", [
    ("2026-11-05T25:30:00+00:00", "invalid_clock"),
    ("2026-11-05T17:30:00+25:00", "unknown_timezone"),
])
def test_source_structured_invalid_clock_retains_calendar_and_caveat(monkeypatch, path, value, uncertainty):
    title, url = "Software Intern", "https://jobs.example/A"
    description = "Computer science undergraduates welcome."
    if path == "ats":
        row = adapters._lever({"id": "fixture-lever", "name": "Fixture", "type": "lever"}, {
            "id": "fixture-A", "text": title, "hostedUrl": url,
            "categories": {"location": "Toronto, ON, Canada"},
            "descriptionPlain": description, "validThrough": value,
        })
    elif path == "jsonld":
        item = {"@type": "JobPosting", "title": title, "description": description,
                "identifier": {"value": "fixture-A"}, "url": url, "validThrough": value,
                "hiringOrganization": {"name": "Fixture"},
                "jobLocation": {"address": {"addressLocality": "Toronto", "addressRegion": "ON", "addressCountry": "CA"}}}
        page = '<script type="application/ld+json">' + json.dumps(item) + '</script>'
        row, = extras.parse_jobposting_jsonld(page, {"id": "fixture-jsonld", "name": "Fixture", "type": "jsonld"}, url)
    else:
        payloads = iter([
            {"total": 1, "jobPostings": [{"title": title, "externalPath": "/job/Fixture_A", "locationsText": "Toronto, ON, Canada"}]},
            {"jobPostingInfo": {"id": "fixture-A", "title": title, "jobDescription": description, "validThrough": value}},
        ])
        monkeypatch.setattr(extras, "_PublicTransport", lambda **kw: httpx.MockTransport(lambda req: transport_response(next(payloads))))
        row, = extras.fetch_source(SOURCE)
    job = core.normalize_job(json.loads(json.dumps(row)), NOW)
    assert job["deadline_kind"] == "date"
    assert job["deadline_date"] == "2026-11-05"
    assert job["deadline_uncertainty"] == uncertainty
    assert value in job["deadline_evidence"]


@pytest.mark.parametrize("value,expected", [
    (None, None), (123, None), ({"date": "2026-11-05"}, None), ("tomorrow", None),
    ("2026-02-30", None), ("2026-13-01 Deadline: 2026-11-05", None),
    ("2026-11-05", ("2026-11-05", None)),
    (" 2026-11-05T17:30:00-00:00 ", ("2026-11-05", "unknown_timezone")),
    ("2026-11-05Tgarbage", ("2026-11-05", "invalid_clock")),
    ("2026-11-05T17:30:00+25:00", ("2026-11-05", "unknown_timezone")),
])
def test_structured_shared_entrypoint_keeps_original_evidence(value, expected):
    parsed = deadlines.parse_structured_deadline(value)
    if expected is None:
        assert parsed is None
    else:
        assert (parsed.value, parsed.uncertainty) == expected
        assert parsed.evidence == value


@pytest.mark.parametrize("module", [adapters, extras], ids=lambda module: module.__name__)
def test_all_http_paths_use_shared_reader_with_real_deadline(monkeypatch, module):
    observed = []
    original = bounded_http.read_bounded
    def spy(response, **kwargs):
        observed.append(kwargs)
        return original(response, **kwargs)
    monkeypatch.setattr(module, "read_bounded", spy)
    transport = httpx.MockTransport(lambda req: transport_response({"ok": True}))
    if module is extras:
        monkeypatch.setattr(extras, "_PublicTransport", lambda **kw: transport)
        result = extras._json_response("GET", "https://jobs.example/fixture")
    else:
        with httpx.Client(transport=transport) as client:
            result, _ = adapters._request(client, "https://jobs.example/fixture")
    assert result == {"ok": True}
    assert len(observed) == 1
    assert observed[0]["max_bytes"] == module.MAX_RESPONSE_BYTES
    assert observed[0]["deadline"] is not None


def test_extra_empty_chunks_cannot_evade_overall_deadline(monkeypatch):
    clock, consumed = [0.0], []
    class Delayed(httpx.SyncByteStream):
        def __iter__(self):
            for index, chunk in enumerate([b"", b"", b"", b"{}"]):
                clock[0] += 11.0
                consumed.append(index)
                yield chunk
    monkeypatch.setattr(extras.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(extras, "_PublicTransport", lambda **kw: httpx.MockTransport(
        lambda req: httpx.Response(200, stream=Delayed())))
    with pytest.raises(extras.SourceError, match="deadline"):
        extras._request("GET", "https://jobs.example/fixture")
    assert consumed == [0, 1, 2]
