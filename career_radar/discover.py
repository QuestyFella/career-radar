"""Career Radar source discovery.

Harvests employer job boards (Greenhouse, Lever, Ashby, Workday, SmartRecruiters,
Workable, Recruitee, BambooHR) from lead files written by research/scout agents and from
employer links already stored in the board database. Every candidate is verified against
its real public API with the production adapters; boards with at least one current
technical posting in the configured region are written to discovered_sources.json, which
collect.load_registry() picks up automatically on the next collection.

Read-only towards employers: public job-board endpoints only, at most six concurrent
probes, no accounts, cookies or bypasses. A lead never becomes a source without a passing
probe. A discovered board that starts failing is re-probed and dropped after repeated
failures; boards that did not qualify are re-checked on a schedule, so seasonal hiring is
picked up later.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from . import collect
from . import config
from . import core
from . import regions
from . import registry
SCHEMA_VERSION = 1
WORKERS = 6
# Days before a candidate with this outcome is probed again.
RECHECK_DAYS = {"verified": 30, "no_canada_tech": 10, "no_country_facet": 30, "not_found": 45, "error": 3, "excluded": 365}
# Job aggregators that repost other employers' roles, and crowd-work / survey-gig boards.
_DENIED = re.compile(r"jobgether|usasurveyjob|toloka|mindrift|remotasks|dataannotation|crowdgen", re.I)
# Sandbox tenants of ATS-integration vendors list fake postings ("TestE2E002", "Test - Disposition",
# "dsa"). A board where at least half of 3+ postings look like this is not an employer.
_TEST_POSTING = re.compile(r"\btest(?:e2e|\d)|\be2e\b|\btest\s*[-–:]\s|\bdo not apply\b|\bdummy\b|"
                           r"^\s*(?:test|dsa|asdf|qwerty)\s*(?:\d+|sep\w*|job|posting|position|ad|role|opening)?\s*$", re.I)
DROP_AFTER = 3  # consecutive failed re-probes before a discovered board is removed
MAX_LEAD_BYTES = 60_000_000
LEAD_RETENTION_DAYS = 60
CHECKPOINT_SECONDS = 60
_ENTRY_KEYS = ("status", "checked_at", "raw", "canada", "tech", "matched", "error", "spelling")
LEAD_SUFFIXES = {".json", ".jsonl", ".txt", ".md", ".csv"}

_IDENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")
_IGNORED = {
    "greenhouse": {"embed", "jobs", "job", "v1", "boards", "api", "job_app", "job_board", "static", "assets", "careers"},
    "lever": {"api", "v0", "jobs", "static", "assets", "careers"},
    "ashby": {"api", "embed", "jobs", "static", "assets", "careers", "posting-api"},
    "smartrecruiters": {"oneclick-ui", "sr-jobs", "company", "companies", "jobs", "search", "api", "app", "static", "careers"},
    "workable": {"j", "api", "careers", "jobs", "embed", "widget", "backend", "static", "assets", "apply", "www", "help",
                 "resources", "blog", "status", "developers", "app", "id", "info", "marketplace", "workable"},
    "recruitee": {"www", "app", "api", "careers", "blog", "help", "support", "status", "cdn", "docs", "marketplace", "demo"},
    "bamboohr": {"www", "api", "help", "marketplace", "partners", "status", "blog", "support", "app", "login",
                 "documentation", "go", "info", "resources", "careers", "demo"},
    "workday": {"wday", "job", "jobs", "login", "search", "apply", "details", "en-us", "recruiting"},
}
_PATTERNS = [
    ("greenhouse", re.compile(r"https?://(?:job-boards|boards)\.greenhouse\.io/(?:embed/job_(?:board|app)(?:/js)?\?(?:[^\"'\s<>]*?&)?for=)?([A-Za-z0-9_-]+)", re.I)),
    ("greenhouse", re.compile(r"https?://boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)", re.I)),
    ("ashby", re.compile(r"https?://jobs\.ashbyhq\.com/([A-Za-z0-9_.-]+)", re.I)),
    ("ashby", re.compile(r"https?://api\.ashbyhq\.com/posting-api/job-board/([A-Za-z0-9_.-]+)", re.I)),
    ("smartrecruiters", re.compile(r"https?://(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)", re.I)),
    ("smartrecruiters", re.compile(r"https?://api\.smartrecruiters\.com/v1/companies/([A-Za-z0-9_-]+)", re.I)),
    ("workable", re.compile(r"https?://apply\.workable\.com/(?:api/v\d/widget/accounts/)?([A-Za-z0-9_-]+)", re.I)),
    # Older Workable boards live on <account>.workable.com; the widget API uses the same account.
    ("workable", re.compile(r"https?://([a-z0-9][a-z0-9-]*)\.workable\.com\b", re.I)),
    ("recruitee", re.compile(r"https?://([a-z0-9][a-z0-9-]*)\.recruitee\.com\b", re.I)),
    ("bamboohr", re.compile(r"https?://([a-z0-9][a-z0-9-]*)\.bamboohr\.com\b", re.I)),
]
_LEVER = re.compile(r"https?://(?:jobs|api)\.(eu\.)?lever\.co/(?:v0/postings/)?([A-Za-z0-9_.-]+)", re.I)
_WORKDAY = re.compile(r"https?://([a-z0-9][a-z0-9-]*)\.(wd\d{1,3})\.myworkdayjobs\.com/"
                      r"(?:wday/cxs/[a-z0-9-]+/|[a-z]{2}-[a-z]{2}/)?([A-Za-z0-9_-]+)", re.I)
_WORKDAY_SITE = re.compile(r"https?://(wd\d{1,3})\.myworkdaysite\.com/(?:[a-z]{2}-[a-z]{2}/)?recruiting/"
                           r"([a-z0-9][a-z0-9-]*)/([A-Za-z0-9_-]+)", re.I)
_NAME_KEYS = ("company", "company_name", "employer", "organization")
# Workday sites meant for employees, referrals or alumni rather than public applicants.
_WORKDAY_PRIVATE = re.compile(r"referr|internal|employee|alumni|colleague|associate_only|team_?member", re.I)
# Production collection limits for discovered boards (keep twice-daily collection bounded).
CAPS = {"workday": {"max_pages": 25, "detail_limit": 50}, "smartrecruiters": {"max_pages": 10, "detail_limit": 60},
        "bamboohr": {"detail_limit": 40}}
# A probe only needs to prove the board works and has technical titles in the selected region.
PROBE_CAPS = {"workday": {"max_pages": 3, "detail_limit": 2}, "smartrecruiters": {"max_pages": 2, "detail_limit": 2},
              "bamboohr": {"detail_limit": 2}}
_DISCOVERY = "Added by Career Radar source discovery after a live public-API check."
NOTES = {
    "greenhouse": f"Employer public Greenhouse board, complete snapshot. {_DISCOVERY}",
    "lever": f"Employer public Lever board, complete snapshot. {_DISCOVERY}",
    "ashby": f"Employer public Ashby board, complete snapshot. {_DISCOVERY}",
    "workday": ("Employer public Workday board filtered by its live Canada facet; newest 500 index records, descriptions "
                f"for up to 50 technical titles, early-career first. {_DISCOVERY}"),
    "smartrecruiters": f"Employer public SmartRecruiters postings in Canada; descriptions for technical titles. {_DISCOVERY}",
    "workable": f"Employer public Workable board. {_DISCOVERY}",
    "recruitee": f"Employer public Recruitee board. {_DISCOVERY}",
    "bamboohr": f"Employer public BambooHR careers list; descriptions for Canadian technical titles. {_DISCOVERY}",
}
FOCUS = [
    "Aerospace, space and satellite companies with Canadian sites: launch, propulsion, avionics, flight simulation and training, drones/UAV, space robotics, Earth observation, MRO and aircraft/engine suppliers.",
    "Defence, security electronics, simulation and government technology contractors in Canada (Ottawa, Montreal, Halifax, Victoria, Kingston).",
    "Embedded systems, firmware, semiconductors, FPGA and electronics companies with Canadian engineering sites (Kanata/Ottawa, Markham, Waterloo, Burnaby, Montreal).",
    "Robotics, autonomy, computer vision, industrial automation and drone companies in Canada.",
    "Automotive, EV, battery, rail and mobility technology employers with Canadian engineering sites (Waterloo, Toronto, Windsor, Montreal, Vancouver).",
    "AI/ML labs, AI startups and ML-infrastructure companies in Toronto, Montreal, Edmonton, Vancouver and Waterloo (Vector, Mila and Amii partners, Creative Destruction Lab, recently funded).",
    "Developer tools, data, analytics and SaaS startups headquartered in Canada (BetaKit funding news, Communitech, MaRS, DMZ, Y Combinator companies in Canada).",
    "Fintech, banks, insurers, payments and investment firms with Canadian technology teams and co-op programmes.",
    "Telecom, networking, cloud, infrastructure and cybersecurity companies with Canadian offices.",
    "Video game studios and creative/graphics technology in Montreal, Vancouver, Toronto, Quebec City and Edmonton.",
    "Health tech, bioinformatics, medical-device and life-science computing companies in Canada.",
    "Climate, energy, cleantech, utilities, agriculture and mining technology in Canada (Calgary, Edmonton, Vancouver, Saskatoon).",
    "Big tech and international software companies with Canadian engineering offices and student or new-grad programmes.",
    "Consulting, IT services, e-commerce, retail, logistics and supply-chain technology employers in Canada.",
    "Quantum computing, photonics, deep tech and university spin-offs in Canada (Waterloo, Toronto, Montreal, Vancouver, Sherbrooke).",
    "Smaller Canadian tech hubs: Halifax, Fredericton, Moncton, St. John's, Winnipeg, Regina, Saskatoon, Kelowna, Victoria, London ON, Hamilton, Kingston, Guelph, Sherbrooke.",
    "ATS sweep: site:job-boards.greenhouse.io, site:jobs.lever.co and site:jobs.ashbyhq.com searches combining Canadian cities with intern, co-op, new grad, junior, software, data, embedded and robotics.",
    "ATS sweep: site:myworkdayjobs.com, site:jobs.smartrecruiters.com, site:apply.workable.com, site:bamboohr.com and site:recruitee.com searches combining Canadian cities with student, intern, co-op, graduate and engineer.",
]


def _now():
    return datetime.now(timezone.utc)


def _parse_time(value):
    try:
        moment = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def _write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=1))
    os.replace(temporary, path)


# --- harvesting ------------------------------------------------------------------------

def parse_urls(text):
    """Every recognizable employer-board identity in a piece of text."""
    text = str(text or "")
    hits = []
    for kind, pattern in _PATTERNS:
        for match in pattern.finditer(text):
            ident = match.group(1).rstrip(".-_")
            if ident and ident.lower() not in _IGNORED[kind] and _IDENT.fullmatch(ident):
                hits.append({"type": kind, "key": f"{kind}:{ident.lower()}", "id": ident, "evidence": match.group(0)})
    for match in _LEVER.finditer(text):
        ident = match.group(2).rstrip(".-_")
        if ident and ident.lower() not in _IGNORED["lever"] and _IDENT.fullmatch(ident):
            region = "eu" if match.group(1) else None
            hits.append({"type": "lever", "key": f"lever:{'eu:' if region else ''}{ident.lower()}", "id": ident,
                         "region": region, "evidence": match.group(0)})
    for pattern, order in ((_WORKDAY, (1, 2, 3)), (_WORKDAY_SITE, (2, 1, 3))):
        for match in pattern.finditer(text):
            tenant, wd, site = (match.group(i) for i in order)
            tenant, wd = tenant.lower(), wd.lower()
            if site.lower() in _IGNORED["workday"] or _WORKDAY_PRIVATE.search(site):
                continue
            hits.append({"type": "workday", "key": f"workday:{tenant}:{site.lower()}", "id": site, "tenant": tenant,
                         "wd": wd, "evidence": match.group(0)})
    return hits


def _title_name(kind, title):
    title = str(title or "").strip()
    if kind == "lever" and " - " in title:
        return title.split(" - ", 1)[0]
    if kind == "ashby" and " @ " in title:
        return title.rsplit(" @ ", 1)[1]
    match = re.search(r"^Job Application for .+ at (.+)$", title)
    return match.group(1) if match else None


def _clean_name(value):
    value = re.sub(r"\s+", " ", str(value or "")).strip(" -|·")
    if not value or len(value) > 80 or re.search(r"https?:|www\.|\.(?:com|io|co|ca)\b|[{}<>]", value, re.I):
        return None
    return value


def _new_candidate(hit):
    return {"key": hit["key"], "type": hit["type"], "spellings": Counter(), "wd": Counter(), "names": Counter(),
            "origins": Counter(), "evidence": hit["evidence"], "canada": False, "tenant": hit.get("tenant"),
            "region": hit.get("region")}


def _add(found, hit, origin, name=None, title=None, canada=False):
    cand = found.setdefault(hit["key"], _new_candidate(hit))
    cand["spellings"][hit["id"]] += 1
    if hit.get("wd"):
        cand["wd"][hit["wd"]] += 1
    cand["origins"][origin] += 1
    for value in (name, _title_name(hit["type"], title)):
        if (clean := _clean_name(value)):
            cand["names"][clean] += 1
    cand["canada"] = cand["canada"] or bool(canada)


def _walk(value, origin, found, unsupported):
    if isinstance(value, dict):
        strings = [v for v in value.values() if isinstance(v, str)]
        hits = [hit for text in strings for hit in parse_urls(text)]
        single = len({hit["key"] for hit in hits}) == 1
        name = next((value[k] for k in _NAME_KEYS if isinstance(value.get(k), str) and value[k].strip()), None)
        title = value.get("title") if isinstance(value.get("title"), str) else None
        titles = value.get("titles")
        if title is None and isinstance(titles, list) and titles and isinstance(titles[0], str):
            title = titles[0]
        evidence = value.get("canada_evidence")
        canada = bool(evidence) if evidence is not None else bool(core.CANADA.search(" ".join(strings)[:4000]))
        for hit in hits:
            _add(found, hit, origin, name if single else None, title if single else None, canada)
        if not hits and name and str(value.get("ats", "")).lower() in {"other", "unknown", "custom"} and value.get("board_url"):
            unsupported[name.strip().lower()] = {"company": name.strip(), "board_url": value.get("board_url"),
                                                 "notes": str(value.get("notes") or "")[:200], "seen": origin}
        for nested in value.values():
            if isinstance(nested, (dict, list)):
                _walk(nested, origin, found, unsupported)
    elif isinstance(value, list):
        for nested in value:
            _walk(nested, origin, found, unsupported)


def harvest_files(paths, found, unsupported):
    files = []
    for path in map(Path, paths):
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            files.extend(p for p in sorted(path.rglob("*")) if p.is_file() and p.suffix.lower() in LEAD_SUFFIXES)
    for path in files:
        if path.stat().st_size > MAX_LEAD_BYTES:
            continue
        text = path.read_text(errors="ignore")
        origin = f"lead:{path.name}"
        try:
            _walk(json.loads(text), origin, found, unsupported)
            continue
        except ValueError:
            pass
        leftovers = []
        for line in text.splitlines():
            try:
                _walk(json.loads(line), origin, found, unsupported)
            except ValueError:
                leftovers.append(line)
        for hit in parse_urls("\n".join(leftovers)):
            _add(found, hit, origin)
    return len(files)


def harvest_db(db, found):
    """Employer links already collected (Simplify, LinkedIn, Job Bank ...) name new boards."""
    if not db or not Path(db).is_file():
        return 0
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    links = 0
    try:
        for (data,) in connection.execute("SELECT data FROM observations"):
            try:
                item = json.loads(data)
            except ValueError:
                continue
            for hit in parse_urls(item.get("url")):
                _add(found, hit, "board-db", item.get("company"), None, True)
                links += 1
        for (url,) in connection.execute("SELECT url FROM aliases"):  # postings already on the board
            for hit in parse_urls(url):
                _add(found, hit, "board-db", canada=True)
                links += 1
        for (url,) in connection.execute("SELECT url FROM source_rejections"):
            for hit in parse_urls(url):
                _add(found, hit, "board-db-rejected")
                links += 1
    except sqlite3.Error:
        pass
    finally:
        connection.close()
    return links


def registry_key(source):
    kind = source.get("type")
    if kind == "workday":
        return f"workday:{str(source.get('tenant') or '').lower()}:{str(source.get('site') or '').lower()}"
    ident = source.get("board") or source.get("board_token") or source.get("company_id")
    if ident:
        region = "eu:" if kind == "lever" and source.get("region") == "eu" else ""
        return f"{kind}:{region}{str(ident).lower()}"
    hits = [hit for hit in parse_urls(source.get("url")) if hit["type"] == kind]
    return hits[0]["key"] if hits else f"{kind}:{str(source.get('url') or '').lower()}"


# --- probing ---------------------------------------------------------------------------

def _pretty(slug):
    words = [w for w in re.split(r"[-_.\s]+", str(slug)) if w]
    name = " ".join(w[:1].upper() + w[1:] for w in words) or str(slug)
    return name.upper() if len(name) <= 4 and name.isalnum() else name


def _unique_id(base, taken):
    base = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")[:90]
    ident, n = base, 2
    while ident in taken:
        ident, n = f"{base}-{n}", n + 1
    return ident


def build_source(cand, spelling, name=None, probe=False, taken=(), region=None):
    """A registry entry; probes omit `company` so adapters report the API's own name."""
    kind = cand["type"]
    region = regions.normalize_region(region)
    canada = regions.is_canada(region)
    placeholder = _pretty(cand.get("tenant") if kind == "workday" else spelling)
    name = name or placeholder
    if kind in {"greenhouse", "lever", "ashby"}:
        host = {"greenhouse": "job-boards.greenhouse.io", "ashby": "jobs.ashbyhq.com",
                "lever": "jobs.eu.lever.co" if cand.get("region") == "eu" else "jobs.lever.co"}[kind]
        source = {"type": kind, "board": spelling, "url": f"https://{host}/{spelling}"}
        if cand.get("region") == "eu":
            source["region"] = "eu"
        if kind == "lever":
            source["page_size"] = 100
        base = f"{kind}-{spelling}"
    elif kind == "workday":
        wd = cand["wd"].most_common(1)[0][0] if cand.get("wd") else "wd3"
        tenant = cand["tenant"]
        source = {"type": kind, "tenant": tenant, "site": spelling, "url": f"https://{tenant}.{wd}.myworkdayjobs.com/{spelling}",
                  "keywords": ""}
        if canada:
            # The live Canada facet keeps the board small; other regions are filtered
            # after collection, so no guessed facet value is stored.
            source.update(country="Canada", canada_only=True)
        base = f"workday-{tenant}-{spelling}"
    elif kind == "smartrecruiters":
        source = {"type": kind, "company_id": spelling, "url": f"https://jobs.smartrecruiters.com/{spelling}"}
        if canada:
            source["canada_only"] = True
        base = f"smartrecruiters-{spelling}"
    else:
        url = {"workable": f"https://apply.workable.com/{spelling}/", "recruitee": f"https://{spelling}.recruitee.com/",
               "bamboohr": f"https://{spelling}.bamboohr.com/careers"}[kind]
        source = {"type": kind, "board": spelling, "url": url}
        if canada:
            source["canada_only"] = True
        base = f"{kind}-{spelling}"
    source.update(id=_unique_id(base, taken), name=name, coverage_note=NOTES[kind])
    if not canada:
        place = "worldwide" if regions.is_global(region) else region["label"]
        note = source["coverage_note"].replace("filtered by its live Canada facet; ", "")
        note = note.replace("postings in Canada", f"postings in {place}")
        note = note.replace("Canadian technical titles", f"technical titles in {place}")
        source["coverage_note"] = note.replace("Canadian", place).replace("Canada", place)
    if not probe:
        source["company"] = name
    source.update((PROBE_CAPS if probe else CAPS).get(kind, {}))
    return source


def _in_region(row, region):
    return regions.matches(row.get("location"), region, row.get("country_hint"))


def _error_status(error):
    if re.search(r"\b(?:HTTP|HTTP error) (?:404|410)\b", error):
        return "not_found"
    if "lacks requested values" in error:
        return "no_canada_tech"
    if re.search(r"facet .* unavailable", error):
        return "no_country_facet"
    return "error"


def _page_title(url):
    """Display name from a Lever/Ashby board page <title> (their APIs carry no company name)."""
    import html

    from . import extras
    try:
        text = extras._request("GET", url).text
    except Exception:  # A missing display name only falls back to lead names.
        return None
    match = re.search(r"<title[^>]*>([^<]{1,160})</title>", text, re.I)
    title = html.unescape(match.group(1)).strip().split(" | ")[0].strip() if match else ""
    title = re.sub(r"\s+(?:jobs|careers)$", "", title, flags=re.I)
    if not title or re.search(r"^(?:jobs|careers|home)$|not found|404|error", title, re.I):
        return None
    return _clean_name(title)


def _authoritative_name(cand, spelling, rows, placeholder):
    names = Counter(str(r.get("company") or "") for r in rows if r.get("company") and r.get("company") != placeholder)
    if names:
        return _clean_name(names.most_common(1)[0][0])
    if cand["type"] in {"lever", "ashby"}:
        return _page_title(build_source(cand, spelling, probe=True)["url"])
    if cand["type"] == "greenhouse":
        try:
            from . import extras
            data = extras._json_response("GET", f"https://boards-api.greenhouse.io/v1/boards/{spelling}")
            return _clean_name(data.get("name")) if isinstance(data, dict) else None
        except Exception:  # A missing display name only falls back to lead names.
            return None
    return None


def _excluded_result(cand, recheck, started, note="job aggregator or crowd-work board"):
    return {"key": cand["key"], "type": cand["type"], "spelling": next(iter(cand["spellings"]), None), "status": "excluded",
            "raw": 0, "canada": 0, "tech": 0, "matched": 0, "error": None,
            "partial_note": note, "sample_titles": [], "checked_at": _now().isoformat(),
            "seconds": round(time.monotonic() - started, 2), "recheck_id": recheck.get("id") if recheck else None}


def probe_candidate(cand, fetcher, region=None, levels=None):
    from .profile import includes_all_levels
    region = regions.normalize_region(region)
    all_levels = includes_all_levels(levels)
    started = time.monotonic()
    recheck = cand.get("recheck")
    if _DENIED.search(" ".join([cand["key"], *cand["names"], str((recheck or {}).get("name") or "")])):
        return _excluded_result(cand, recheck, started)
    if recheck:
        probe_source = dict(recheck, **PROBE_CAPS.get(recheck["type"], {}))
        probe_source = regions.source_filters(probe_source, region)
        probe_source["search_levels"] = list(levels or core.EARLY_LEVELS)
        attempts = [(next(iter(cand["spellings"]), None), probe_source)]
    else:
        attempts = []
        for spelling, _ in cand["spellings"].most_common(3):
            probe_source = build_source(cand, spelling, probe=True, region=region)
            probe_source = regions.source_filters(probe_source, region)
            probe_source["search_levels"] = list(levels or core.EARLY_LEVELS)
            attempts.append((spelling, probe_source))
    rows, error, partial, spelling, source = [], None, False, None, None
    for spelling, source in attempts:
        try:
            rows, error, partial = fetcher(source), None, False
        except Exception as exc:  # A probe records any adapter failure as evidence.
            rows = list(getattr(exc, "partial_jobs", None) or [])
            error = f"{type(exc).__name__}: {exc}"[:400]
            partial = getattr(exc, "status", None) == "partial"
        if not (error and not partial and _error_status(error) == "not_found"):
            break
    rows = [row for row in rows if isinstance(row, dict)]
    fake = sum(bool(_TEST_POSTING.search(str(row.get("title") or ""))) for row in rows)
    if len(rows) >= 3 and fake * 2 >= len(rows):
        return _excluded_result(cand, recheck, started, note="ATS test/sandbox tenant")
    canada = [row for row in rows if _in_region(row, region)]
    # Relevant = a regional title that itself names technical work and that the core could
    # still accept (not senior/manager-only unless all levels are enabled; not "Associate"/
    # "Analyst"-only retail or bank titles).
    tech = [row for row in canada if core.technical_title(str(row.get("title") or ""), all_levels)]
    matched = 0
    for row in canada:
        try:
            job = core.normalize_job(row, region=region, levels=levels)
        except Exception:  # A malformed row must not hide the rest of the board.
            job = None
        matched += bool(job and job.get("active"))
    healthy = error is None or partial
    # A board qualifies when a regional title names technical work, or when the production
    # rules already accept one of its regional postings (e.g. "Analyst" + a CS description).
    status = "verified" if (tech or matched) and healthy else "no_canada_tech" if healthy else _error_status(error)
    result = {"key": cand["key"], "type": cand["type"], "spelling": spelling, "status": status, "raw": len(rows),
              "canada": len(canada), "tech": len(tech), "matched": matched, "error": None if healthy else error,
              "partial_note": error if healthy and error else None,
              "sample_titles": [str(r.get("title"))[:90] for r in tech[:5]], "checked_at": _now().isoformat(),
              "seconds": round(time.monotonic() - started, 2), "recheck_id": recheck.get("id") if recheck else None}
    if status == "verified" and not recheck:
        api_name = _authoritative_name(cand, spelling, rows, source["name"])
        leads = [n for n, _ in cand["names"].most_common() if n.lower() != str(spelling).lower()]
        result["name"] = api_name or (leads[0] if leads else source["name"])
        if _DENIED.search(result["name"]):
            return _excluded_result(cand, recheck, started)
    return result


# --- orchestration ---------------------------------------------------------------------

def _fresh(entry, now):
    """Recently checked; repeat non-qualifying outcomes back off (x2 each time, at most x8)."""
    checked = _parse_time(entry.get("checked_at"))
    days = RECHECK_DAYS.get(entry.get("status"), 7)
    if entry.get("status") != "verified":
        days *= min(8, 2 ** max(0, int(entry.get("failures") or 1) - 1))
    return bool(checked and now - checked < timedelta(days=days))


def _failed_result(cand, exc):
    recheck = cand.get("recheck") or {}
    return {"key": cand["key"], "type": cand["type"], "spelling": None, "status": "error", "raw": 0, "canada": 0,
            "tech": 0, "matched": 0, "error": f"probe failure: {type(exc).__name__}: {exc}"[:400], "partial_note": None,
            "sample_titles": [], "checked_at": _now().isoformat(), "seconds": 0.0, "recheck_id": recheck.get("id")}


def _curated(root):
    """Boards discovery treats as already monitored: built-in and user-added."""
    return registry.curated_sources(root)


def _db_failing(db):
    """Discovered boards whose last collection failed; they are re-probed every run."""
    if not db or not Path(db).is_file():
        return set()
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = [json.loads(data) for (data,) in connection.execute("SELECT data FROM sources")]
    except (sqlite3.Error, ValueError):
        return set()
    finally:
        connection.close()
    return {r.get("id") for r in rows if r.get("status") in {"error", "blocked"}}


def _recheck_candidate(source):
    ident = str(source.get("board") or source.get("company_id") or source.get("site") or "")
    wd = re.search(r"\.(wd\d+)\.myworkdayjobs\.com$", urlsplit(str(source.get("url") or "")).hostname or "")
    return {"key": registry_key(source), "type": source["type"], "spellings": Counter({ident: 1} if ident else {}),
            "wd": Counter({wd.group(1): 1} if wd else {}), "names": Counter({source["name"]: 1} if source.get("name") else {}),
            "origins": Counter({"recheck": 1}), "evidence": source.get("url"), "canada": True,
            "tenant": source.get("tenant"), "region": source.get("region"), "recheck": source}


def _snapshot(cand):
    return {"type": cand["type"], "spellings": [s for s, _ in cand["spellings"].most_common(3)],
            "wd": cand["wd"].most_common(1)[0][0] if cand["wd"] else None, "tenant": cand.get("tenant"),
            "region": cand.get("region"), "names": [n for n, _ in cand["names"].most_common(3)],
            "evidence": cand.get("evidence"), "canada": bool(cand.get("canada"))}


def _from_snapshot(key, snap):
    return {"key": key, "type": snap["type"], "spellings": Counter({s: 3 - i for i, s in enumerate(snap["spellings"])}),
            "wd": Counter({snap["wd"]: 1}) if snap.get("wd") else Counter(),
            "names": Counter({n: 3 - i for i, n in enumerate(snap.get("names") or [])}),
            "origins": Counter({"cache": 1}), "evidence": snap.get("evidence"), "canada": snap.get("canada", False),
            "tenant": snap.get("tenant"), "region": snap.get("region")}


def _score(cand):
    origins = cand["origins"]
    return (bool(cand["canada"]), "board-db" in origins, sum(origins.values()), len(cand["names"]))


def run(leads=(), db=None, root=None, fetcher=None, workers=WORKERS, max_seconds=None, limit=None, log=sys.stderr,
        reverify=False, region=None, levels=None):
    root = Path(root or config.data_dir())
    if region is None:
        from . import settings
        region = settings.load(root)["region"]
    if levels is None:
        from .profile import load_profile
        levels = load_profile(root)["levels"]
    state = config.discovery_dir(root)
    state.mkdir(parents=True, exist_ok=True)
    lock = (state / "discover.lock").open("a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return {"ok": True, "skipped": True, "reason": "Another discovery run is in progress."}
    try:
        return _run(leads, db, root, state, fetcher or collect.fetch_source, workers, max_seconds, limit, log, reverify,
                    region, levels)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def _run(leads, db, root, state, fetcher, workers, max_seconds, limit, log, reverify=False, region=None, levels=None):
    region = regions.normalize_region(region)
    levels = tuple(levels or core.EARLY_LEVELS)
    started_wall, started = _now(), time.monotonic()
    deadline = started + max_seconds if max_seconds is not None else None
    cache = _load_json(state / "probe-cache.json", {})
    entries = cache.get("candidates", {}) if isinstance(cache, dict) else {}
    registry_path = root / collect.DISCOVERED_FILE
    document = _load_json(registry_path, {})
    discovered = [s for s in (document.get("sources", []) if isinstance(document, dict) else [])
                  if isinstance(s, dict) and s.get("id") and s.get("type") in NOTES]
    curated = _curated(root)
    # Boards the user removed count as known, so discovery never re-adds them.
    known = {registry_key(s) for s in curated} | registry.removed_keys(root)
    discovered = [s for s in discovered if registry_key(s) not in known]
    monitored = {registry_key(s) for s in discovered}
    taken = {s["id"] for s in curated} | {s["id"] for s in discovered}

    found = {}
    unsupported = _load_json(state / "unsupported-leads.json", {})
    unsupported = unsupported if isinstance(unsupported, dict) else {}
    files = harvest_files(leads, found, unsupported)
    db_links = harvest_db(db, found)
    now = _now()
    counts = Counter(harvested=len(found), lead_files=files, db_links=db_links)
    todo = []
    for key, cand in found.items():
        if key in known or key in monitored:
            counts["already_monitored"] += 1
        elif key in entries and _fresh(entries[key], now):
            counts["recently_checked"] += 1
        else:
            todo.append(cand)
    for key, entry in entries.items():  # Due re-checks whose lead file is gone.
        if key not in found and key not in known and key not in monitored and entry.get("candidate") \
                and entry.get("status") != "verified" and not _fresh(entry, now):
            todo.append(_from_snapshot(key, entry["candidate"]))
            counts["requeued_from_cache"] += 1
    todo.sort(key=_score, reverse=True)
    failing = _db_failing(db)
    # Failing boards are re-probed every run; every other discovered board once per
    # RECHECK_DAYS["verified"] (all of them with reverify), so boards that stop listing
    # Canadian technical roles, or no longer meet the current rules, leave the registry.
    stale = [s for s in discovered if s["id"] not in failing
             and (reverify or not _fresh({"status": "verified", "checked_at": s.get("verified_at")}, now))]
    rechecks = [_recheck_candidate(s) for s in discovered if s["id"] in failing] + [_recheck_candidate(s) for s in stale]
    queue = rechecks + (todo[:limit] if limit else todo)
    counts["candidates_due"] = len(todo)
    counts["reverified"] = len(stale)
    results, added, dropped, statuses = [], [], [], Counter()
    refreshed = 0
    by_id = {s["id"]: s for s in discovered}

    def drop(source, cand, result, failures):
        dropped.append(source)
        discovered.remove(source)
        del by_id[source["id"]]
        # Remembered with a snapshot, so the board is re-probed after the usual back-off.
        entries[registry_key(source)] = {**{k: result[k] for k in _ENTRY_KEYS}, "failures": failures,
                                         "name": source.get("name"), "dropped_source_id": source["id"],
                                         "candidate": _snapshot(cand)}

    def apply(cand, result):
        nonlocal refreshed
        if result["recheck_id"]:
            source = by_id.get(result["recheck_id"])
            if source is None:
                return
            source["last_recheck"] = {"status": result["status"], "checked_at": result["checked_at"], "error": result["error"]}
            if result["status"] == "verified":
                source["recheck_failures"] = 0
                refreshed += 1
                source.update(verified_at=result["checked_at"], probe_canada_tech_count=result["tech"],
                              probe_matched_count=result["matched"], probe_sample_titles=result["sample_titles"])
                return
            if result["status"] in {"no_canada_tech", "excluded"}:
                drop(source, cand, result, failures=1)  # Healthy but irrelevant now; re-probed after back-off.
                return
            source["recheck_failures"] = int(source.get("recheck_failures", 0)) + 1
            if source["recheck_failures"] >= DROP_AFTER:
                drop(source, cand, result, failures=source["recheck_failures"])
            return
        statuses[result["status"]] += 1
        entry = entries.get(result["key"], {})
        entries[result["key"]] = {**{k: result[k] for k in _ENTRY_KEYS},
                                  "failures": 0 if result["status"] == "verified" else int(entry.get("failures", 0)) + 1,
                                  "name": result.get("name") or entry.get("name"), "candidate": _snapshot(cand)}
        if result["status"] == "verified":
            source = build_source(cand, result["spelling"], result["name"], taken=taken, region=region)
            taken.add(source["id"])
            source.update(discovered=True, discovered_at=result["checked_at"], verified_at=result["checked_at"],
                          probe_raw_count=result["raw"], probe_canada_count=result["canada"],
                          probe_canada_tech_count=result["tech"], probe_matched_count=result["matched"],
                          probe_sample_titles=result["sample_titles"], evidence_url=cand.get("evidence"),
                          lead_origins=[o for o, _ in cand["origins"].most_common(3)])
            discovered.append(source)
            by_id[source["id"]] = source
            added.append(source)

    def save():
        stamp = _now().isoformat()
        ordered = sorted(discovered, key=lambda s: (str(s.get("name", "")).casefold(), s["id"]))
        _write_json(registry_path, {"schema_version": SCHEMA_VERSION, "generated_at": stamp,
                                    "method": ("discover.py: harvested leads and stored board links, verified by live public-API "
                                               "probes with the production adapters; kept when a current regional technical "
                                               "posting exists."),
                                    "sources": ordered})
        _write_json(state / "probe-cache.json", {"schema_version": SCHEMA_VERSION, "updated_at": stamp, "candidates": entries})
        _write_json(state / "unsupported-leads.json", unsupported)

    workers = max(1, min(workers, WORKERS))
    last_save = time.monotonic()
    try:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="discover") as pool:
            pending, iterator = {}, iter(queue)
            while True:
                while len(pending) < workers and (deadline is None or time.monotonic() < deadline):
                    cand = next(iterator, None)
                    if cand is None:
                        break
                    pending[pool.submit(probe_candidate, cand, fetcher, region=region, levels=levels)] = cand
                if not pending:
                    break
                finished, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in finished:
                    cand = pending.pop(future)
                    try:
                        result = future.result()
                    except Exception as exc:  # A probe bug must not lose the rest of the run.
                        result = _failed_result(cand, exc)
                    results.append(result)
                    apply(cand, result)
                    if len(results) % 100 == 0:
                        print(f"[discover] {len(results)}/{len(queue)} probed, {len(added)} added, "
                              f"{round(time.monotonic() - started)}s", file=log, flush=True)
                if time.monotonic() - last_save >= CHECKPOINT_SECONDS:
                    save()  # Progress survives a crash or kill during long runs.
                    last_save = time.monotonic()
    finally:
        save()
    finished = _now()
    summary = {"ok": True, "skipped": False, "started_at": started_wall.isoformat(), "finished_at": finished.isoformat(),
               "duration_seconds": round(time.monotonic() - started, 1), **counts, "probed": len(results),
               "not_probed_time_budget": len(queue) - len(results), "by_status": dict(statuses), "rechecked": len(rechecks),
               "refreshed": refreshed, "dropped": [s["name"] for s in dropped], "added_count": len(added),
               "added_by_type": dict(Counter(s["type"] for s in added)),
               "added": [{"name": s["name"], "type": s["type"], "canada_tech": s["probe_canada_tech_count"],
                          "matched_now": s["probe_matched_count"]} for s in sorted(added, key=lambda s: -s["probe_canada_tech_count"])],
               "discovered_total": len(discovered), "curated_total": len(curated), "unsupported_leads": len(unsupported)}
    runs = state / "runs"
    _write_json(runs / f"{finished.strftime('%Y%m%dT%H%M%S%fZ')}.json", summary | {"results": results})
    for old in sorted(runs.glob("*.json"))[:-60]:
        old.unlink()
    # Probed leads live on in the probe cache (with re-check snapshots); old scout files can go.
    cutoff = time.time() - LEAD_RETENTION_DAYS * 86400
    for lead in config.leads_dir(root).glob("scout-*"):
        if lead.is_file() and lead.stat().st_mtime < cutoff:
            lead.unlink()
    return summary


def _regional_focus(region):
    """Scout themes for any region; Canada keeps its curated, city-specific list."""
    if regions.is_canada(region):
        return FOCUS
    place = "worldwide" if regions.is_global(region) else regions.label(region)
    return [
        f"Technology employers headquartered in {place} with public job boards (Greenhouse, Lever, Ashby, Workday, SmartRecruiters, Workable, Recruitee or BambooHR).",
        f"Software, data, AI/ML, cloud and infrastructure companies hiring in {place}.",
        f"Embedded, firmware, robotics, semiconductor and hardware employers in {place}.",
        f"Fintech, banking, insurance, payments and investment firms with technology teams in {place}.",
        f"Telecom, networking, cybersecurity and enterprise software companies with offices in {place}.",
        f"Health tech, climate, energy, aerospace and deep-tech companies in {place}.",
        f"Consulting, IT services, e-commerce, retail, logistics and supply-chain technology employers in {place}.",
        f"Big tech and international software companies with engineering offices in {place}, including internships, new-grad and experienced roles.",
        f"ATS sweep: site:job-boards.greenhouse.io, site:jobs.lever.co and site:jobs.ashbyhq.com searches combining {place} locations with software, data, embedded, robotics, intern, graduate and senior engineer.",
        f"ATS sweep: site:myworkdayjobs.com, site:jobs.smartrecruiters.com, site:apply.workable.com, site:bamboohr.com and site:recruitee.com searches combining {place} locations with student, graduate, engineer and developer.",
    ]


def focus(count=4, root=None, region=None):
    """Next research themes for scout subagents, plus the list of boards to skip."""
    root = Path(root or config.data_dir())
    if region is None:
        from . import settings
        region = settings.load(root)["region"]
    region = regions.normalize_region(region)
    themes_pool = _regional_focus(region)
    state = config.discovery_dir(root)
    state.mkdir(parents=True, exist_ok=True)
    rotation = _load_json(state / "focus-state.json", {})
    start = int(rotation.get("next", 0)) if isinstance(rotation, dict) else 0
    start %= len(themes_pool)
    themes = [themes_pool[(start + i) % len(themes_pool)] for i in range(count)]
    _write_json(state / "focus-state.json", {"next": (start + count) % len(themes_pool), "updated_at": _now().isoformat()})
    document = _load_json(root / collect.DISCOVERED_FILE, {})
    sources = _curated(root) + (document.get("sources", []) if isinstance(document, dict) else [])
    entries = _load_json(state / "probe-cache.json", {}).get("candidates", {})
    lines = sorted({s.get("url", "") for s in sources if s.get("url")} | {registry_key(s) for s in sources} | set(entries))
    # Companies already known to use unsupported systems (UKG, iCIMS, Oracle, custom pages), so scouts
    # spend their searches on new employers instead of re-reporting these.
    unsupported = _load_json(state / "unsupported-leads.json", {})
    unsupported = [v for v in unsupported.values() if isinstance(v, dict)] if isinstance(unsupported, dict) else []
    lines += sorted(f"unsupported system: {v.get('company')} {v.get('board_url')}" for v in unsupported)
    (state / "known-boards.txt").write_text("\n".join(lines) + "\n")
    leads = config.leads_dir(root)
    leads.mkdir(parents=True, exist_ok=True)
    return {"themes": themes, "known_boards_file": str(state / "known-boards.txt"), "leads_dir": str(leads),
            "monitored_sources": len(sources), "candidates_already_checked": len(entries),
            "known_unsupported_companies": len(unsupported), "region": region, "region_label": region["label"],
            "today": _now().astimezone(core.TORONTO).date().isoformat()}


def status(root=None):
    root = Path(root or config.data_dir())
    document = _load_json(root / collect.DISCOVERED_FILE, {})
    entries = _load_json(config.discovery_dir(root) / "probe-cache.json", {}).get("candidates", {})
    sources = document.get("sources", []) if isinstance(document, dict) else []
    return {"discovered_sources": len(sources), "by_type": dict(Counter(s.get("type") for s in sources)),
            "curated_sources": len(_curated(root)), "candidates_checked": len(entries),
            "by_status": dict(Counter(v.get("status") for v in entries.values()))}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    go = sub.add_parser("run", help="harvest leads, probe candidates, update discovered_sources.json")
    go.add_argument("--leads", action="append", default=None, help="lead file or folder (default: data/leads)")
    go.add_argument("--db", default=None)
    go.add_argument("--no-db", action="store_true")
    go.add_argument("--max-seconds", type=float, default=None)
    go.add_argument("--limit", type=int, default=None)
    go.add_argument("--workers", type=int, default=WORKERS)
    go.add_argument("--quiet", action="store_true", help="print only a one-line summary")
    go.add_argument("--reverify", action="store_true", help="re-probe every discovered board now under the current rules")
    nxt = sub.add_parser("focus", help="next scout themes and the known-board list")
    nxt.add_argument("--count", type=int, default=4)
    sub.add_parser("status")
    args = parser.parse_args(argv)
    if args.command == "run":
        leads = args.leads or [str(config.leads_dir())]
        summary = run(leads, None if args.no_db else (args.db or config.db_path()), workers=args.workers, max_seconds=args.max_seconds,
                      limit=args.limit, log=open(os.devnull, "w") if args.quiet else sys.stderr, reverify=args.reverify)
        if args.quiet:
            print(f"discovery: probed {summary.get('probed', 0)}, added {summary.get('added_count', 0)}, "
                  f"dropped {len(summary.get('dropped', []))}, discovered total {summary.get('discovered_total', 0)}"
                  if not summary.get("skipped") else f"discovery skipped: {summary.get('reason')}")
        else:
            print(json.dumps(summary, ensure_ascii=False, indent=1))
        return 0 if summary.get("ok") else 2
    if args.command == "focus":
        print(json.dumps(focus(args.count), ensure_ascii=False, indent=1))
        return 0
    print(json.dumps(status(), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
