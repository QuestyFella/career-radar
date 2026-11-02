"""Bounded collection orchestration. Adapters never own persistence or alerts."""
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import timedelta
from pathlib import Path
import fcntl
import json
import os
import time
from . import config, core
from . import registry as _registry
ATS_TYPES = {"greenhouse", "lever", "ashby"}
DISCOVERED_FILE = config.DISCOVERED_FILE
source_identity = _registry.source_identity


def load_registry(data=None):
    """Built-in, user and discovered sources with the user's enable/remove choices applied."""
    return _registry.load_registry(data)


def fetch_source(source):
    if source["type"] in ATS_TYPES:
        from .adapters import fetch_source as fetch
    else:
        from .extras import fetch_source as fetch
    return fetch(source)


def _record(db, source, future, results, new_ids):
    """Persist one finished source fetch and its health; never raises."""
    coverage = str(source.get("coverage") or source.get("coverage_note") or "").lower()
    status = "partial" if source.get("partial") or any(s in coverage for s in ("limited", "sample", "capped", "bounded")) else "ok"
    error = None
    try:
        rows = future.result()
    except Exception as exc:
        # Preserve real rows fetched before a cap or late failure.
        # Success of an individual HTTP page is not full coverage.
        rows = getattr(exc, "partial_jobs", [])
        error = f"{type(exc).__name__}: {exc}"[:1200]
        if getattr(exc, "status", None) == "partial":
            status = "partial"
        else:
            status = "blocked" if any(word in error.lower() for word in ("403", "401", "429", "login", "captcha", "access denied", "challenge")) else "error"
    try:
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            raise ValueError("Adapter returned invalid job list")
        for row in rows:
            row.setdefault("source_id", source["id"])
            row.setdefault("source", source["name"])
            row.setdefault("source_type", source["type"])
        imported = core.upsert_jobs(db, rows)
        new_ids.update(imported["new_ids"])
        if status == "ok" and source["type"] in ATS_TYPES:
            identifiers = {str(row.get("source_job_id") or core.canonical_url(row.get("url")) or "") for row in rows}
            core.reconcile_source(db, source["id"], identifiers)
        matched_active = sum(bool((j := core.normalize_job(row)) and j["active"]) for row in rows)
        core.record_source(db, source, status=status, raw_count=len(rows), matched_count=matched_active, error=error)
        results.append({"id": source["id"], "name": source["name"], "status": status,
                        "raw": len(rows), "matched": matched_active, "new": imported["new"], "error": error})
    except Exception as exc:
        error = f"Persistence/schema error: {type(exc).__name__}: {exc}"[:1200]
        core.record_source(db, source, status="error", error=error)
        results.append({"id": source["id"], "name": source["name"], "status": "error", "raw": 0, "matched": 0, "error": error})


def run_collection(db=None, registry=None, fetcher=None, workers=6, max_seconds=None):
    """Collect every source. With max_seconds, sources not yet started when the budget runs
    out are skipped as "deferred": their existing jobs stay untouched (no reconciliation),
    and the least recently checked sources go first next time, so all sources rotate."""
    db = Path(db or config.db_path())
    db.parent.mkdir(parents=True, exist_ok=True)
    full_registry = registry is None  # only a full-registry run may retire removed sources
    registry = load_registry() if registry is None else registry
    seen = set()
    for source in registry:
        if not isinstance(source, dict) or not all(source.get(k) for k in ("id", "name", "type", "url")):
            raise ValueError("Every source needs id, name, type, url")
        if source["id"] in seen:
            raise ValueError(f"Duplicate source id: {source['id']}")
        seen.add(source["id"])
    fetcher = fetcher or fetch_source
    lock_file = db.with_suffix(".collect.lock").open("a+")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        return {"ok": True, "skipped": True, "reason": "A collection is already running."}
    started = core.utcnow()
    core.set_meta(db, "collection_running", True)
    core.set_meta(db, "collection_started", core.iso(started))
    results, new_ids = [], set()
    previous = {s["id"]: s for s in core.sources(db)}
    try:
        workers = min(6, max(1, workers))
        deadline = time.monotonic() + max_seconds if max_seconds is not None else None
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="job-source") as pool:
            due = []
            for source in registry:
                if source.get("enabled") is False:
                    core.record_source(db, source, status="disabled")
                    results.append({"id": source["id"], "name": source["name"], "status": "disabled", "error": None, "raw": 0, "matched": 0})
                else:
                    prior = previous.get(source["id"], {})
                    checked = core.parsed_date(prior.get("last_checked"))
                    minimum = max(0, float(source.get("min_interval_seconds", 0)))
                    if checked and minimum and (started - checked).total_seconds() < minimum:
                        # Keep the genuine last_checked timestamp and existing rows.
                        healthy = prior.get("status") in {"ok", "partial"}
                        results.append({"id": source["id"], "name": source["name"],
                                        "status": "cached" if healthy else "error", "cached": True,
                                        "error": prior.get("error") if not healthy else None,
                                        "raw": 0, "matched": 0, "last_checked": prior["last_checked"]})
                    else:
                        due.append(source)
            if deadline is not None:
                # Never-checked sources first, then the stalest, so a budget rotates through all.
                due.sort(key=lambda s: core.parsed_date(previous.get(s["id"], {}).get("last_checked")) or started - timedelta(days=36500))
            queue, pending = iter(due), {}

            def fill():
                while len(pending) < workers * 2 and (deadline is None or time.monotonic() < deadline):
                    source = next(queue, None)
                    if source is None:
                        return
                    pending[pool.submit(fetcher, source)] = source

            fill()
            while pending:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    _record(db, pending.pop(future), future, results, new_ids)
                fill()
            for source in queue:  # Budget exhausted before these started.
                prior = previous.get(source["id"], {})
                results.append({"id": source["id"], "name": source["name"], "status": "deferred", "deferred": True,
                                "error": None, "raw": 0, "matched": 0, "last_checked": prior.get("last_checked")})
        core.expire_stale(db)
        retired = core.retire_sources(db, {s["id"] for s in registry}) if full_registry else []
        finished = core.utcnow()
        good = sum(r["status"] in {"ok", "partial", "cached"} for r in results)
        active_new = [j["id"] for j in core.all_jobs(db) if j["id"] in new_ids and j["active"]]
        report = {"ok": good > 0 or not registry, "skipped": False, "started_at": core.iso(started),
                  "finished_at": core.iso(finished), "duration_seconds": round((finished - started).total_seconds(), 2),
                  "sources_total": len(results), "sources_ok": good, "sources_failed": sum(r["status"] in {"error", "blocked"} for r in results),
                  "sources_disabled": sum(r["status"] == "disabled" for r in results),
                  "sources_cached": sum(bool(r.get("cached")) for r in results),
                  "sources_deferred": sum(bool(r.get("deferred")) for r in results),
                  "sources_retired": retired,
                  "raw_jobs": sum(r["raw"] for r in results), "matched_rows": sum(r["matched"] for r in results),
                  "new_jobs": len(active_new), "new_ids": sorted(active_new), "sources": sorted(results, key=lambda x: x["id"])}
        core.set_meta(db, "last_collection", report)
        if good:
            core.set_meta(db, "last_refresh", core.iso(finished))
        directory = db.parent / "runs"
        directory.mkdir(exist_ok=True)
        path = directory / f"{finished.strftime('%Y%m%dT%H%M%S%fZ')}.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        for old in sorted(directory.glob("*.json"))[:-120]:  # keep about two months of twice-daily runs
            old.unlink(missing_ok=True)
        report["report_path"] = str(path)
        return report
    finally:
        core.set_meta(db, "collection_running", False)
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()


if __name__ == "__main__":
    report = run_collection(db=config.db_path())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["ok"] else 2)
