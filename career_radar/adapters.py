"""Public, read-only direct-employer ATS collectors for Career Radar.

Contract: fetch_source(source) -> list[dict]; errors are never successful empty
boards. The caller owns filtering, storage, scheduling and source health.

Pagination follows the *public* APIs, not invented endpoints:
- Greenhouse Job Board: all published jobs with content=true; verify meta.total.
- Lever Postings: actual skip/limit requests until a short/empty terminal page.
- Ashby Posting API: all published jobs in one snapshot (NOT the authenticated
  jobPosting.list API). Unlisted/direct-link-only postings are not indexed.
Explicit same-endpoint HTTP next links are followed; unknown continuation flags,
repeated pages, inconsistent totals and safety caps raise SourceError.

References:
https://developers.greenhouse.io/job-board.html
https://github.com/lever/postings-api
https://developers.ashbyhq.com/docs/public-job-posting-api

Only public HTTPS API hosts are requested. No credentials, application POSTs,
login/challenge bypass, DB writes or internal thread pools. A process-wide
semaphore bounds concurrent HTTP requests to six, including multiple callers.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
import re
import threading
import time
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from .bounded_http import ResponseBoundError, read_bounded
from .deadlines import parse_structured_deadline


USER_AGENT = "CareerRadar/2.0 (+https://github.com/QuestyFella/career-radar; read-only public job-board collector)"
TIMEOUT = httpx.Timeout(25.0, connect=8.0, pool=10.0)
MAX_RESPONSE_BYTES = 40 * 1024 * 1024
MAX_JOBS = 50_000
MAX_PAGES = 200
MAX_ATTEMPTS = 3
_REQUEST_SLOTS = threading.BoundedSemaphore(6)
_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_BLOCK_TAGS = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "tr", "blockquote", "hr"}


class SourceError(RuntimeError):
    """A source failed, changed schema, or could not be collected completely."""


class _PlainHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in {"script", "style"}:
            self.hidden += 1
        elif not self.hidden and tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        elif not self.hidden and tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def _text(value: Any, *, html: bool = False) -> str:
    if not isinstance(value, str):
        return ""
    if html:
        # Decode encoded *markup* only, stopping once HTML is exposed. Decoding
        # every entity first would turn literal &lt;T&gt; into a discarded tag.
        tags = r"(?:p|div|span|strong|em|b|i|u|a|ul|ol|li|br|h[1-6]|table|tr|td|th|section|article|blockquote|script|style)"
        for _ in range(3):
            if re.search(r"</?" + tags + r"\b", value, re.I) or not re.search(r"&(?:amp;)*lt;/?" + tags + r"\b", value, re.I):
                break
            value = unescape(value)
        parser = _PlainHTML()
        parser.feed(value)
        parser.close()
        value = "".join(parser.parts)
    else:
        value = unescape(value)
    return "\n".join(line for part in value.splitlines() if (line := " ".join(part.split())))


def _iso(value: Any, *, milliseconds: bool = False) -> str | None:
    """Normalize supplied dates only. Never substitute now or an update date."""
    try:
        if milliseconds and isinstance(value, (int, float)) and not isinstance(value, bool):
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()
        if not isinstance(value, str) or not value.strip():
            return None
        value = value.strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return date.fromisoformat(value).isoformat()
        # Reject informal dates rather than guessing a year, timezone or locale.
        if not re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", value):
            return None
        return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def _public_url(value: Any) -> str:
    if not isinstance(value, str):
        raise SourceError("Missing public listing URL")
    parsed = urlsplit(value)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
        raise SourceError("Invalid public listing URL")
    return value


def _endpoint(source: dict) -> str:
    kind = source.get("type")
    if kind not in {"greenhouse", "lever", "ashby"}:
        raise SourceError(f"Unsupported ATS type: {kind!r}")
    if not source.get("id") or not source.get("name"):
        raise SourceError("Source requires stable id and name")
    board = source.get("board") or source.get("board_token")
    region = source.get("region", "global")
    if region not in {"global", "eu"} or (region == "eu" and kind != "lever"):
        raise SourceError("Unsupported ATS region")
    if board is None:
        public = urlsplit(source.get("url", ""))
        hosts = {"greenhouse": {"boards.greenhouse.io", "job-boards.greenhouse.io"}, "lever": {"jobs.lever.co", "jobs.eu.lever.co"}, "ashby": {"jobs.ashbyhq.com"}}
        if public.scheme != "https" or public.hostname not in hosts[kind] or public.username:
            raise SourceError("A board slug or recognized public board URL is required")
        board = public.path.strip("/").split("/")[0]
        if public.hostname == "jobs.eu.lever.co":
            region = "eu"
    if not isinstance(board, str) or not _SLUG.fullmatch(board) or ".." in board:
        raise SourceError("Invalid ATS board slug")
    endpoint = {
        "greenhouse": f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs",
        "lever": f"https://api.{'eu.' if region == 'eu' else ''}lever.co/v0/postings/{board}",
        "ashby": f"https://api.ashbyhq.com/posting-api/job-board/{board}",
    }[kind]
    if source.get("api_url") and source["api_url"].rstrip("/") != endpoint:
        raise SourceError("api_url must match the recognized HTTPS endpoint for this board")
    return endpoint


def _positive_int(value: Any, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise SourceError(f"{name} must be an integer between 1 and {maximum}")
    return value


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    value = response.headers.get("Retry-After")
    delay = 0.5 * 2 ** attempt
    if value:
        try:
            delay = float(value)
        except ValueError:
            try:
                when = parsedate_to_datetime(value)
                delay = (when - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                pass
    if delay > 60:
        raise SourceError(f"HTTP {response.status_code}: Retry-After exceeds bounded retry window; try on a later refresh")
    return max(0.0, delay)


def _request(client: httpx.Client, url: str, params: dict | None = None) -> tuple[Any, httpx.Response]:
    for attempt in range(MAX_ATTEMPTS):
        try:
            with _REQUEST_SLOTS:
                deadline = time.monotonic() + TIMEOUT.read
                with client.stream("GET", url, params=params, timeout=TIMEOUT, auth=None,
                                   follow_redirects=False, headers={"Accept-Encoding": "gzip, deflate"}) as response:
                    if response.status_code in {429, 500, 502, 503, 504} and attempt + 1 < MAX_ATTEMPTS:
                        delay = _retry_delay(response, attempt)
                    elif response.status_code != 200:
                        raise SourceError(f"HTTP {response.status_code} from public ATS endpoint (no login or challenge bypass attempted)")
                    else:
                        wire = read_bounded(response, max_bytes=MAX_RESPONSE_BYTES, deadline=deadline)
                        result = httpx.Response(response.status_code, headers=response.headers, content=wire,
                                                request=response.request, extensions=response.extensions,
                                                default_encoding=response.default_encoding)
                        result.elapsed = response.elapsed
                        try:
                            data = result.json()
                        except ValueError as exc:
                            raise SourceError("Expected JSON from public ATS; received HTML/challenge or invalid JSON") from exc
                        if time.monotonic() >= deadline:
                            raise SourceError("Response deadline exceeded")
                        return data, result
        except ResponseBoundError as exc:
            raise SourceError(f"Response safety cap: {exc}") from exc
        except httpx.RequestError as exc:
            if attempt + 1 == MAX_ATTEMPTS:
                reason = "HTTP request timeout" if isinstance(exc, httpx.TimeoutException) else f"HTTP transport failure ({type(exc).__name__})"
                raise SourceError(reason) from exc
            delay = 0.5 * 2 ** attempt
        # Streaming contexts close retries/errors without draining their bodies;
        # release the concurrency slot before the bounded retry delay.
        time.sleep(delay)
    raise SourceError("HTTP retry limit exceeded")  # defensive, loop always returns/raises


def _identity(job: dict, kind: str) -> str:
    value = job.get("id")
    if value is None and kind == "ashby":
        value = urlsplit(_public_url(job.get("jobUrl") or job.get("applyUrl"))).path.rstrip("/").rsplit("/", 1)[-1]
        if value in {"apply", "application"}:
            value = urlsplit(job.get("applyUrl", "")).path.rstrip("/").rsplit("/", 2)[-2]
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).strip():
        raise SourceError("Job is missing a valid stable id")
    return str(value)


def _next_link(response: httpx.Response, endpoint: str) -> str | None:
    value = response.links.get("next", {}).get("url")
    if not value:
        return None
    url = urljoin(str(response.url), value)
    parsed, expected = urlsplit(url), urlsplit(endpoint)
    if (parsed.scheme, parsed.netloc, parsed.path) != (expected.scheme, expected.netloc, expected.path) or parsed.fragment:
        raise SourceError("Unsafe/foreign pagination next link; only the same public ATS endpoint is allowed")
    return url


def _snapshot(client: httpx.Client, endpoint: str, kind: str, max_pages: int) -> list[dict]:
    params = {"content": "true"} if kind == "greenhouse" else {"includeCompensation": "true"}
    url = endpoint
    seen_urls: set[str] = set()
    jobs: dict[str, dict] = {}
    declared_total: int | None = None
    for _ in range(max_pages):
        data, response = _request(client, url, params)
        request_url = str(response.url)
        if request_url in seen_urls:
            raise SourceError("Repeated pagination URL; collection did not advance")
        seen_urls.add(request_url)
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            raise SourceError(f"Invalid {kind} schema: expected object with a jobs array")
        if data.get("error") or data.get("errors") or data.get("success") is False:
            raise SourceError(f"{kind} API reported an error, not a successful empty board")
        meta = data.get("meta") or {}
        if not isinstance(meta, dict):
            raise SourceError("Invalid pagination metadata")
        total = meta.get("total", data.get("total", data.get("totalCount")))
        if total is not None:
            if isinstance(total, bool) or not isinstance(total, int) or total < 0:
                raise SourceError("Invalid declared job total")
            if declared_total is not None and declared_total != total:
                raise SourceError("Declared job total changed during pagination; retry snapshot")
            declared_total = total
        previous_count = len(jobs)
        for job in data["jobs"]:
            if not isinstance(job, dict):
                raise SourceError("Invalid job item: expected object")
            jobs[_identity(job, kind)] = job
        if len(jobs) > MAX_JOBS:
            raise SourceError(f"Collection exceeds {MAX_JOBS}-job safety cap; possible truncation")
        next_url = _next_link(response, endpoint)
        continuation = any(data.get(key) for key in ("hasMore", "has_more", "moreDataAvailable", "nextCursor", "next_cursor", "nextPage", "next"))
        continuation = continuation or any(meta.get(key) for key in ("hasMore", "has_more", "nextCursor", "next"))
        if continuation and not next_url:
            raise SourceError("Unsupported pagination continuation on full-snapshot public API; refusing truncation")
        if not next_url:
            if declared_total is not None and declared_total != len(jobs):
                raise SourceError(f"Truncated/inconsistent snapshot: declared total {declared_total}, received {len(jobs)} unique jobs")
            return list(jobs.values())
        if len(jobs) == previous_count:
            raise SourceError("Pagination returned a repeated/empty page without advancing")
        url, params = next_url, None
    raise SourceError(f"Pagination hit {max_pages}-page safety cap; refusing truncated results")


def _lever_pages(client: httpx.Client, endpoint: str, page_size: int, max_pages: int) -> list[dict]:
    jobs: dict[str, dict] = {}
    offset = 0
    for _ in range(max_pages):
        data, _response = _request(client, endpoint, {"mode": "json", "skip": offset, "limit": page_size})
        if not isinstance(data, list):
            raise SourceError("Invalid Lever schema: expected a job array")
        if len(data) > page_size:
            raise SourceError("Lever ignored pagination limit; refusing possibly truncated results")
        previous_count = len(jobs)
        for job in data:
            if not isinstance(job, dict):
                raise SourceError("Invalid Lever job item: expected object")
            identity = _identity(job, "lever")
            if identity in jobs:
                raise SourceError("Lever pagination returned duplicate/overlapping ids; inconsistent snapshot, retry later")
            jobs[identity] = job
        if data and len(jobs) == previous_count:
            raise SourceError("Lever pagination repeated a page and did not advance")
        if len(jobs) > MAX_JOBS:
            raise SourceError(f"Collection exceeds {MAX_JOBS}-job safety cap; possible truncation")
        if len(data) < page_size:
            return list(jobs.values())
        offset += len(data)
    raise SourceError(f"Lever pagination hit {max_pages}-page safety cap; refusing truncated results")


def _join_locations(values: list[Any]) -> str:
    return "; ".join(dict.fromkeys(text for value in values if (text := _text(value))))


def _address(address: Any) -> tuple[str, str]:
    if not isinstance(address, dict):
        return "", ""
    address = address.get("postalAddress", address)
    if not isinstance(address, dict):
        return "", ""
    country = _text(address.get("addressCountry"))
    label = {"CA": "Canada", "CAN": "Canada", "US": "United States", "USA": "United States"}.get(country.upper(), country)
    return ", ".join(value for field in ("addressLocality", "addressRegion") if (value := _text(address.get(field)))) + (f", {label}" if label else ""), country


def _base(source: dict, job: dict, title: Any, location: str, url: Any, description: str, employment: Any = None) -> dict:
    title = _text(title)
    if not title:
        raise SourceError("Job is missing a title")
    if not description:
        raise SourceError("Job is missing a full description")
    row = {
        "source_id": source["id"], "source": source["name"], "source_type": source["type"],
        "source_job_id": _identity(job, source["type"]), "company": source.get("company") or source["name"],
        "title": title, "location": location, "url": _public_url(url), "description": description,
        "posted_at": None, "deadline": None, "deadline_evidence": None, "deadline_source_url": None,
        "employment_type": _text(employment) or None, "verification": "direct", "is_closed": False,
    }
    if re.search(r"\b(?:intern(?:ship)?|co[ -]?op)\b", title, re.I) or re.search(r"\bintern(?:ship)?\b", _text(employment), re.I):
        row["level_hint"] = "internship"
    elif re.search(r"\b(?:new[ -]?grad(?:uate)?|university grad(?:uate)?|recent grad(?:uate)?)\b", title, re.I):
        row["level_hint"] = "new_grad"
    elif re.search(r"\b(?:junior|jr\.?|entry[ -]?level)\b", title, re.I):
        row["level_hint"] = "entry_level"
    if re.search(r"\bCanada\b", location, re.I):
        row["country_hint"] = "CA"
    # Structured fields only here. Free-text evidence/deadlines belong to core.
    for key in ("application_deadline", "applicationDeadline", "validThrough"):
        deadline = job.get(key)
        if parse_structured_deadline(deadline):
            # Preserve the raw clock, including shared-parser uncertainty.
            # Posting-date normalization remains a separate contract.
            row.update(deadline=deadline.strip(), deadline_evidence=f"{key}: {job[key]}", deadline_source_url=row["url"])
            break
    return row


def _greenhouse(source: dict, job: dict, client: httpx.Client, endpoint: str) -> dict:
    if not _text(job.get("content"), html=True):
        identity = _identity(job, "greenhouse")
        if not re.fullmatch(r"\d+", identity):
            raise SourceError("Cannot retrieve missing description for a nonnumeric Greenhouse id")
        detail, _ = _request(client, f"{endpoint}/{identity}")
        if not isinstance(detail, dict) or _identity(detail, "greenhouse") != identity:
            raise SourceError("Invalid Greenhouse detail response")
        job = {**job, **detail}
    location = job.get("location") or {}
    if not isinstance(location, dict):
        raise SourceError("Invalid Greenhouse location object")
    metadata = job.get("metadata") or []
    employment = next((item.get("value") for item in metadata if isinstance(item, dict) and str(item.get("name", "")).lower() in {"employment type", "job type", "commitment"}), None)
    row = _base(source, job, job.get("title"), _text(location.get("name")), job.get("absolute_url"), _text(job.get("content"), html=True), employment)
    row["posted_at"] = _iso(job.get("first_published"))
    row["updated_at"] = _iso(job.get("updated_at"))
    row["posted_at_source"] = "first_published" if row["posted_at"] else None
    row["departments"] = [item.get("name") for item in job.get("departments", []) if isinstance(item, dict) and item.get("name")]
    row["offices"] = job.get("offices") or []
    return row


def _lever(source: dict, job: dict) -> dict:
    categories = job.get("categories") or {}
    if not isinstance(categories, dict):
        raise SourceError("Invalid Lever categories object")
    locations = categories.get("allLocations") or []
    if not isinstance(locations, list):
        raise SourceError("Invalid Lever allLocations array")
    parts = [_text(job.get("descriptionPlain")) or _text(job.get("description"), html=True)]
    if not parts[0]:
        parts = [_text(job.get("openingPlain")) or _text(job.get("opening"), html=True), _text(job.get("descriptionBodyPlain")) or _text(job.get("descriptionBody"), html=True)]
    lists = job.get("lists") or []
    if not isinstance(lists, list):
        raise SourceError("Invalid Lever description lists")
    for section in lists:
        if not isinstance(section, dict):
            raise SourceError("Invalid Lever description section")
        parts.extend([_text(section.get("text")), _text(section.get("content"), html=True)])
    parts.extend([_text(job.get("additionalPlain")) or _text(job.get("additional"), html=True), _text(job.get("salaryDescriptionPlain")) or _text(job.get("salaryDescription"), html=True)])
    row = _base(source, job, job.get("text"), _join_locations([categories.get("location"), *locations]), job.get("hostedUrl") or job.get("applyUrl"), "\n\n".join(part for part in parts if part), categories.get("commitment"))
    row["posted_at"] = _iso(job.get("createdAt"), milliseconds=True)
    row["posted_at_source"] = "createdAt" if row["posted_at"] else None
    if _text(job.get("country")).upper() in {"CA", "CAN", "CANADA"}:
        row["country_hint"] = "CA"
    row["workplace_type"] = job.get("workplaceType")
    row["department"] = categories.get("department")
    row["team"] = categories.get("team")
    if job.get("salaryRange"):
        row["salary_range"] = job["salaryRange"]
    return row


def _ashby(source: dict, job: dict) -> dict:
    primary_address, country = _address(job.get("address"))
    locations = [job.get("location"), primary_address.lstrip(", ")]
    countries = [country]
    secondary = job.get("secondaryLocations") or []
    if not isinstance(secondary, list):
        raise SourceError("Invalid Ashby secondaryLocations array")
    for place in secondary:
        if not isinstance(place, dict):
            raise SourceError("Invalid Ashby secondary location")
        address, country = _address(place.get("address"))
        locations.extend([place.get("location"), address.lstrip(", ")])
        countries.append(country)
    row = _base(source, job, job.get("title"), _join_locations(locations), job.get("jobUrl") or job.get("applyUrl"), _text(job.get("descriptionHtml"), html=True) or _text(job.get("descriptionPlain")), job.get("employmentType"))
    row["posted_at"] = _iso(job.get("publishedAt"))
    row["posted_at_source"] = "publishedAt (last published)" if row["posted_at"] else None
    if any(country.upper() in {"CA", "CAN", "CANADA"} for country in countries):
        row["country_hint"] = "CA"
    row["workplace_type"] = job.get("workplaceType")
    row["department"] = job.get("department")
    row["team"] = job.get("team")
    compensation = job.get("compensation")
    if isinstance(compensation, dict):
        row["salary"] = compensation.get("scrapeableCompensationSalarySummary") or compensation.get("compensationTierSummary")
    return row


def fetch_source(source: dict) -> list[dict]:
    """Collect one complete public employer board, or raise SourceError.

    Required source keys: id, name, type; board (or recognized public url).
    Optional: company, region='eu' (Lever), api_url (exact canonical URL),
    page_size=100 (Lever, 1..100), max_pages=200 (1..2000, explicit failure).
    Returns all locations/seniorities; regional eligibility is the caller's job.
    """
    if not isinstance(source, dict):
        raise SourceError("Source must be an object")
    try:
        endpoint = _endpoint(source)
        kind = source["type"]
        max_pages = _positive_int(source.get("max_pages", MAX_PAGES), "max_pages", 2000)
        with httpx.Client(timeout=TIMEOUT, follow_redirects=False, trust_env=False, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}, limits=httpx.Limits(max_connections=1, max_keepalive_connections=1)) as client:
            if kind == "lever":
                page_size = _positive_int(source.get("page_size", 100), "page_size", 100)
                jobs = _lever_pages(client, endpoint, page_size, max_pages)
                return [_lever(source, job) for job in jobs]
            jobs = _snapshot(client, endpoint, kind, max_pages)
            if kind == "greenhouse":
                return [_greenhouse(source, job, client, endpoint) for job in jobs]
            return [_ashby(source, job) for job in jobs if job.get("isListed") is not False]
    except SourceError as exc:
        raise SourceError(f"{source.get('id', 'unknown-source')}: {exc}") from exc
    except (TypeError, ValueError, KeyError) as exc:
        raise SourceError(f"{source.get('id', 'unknown-source')}: invalid ATS data ({type(exc).__name__})") from exc
