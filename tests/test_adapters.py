"""Synthetic ATS fixtures only; never added to a live source or job database."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import httpx

from career_radar import adapters
import pytest
from test_bounded_http import ProbeStream, coding_headers, compressed, unread_response


GH = {"id": "greenhouse-fixture", "name": "Fixture Employer", "type": "greenhouse", "board": "fixture", "url": "https://job-boards.greenhouse.io/fixture"}
LEVER = {"id": "lever-fixture", "name": "Fixture Employer", "type": "lever", "board": "fixture", "url": "https://jobs.lever.co/fixture", "page_size": 2}
ASHBY = {"id": "ashby-fixture", "name": "Fixture Employer", "type": "ashby", "board": "fixture", "url": "https://jobs.ashbyhq.com/fixture"}


def gh_job(job_id=1, **overrides):
    job = {
        "id": job_id, "title": "Junior Firmware Developer", "location": {"name": "Toronto, Canada"},
        "absolute_url": f"https://job-boards.greenhouse.io/fixture/jobs/{job_id}",
        "content": "&lt;p&gt;Build &lt;strong&gt;flight&lt;/strong&gt; software.&lt;/p&gt;&lt;ul&gt;&lt;li&gt;0–2 years experience&lt;/li&gt;&lt;/ul&gt;",
        "updated_at": "2026-10-20T09:10:00-04:00", "first_published": "2026-10-01T09:10:00-04:00",
        "application_deadline": "2026-11-15T23:59:00-05:00",
    }
    job.update(overrides)
    return job


def lever_job(job_id="one", **overrides):
    job = {
        "id": job_id, "text": "Software Developer Intern", "country": "CA",
        "categories": {"location": "Toronto", "allLocations": ["Toronto", "Vancouver"], "commitment": "Intern", "team": "Engineering"},
        "descriptionPlain": "Build tools with our team.",
        "lists": [{"text": "Requirements", "content": "<li>Currently enrolled in a CS degree.</li><li>Python and C++.</li>"}],
        "additional": "<p>Apply by November 15, 2026.</p>",
        "salaryDescriptionPlain": "Pay: CAD 35 per hour.",
        "hostedUrl": f"https://jobs.lever.co/fixture/{job_id}",
        "applyUrl": f"https://jobs.lever.co/fixture/{job_id}/apply", "createdAt": 1767225600000,
    }
    job.update(overrides)
    return job


def ashby_job(job_id="one", **overrides):
    job = {
        "title": "Software Engineer, New Grad", "location": "San Francisco, CA", "isListed": True,
        "address": {"postalAddress": {"addressCountry": "USA"}},
        "secondaryLocations": [{"location": "Toronto", "address": {"addressCountry": "CAN", "addressRegion": "Ontario"}}],
        "descriptionHtml": "<p>Develop our platform.</p><ul><li>Graduating students welcome.</li></ul>",
        "publishedAt": "2026-10-01T12:00:00Z", "employmentType": "FullTime", "isRemote": True,
        "jobUrl": f"https://jobs.ashbyhq.com/fixture/{job_id}", "applyUrl": f"https://jobs.ashbyhq.com/fixture/{job_id}/application",
    }
    job.update(overrides)
    return job


class AdapterTests(unittest.TestCase):
    def fetch(self, source, handler):
        """Exercise the real HTTP path through an in-memory transport."""
        real_client = httpx.Client
        calls = []

        def record(request):
            calls.append(request)
            self.assertEqual(request.method, "GET")
            self.assertIn("CareerRadar", request.headers["User-Agent"])
            self.assertIn("timeout", request.extensions)
            return handler(request)

        def factory(**kwargs):
            return real_client(transport=httpx.MockTransport(record), **kwargs)

        with patch.object(adapters.httpx, "Client", side_effect=factory), patch.object(adapters.time, "sleep"):
            result = adapters.fetch_source(copy.deepcopy(source))
        return result, calls

    def test_greenhouse_full_content_dates_and_contract(self):
        result, calls = self.fetch(GH, lambda r: unread_response(200, json={"jobs": [gh_job()], "meta": {"total": 1}}))
        self.assertEqual(calls[0].url.params["content"], "true")
        row = result[0]
        required = {"source_id", "source", "source_type", "source_job_id", "company", "title", "location", "url", "description", "posted_at", "deadline", "deadline_evidence", "deadline_source_url", "verification", "is_closed"}
        self.assertTrue(required <= row.keys())
        self.assertEqual(row["source_job_id"], "1")
        self.assertEqual(row["company"], "Fixture Employer")
        self.assertEqual(row["source_id"], GH["id"])
        self.assertIn("Build flight software.", row["description"])
        self.assertIn("0–2 years", row["description"])
        self.assertNotIn("<", row["description"])
        self.assertEqual(row["posted_at"], "2026-10-01T09:10:00-04:00")
        self.assertEqual(row["updated_at"], "2026-10-20T09:10:00-04:00")
        self.assertEqual(row["deadline"], "2026-11-15T23:59:00-05:00")
        self.assertIn("application_deadline", row["deadline_evidence"])
        self.assertEqual(row["deadline_source_url"], row["url"])
        self.assertEqual(row["country_hint"], "CA")
        self.assertEqual(row["verification"], "direct")
        self.assertFalse(row["is_closed"])

    def test_updated_at_is_not_a_posting_date_and_no_invented_expiry(self):
        job = gh_job(first_published=None, application_deadline=None, location={"name": "San Francisco, CA"})
        result, _ = self.fetch(GH, lambda r: unread_response(200, json={"jobs": [job], "meta": {"total": 1}}))
        self.assertIsNone(result[0]["posted_at"])
        self.assertIsNone(result[0]["deadline"])
        self.assertIsNone(result[0]["deadline_evidence"])
        self.assertIsNone(result[0].get("country_hint"))

    def test_greenhouse_missing_content_fetches_detail(self):
        def handler(request):
            if request.url.path.endswith("/jobs"):
                return unread_response(200, json={"jobs": [gh_job(content=None)], "meta": {"total": 1}})
            self.assertTrue(request.url.path.endswith("/jobs/1"))
            return unread_response(200, json=gh_job())
        result, calls = self.fetch(GH, handler)
        self.assertEqual(len(calls), 2)
        self.assertIn("flight", result[0]["description"])

    def test_greenhouse_truncation_is_a_failure_not_success(self):
        with self.assertRaisesRegex(adapters.SourceError, "(?i)(truncat|total)"):
            self.fetch(GH, lambda r: unread_response(200, json={"jobs": [gh_job()], "meta": {"total": 2}}))

    def test_greenhouse_follows_actual_same_endpoint_next_link(self):
        next_url = "https://boards-api.greenhouse.io/v1/boards/fixture/jobs?content=true&page=2"
        def handler(request):
            if request.url.params.get("page") == "2":
                return unread_response(200, json={"jobs": [gh_job(2)], "meta": {"total": 2}})
            return unread_response(200, json={"jobs": [gh_job()], "meta": {"total": 2}}, headers={"Link": f'<{next_url}>; rel="next"'})
        result, calls = self.fetch(GH, handler)
        self.assertEqual([j["source_job_id"] for j in result], ["1", "2"])
        self.assertEqual(len(calls), 2)

    def test_lever_real_skip_limit_pagination_and_full_description(self):
        jobs = [lever_job(str(i)) for i in range(5)]
        def handler(request):
            start = int(request.url.params["skip"])
            self.assertEqual(request.url.params["mode"], "json")
            self.assertEqual(request.url.params["limit"], "2")
            return unread_response(200, json=jobs[start:start + 2])
        result, calls = self.fetch(LEVER, handler)
        self.assertEqual(len(result), 5)
        self.assertEqual([int(c.url.params["skip"]) for c in calls], [0, 2, 4])
        self.assertIn("Currently enrolled", result[0]["description"])
        self.assertIn("Apply by November 15", result[0]["description"])
        self.assertIn("CAD 35", result[0]["description"])
        self.assertIn("Vancouver", result[0]["location"])
        self.assertEqual(result[0]["posted_at"], "2026-01-01T00:00:00+00:00")
        self.assertIsNone(result[0]["deadline"], "Text deadlines belong to the parent's evidence parser")
        self.assertEqual(result[0]["level_hint"], "internship")

    def test_lever_exact_page_multiple_requests_terminal_empty_page(self):
        result, calls = self.fetch(LEVER, lambda r: unread_response(200, json=[lever_job("a"), lever_job("b")] if r.url.params["skip"] == "0" else []))
        self.assertEqual(len(result), 2)
        self.assertEqual(len(calls), 2)

    def test_lever_repeated_page_and_explicit_page_cap_raise(self):
        with self.assertRaisesRegex(adapters.SourceError, "(?i)(repeat|advance|pagination)"):
            self.fetch(LEVER, lambda r: unread_response(200, json=[lever_job("a"), lever_job("b")]))
        with self.assertRaisesRegex(adapters.SourceError, "(?i)(cap|truncat|maximum)"):
            self.fetch(dict(LEVER, max_pages=1), lambda r: unread_response(200, json=[lever_job("a"), lever_job("b")]))

    def test_lever_eu_endpoint_and_opening_body_fallback(self):
        source = dict(LEVER, region="eu", url="https://jobs.eu.lever.co/fixture")
        job = lever_job(descriptionPlain=None, opening="<p>Opening.</p>", descriptionBody="<p>Body.</p>", createdAt=None)
        result, calls = self.fetch(source, lambda r: unread_response(200, json=[job]))
        self.assertEqual(calls[0].url.host, "api.eu.lever.co")
        self.assertIn("Opening.", result[0]["description"])
        self.assertIn("Body.", result[0]["description"])
        self.assertIsNone(result[0]["posted_at"])

    def test_ashby_full_snapshot_secondary_locations_and_unlisted(self):
        result, calls = self.fetch(ASHBY, lambda r: unread_response(200, json={"apiVersion": "1", "jobs": [ashby_job(), ashby_job("unlisted", isListed=False)]}))
        self.assertEqual(len(calls), 1, "Ashby's public posting API is a full snapshot, not its authenticated cursor API")
        self.assertEqual(calls[0].url.params["includeCompensation"], "true")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["source_job_id"], "one")
        self.assertIn("Toronto", result[0]["location"])
        self.assertEqual(result[0]["country_hint"], "CA")
        self.assertEqual(result[0]["posted_at"], "2026-10-01T12:00:00+00:00")
        self.assertEqual(result[0]["employment_type"], "FullTime")
        self.assertEqual(result[0]["level_hint"], "new_grad")
        self.assertIn("Graduating students welcome", result[0]["description"])

    def test_ashby_unknown_continuation_is_not_silently_ignored(self):
        with self.assertRaisesRegex(adapters.SourceError, "(?i)(pagination|continuation|truncat)"):
            self.fetch(ASHBY, lambda r: unread_response(200, json={"jobs": [ashby_job()], "moreDataAvailable": True, "nextCursor": "opaque"}))

    def test_empty_boards_are_valid_but_missing_job_list_is_not(self):
        for source, payload in [(GH, {"jobs": [], "meta": {"total": 0}}), (LEVER, []), (ASHBY, {"jobs": []})]:
            with self.subTest(source=source["type"]):
                result, _ = self.fetch(source, lambda r: unread_response(200, json=payload))
                self.assertEqual(result, [])
                with self.assertRaises(adapters.SourceError):
                    self.fetch(source, lambda r: unread_response(200, json={"error": "board unavailable"}))

    def test_invalid_item_or_missing_description_is_a_visible_failure(self):
        for payload in [{"jobs": ["not a job"]}, {"jobs": [gh_job(title="")]}, {"jobs": [gh_job(absolute_url="javascript:alert(1)")]}]:
            with self.subTest(payload=payload), self.assertRaises(adapters.SourceError):
                self.fetch(GH, lambda r: unread_response(200, json=payload))
        with self.assertRaisesRegex(adapters.SourceError, "(?i)description"):
            self.fetch(ASHBY, lambda r: unread_response(200, json={"jobs": [ashby_job(descriptionHtml=None)]}))

    def test_http_failure_challenge_and_retry_exhaustion_are_visible(self):
        for code in [401, 403, 404, 429, 503]:
            with self.subTest(status=code), self.assertRaisesRegex(adapters.SourceError, str(code)):
                self.fetch(GH, lambda r: unread_response(code, text="Unavailable"))
        with self.assertRaisesRegex(adapters.SourceError, "(?i)(json|html|challenge)"):
            self.fetch(GH, lambda r: unread_response(200, text="<html>Verify you are human</html>", headers={"Content-Type": "text/html"}))

    def test_transient_failure_retries_then_returns_real_response(self):
        attempted = []
        def handler(request):
            attempted.append(request)
            return unread_response(503 if len(attempted) == 1 else 200, json={"jobs": [], "meta": {"total": 0}})
        result, calls = self.fetch(GH, handler)
        self.assertEqual(result, [])
        self.assertEqual(len(calls), 2)

    def test_transport_timeout_is_bounded_and_reported(self):
        def handler(request):
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        with self.assertRaisesRegex(adapters.SourceError, "(?i)(timeout|timed)"):
            self.fetch(GH, handler)

    def test_foreign_next_link_and_endpoint_override_are_rejected(self):
        with self.assertRaises(adapters.SourceError):
            self.fetch(GH, lambda r: unread_response(200, json={"jobs": [gh_job()], "meta": {"total": 2}}, headers={"Link": '<http://127.0.0.1/private>; rel="next"'}))
        for source in [dict(GH, board="../private"), dict(GH, api_url="http://127.0.0.1/private"), dict(GH, type="unknown")]:
            with self.subTest(source=source), self.assertRaises(adapters.SourceError):
                self.fetch(source, lambda r: self.fail("Invalid configuration must not perform network I/O"))

    def test_date_only_deadline_and_malformed_dates(self):
        result, _ = self.fetch(GH, lambda r: unread_response(200, json={"jobs": [gh_job(first_published="not a date", application_deadline="2026-11-15")], "meta": {"total": 1}}))
        self.assertEqual(result[0]["deadline"], "2026-11-15")
        self.assertIsNone(result[0]["posted_at"])

    def test_html_encoded_literal_types_survive_html_cleanup(self):
        job = gh_job(content="&lt;p&gt;Use vector&amp;lt;T&amp;gt; and x &amp;lt; 5 in C++.&lt;/p&gt;")
        result, _ = self.fetch(GH, lambda r: unread_response(200, json={"jobs": [job], "meta": {"total": 1}}))
        self.assertIn("vector<T>", result[0]["description"])
        self.assertIn("x < 5", result[0]["description"])

    def test_partial_lever_overlap_reports_inconsistent_snapshot(self):
        pages = {"0": [lever_job("a"), lever_job("b")], "2": [lever_job("b"), lever_job("c")], "4": []}
        with self.assertRaisesRegex(adapters.SourceError, "(?i)(duplicate|overlap|inconsistent)"):
            self.fetch(LEVER, lambda r: unread_response(200, json=pages[r.url.params["skip"]]))

    def test_explicit_api_error_cannot_be_an_empty_success(self):
        for source in (GH, ASHBY):
            with self.subTest(source=source["type"]), self.assertRaisesRegex(adapters.SourceError, "(?i)(error|failed)"):
                self.fetch(source, lambda r: unread_response(200, json={"jobs": [], "error": "board unavailable"}))

    def test_large_retry_after_fails_instead_of_retrying_early(self):
        attempted = []
        def handler(request):
            attempted.append(request)
            return unread_response(429, headers={"Retry-After": "3600"})
        with self.assertRaisesRegex(adapters.SourceError, "Retry-After"):
            self.fetch(GH, handler)
        self.assertEqual(len(attempted), 1)

    def test_invalid_page_sizes_and_redirects_fail_without_following(self):
        for value in (0, -1, 101, True, "100"):
            with self.subTest(value=value), self.assertRaises(adapters.SourceError):
                self.fetch(dict(LEVER, page_size=value), lambda r: self.fail("Invalid size must not reach HTTP"))
        calls = []
        def redirect(request):
            calls.append(request)
            return unread_response(302, headers={"Location": "http://127.0.0.1/private"})
        with self.assertRaisesRegex(adapters.SourceError, "302"):
            self.fetch(GH, redirect)
        self.assertEqual(len(calls), 1)

    def test_response_size_cap_is_explicit_and_timeouts_are_finite(self):
        with patch.object(adapters, "MAX_RESPONSE_BYTES", 10), self.assertRaisesRegex(adapters.SourceError, "(?i)(cap|truncat)"):
            self.fetch(ASHBY, lambda r: unread_response(200, json={"jobs": [ashby_job()]}))
        _, calls = self.fetch(ASHBY, lambda r: unread_response(200, json={"jobs": []}))
        self.assertTrue(all(isinstance(v, (int, float)) and 0 < v <= 30 for v in calls[0].extensions["timeout"].values()))

    def test_generic_remote_does_not_infer_canada(self):
        job = ashby_job(location="Remote - North America", address=None, secondaryLocations=[])
        result, _ = self.fetch(ASHBY, lambda r: unread_response(200, json={"jobs": [job]}))
        self.assertNotIn("country_hint", result[0])

    def test_board_slug_can_be_discovered_from_recognized_public_url(self):
        source = {k: v for k, v in GH.items() if k != "board"}
        _, calls = self.fetch(source, lambda r: unread_response(200, json={"jobs": []}))
        self.assertEqual(calls[0].url.path, "/v1/boards/fixture/jobs")

    def test_html_cleanup_does_not_include_script_or_style(self):
        job = ashby_job(descriptionHtml="<h2>Requirements</h2><p>Use C<strong>++</strong>.</p><script>fake expiry 2030</script><style>.x{}</style><ul><li>A</li><li>B</li></ul>")
        result, _ = self.fetch(ASHBY, lambda r: unread_response(200, json={"jobs": [job]}))
        self.assertIn("C++", result[0]["description"])
        self.assertNotIn("fake expiry", result[0]["description"])
        self.assertNotIn(".x", result[0]["description"])
        self.assertIn("A\nB", result[0]["description"])


class BundledRegistryTests(unittest.TestCase):
    """Offline checks of the board lists that ship inside the package (no network)."""
    @classmethod
    def setUpClass(cls):
        boards = Path(__file__).resolve().parents[1] / "career_radar" / "boards"
        cls.sources = json.loads((boards / "sources.json").read_text())
        seed = json.loads((boards / "seed_discovered_sources.json").read_text())
        cls.seed = seed.get("sources", seed) if isinstance(seed, dict) else seed

    def assert_public_https(self, url):
        import ipaddress
        from urllib.parse import urlparse
        parsed = urlparse(url)
        self.assertEqual(parsed.scheme, "https")
        self.assertIn(".", parsed.hostname or "")
        with self.assertRaises(ValueError):
            ipaddress.ip_address(parsed.hostname)

    def test_builtin_boards_are_unique_supported_and_consistent(self):
        from career_radar import registry
        self.assertGreaterEqual(len(self.sources), 50)
        self.assertEqual(len(self.sources), len({s["id"] for s in self.sources}))
        for source in self.sources:
            with self.subTest(source=source["id"]):
                self.assertIn(source["type"], registry.BOARD_LABELS)
                self.assertTrue(source["name"].strip())
                self.assert_public_https(source["url"])
                self.assertEqual(source["api_url"], adapters._endpoint(source))

    def test_seed_boards_are_clean_unique_and_never_shadow_builtin_ids(self):
        from career_radar import registry
        self.assertGreaterEqual(len(self.seed), 100)
        ids = [s["id"] for s in self.seed]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertFalse(set(ids) & {s["id"] for s in self.sources})
        for source in self.seed:
            with self.subTest(source=source["id"]):
                self.assertIn(source["type"], registry.BOARD_LABELS)
                self.assert_public_https(source["url"])
                self.assertEqual(source.get("lead_origins"), ["seed"])
                self.assertFalse({"last_recheck", "recheck_failures"} & set(source))


@pytest.mark.parametrize("config,make_job,posted_at", [
    pytest.param(GH, gh_job, "2026-10-01T09:10:00-04:00", id="greenhouse"),
    pytest.param(LEVER, lever_job, "2026-01-01T00:00:00+00:00", id="lever"),
    pytest.param(ASHBY, ashby_job, "2026-10-01T12:00:00+00:00", id="ashby"),
])
@pytest.mark.parametrize("field", ["application_deadline", "applicationDeadline", "validThrough"])
@pytest.mark.parametrize("published,expected,uncertainty", [
    ("2026-11-06T21:14:59-00:00", "2026-11-06", "unknown_timezone"),
    ("2026-11-06T21:14:59.1234567Z", "2026-11-06", "invalid_clock"),
    ("2026-11-06T21:14:59.654321Z", "2026-11-06T21:14:59.654321+00:00", None),
    ("2026-11-06T21:14:59.654321-03:30", "2026-11-06T21:14:59.654321-03:30", None),
    ("2026-11-06", "2026-11-06", None),
])
def test_structured_deadline_preserves_raw_clock_until_shared_validation(config, make_job, posted_at,
                                                                       field, published, expected, uncertainty):
    from career_radar.deadlines import parse_published_deadline

    item = make_job(application_deadline=None)
    item[field] = published
    payload = [item] if config["type"] == "lever" else {"jobs": [item], "meta": {"total": 1}}
    rows, calls = AdapterTests().fetch(config, lambda request: unread_response(200, json=payload))
    assert len(rows) == 1 and len(calls) == 1
    row = json.loads(json.dumps(rows[0]))
    assert row["deadline"] == published  # Never normalize -00:00 or truncate fractions here.
    assert row["deadline_evidence"] == f"{field}: {published}"
    assert row["deadline_source_url"] == row["url"]
    assert row["posted_at"] == posted_at  # Posting-date normalization is a separate contract.
    parsed = parse_published_deadline("Deadline: " + row["deadline"])
    assert parsed is not None
    assert (parsed.value, parsed.uncertainty) == (expected, uncertainty)


ATS_ENDPOINT = "https://boards-api.greenhouse.io/v1/boards/fixture/jobs"


@pytest.mark.parametrize("declared", [False, True])
def test_streamed_cap_stops_before_remainder(monkeypatch, declared):
    stream = ProbeStream([b"a" * 10, b"b" * 10, b"never-read"])
    headers = {"Content-Length": "1000"} if declared else {}
    monkeypatch.setattr(adapters, "MAX_RESPONSE_BYTES", 16)
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, headers=headers, stream=stream))) as client:
        with pytest.raises(adapters.SourceError, match="safety cap"):
            adapters._request(client, ATS_ENDPOINT)
    assert stream.consumed == ([] if declared else [0, 1]) and stream.closed


@pytest.mark.parametrize("status", [302, 401, 429, 503])
def test_redirect_error_retry_bodies_are_closed_without_draining(monkeypatch, status):
    streams = []
    sleeps = []
    monkeypatch.setattr(adapters.time, "sleep", sleeps.append)

    def handler(request):
        assert request.method == "GET" and str(request.url) == ATS_ENDPOINT
        stream = ProbeStream([b"never-read"])
        streams.append(stream)
        return httpx.Response(status, stream=stream,
                              headers={"Location": "https://elsewhere.example/", "Retry-After": "0.25"})

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        with pytest.raises(adapters.SourceError, match=str(status)):
            adapters._request(client, ATS_ENDPOINT)
    assert all(not stream.consumed and stream.closed for stream in streams)
    assert len(streams) == (adapters.MAX_ATTEMPTS if status in {429, 503} else 1)
    assert sleeps == ([0.25, 0.25] if status in {429, 503} else [])


@pytest.mark.parametrize("coding", ["identity", "gzip", "deflate", "raw-deflate"])
def test_validated_json_retains_charset_links_and_metadata(coding):
    text = json.dumps({"jobs": [], "name": "café", "html": "<p>café</p>"}, ensure_ascii=False)
    wire = compressed(text.encode("utf-16"), coding)
    stream = ProbeStream([wire[i:i + 1] for i in range(len(wire))])
    headers = coding_headers(coding) | {"Content-Length": str(len(wire)),
        "Content-Type": "application/json; charset=utf-16", "Link": '</v1/boards/fixture/jobs?page=2>; rel="next"'}
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(
            200, headers=headers, stream=stream, extensions={"http_version": b"HTTP/2"}))) as client:
        value, response = adapters._request(client, ATS_ENDPOINT, {"content": "true"})
    assert value == json.loads(text) and response.text == text
    assert response.content == text.encode("utf-16") and response.headers["Content-Length"] == str(len(wire))
    assert response.http_version == "HTTP/2" and response.url.params["content"] == "true"
    assert adapters._next_link(response, ATS_ENDPOINT) == ATS_ENDPOINT + "?page=2"
    assert stream.closed and response.elapsed.total_seconds() >= 0


def test_ats_slow_compressed_header_exhausts_deadline_before_next_chunk(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(adapters.time, "monotonic", lambda: clock[0])
    wire = compressed(b"{}", "gzip")
    stream = ProbeStream([wire[:1], wire[1:]], on_chunk=lambda i: clock.__setitem__(0, 31.0))
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(
            200, headers=coding_headers("gzip"), stream=stream))) as client:
        with pytest.raises(adapters.SourceError, match="deadline|time"):
            adapters._request(client, ATS_ENDPOINT)
    assert stream.consumed == [0] and stream.closed


def test_six_request_slots_cover_entire_stream_lifetime():
    import threading
    from concurrent.futures import ThreadPoolExecutor
    gate, six = threading.Event(), threading.Event()
    lock = threading.Lock()
    active, peaks = [], []

    class HeldStream(ProbeStream):
        def __iter__(self):
            assert gate.wait(3), "fixture body gate timed out"
            yield b"{}"

        def close(self):
            if not self.closed:
                with lock:
                    active.remove(self)
                super().close()

    def handler(request):
        stream = HeldStream([])
        with lock:
            active.append(stream)
            peaks.append(len(active))
            if len(active) == 6:
                six.set()
        return httpx.Response(200, stream=stream)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client, ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(adapters._request, client, ATS_ENDPOINT) for _ in range(8)]
        try:
            assert six.wait(3)
            assert max(peaks) == 6
        finally:
            gate.set()
        assert all(f.result()[0] == {} for f in futures)
    assert not active and max(peaks) == 6


if __name__ == "__main__":
    unittest.main()
