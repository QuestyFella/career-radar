"""Public, unauthenticated extra sources for Career Radar.

Contract: fetch_source(source) -> list[dict]. No DB writes, cookies, accounts,
API keys, browser impersonation, challenge bypass, or nested request pools.
The caller owns a pool of at most six workers. LinkedIn is additionally paced
across those workers. Registry coverage/maintenance notes are intentional.

IMPORTANT: SourceTruncated and late SourceError instances expose partial_jobs
(and its alias jobs). An orchestrator may ingest these real records while
marking the source PARTIAL/FAILED; it must not call that a complete refresh.
A cap never silently returns an apparently complete list.

Curated dates are not employer verification. Simplify's discovery timestamp
is index_posted_at, NOT posted_at. Relative Workday/LinkedIn dates are never
converted to invented posting dates. Deadlines require explicit source text
or JobPosting.validThrough; internship terms are never application deadlines.
"""
from __future__ import annotations

import html
import ipaddress
import json
import re
import socket
import threading
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup
from .url_safety import UnsafeURL, normalize_public_url, is_public_ip
from .deadlines import explicit_deadline as _shared_explicit_deadline, parse_structured_deadline
from .bounded_http import ResponseBoundError, read_bounded

USER_AGENT = "CareerRadar/2.0 (+https://github.com/QuestyFella/career-radar; read-only public job-board collector)"
MAX_RESPONSE_BYTES = 35_000_000
_DNS_TIMEOUT = 5.0
_DNS_TTL = 60.0
_DNS_CACHE_MAX = 256
_DNS_CACHE: dict[tuple[str, int], tuple[float, tuple[str, ...]]] = {}
_DNS_LOCK = threading.Lock()
_DNS_SLOTS = threading.BoundedSemaphore(6)
_LINKEDIN_LOCK = threading.Lock()
_LINKEDIN_LAST_REQUEST = 0.0
CANADA_RE = re.compile(
    r"(?i:\bCanada\b|\b(?:Ontario(?!\s*,\s*(?:CA\b|California|United States|USA))|Quebec|Québec|Alberta|British Columbia|Manitoba|"
    r"Saskatchewan|Nova Scotia|New Brunswick|Newfoundland|Prince Edward Island|"
    r"Yukon|Nunavut|Northwest Territories)\b)|[,\(]\s*(?:ON|QC|BC|AB|MB|SK|NS|NB|NL|PE|NT|NU|YT)\b"
)


class SourceError(RuntimeError):
    """Visible source failure, optionally retaining already fetched raw rows."""
    status = "failed"

    def __init__(self, message: str, partial_jobs: list[dict] | None = None):
        super().__init__(message)
        self.partial_jobs = partial_jobs or []
        self.jobs = self.partial_jobs
        self.raw_count = len(self.partial_jobs)


class SourceTruncated(SourceError):
    """An explicit bounded sample, incomplete page, or pagination mismatch."""
    status = "partial"


def _is_public_ip(address) -> bool:
    return is_public_ip(address)


def _safe_url(value: Any) -> str:
    """Shared lexical policy; fetching still validates/pins every DNS address."""
    try:
        return normalize_public_url(value)
    except UnsafeURL as exc:
        raise SourceError(str(exc)) from exc


def _public_addresses(host: str, port: int) -> tuple[str, ...]:
    """Bounded resolver/cache; reject a host if ANY answer is non-public."""
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not _is_public_ip(literal):
            raise SourceError("Private/local IP destination is not permitted")
        return (str(literal),)
    key = (host, port)
    with _DNS_LOCK:
        cached = _DNS_CACHE.get(key)
        if cached and time.monotonic() - cached[0] < _DNS_TTL:
            return cached[1]
        _DNS_CACHE.pop(key, None)
    # getaddrinfo has no portable timeout. At most six daemon resolvers may be
    # outstanding, including timed-out calls; no unbounded executor queue.
    if not _DNS_SLOTS.acquire(blocking=False):
        raise SourceError("DNS resolver capacity exhausted; public request not attempted")
    done, answers, errors = threading.Event(), [], []
    resolver = socket.getaddrinfo

    def resolve():
        try:
            answers.extend(resolver(host, port, type=socket.SOCK_STREAM))
        except Exception as exc:
            errors.append(exc)
        finally:
            _DNS_SLOTS.release()
            done.set()

    worker = threading.Thread(target=resolve, name="public-job-dns", daemon=True)
    try:
        worker.start()
    except RuntimeError as exc:
        _DNS_SLOTS.release()
        raise SourceError(f"DNS resolver could not start: {host}") from exc
    if not done.wait(_DNS_TIMEOUT):
        raise SourceError(f"DNS lookup timed out: {host}")
    if errors:
        raise SourceError(f"DNS lookup failed: {host}: {errors[0]}") from errors[0]
    if not answers or len(answers) > 64:
        raise SourceError(f"DNS returned no addresses or too many answers: {host}")
    addresses = []
    for family, _, _, _, sockaddr in answers:
        try:
            address = ipaddress.ip_address(sockaddr[0])
        except ValueError as exc:
            raise SourceError(f"Invalid DNS address for {host}") from exc
        if family not in {socket.AF_INET, socket.AF_INET6} or not _is_public_ip(address):
            raise SourceError(f"DNS destination is private/local, not public: {host}")
        addresses.append(str(address))
    result = tuple(dict.fromkeys(addresses))
    with _DNS_LOCK:
        _DNS_CACHE[key] = (time.monotonic(), result)
        while len(_DNS_CACHE) > _DNS_CACHE_MAX:
            _DNS_CACHE.pop(next(iter(_DNS_CACHE)))
    return result


class _PublicTransport(httpx.HTTPTransport):
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        _safe_url(str(request.url))
        host = request.url.raw_host.decode("ascii")
        port = request.url.port or (443 if request.url.scheme == "https" else 80)
        addresses = _public_addresses(host, port)
        # Pin the connection to checked numeric IPs, eliminating a second DNS
        # lookup/rebinding race. Host and TLS SNI/certificate identity stay original.
        headers = request.headers.copy()
        headers["Host"] = request.url.netloc.decode("ascii")
        for index, address in enumerate(addresses[:4]):
            pinned = httpx.Request(request.method, request.url.copy_with(host=address),
                                   headers=headers, stream=request.stream,
                                   extensions={**request.extensions, "sni_hostname": host})
            try:
                return super().handle_request(pinned)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                if index + 1 == min(len(addresses), 4):
                    raise
        raise SourceError(f"No public destination available: {host}")


def _pace(url: str) -> None:
    global _LINKEDIN_LAST_REQUEST
    host = urlsplit(url).hostname or ""
    if host == "linkedin.com" or host.endswith(".linkedin.com"):
        with _LINKEDIN_LOCK:
            delay = 1.0 - (time.monotonic() - _LINKEDIN_LAST_REQUEST)
            if delay > 0:
                time.sleep(delay)
            _LINKEDIN_LAST_REQUEST = time.monotonic()


def _bounded_response_body(response: httpx.Response, url: str, *, deadline: float) -> bytes:
    """Translate the shared unread-wire budget into this source's error type."""
    try:
        return read_bounded(response, max_bytes=MAX_RESPONSE_BYTES, deadline=deadline)
    except ResponseBoundError as exc:
        raise SourceError(f"{exc}: {url}") from exc


def _request(method: str, url: str, **kwargs) -> httpx.Response:
    """A new stateless, size-bounded stream; redirects never carry cookies."""
    deadline = time.monotonic() + 30.0
    url = _safe_url(url)
    for _ in range(4):
        if time.monotonic() >= deadline:
            raise SourceError("Response deadline exceeded")
        _pace(url)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SourceError("Response deadline exceeded")
        try:
            # Fresh clients prevent redirect cookies; disabling environment
            # proxies ensures no alternate resolver/route bypasses IP pinning.
            with httpx.Client(transport=_PublicTransport(trust_env=False), trust_env=False) as client:
                with client.stream(
                    method, url, headers={"User-Agent": USER_AGENT, "Accept": "application/json,text/html;q=0.9,*/*;q=0.5",
                                          "Accept-Encoding": "gzip, deflate"},
                    timeout=httpx.Timeout(min(30.0, remaining), connect=min(10.0, remaining)), follow_redirects=False, **kwargs,
                ) as response:
                    if time.monotonic() >= deadline:
                        raise SourceError("Response deadline exceeded")
                    if response.status_code in {301, 302, 303, 307, 308}:
                        target = _safe_url(urljoin(url, response.headers.get("location", "")))
                        if re.search(r"/(?:login|authwall|checkpoint)(?:[/?]|$)", target, re.I):
                            raise SourceError(f"Login/challenge redirect; public access unavailable: {target}")
                        if target == url or not response.headers.get("location"):
                            raise SourceError(f"Invalid redirect from {url}")
                        url = target
                        # The redirect Location already contains any query string.
                        kwargs.pop("params", None)
                        if response.status_code == 303:
                            method = "GET"
                            kwargs.pop("json", None)
                        continue  # Close the redirect without reading its body.
                    if response.status_code >= 400:
                        label = "restricted/rate-limited" if response.status_code in {401, 403, 429, 999} else "HTTP error"
                        raise SourceError(f"{label} {response.status_code}: {url}; no bypass attempted")
                    content = _bounded_response_body(response, url, deadline=deadline)
                    # Only replay a fully bounded/validated wire body. Preserve
                    # HTTPX's normal JSON, charset and Content-Encoding behavior.
                    result = httpx.Response(response.status_code, headers=response.headers, content=content,
                                            request=response.request, extensions=response.extensions,
                                            default_encoding=response.default_encoding)
                    result.elapsed = response.elapsed
            # Include client/transport teardown in the successful-return deadline.
            if time.monotonic() >= deadline:
                raise SourceError("Response deadline exceeded")
            return result
        except httpx.HTTPError as exc:
            raise SourceError(f"Network error fetching {url}: {type(exc).__name__}: {exc}") from exc
    raise SourceError(f"Too many public redirects: {url}")


def _json_response(method: str, url: str, **kwargs) -> Any:
    response = _request(method, url, **kwargs)
    try:
        return response.json()
    except (ValueError, UnicodeError) as exc:
        raise SourceError(f"Invalid JSON / possible login or challenge: {url}") from exc


def _soup(text: str) -> BeautifulSoup:
    soup = BeautifulSoup(text, "html.parser")
    title = soup.title.get_text(" ", strip=True).lower() if soup.title else ""
    if any(term in title for term in ("security verification", "just a moment", "access denied", "captcha")):
        raise SourceError(f"Public page challenge/restriction: {title}")
    if soup.select('form[action*="/checkpoint"], iframe[src*="/challenge"]'):
        raise SourceError("Public page requires login/security challenge; no bypass attempted")
    return soup


def _text(value: Any) -> str:
    if value is None:
        return ""
    value = html.unescape(html.unescape(str(value)))
    soup = BeautifulSoup(value, "html.parser")
    for element in soup.select("script,style,noscript"):
        element.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()


def _iso(value: Any) -> str | None:
    """Only complete calendar dates/times; never relative or yearless text."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}(?:$|T| )", value):
        return None
    try:
        if len(value) == 10:
            date.fromisoformat(value)
        else:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value
    except ValueError:
        return None


def _epoch(value: Any) -> str | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError):
            return None
    return None


def _calendar_date(value: Any) -> str | None:
    if _iso(value):
        return _iso(value)
    value = _text(value)
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%d %B %Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def explicit_deadline(text: str) -> tuple[str | None, str | None]:
    """Use the same published-clock policy as direct ATS/core normalization."""
    return _shared_explicit_deadline(text)


def _hint(title: str) -> str | None:
    if re.search(r"\b(intern(?:ship)?|co[- ]?op|student|stagiaire|stage)\b", title, re.I):
        return "internship"
    if re.search(r"\b(new[- ]?grad(?:uate)?|recent graduate|graduate program|early career)\b", title, re.I):
        return "new_grad"
    if re.search(r"\b(junior|jr\.?|entry[- ]level)\b|\b(?:engineer|developer|analyst)\s+I\b", title, re.I):
        return "entry_level"
    return None


def _raw(source: dict, *, title: Any, company: Any, location: Any, url: Any,
         job_id: Any = None, description: Any = "", verification: str = "public_listing",
         posted_at: Any = None, **extra) -> dict:
    public_url = _safe_url(url)
    title, company, location = _text(title), _text(company), _text(location)
    if not title or not company:
        raise SourceError("Invalid job schema: missing title or company")
    row = {
        "source_id": source["id"], "source": source["name"], "source_type": source["type"],
        "source_job_id": str(job_id if job_id is not None else public_url),
        "company": company, "title": title, "location": location, "url": public_url,
        "description": _text(description), "posted_at": _iso(posted_at),
        "deadline": None, "deadline_evidence": None, "deadline_source_url": None,
        "verification": verification, "is_closed": False,
        "level_hint": _hint(title), "country_hint": "CA" if CANADA_RE.search(location) else None,
    }
    row.update(extra)
    if source.get("coverage_note"):
        row["coverage_note"] = source["coverage_note"]
    return row


def _deadline(row: dict, value: str | None, evidence: str | None, source_url: str | None = None) -> None:
    if evidence and parse_structured_deadline(value):
        row.update(deadline=value, deadline_evidence=evidence, deadline_source_url=source_url or row["url"])


def _finish(source: dict, jobs: list[dict]) -> list[dict]:
    result, seen = [], set()
    for job in jobs:
        if job.get("is_closed") and not source.get("include_closed", False):
            continue
        if source.get("canada_only") and job.get("country_hint") != "CA" and not CANADA_RE.search(job["location"]):
            continue
        key = (job["source_job_id"], job["url"], job["location"])
        if key not in seen:
            seen.add(key)
            result.append(job)
    return result


def _list(data: Any, key: str | None = None) -> list:
    rows = data.get(key) if key and isinstance(data, dict) else data if key is None else None
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise SourceError(f"Invalid source schema: expected list of objects{(' in ' + key) if key else ''}")
    return rows


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SourceError(f"Invalid source schema: {label} must be a non-negative integer")
    return value


def _cap(source: dict, key: str, default: int, maximum: int = 300) -> int:
    value = _integer(source.get(key, default), key)
    if value > maximum or (key in {"max_pages", "page_size"} and value < 1):
        raise SourceError(f"Invalid configured {key}: {value}; maximum {maximum}")
    return value


def _raise_partial(source: dict, exc: SourceError, jobs: list[dict]) -> None:
    raise type(exc)(str(exc), _finish(source, jobs)) from exc


def _fetch_simplify(source: dict) -> list[dict]:
    data = _list(_json_response("GET", source["url"]))
    jobs = []
    for item in data:
        # Hidden openings stay suppressed; explicit closures still reconcile
        # records that were visible on an earlier collection.
        if item.get("is_visible") is False and (item.get("active") is not False or not source.get("include_closed")):
            continue
        if not isinstance(item.get("active"), bool):
            raise SourceError("Simplify schema: active flag missing or not boolean")
        if not item["active"] and not source.get("include_closed"):
            continue
        locations = item.get("locations")
        if not isinstance(locations, list) or not all(isinstance(v, str) for v in locations):
            raise SourceError("Simplify schema: locations must be a string list")
        location = "; ".join(locations)
        if source.get("canada_only") and not CANADA_RE.search(location):
            continue
        terms = item.get("terms", [])
        if not isinstance(terms, list):
            raise SourceError("Simplify schema: terms must be a list")
        notes = ["Curated listing; employer requirements and availability require verification."]
        if terms:
            notes.append("Listed terms: " + ", ".join(map(str, terms)))
        if item.get("degrees"):
            notes.append("Listed degrees: " + ", ".join(map(str, item["degrees"])))
        jobs.append(_raw(source, title=item.get("title"), company=item.get("company_name"), location=location,
                         url=item.get("url"), job_id=item.get("id"), description=" ".join(notes), verification="curated",
                         index_posted_at=_epoch(item.get("date_posted")), index_updated_at=_epoch(item.get("date_updated")),
                         terms=terms, category_hint=item.get("category"), is_closed=not item["active"],
                         level_hint=source.get("level_hint") or _hint(item.get("title", ""))))
    return jobs


def _fetch_frogg(source: dict) -> list[dict]:
    jobs = []
    for item in _list(_json_response("GET", source["url"])):
        status = item.get("status")
        if status not in {"open", "closed"}:
            raise SourceError(f"Canadian internship feed has unknown status: {status!r}")
        if status == "closed" and not source.get("include_closed"):
            continue
        location = item.get("location", "")
        if source.get("canada_only") and not CANADA_RE.search(location):
            continue
        term = item.get("term") or "Unspecified"
        jobs.append(_raw(source, title=item.get("role"), company=item.get("company"), location=location,
                         url=item.get("link"), description=f"Curated listing. Listed work term: {term}. Verify eligibility with employer.",
                         verification="curated", posted_at=item.get("date_posted"),
                         posted_at_evidence=f"Curated feed date_posted: {item.get('date_posted')}",
                         index_found_at=_iso(item.get("date_found")), terms=[term],
                         is_closed=status == "closed", level_hint="internship"))
    return jobs


def _linkedin_cards(text: str, source: dict, first_page: bool) -> list[dict]:
    soup = _soup(text)
    cards = soup.select('[data-entity-urn^="urn:li:jobPosting:"], .base-search-card')
    if not cards:
        if not first_page and not soup.get_text(" ", strip=True):
            return []
        if re.search(r"no (?:matching )?jobs found|no results found", soup.get_text(" ", strip=True), re.I):
            return []
        raise SourceError("LinkedIn public search returned no recognizable cards (empty/login/schema); coverage unavailable")
    jobs, seen = [], set()
    for card in cards:
        link = card if card.name == "a" and card.get("href") else card.select_one("a.base-card__full-link")
        title = card.select_one(".base-search-card__title")
        company = card.select_one(".base-search-card__subtitle")
        location = card.select_one(".job-search-card__location")
        if not all([link, title, company, location]):
            raise SourceError("LinkedIn card schema incomplete")
        urn = card.get("data-entity-urn", "")
        job_id = urn.rsplit(":", 1)[-1]
        if not job_id.isdigit():
            found = re.search(r"-(\d+)(?:\?|$)", link.get("href", ""))
            if not found:
                raise SourceError("LinkedIn job ID missing")
            job_id = found.group(1)
        if job_id in seen:
            continue
        seen.add(job_id)
        parts = urlsplit(link["href"])
        public_url = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        posted = card.select_one("time[datetime]")
        row = _raw(source, title=title.get_text(), company=company.get_text(), location=location.get_text(),
                   url=public_url, job_id=job_id, posted_at=posted.get("datetime") if posted else None,
                   details_fetched=False)
        row["level_hint"] = row["level_hint"] or "entry_level"
        row["level_evidence"] = "LinkedIn public search f_E=1,2 (internship/entry-level); index label, not employer verification"
        jobs.append(row)
    return jobs


def _fetch_linkedin(source: dict) -> list[dict]:
    jobs, seen = [], set()
    max_pages = _cap(source, "max_pages", 3, 10)
    detail_limit = _cap(source, "detail_limit", 0, 50)
    offset, capped = 0, True
    try:
        for page in range(max_pages):
            params = {"keywords": source.get("keywords", "software"), "location": "Canada", "f_E": "1,2",
                      "sortBy": "DD", "start": offset, "f_TPR": source.get("time_filter", "r2592000")}
            response = _request("GET", source["url"], params=params)
            rows = _linkedin_cards(response.text, source, first_page=page == 0)
            if not rows:
                capped = False
                break
            new = [j for j in rows if j["source_job_id"] not in seen]
            if not new:
                raise SourceTruncated("LinkedIn pagination repeated IDs; bounded coverage only", jobs)
            jobs.extend(new)
            seen.update(j["source_job_id"] for j in rows)
            offset += len(rows)  # Guest endpoint may return 10, not 25.
        for job in jobs[:detail_limit]:
            url = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/" + job["source_job_id"]
            soup = _soup(_request("GET", url).text)
            body = soup.select_one(".show-more-less-html__markup")
            if body is None:
                raise SourceError("LinkedIn public detail missing description / possible login wall")
            job["description"] = _text(str(body))
            job["details_fetched"] = True
            _deadline(job, *explicit_deadline(job["description"]))
    except SourceError as exc:
        _raise_partial(source, exc, jobs)
    if capped:
        raise SourceTruncated(f"LinkedIn bounded search: {max_pages} pages, {len(jobs)} unique public listings; no total supplied, more may exist", jobs)
    return jobs


def _fetch_muse(source: dict) -> list[dict]:
    jobs, fetched, total, page_count = [], 0, None, None
    max_pages = _cap(source, "max_pages", 20, 50)
    try:
        for page in range(max_pages):
            params = {"page": page, "level": source.get("levels", ["Entry Level", "Internship"]),
                      "location": source.get("locations", ["Canada"]), "descending": "true"}
            data = _json_response("GET", source["url"], params=params)
            rows = _list(data, "results")
            total = _integer(data.get("total"), "total")
            page_count = _integer(data.get("page_count"), "page_count")
            if data.get("timed_out"):
                raise SourceTruncated("The Muse reports timed_out; incomplete search")
            if data.get("page") != page:
                raise SourceTruncated("The Muse returned unexpected/repeated page")
            fetched += len(rows)
            for item in rows:
                levels = [v.get("name", "") for v in item.get("levels", [])]
                location = "; ".join(v.get("name", "") for v in item.get("locations", []))
                row = _raw(source, title=item.get("name"), company=item.get("company", {}).get("name"), location=location,
                           url=item.get("refs", {}).get("landing_page"), job_id=item.get("id"),
                           description=item.get("contents"), posted_at=item.get("publication_date"))
                row["level_hint"] = _hint(row["title"]) or ("internship" if "Internship" in levels else "entry_level" if "Entry Level" in levels else None)
                jobs.append(row)
            if page + 1 >= page_count:
                if fetched != total:
                    raise SourceTruncated(f"The Muse total mismatch: {fetched} records fetched, declared total {total}")
                return jobs
            if not rows:
                raise SourceTruncated(f"The Muse empty page before declared total {total}")
    except SourceError as exc:
        _raise_partial(source, exc, jobs)
    raise SourceTruncated(f"The Muse capped at {max_pages}/{page_count} pages; fetched {fetched}/{total} records", jobs)


def _fetch_remotive(source: dict) -> list[dict]:
    data = _json_response("GET", source["url"])
    rows = _list(data, "jobs")
    jobs = [_raw(source, title=j.get("title"), company=j.get("company_name"),
                 location="Remote — " + str(j.get("candidate_required_location", "Unspecified")),
                 url=j.get("url"), job_id=j.get("id"), description=j.get("description"),
                 posted_at=j.get("publication_date"), employment_type=j.get("job_type"), salary=j.get("salary")) for j in rows]
    count = _integer(data.get("job-count"), "job-count")
    total = _integer(data.get("total-job-count", count), "total-job-count")
    if count != len(rows) or total > len(rows):
        raise SourceTruncated(f"Remotive feed count mismatch/limited subset: received {len(rows)}, job-count {count}, total {total}", jobs)
    return jobs


def _fetch_remoteok(source: dict) -> list[dict]:
    jobs = []
    for item in _list(_json_response("GET", source["url"])):
        if "legal" in item and not item.get("id"):
            continue
        jobs.append(_raw(source, title=item.get("position"), company=item.get("company"), location=item.get("location", ""),
                         url=item.get("url"), job_id=item.get("id"), description=item.get("description"), posted_at=item.get("date")))
    return jobs


def _fetch_arbeitnow(source: dict) -> list[dict]:
    jobs, visited = [], set()
    url = source["url"]
    origin = urlsplit(url).netloc
    max_pages = _cap(source, "max_pages", 3, 20)
    try:
        for _ in range(max_pages):
            if url in visited or urlsplit(url).netloc != origin or urlsplit(url).scheme != "https":
                raise SourceError("Arbeitnow unsafe or repeated pagination URL")
            visited.add(url)
            data = _json_response("GET", url)
            rows = _list(data, "data")
            for item in rows:
                jobs.append(_raw(source, title=item.get("title"), company=item.get("company_name"), location=item.get("location", ""),
                                 url=item.get("url"), job_id=item.get("slug"), description=item.get("description"),
                                 posted_at=_epoch(item.get("created_at")), employment_type=", ".join(item.get("job_types", []))))
            links = data.get("links")
            if not isinstance(links, dict) or "next" not in links:
                raise SourceError("Arbeitnow pagination schema missing links.next")
            if not links["next"]:
                return jobs
            url = links["next"]
    except SourceError as exc:
        _raise_partial(source, exc, jobs)
    raise SourceTruncated(f"Arbeitnow capped at {max_pages} pages; next page exists (Europe-focused, no Canada assumed)", jobs)


def _jobposting_objects(value: Any):
    if isinstance(value, list):
        for item in value:
            yield from _jobposting_objects(item)
    elif isinstance(value, dict):
        types = value.get("@type", [])
        types = types if isinstance(types, list) else [types]
        if any(str(t).rsplit("/", 1)[-1] == "JobPosting" for t in types):
            yield value
        for key in ("@graph", "mainEntity", "itemListElement", "item"):
            if key in value:
                yield from _jobposting_objects(value[key])


def _schema_location(item: dict) -> tuple[str, bool]:
    locations = item.get("jobLocation", [])
    locations = locations if isinstance(locations, list) else [locations]
    parts, canada = [], False
    for loc in locations:
        if not isinstance(loc, dict):
            continue
        address = loc.get("address", {})
        if isinstance(address, str):
            parts.append(address)
            continue
        country = address.get("addressCountry", "")
        country = country.get("name", country.get("identifier", "")) if isinstance(country, dict) else country
        if str(country).upper() in {"CA", "CAN", "CANADA"}:
            canada, country = True, "Canada"
        parts.append(", ".join(str(address[k]) for k in ("addressLocality", "addressRegion") if address.get(k)) + (", " + str(country) if country else ""))
    requirements = item.get("applicantLocationRequirements", [])
    requirements = requirements if isinstance(requirements, list) else [requirements]
    for req in requirements:
        name = req.get("name", "") if isinstance(req, dict) else str(req)
        if name:
            parts.append("Eligible location: " + name)
        if str(name).upper() == "CANADA" or (str(name).upper() == "CA" and isinstance(req, dict) and req.get("@type") == "Country"):
            canada = True
    if item.get("jobLocationType") == "TELECOMMUTE":
        parts.insert(0, "Remote")
    return "; ".join(p.strip(", ") for p in parts if p), canada


def parse_jobposting_jsonld(text: str, source: dict, page_url: str) -> list[dict]:
    jobs = []
    for script in _soup(text).select('script[type="application/ld+json"]'):
        try:
            value = json.loads(script.string or script.get_text())
        except ValueError:
            continue
        for item in _jobposting_objects(value):
            location, canada = _schema_location(item)
            organization = item.get("hiringOrganization", {})
            company = organization.get("name") if isinstance(organization, dict) else organization
            identifier = item.get("identifier")
            identifier = identifier.get("value") if isinstance(identifier, dict) else identifier
            row = _raw(source, title=item.get("title"), company=company or source.get("company"), location=location,
                       url=item.get("url") or page_url, job_id=identifier, description=item.get("description"),
                       posted_at=item.get("datePosted"), verification=source.get("verification", "direct"),
                       employment_type=item.get("employmentType"))
            if canada:
                row["country_hint"] = "CA"
            _deadline(row, item.get("validThrough"), f"JobPosting.validThrough: {item['validThrough']}" if item.get("validThrough") else None, page_url)
            jobs.append(row)
    if not jobs:
        raise SourceError(f"No valid JobPosting JSON-LD found: {page_url}")
    return jobs


def _fetch_jsonld(source: dict) -> list[dict]:
    return parse_jobposting_jsonld(_request("GET", source["url"]).text, source, source["url"])


_COUNTRY_FACET_LABELS = {"country", "countries", "country/region", "country / region", "location country",
                         "location (country)", "country of location"}
# A Canadian location written as country code + province code, e.g. "CA-QC-LONGUEUIL-1000 ...".
_CANADIAN_CODE_PLACE = re.compile(r"^\s*CAN?\s*-\s*(?:ON|QC|BC|AB|MB|SK|NS|NB|NL|PE|YT|NT|NU)\b", re.I)


def _canadian_facet_value(descriptor: Any) -> bool:
    """A live location-facet value naming a Canadian place ("Canada, Toronto", "Calgary",
    "CA-QC-Longueuil ..."). Ambiguous names (London, Victoria) and "Ontario, CA" stay out."""
    from .core import canadian_options

    text = str(descriptor or "")
    return bool(_CANADIAN_CODE_PLACE.search(text) or canadian_options(text))


def _workday_country_fallback(groups: list[dict], names: list) -> dict | None:
    """No country facet: use a live geographic facet that lists the country itself (e.g. a
    nested "Locations" facet of countries); for Canada, else every Canadian value of a
    location facet. Values come from the live response; nothing is guessed."""
    wanted = {str(name).casefold() for name in names}

    def geographic(group):
        label = f"{group.get('facetParameter') or ''} {group.get('descriptor') or ''}".casefold()
        return bool(group.get("facetParameter")) and any(word in label for word in ("location", "country", "region"))

    for group in filter(geographic, groups):
        hits = [v["id"] for v in group.get("values") or [] if isinstance(v, dict) and v.get("id")
                and str(v.get("descriptor") or "").casefold() in wanted]
        if hits:
            return {group["facetParameter"]: hits}
    if wanted != {"canada"}:
        return None
    for group in filter(geographic, groups):
        hits = [v["id"] for v in group.get("values") or [] if isinstance(v, dict) and v.get("id")
                and not v.get("facetParameter") and _canadian_facet_value(v.get("descriptor"))]
        if hits:
            return {group["facetParameter"]: hits}
    return None


def _workday_facet_groups(data: dict) -> list[dict]:
    """Top-level facets plus nested groups such as locationMainGroup -> locationCountry."""
    groups = []
    for facet in data.get("facets") or []:
        if not isinstance(facet, dict):
            continue
        groups.append(facet)
        for value in facet.get("values") or []:
            if isinstance(value, dict) and value.get("facetParameter") and isinstance(value.get("values"), list):
                groups.append(value)
    return groups


def _workday_facets(data: dict, source: dict) -> dict:
    requested = [(label, names, False) for label, names in dict(source.get("facet_filters", {})).items()]
    if source.get("country"):
        requested.append(("Country", [source["country"]], True))
    groups = _workday_facet_groups(data)
    applied = {}
    for label, names, is_country in requested:
        names = names if isinstance(names, list) else [names]
        facet = next((g for g in groups if str(g.get("descriptor") or "").casefold() == label.casefold()), None)
        if facet is None and is_country:
            # Discovered from the live response: a country facet under another label/nesting.
            facet = next((g for g in groups if str(g.get("descriptor") or "").casefold() in _COUNTRY_FACET_LABELS
                          or "country" in str(g.get("facetParameter") or "").casefold()), None)
        if facet is None and is_country:
            fallback = _workday_country_fallback(groups, names)
            if fallback:
                applied.update(fallback)
                continue
        if not facet or not facet.get("facetParameter"):
            raise SourceError(f"Workday facet {label!r} unavailable; refusing an unfiltered/guessed country query")
        values = {str(v.get("descriptor")).casefold(): v.get("id") for v in facet.get("values", []) if isinstance(v, dict)}
        if any(str(name).casefold() not in values for name in names):
            raise SourceError(f"Workday facet {label!r} lacks requested values {names!r}")
        applied[facet["facetParameter"]] = [values[str(name).casefold()] for name in names]
    return applied


def _detail_order(rows: list[dict]) -> list[int]:
    """Indexes of rows whose titles can still match, early-career titles first.

    A title that fails core.title_could_match is rejected whatever its description
    says, so its detail request would be wasted."""
    from .core import detail_priority, title_could_match

    picks = [index for index, row in enumerate(rows) if title_could_match(row.get("title"))]
    return sorted(picks, key=lambda index: detail_priority(rows[index]["title"]))


def _fetch_workday(source: dict) -> list[dict]:
    parsed = urlsplit(source["url"])
    if not re.fullmatch(r"[a-z0-9-]+\.wd\d+\.myworkdayjobs\.com", parsed.hostname or "", re.I):
        raise SourceError("Workday source must use a public tenant.wdN.myworkdayjobs.com hostname")
    tenant, site = source.get("tenant"), source.get("site")
    if not all(isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9_-]+", v) for v in (tenant, site)):
        raise SourceError("Invalid Workday tenant/site identifiers")
    origin = f"https://{parsed.netloc}"
    endpoint = f"{origin}/wday/cxs/{tenant}/{site}"
    page_size = _cap(source, "page_size", 20, 20)
    max_pages = _cap(source, "max_pages", 20, 50)
    detail_limit = _cap(source, "detail_limit", 150, 300)
    body = {"appliedFacets": {}, "limit": page_size, "offset": 0, "searchText": source.get("keywords", "")}
    jobs, paths, fetched, total = [], [], 0, None
    capped = False
    try:
        data = _json_response("POST", endpoint + "/jobs", json=body.copy())
        if _integer(data.get("total"), "total") == 0:
            _list(data, "jobPostings")
            return []
        if source.get("country") or source.get("facet_filters"):
            body["appliedFacets"] = _workday_facets(data, source)
            data = _json_response("POST", endpoint + "/jobs", json=body.copy())
        seen = set()
        for page in range(max_pages):
            if page:
                body["offset"] = fetched
                data = _json_response("POST", endpoint + "/jobs", json=body.copy())
            rows = _list(data, "jobPostings")
            reported_total = _integer(data.get("total"), "total")
            # CXS reports the real total only at offset=0; later pages commonly
            # carry total=0 with valid records. Preserve the initial assertion.
            if page == 0:
                total = reported_total
            elif reported_total not in {0, total}:
                raise SourceTruncated(f"Workday total changed during pagination: {total} to {reported_total}")
            if not rows and fetched < total:
                raise SourceTruncated(f"Workday empty page before total: {fetched}/{total}")
            for item in rows:
                path = item.get("externalPath", "")
                if not isinstance(path, str) or not path.startswith("/job/") or ".." in path or "?" in path:
                    raise SourceError("Workday externalPath is missing/invalid")
                if path in seen:
                    raise SourceTruncated("Workday repeated job across pages")
                seen.add(path)
                paths.append(path)
                row = _raw(source, title=item.get("title"), company=source.get("company", source["name"]),
                           location=item.get("locationsText", ""), url=f"{origin}/en-US/{site}{path}",
                           job_id=(item.get("bulletFields") or [path])[-1], verification="direct", details_fetched=False)
                if source.get("country") == "Canada":
                    row["country_hint"] = "CA"  # The facet was discovered and applied, not guessed.
                jobs.append(row)
            fetched += len(rows)
            if fetched >= total:
                if fetched != total:
                    raise SourceTruncated(f"Workday total mismatch: {fetched}/{total}")
                break
        else:
            capped = True
        wanted = _detail_order(jobs)
        for index in wanted[:detail_limit]:
            row, path = jobs[index], paths[index]
            detail = _json_response("GET", endpoint + path)
            info = detail.get("jobPostingInfo") if isinstance(detail, dict) else None
            if not isinstance(info, dict) or not info.get("jobDescription"):
                raise SourceError("Workday detail schema missing jobPostingInfo.jobDescription")
            row["title"] = _text(info.get("title") or row["title"])
            row["description"] = _text(info["jobDescription"])
            row["source_job_id"] = str(info.get("jobReqId") or info.get("id") or row["source_job_id"])
            row["posted_at"] = _iso(info.get("startDate"))
            row["posted_at_evidence"] = f"Workday jobPostingInfo.startDate: {info['startDate']}" if row["posted_at"] else None
            location = info.get("location")
            others = info.get("additionalLocations", [])
            others = others if isinstance(others, list) else []
            country = info.get("country", {})
            country_name = country.get("descriptor", "") if isinstance(country, dict) else str(country)
            if location:
                row["location"] = "; ".join(str(v) for v in [location, *others, country_name] if v)
            if country_name == "Canada":
                row["country_hint"] = "CA"
            row["level_hint"] = _hint(row["title"]) or source.get("level_hint")
            row["employment_type"] = info.get("timeType")
            row["details_fetched"] = True
            value, evidence = explicit_deadline(row["description"])
            # Metadata is a fallback, not a replacement for published prose/notes.
            if value is None and parse_structured_deadline(info.get("validThrough")):
                value, evidence = info["validThrough"], f"Workday validThrough: {info['validThrough']}"
            _deadline(row, value, evidence)
    except (SourceError, AttributeError) as exc:
        _raise_partial(source, exc if isinstance(exc, SourceError) else SourceError("Invalid Workday response object"), jobs)
    if capped or len(wanted) > detail_limit:
        raise SourceTruncated(f"Workday cap: {fetched}/{total} index records; {min(len(wanted), detail_limit)}/{len(wanted)} "
                              f"relevant-title details; max_pages={max_pages}", jobs)
    return jobs


def _jobbank_cards(text: str, source: dict) -> tuple[list[dict], int]:
    soup = _soup(text)
    metadata = soup.select_one('meta[name="description"]')
    count = re.search(r"View\s+([\d,]+)\s+job postings", metadata.get("content", "") if metadata else "", re.I)
    if count is None:
        raise SourceError("Job Bank result total missing; refusing unknown/changed search schema")
    total = int(count.group(1).replace(",", ""))
    cards, jobs = soup.select("article a.resultJobItem"), []
    if total and not cards:
        raise SourceError("Job Bank search has a nonzero total but no recognizable job cards")
    for card in cards:
        title, company, location = [card.select_one(selector) for selector in (".noctitle", ".business", ".location")]
        if not all([title, company, location]):
            raise SourceError("Job Bank incomplete card schema")
        href = urljoin(source["url"], card.get("href", ""))
        parsed = urlsplit(href)
        # Remove transient server session IDs and tracking, never retain a cookie.
        path = parsed.path.split(";", 1)[0]
        if parsed.hostname != "www.jobbank.gc.ca" or not re.fullmatch(r"/jobsearch/jobposting/\d+", path):
            raise SourceError("Job Bank unexpected posting URL")
        url = urlunsplit(("https", parsed.netloc, path, "", ""))
        posted = card.select_one(".date")
        jobs.append(_raw(source, title=title.get_text(), company=company.get_text(),
                         location=re.sub(r"^Location\s*", "", location.get_text(" ", strip=True)),
                         url=url, job_id=path.rsplit("/", 1)[-1], posted_at=_calendar_date(posted.get_text() if posted else ""),
                         details_fetched=False))
    return jobs, total


def _fetch_jobbank(source: dict) -> list[dict]:
    jobs, seen, total = [], set(), 0
    max_pages = _cap(source, "max_pages", 2, 20)
    detail_limit = _cap(source, "detail_limit", 40, 100)
    capped = False
    try:
        for page in range(1, max_pages + 1):
            params = {"searchstring": source.get("keywords", "software"), "sort": "D", "page": page}
            rows, total = _jobbank_cards(_request("GET", source["url"], params=params).text, source)
            new = [r for r in rows if r["source_job_id"] not in seen]
            if total and not new:
                raise SourceTruncated("Job Bank repeated/empty page before completion")
            jobs.extend(new)
            seen.update(r["source_job_id"] for r in rows)
            if len(jobs) >= total:
                if len(jobs) != total:
                    raise SourceTruncated(f"Job Bank total mismatch: {len(jobs)}/{total}")
                break
        else:
            capped = True
        for row in jobs[:detail_limit]:
            response = _request("GET", row["url"])
            soup = _soup(response.text)
            main = soup.select_one("main")
            if main is None:
                raise SourceError("Job Bank posting detail has no main content / schema changed")
            for element in main.select("script,style,nav,header,footer"):
                element.decompose()
            row["description"] = _text(str(main))
            row["details_fetched"] = True
            row["is_closed"] = bool(re.search(r"this job posting (?:has expired|is no longer available)", row["description"], re.I))
            _deadline(row, *explicit_deadline(row["description"]))
            # Some Job Bank templates expose JobPosting; others publish only text.
            if soup.select('script[type="application/ld+json"]'):
                try:
                    schema = parse_jobposting_jsonld(response.text, {**source, "verification": "public_listing"}, row["url"])
                    exact = next((j for j in schema if j["title"].casefold() == row["title"].casefold()), None)
                    if exact:
                        for key in ("posted_at", "deadline", "deadline_evidence", "deadline_source_url"):
                            if exact.get(key):
                                row[key] = exact[key]
                except SourceError:
                    pass  # Text is verified; absent unrelated JSON-LD is not a failure.
    except SourceError as exc:
        _raise_partial(source, exc, jobs)
    if capped or len(jobs) > detail_limit:
        raise SourceTruncated(f"Job Bank bounded search: {len(jobs)}/{total} index records, {min(len(jobs), detail_limit)} details; max_pages={max_pages}", jobs)
    return jobs


def _fetch_employer_jsonld(source: dict) -> list[dict]:
    """Use a curated index for discovery, then verify actual employer pages."""
    feed_type = source.get("feed_type")
    if feed_type not in {"frogg", "simplify"}:
        raise SourceError("employer_jsonld requires a supported curated feed_type")
    company = source.get("company_filter")
    host = source.get("employer_host")
    if not isinstance(company, str) or not company or not isinstance(host, str) or not host:
        raise SourceError("employer_jsonld requires company_filter and employer_host")
    feed_source = {**source, "type": feed_type, "include_closed": False}
    candidates = _finish(feed_source, (_fetch_frogg if feed_type == "frogg" else _fetch_simplify)(feed_source))
    candidates = [j for j in candidates if j["company"].casefold() == company.casefold()
                  and (urlsplit(j["url"]).hostname or "").casefold() == host.casefold()]
    # Different indexed terms can point to the same employer page.
    candidates = list({j["url"]: j for j in candidates}.values())
    limit = _cap(source, "detail_limit", 30, 100)
    jobs = []
    try:
        for candidate in candidates[:limit]:
            response = _request("GET", candidate["url"])
            for job in parse_jobposting_jsonld(response.text, source, candidate["url"]):
                job["discovery_verification"] = "curated"
                job["discovery_url"] = source["url"]
                job["details_fetched"] = True
                jobs.append(job)
    except SourceError as exc:
        _raise_partial(source, exc, jobs)
    if len(candidates) > limit:
        raise SourceTruncated(f"Employer JSON-LD detail cap: {limit}/{len(candidates)} indexed open URLs verified", jobs)
    return jobs


def _sap_microdata(text: str, source: dict, url: str) -> list[dict]:
    """Verified SAP JobPosting markup; no fabricated JSON-LD fallback values."""
    soup = _soup(text)
    scope = soup.select_one('[itemtype$="/JobPosting"]')
    title = soup.select_one('[itemprop="title"]')
    descriptions = soup.select('[itemprop="description"]')
    if scope is None or title is None or not descriptions:
        raise SourceError(f"Employer page has neither JobPosting JSON-LD nor supported microdata: {url}")
    page_text = scope.get_text(" ", strip=True)
    requisition = re.search(r"Requisition ID:\s*([A-Za-z0-9_-]+)", page_text)
    posted = re.search(r"Posting Start Date:\s*(\d{1,2}/\d{1,2}/(?:\d{4}|\d{2}))\b", page_text)
    posted_at = None
    if posted:
        try:
            fmt = "%m/%d/%Y" if len(posted.group(1).split("/")[-1]) == 4 else "%m/%d/%y"
            posted_at = datetime.strptime(posted.group(1), fmt).date().isoformat()
        except ValueError:
            pass
    location = next((span.get_text(" ", strip=True) for span in scope.select("span[lang]")
                     if re.search(r",\s*[A-Z]{2},\s*[A-Z]{3},", span.get_text(" ", strip=True))), "")
    row = _raw(source, title=title.get_text(), company=source.get("company"), location=location,
               url=url, job_id=requisition.group(1) if requisition else None,
               description=" ".join(str(part) for part in descriptions), posted_at=posted_at,
               verification="direct", details_fetched=True, structured_data_format="JobPosting microdata",
               posted_at_evidence=posted.group(0) if posted_at else None)
    _deadline(row, *explicit_deadline(page_text))
    valid = scope.select_one('[itemprop="validThrough"]')
    if valid:
        value = valid.get("content") or valid.get("datetime") or valid.get_text(strip=True)
        _deadline(row, _iso(value), f"JobPosting microdata validThrough: {value}", url)
    return [row]


def _fetch_sitemap_jobposting(source: dict) -> list[dict]:
    """Bounded dynamic employer discovery from an actual public urlset."""
    text = _request("GET", source["url"]).text
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        raise SourceError("Sitemap DTD/entity declarations are not supported")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise SourceError("Invalid public sitemap XML") from exc
    if root.tag.rsplit("}", 1)[-1] != "urlset":
        raise SourceError("Expected a job urlset sitemap, not an index/login/unknown schema")
    host = source.get("employer_host")
    if not host or host != urlsplit(source["url"]).hostname:
        raise SourceError("Employer sitemap must use the configured public employer host")
    early = re.compile(r"(?:^|[-,/ ])(?:intern|junior|new[- ]?grad|graduate|co[- ]?op)(?:[-,/ (]|$)", re.I)
    technical = re.compile(r"software|developer|engineer|data|analyst|digital|cyber|security|information|cloud|avionic|embedded|firmware|MDO|(?:^|-)IT(?:-|,)", re.I)
    urls = []
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] != "loc" or not element.text:
            continue
        url = element.text.strip()
        parts = urlsplit(url)
        decoded = unquote(parts.path)
        if parts.hostname == host and parts.scheme == "https" and parts.path.startswith("/job/") and early.search(decoded) and technical.search(decoded):
            urls.append(url)
    urls = list(dict.fromkeys(urls))
    limit = _cap(source, "detail_limit", 50, 100)
    jobs = []
    try:
        for url in urls[:limit]:
            response = _request("GET", url)
            try:
                rows = parse_jobposting_jsonld(response.text, source, url)
            except SourceError as exc:
                if "No valid JobPosting JSON-LD" not in str(exc):
                    raise
                rows = _sap_microdata(response.text, source, url)
            for row in rows:
                row["discovery_verification"] = "direct"
                row["discovery_url"] = source["url"]
                row["details_fetched"] = True
                jobs.append(row)
    except SourceError as exc:
        _raise_partial(source, exc, jobs)
    if len(urls) > limit:
        raise SourceTruncated(f"Employer sitemap bounded subset: {limit}/{len(urls)} matching early-career technical URLs fetched", jobs)
    return jobs


_ATS_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}")


def _ats_identifier(source: dict, key: str = "board") -> str:
    value = source.get(key)
    if not isinstance(value, str) or not _ATS_SLUG.fullmatch(value):
        raise SourceError(f"Invalid ATS identifier for {source.get('type')}: {value!r}")
    return value


def _place(*parts: Any) -> str:
    seen = []
    for part in parts:
        text = _text(part)
        if text and text.casefold() not in {p.casefold() for p in seen}:
            seen.append(text)
    return ", ".join(seen)


def _country_hint(row: dict, *countries: Any) -> dict:
    if any(str(c or "").strip().upper() in {"CA", "CAN", "CANADA"} for c in countries):
        row["country_hint"] = "CA"
    return row


def _prose_or_metadata_deadline(row: dict, metadata: Any, label: str) -> None:
    """Published prose wins; a structured close date is only the fallback."""
    value, evidence = explicit_deadline(row["description"])
    if value is None and metadata:
        candidate = _iso(str(metadata).replace(" UTC", "+00:00").replace(" ", "T", 1)) if isinstance(metadata, str) else None
        if candidate and parse_structured_deadline(candidate):
            value, evidence = candidate, f"{label}: {metadata}"
    _deadline(row, value, evidence)


def _fetch_smartrecruiters(source: dict) -> list[dict]:
    company_id = _ats_identifier(source, "company_id")
    endpoint = f"https://api.smartrecruiters.com/v1/companies/{company_id}/postings"
    page_size = _cap(source, "page_size", 100, 100)
    max_pages = _cap(source, "max_pages", 20, 50)
    detail_limit = _cap(source, "detail_limit", 150, 300)
    country = str(source.get("country_code", "ca")).lower()
    rows, offset, total = [], 0, None
    try:
        for _ in range(max_pages):
            data = _json_response("GET", endpoint, params={"limit": page_size, "offset": offset, "country": country})
            items = _list(data, "content")
            found = _integer(data.get("totalFound"), "totalFound")
            total = found if total is None else total
            for item in items:
                identity = str(item.get("id") or "")
                if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", identity):
                    raise SourceError("SmartRecruiters posting id missing/invalid")
                location = item.get("location") if isinstance(item.get("location"), dict) else {}
                place = location.get("fullLocation") or _place(location.get("city"), location.get("region"), location.get("country"))
                if location.get("remote"):
                    place = f"Remote — {place}" if place else "Remote"
                company = (item.get("company") or {}).get("name") if isinstance(item.get("company"), dict) else None
                employment = item.get("typeOfEmployment") if isinstance(item.get("typeOfEmployment"), dict) else {}
                row = _raw(source, title=item.get("name"), company=source.get("company") or company or source["name"],
                           location=place, url=f"https://jobs.smartrecruiters.com/{company_id}/{identity}",
                           job_id=identity, verification="direct", posted_at=item.get("releasedDate"),
                           details_fetched=False, employment_type=employment.get("label"))
                rows.append(_country_hint(row, location.get("country")))
            offset += len(items)
            if not items or offset >= total:
                break
        if offset < (total or 0):
            raise SourceTruncated(f"SmartRecruiters snapshot incomplete: {offset}/{total} postings", rows)
        wanted = _detail_order(rows)
        for index in wanted[:detail_limit]:
            row = rows[index]
            detail = _json_response("GET", f"{endpoint}/{row['source_job_id']}")
            job_ad = detail.get("jobAd") if isinstance(detail, dict) else None
            sections = job_ad.get("sections") if isinstance(job_ad, dict) else None
            if not isinstance(sections, dict):
                raise SourceError("SmartRecruiters detail schema missing jobAd sections")
            parts = [_text(section.get("text")) if isinstance(section := sections.get(key), dict) else ""
                     for key in ("jobDescription", "qualifications", "additionalInformation")]
            # An employer can publish an ad whose sections are all empty. Keep that posting
            # with no description (eligibility then rests on its title) instead of failing
            # the whole board; only a missing schema is a source error.
            description = "\n\n".join(part for part in parts if part)
            row["description"] = description
            if isinstance(detail.get("postingUrl"), str):
                row["url"] = _safe_url(detail["postingUrl"])
            row["details_fetched"] = True
            value, evidence = explicit_deadline(description)
            _deadline(row, value, evidence)
    except SourceTruncated:
        raise
    except SourceError as exc:
        _raise_partial(source, exc, rows)
    if len(wanted) > detail_limit:
        raise SourceTruncated(f"SmartRecruiters detail cap: {detail_limit}/{len(wanted)} relevant-title details", rows)
    return rows


def _fetch_workable(source: dict) -> list[dict]:
    account = _ats_identifier(source)
    data = _json_response("GET", f"https://apply.workable.com/api/v1/widget/accounts/{account}", params={"details": "true"})
    jobs = []
    company = source.get("company") or (data.get("name") if isinstance(data, dict) else None) or source["name"]
    for item in _list(data, "jobs"):
        shortcode = str(item.get("shortcode") or "")
        if not re.fullmatch(r"[A-Za-z0-9]{1,32}", shortcode):
            raise SourceError("Workable posting shortcode missing/invalid")
        places = [loc for loc in (item.get("locations") or []) if isinstance(loc, dict) and not loc.get("hidden")]
        labels = [_place(loc.get("city"), loc.get("region"), loc.get("country")) for loc in places] or [
            _place(item.get("city"), item.get("state"), item.get("country"))]
        location = "; ".join(label for label in labels if label)
        if item.get("telecommuting"):
            location = f"Remote — {location}" if location else "Remote"
        row = _raw(source, title=item.get("title"), company=company, location=location,
                   url=item.get("url") or f"https://apply.workable.com/j/{shortcode}", job_id=shortcode,
                   description=item.get("description"), verification="direct", posted_at=item.get("published_on"),
                   employment_type=item.get("employment_type"), details_fetched=True)
        _country_hint(row, item.get("country"), *[loc.get("countryCode") or loc.get("country") for loc in places])
        value, evidence = explicit_deadline(row["description"])
        _deadline(row, value, evidence)
        jobs.append(row)
    return jobs


def _fetch_recruitee(source: dict) -> list[dict]:
    board = _ats_identifier(source)
    data = _json_response("GET", f"https://{board}.recruitee.com/api/offers/")
    jobs = []
    for item in _list(data, "offers"):
        if item.get("status") not in {None, "published"}:
            continue
        places = [loc for loc in (item.get("locations") or []) if isinstance(loc, dict)]
        location = "; ".join(_place(loc.get("city"), loc.get("state"), loc.get("country")) for loc in places) or _place(
            item.get("city"), item.get("state_name"), item.get("country"))
        if item.get("remote") and not item.get("on_site"):
            location = f"Remote — {location}" if location else "Remote"
        description = "\n\n".join(t for t in (_text(item.get("description")), _text(item.get("requirements"))) if t)
        posted = item.get("published_at")
        posted = _iso(posted.replace(" UTC", "+00:00").replace(" ", "T", 1)) if isinstance(posted, str) else None
        row = _raw(source, title=item.get("title"), company=source.get("company") or item.get("company_name") or source["name"],
                   location=location, url=item.get("careers_url"), job_id=item.get("id"), description=description,
                   verification="direct", posted_at=posted, employment_type=item.get("employment_type_code"),
                   details_fetched=True)
        _country_hint(row, item.get("country_code"), *[loc.get("country_code") for loc in places])
        _prose_or_metadata_deadline(row, item.get("close_at"), "Recruitee close_at")
        jobs.append(row)
    return jobs


def _fetch_bamboohr(source: dict) -> list[dict]:
    board = _ats_identifier(source)
    origin = f"https://{board}.bamboohr.com/careers"
    detail_limit = _cap(source, "detail_limit", 60, 200)
    rows = []
    try:
        for item in _list(_json_response("GET", f"{origin}/list"), "result"):
            identity = str(item.get("id") or "")
            if not re.fullmatch(r"\d{1,12}", identity):
                raise SourceError("BambooHR job id missing/invalid")
            ats = item.get("atsLocation") if isinstance(item.get("atsLocation"), dict) else {}
            loc = item.get("location") if isinstance(item.get("location"), dict) else {}
            location = _place(ats.get("city"), ats.get("state") or ats.get("province"), ats.get("country")) or _place(
                loc.get("city"), loc.get("state"))
            if item.get("isRemote"):
                location = f"Remote — {location}" if location else "Remote"
            row = _raw(source, title=item.get("jobOpeningName"), company=source.get("company") or source["name"],
                       location=location, url=f"{origin}/{identity}", job_id=identity, verification="direct",
                       employment_type=item.get("employmentStatusLabel"), details_fetched=False)
            rows.append(_country_hint(row, ats.get("country")))
        relevant = [i for i in _detail_order(rows) if not source.get("canada_only") or rows[i].get("country_hint") == "CA"
                    or CANADA_RE.search(rows[i]["location"])]
        for index in relevant[:detail_limit]:
            row = rows[index]
            detail = _json_response("GET", f"{origin}/{row['source_job_id']}/detail")
            opening = ((detail.get("result") or {}).get("jobOpening") or {}) if isinstance(detail, dict) else {}
            if not isinstance(opening, dict) or not opening.get("description"):
                raise SourceError("BambooHR detail schema missing jobOpening.description")
            row["description"] = _text(opening["description"])
            row["posted_at"] = _iso(opening.get("datePosted"))
            loc = opening.get("location") if isinstance(opening.get("location"), dict) else {}
            if loc.get("addressCountry"):
                row["location"] = _place(loc.get("city"), loc.get("state"), loc.get("addressCountry"))
                _country_hint(row, loc.get("addressCountry"))
            if isinstance(opening.get("jobOpeningShareUrl"), str):
                row["url"] = _safe_url(opening["jobOpeningShareUrl"])
            row["details_fetched"] = True
            value, evidence = explicit_deadline(row["description"])
            _deadline(row, value, evidence)
    except SourceError as exc:
        _raise_partial(source, exc, rows)
    if len(relevant) > detail_limit:
        raise SourceTruncated(f"BambooHR detail cap: {detail_limit}/{len(relevant)} relevant-title details", rows)
    return rows


_HANDLERS = {"simplify": _fetch_simplify, "frogg": _fetch_frogg, "linkedin": _fetch_linkedin,
             "themuse": _fetch_muse, "remotive": _fetch_remotive, "remoteok": _fetch_remoteok,
             "arbeitnow": _fetch_arbeitnow, "jsonld": _fetch_jsonld, "workday": _fetch_workday,
             "jobbank": _fetch_jobbank, "employer_jsonld": _fetch_employer_jsonld,
             "sitemap_jobposting": _fetch_sitemap_jobposting, "smartrecruiters": _fetch_smartrecruiters,
             "workable": _fetch_workable, "recruitee": _fetch_recruitee, "bamboohr": _fetch_bamboohr}


def fetch_source(source: dict) -> list[dict]:
    """Fetch public raw listings. SourceError failures are NEVER empty success."""
    if not isinstance(source, dict) or not all(source.get(key) for key in ("id", "name", "type", "url")):
        raise SourceError("Source requires id, name, type and url")
    handler = _HANDLERS.get(source["type"])
    if handler is None:
        raise SourceError(f"Unsupported extra source type: {source['type']}")
    try:
        return _finish(source, handler(source))
    except SourceError as exc:
        exc.partial_jobs = _finish(source, exc.partial_jobs)
        exc.jobs = exc.partial_jobs
        exc.raw_count = len(exc.partial_jobs)
        raise
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise SourceError(f"Invalid {source['type']} schema/config: {type(exc).__name__}: {exc}") from exc
