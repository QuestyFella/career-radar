"""User-facing board checks: turn pasted careers-board URLs into verified registry entries."""
from __future__ import annotations

from collections import Counter

from . import collect, discover, registry

MAX_URLS = 10


def candidate_for(url):
    """The board identity discovery would harvest from ``url``, or None when unsupported."""
    hits = discover.parse_urls(url)
    if not hits:
        return None
    hit = hits[0]
    cand = discover._new_candidate(hit)  # same shape the discovery probe expects
    cand["spellings"][hit["id"]] += 1
    if hit.get("wd"):
        cand["wd"][hit["wd"]] += 1
    cand["origins"]["user"] += 1
    cand["canada"] = True
    return cand


def _monitored_index(data=None):
    return {discover.registry_key(s): s for s in registry.load_registry(data)}


def check(urls, *, data=None, fetcher=None):
    """Live read-only probe of each supported URL (at most ``MAX_URLS``, four at a time)."""
    from concurrent.futures import ThreadPoolExecutor
    urls = [str(u).strip() for u in urls if str(u).strip()][:MAX_URLS]
    monitored = _monitored_index(data)
    candidates, unsupported, seen, todo = [], [], set(), []
    for url in urls:
        cand = candidate_for(url)
        if cand is None:
            unsupported.append(url)
            continue
        if cand["key"] in seen:
            continue
        seen.add(cand["key"])
        spelling = next(iter(cand["spellings"]))
        preview = discover.build_source(cand, spelling, probe=True)
        row = {"key": cand["key"], "type": cand["type"], "name": preview["name"], "url": preview["url"],
               "status": None, "raw": 0, "canada": 0, "tech": 0, "matched": 0, "sample_titles": [],
               "error": None, "monitored_id": None}
        known = monitored.get(cand["key"])
        if known:
            row.update(status="already_monitored", name=known.get("name"), monitored_id=known.get("id"))
        else:
            todo.append((cand, row))
        candidates.append(row)

    def probe(item):
        cand, row = item
        try:
            result = discover.probe_candidate(cand, fetcher or collect.fetch_source)
        except Exception as exc:  # one broken board must not fail the whole check
            result = {"status": "error", "error": str(exc)[:300]}
        row.update({k: result.get(k, row[k]) for k in ("status", "raw", "canada", "tech", "matched", "sample_titles", "error")})
        if result.get("name"):
            row["name"] = result["name"]

    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="board-check") as pool:
        list(pool.map(probe, todo))
    return {"candidates": candidates, "unsupported": unsupported}


def board_source(url, name=None, *, data=None):
    """A complete user registry entry for a supported board URL (no network)."""
    cand = candidate_for(url)
    if cand is None:
        raise registry.RegistryError(
            "That is not a supported job-board URL. Supported: Greenhouse, Lever, Ashby, Workday, "
            "SmartRecruiters, Workable, Recruitee and BambooHR board links.")
    spelling = next(iter(cand["spellings"]))
    taken = {s.get("id") for s in registry.load_registry(data)}
    clean = str(name or "").strip()[:120] or None
    source = discover.build_source(cand, spelling, clean, taken=taken)
    source["coverage_note"] = source["coverage_note"].replace(
        "Added by Career Radar source discovery after a live public-API check.", "Added by you.")
    source["discovery_key"] = cand["key"]
    return source


def name_counts(rows):
    return Counter(r.get("type") for r in rows)
