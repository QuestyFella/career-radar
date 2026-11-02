"""Career Radar web application: job board API, profile, sources, discovery, settings and UI."""
from __future__ import annotations

import base64
import csv
import hmac
import io
import logging
import os
import threading
import time
from collections import Counter
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import ClientDisconnect

from . import __version__, config, core

log = logging.getLogger("career-radar")
MAX_REQUEST_BODY = 8192
MAX_JSON_BODY = 64 * 1024          # profile/settings bodies
MAX_PDF_BODY = 8 * 1024 * 1024     # resume upload
LARGE_JSON_PATHS = {"/api/profile", "/api/settings", "/api/sources/check", "/api/sources", "/api/settings/secrets"}
UPLOAD_PATHS = {"/api/profile/resume"}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


class StatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["new", "saved", "applied", "dismissed"]


class SourceCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    urls: list[str] = Field(min_length=1, max_length=10)


class SourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str | None = Field(None, max_length=500)
    type: str | None = Field(None, max_length=40)
    keywords: str | None = Field(None, max_length=200)
    name: str | None = Field(None, max_length=200)


class SourceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool | None = None
    name: str | None = Field(None, max_length=200)
    keywords: str | None = Field(None, max_length=200)


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    llm: dict | None = None
    schedule: dict | None = None
    region: dict | None = None


class SecretUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str = Field(max_length=40)
    api_key: str | None = Field(None, max_length=600)


def _job_is_active(job, now):
    """Project deadline expiry without changing persisted job/user state."""
    deadline = core.parsed_date(job.get("deadline"))
    return bool(job["active"] and (deadline is None or deadline >= now))


def _body_limit(path):
    if path in UPLOAD_PATHS:
        return MAX_PDF_BODY
    if path in LARGE_JSON_PATHS:
        return MAX_JSON_BODY
    return MAX_REQUEST_BODY


def _authorized(request, password):
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("basic "):
        return False
    try:
        _, _, supplied = base64.b64decode(header[6:].strip()).decode("utf-8").partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    return hmac.compare_digest(supplied.encode(), password.encode())


def _host_checker(setting):
    """Allow IP-literal hosts, localhost and configured names; blocks DNS-rebinding attacks.

    A rebinding attack reaches the app through the attacker's domain name, so accepting any IP
    address (how most people open a self-hosted app on their LAN) stays safe. Names such as
    ``radar.lan`` must be listed in ``CAREER_RADAR_ALLOWED_HOSTS`` ("*" disables the check)."""
    import ipaddress
    names = [h.strip().lower() for h in (setting or "").split(",") if h.strip()]
    if "*" in names:
        return lambda host: True

    def allowed(host):
        host = (host or "").strip().lower()
        if host.startswith("["):  # [IPv6]:port
            name = host[1:host.find("]")] if "]" in host else host
        else:
            name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        if not name:
            return False
        try:
            ipaddress.ip_address(name)
            return True
        except ValueError:
            pass
        if name in {"localhost", "testserver"} or name.endswith(".localhost"):
            return True
        return any(name == n or (n.startswith("*.") and name.endswith(n[1:])) for n in names)
    return allowed


def _install_middleware(http, password, host_allowed):
    @http.middleware("http")
    async def safety_headers(request: Request, call_next):
        if not host_allowed(request.headers.get("host")):
            return JSONResponse({"detail": "Invalid host header. Add this host name to CAREER_RADAR_ALLOWED_HOSTS."},
                                status_code=400)
        if password and not _authorized(request, password):
            return JSONResponse({"detail": "Authentication required"}, status_code=401,
                                headers={"WWW-Authenticate": 'Basic realm="Career Radar", charset="UTF-8"'})
        if request.method in {"POST", "PATCH", "PUT", "DELETE"}:
            kind = request.headers.get("content-type", "").split(";")[0].strip().lower()
            upload = request.method == "POST" and request.url.path in UPLOAD_PATHS
            wanted = "application/pdf" if upload else "application/json"
            if kind != wanted:
                return JSONResponse({"detail": "PDF uploads only" if wanted == "application/pdf" else "JSON requests only"},
                                    status_code=415)
            origin = request.headers.get("origin")
            if (origin and origin.rstrip("/") != str(request.base_url).rstrip("/")) or request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "Cross-site write blocked"}, status_code=403)
            limit = _body_limit(request.url.path)
            try:
                length = request.headers.get("content-length", "0")
                if not length.isascii() or not length.isdecimal():
                    raise ValueError("Invalid content length")
                size = int(length)
            except ValueError:
                return JSONResponse({"detail": "Invalid content length"}, status_code=400)
            if size > limit:
                return JSONResponse({"detail": "Request too large"}, status_code=413)
            body = bytearray()
            try:
                async for chunk in request.stream():
                    if len(body) + len(chunk) > limit:
                        return JSONResponse({"detail": "Request too large"}, status_code=413)
                    body.extend(chunk)
            except ClientDisconnect:
                return JSONResponse({"detail": "Incomplete request body"}, status_code=400)
            # BaseHTTPMiddleware replays this bounded cache to the JSON parser.
            request._body = bytes(body)
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Frame-Options"] = "DENY"
        return response


def _matcher(data):
    from . import profile as profile_module, settings
    from .ranking import Matcher
    return Matcher(profile_module.load_profile(data), region=settings.load(data)["region"])


def _preferences(data):
    """The region and role levels collection and the feed should honour."""
    from . import profile as profile_module, settings
    return settings.load(data)["region"], profile_module.load_profile(data)["levels"]


def _install_job_routes(http, db, data, refresh_callback):
    def ranked(jobs, matcher, now):
        return [matcher.apply(job) | {"active": _job_is_active(job, now)} for job in jobs]

    @http.get("/api/health")
    def health():
        with core.connection(db) as conn:
            conn.execute("SELECT 1").fetchone()
        return {"ok": True, "service": "career-radar", "version": __version__, "time": core.iso(core.utcnow())}

    @http.get("/api/jobs")
    def jobs(q: str = Query("", max_length=300), level: str = Query("", max_length=40),
             category: str = Query("", max_length=100), location: str = Query("", max_length=300),
             status: str = Query("", max_length=40), sort: Literal["priority", "newest", "deadline"] = "priority",
             deadline_days: int | None = Query(None, ge=0, le=365), page: int = Query(1, ge=1),
             page_size: int = Query(30, ge=1, le=250), priority_only: bool = False, include_closed: bool = False):
        now = core.utcnow()
        matcher = _matcher(data)
        region, levels = _preferences(data)
        # Filter without paging, then rank by the profile, then page: ranking must see every row.
        result = core.list_jobs(db, q=q, level=level, category=category, location=location, status=status,
                                sort="newest" if sort == "priority" else sort, deadline_days=deadline_days,
                                page=1, page_size=250, include_closed=include_closed, now=now, unpaged=True,
                                region=region, levels=levels)
        rows = ranked(result["items"], matcher, now)
        if priority_only:
            rows = [j for j in rows if j["match"]["priority"]]
        if sort == "priority":
            rows.sort(key=lambda j: (j["priority_score"], j.get("posted_at") or j["first_seen"]), reverse=True)
        elif sort == "deadline":
            rows.sort(key=lambda j: (j.get("deadline") or "9999", -j["priority_score"]))
        total = len(rows)
        return {"items": rows[(page - 1) * page_size:page * page_size], "total": total, "page": page,
                "page_size": page_size, "pages": -(-total // page_size)}

    @http.get("/api/jobs/{job_id}")
    def detail(job_id: str):
        job = core.get_job(db, job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        return ranked([job], _matcher(data), core.utcnow())[0]

    @http.patch("/api/jobs/{job_id}")
    def change_status(job_id: str, update: StatusUpdate):
        job = core.set_status(db, job_id, update.status)
        if not job:
            raise HTTPException(404, "Job not found")
        return ranked([job], _matcher(data), core.utcnow())[0]

    @http.get("/api/stats")
    def statistics():
        from . import settings, regions
        matcher = _matcher(data)
        now = core.utcnow()
        region, levels = _preferences(data)
        result = core.stats(db, now)
        active = [j for j in core.all_jobs(db) if j["active"] and j["status"] != "dismissed"
                  and (not levels or j.get("level") in levels)
                  and regions.matches(j.get("location"), region, j.get("country_hint"))
                  and (not j.get("deadline") or core.parsed_date(j["deadline"]) > now)]
        result["priority"] = sum(bool(matcher.score(j)["priority"]) for j in active) if matcher.active else 0
        result["total"] = len(active)
        result["new_today"] = sum(core.parsed_date(j["first_seen"]).astimezone(core.TORONTO).date() == now.astimezone(core.TORONTO).date() for j in active)
        result["closing_soon"] = sum(bool(j.get("deadline") and now < core.parsed_date(j["deadline"]) <= now + timedelta(days=7)) for j in active)
        result["saved"] = sum(j["status"] == "saved" for j in active)
        result["applied"] = sum(j["status"] == "applied" for j in active)
        def counts(key):
            return [{"name": k, "count": v} for k, v in sorted(Counter(j.get(key) for j in active).items())]
        result["categories"] = counts("category")
        result["levels"] = counts("level")
        result["location_counts"] = counts("location")
        result["profile_set"] = bool(matcher.active)
        result["priority_terms"] = list(matcher.terms)
        upcoming = getattr(http.state, "scheduler", None)
        status = upcoming.status() if upcoming else None
        nexts = [j["next_run"] for j in (status or {}).get("jobs", {}).values() if j.get("next_run")]
        collect_next = (status or {}).get("jobs", {}).get("collect", {}).get("next_run")
        result["next_refresh"] = collect_next or (min(nexts) if nexts else None)
        result["timezone"] = settings.timezone_name()
        return result

    @http.get("/api/deadlines")
    def deadlines():
        from . import regions
        now = core.utcnow()
        region, levels = _preferences(data)
        items = [j for j in core.all_jobs(db) if j["active"] and j["status"] not in {"applied", "dismissed"}
                 and (not levels or j.get("level") in levels)
                 and regions.matches(j.get("location"), region, j.get("country_hint"))
                 and j.get("deadline") and now <= core.parsed_date(j["deadline"]) <= now + timedelta(days=30)]
        return {"items": sorted(items, key=lambda j: j["deadline"])}

    @http.get("/api/meta")
    def metadata():
        from . import settings, regions
        current = settings.load(data)
        schedule = current["schedule"]
        region = current["region"]
        _, levels = _preferences(data)
        return {"name": "Career Radar", "version": __version__,
                "scope": "Technical roles at every level, filtered to your region",
                "region": region, "levels": list(levels),
                "timezone": settings.timezone_name(), "collection_times": schedule["collect_times"],
                "application_automation": False,
                "eligibility_note": "Potential matches, not guaranteed eligibility. Confirm requirements, work authorization and graduation dates.",
                "deadline_note": "Only explicit published deadlines/expiry dates. Unknown dates are not guessed."}

    def refresh_worker():
        try:
            if refresh_callback:
                refresh_callback()
            else:
                from . import services
                services.run_collection_job(data, db)
        except Exception as exc:
            log.exception("Manual collection failed")
            core.set_meta(db, "last_manual_error", str(exc)[:300])
        finally:
            http.state.refresh_lock.release()

    @http.post("/api/refresh")
    def refresh():
        if core.stats(db)["collection_running"] or not http.state.refresh_lock.acquire(blocking=False):
            return {"started": False, "message": "A collection is already running."}
        if time.monotonic() - http.state.last_manual_refresh < 300:
            http.state.refresh_lock.release()
            return {"started": False, "message": "Please wait five minutes between manual refreshes."}
        http.state.last_manual_refresh = time.monotonic()
        threading.Thread(target=refresh_worker, daemon=True, name="career-refresh").start()
        return {"started": True, "message": "Source collection started. The board updates automatically."}

    @http.get("/api/export.csv")
    def export_csv():
        fields = ["title", "company", "location", "level", "category", "priority_score", "status", "posted_at",
                  "deadline", "deadline_kind", "url", "verification", "last_seen"]
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        now = core.utcnow()
        matcher = _matcher(data)
        region, levels = _preferences(data)
        from . import regions
        for j in core.all_jobs(db):
            if not _job_is_active(j, now) or j["status"] == "dismissed":
                continue
            if levels and j.get("level") not in levels:
                continue
            if not regions.matches(j.get("location"), region, j.get("country_hint")):
                continue
            j = matcher.apply(j)
            row = {}
            for key in fields:
                value = str(j.get(key) or "")
                row[key] = "'" + value if value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else value
            writer.writerow(row)
        return Response(output.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="career-radar-jobs.csv"'})


def _http_error(exc):
    from . import registry
    status = getattr(exc, "status", 400) if isinstance(exc, registry.RegistryError) else 400
    return HTTPException(status, str(exc))


def _install_profile_routes(http, db, data):
    from . import profile as profile_module

    @http.get("/api/profile")
    def get_profile():
        return {"profile": profile_module.load_profile(data), "resume": profile_module.load_resume(data)}

    @http.put("/api/profile")
    def put_profile(body: dict):
        current = profile_module.load_profile(data)
        known = {k: v for k, v in body.items() if k in profile_module.PROFILE_KEYS and k not in {"updated_at"}}
        merged = current | known
        if "source" not in known and current.get("source") in {"resume", "resume+llm"}:
            merged["source"] = current["source"]
        return {"profile": profile_module.save_profile(data, merged)}

    @http.delete("/api/profile")
    def reset_profile():
        profile_module.delete_profile(data)
        return {"profile": profile_module.load_profile(data), "resume": None}

    @http.post("/api/profile/resume")
    async def upload_resume(request: Request, llm: Literal["auto", "on", "off"] = "auto",
                            filename: str = Query("resume.pdf", max_length=300)):
        from . import services
        payload = await request.body()
        client = None
        if llm != "off":
            try:
                client = services.llm_client(data, required=(llm == "on"))
            except Exception as exc:
                if llm == "on":
                    raise HTTPException(400, str(exc)) from exc
        try:
            import anyio
            return await anyio.to_thread.run_sync(
                lambda: profile_module.process_resume(data, payload, filename, client=client, use_llm=llm))
        except profile_module.ResumeError as exc:
            raise HTTPException(400, str(exc)) from exc

    @http.delete("/api/profile/resume")
    def remove_resume():
        profile_module.delete_resume(data)
        return {"resume": None}


def _source_items(db, data):
    from . import registry
    health = {s["id"]: s for s in core.sources(db)}
    items = []
    for source in registry.load_registry(data):
        checked = health.get(source["id"], {})
        enabled = source.get("enabled", True) is not False
        status = checked.get("status") or "pending"
        if not enabled:
            status = "disabled"
        items.append({"id": source["id"], "name": source.get("name"), "type": source.get("type"), "url": source.get("url"),
                      "origin": source["origin"], "enabled": enabled, "removable": source["origin"] != "builtin",
                      "keywords": source.get("keywords") or "",
                      "coverage": source.get("coverage_note") or checked.get("coverage"),
                      "notes": source.get("disabled_reason") or source.get("maintenance_note") or checked.get("notes"),
                      "status": status, "last_checked": checked.get("last_checked"),
                      "raw_count": checked.get("raw_count", 0), "matched_count": checked.get("matched_count", 0),
                      "error": checked.get("error") if enabled else None})
    return items


def _install_source_routes(http, db, data):
    from collections import Counter
    from . import boards_check, registry

    def one(source_id):
        return next((s for s in _source_items(db, data) if s["id"] == source_id), None)

    @http.get("/api/sources")
    def list_sources():
        items = _source_items(db, data)
        return {"items": items, "last_refresh": core.get_meta(db, "last_refresh"),
                "counts": {"total": len(items), "enabled": sum(s["enabled"] for s in items),
                           "by_origin": dict(Counter(s["origin"] for s in items)),
                           "by_type": dict(Counter(s["type"] for s in items))},
                "types": registry.type_catalog()}

    @http.post("/api/sources/check")
    def check_sources(body: SourceCheck):
        return boards_check.check(body.urls, data=data)

    @http.post("/api/sources", status_code=201)
    def add_source(body: SourceCreate):
        try:
            if body.type in registry.SEARCH_TYPES:
                source = registry.search_source(body.type, body.keywords, body.name)
            elif body.url:
                source = boards_check.board_source(body.url, body.name, data=data)
            else:
                raise registry.RegistryError("Send a board URL, or a search type with keywords.")
            saved = registry.add_user_source(source, data)
        except registry.RegistryError as exc:
            raise _http_error(exc) from exc
        return {"source": one(saved["id"]) or saved}

    @http.patch("/api/sources/{source_id}")
    def edit_source(source_id: str, body: SourceUpdate):
        try:
            registry.update_source(source_id, body.model_dump(exclude_none=True), data)
        except registry.RegistryError as exc:
            raise _http_error(exc) from exc
        return {"source": one(source_id)}

    @http.delete("/api/sources/{source_id}")
    def delete_source(source_id: str):
        try:
            registry.remove_source(source_id, data)
        except registry.RegistryError as exc:
            raise _http_error(exc) from exc
        return {"removed": source_id}


def _install_discovery_routes(http, db, data):
    from . import discover, services

    def scheduler():
        return http.state.scheduler

    @http.get("/api/discovery")
    def discovery_status():
        from . import settings, regions
        result = discover.status(data)
        sched = scheduler()
        result.pop("by_type", None)
        region = settings.load(data)["region"]
        result["region"] = region
        result["region_label"] = regions.label(region)
        result["running"] = bool(sched and sched.is_running("discovery"))
        result["last_run"] = core.get_meta(db, "last_discovery")
        result["scout"] = {"available": services.llm_configured(data),
                           "enabled": bool(http.state.settings_snapshot()["schedule"]["scout_enabled"]),
                           "running": bool(sched and sched.is_running("scout")),
                           "last_run": core.get_meta(db, "last_scout")}
        return result

    @http.post("/api/discovery/run")
    def discovery_run():
        if not scheduler().run_now("discovery"):
            return {"started": False, "message": "Discovery is already running."}
        return {"started": True, "message": "Discovery started. New boards appear under Sources when verified."}

    @http.post("/api/discovery/scout")
    def discovery_scout():
        if not services.llm_configured(data):
            raise HTTPException(400, "Set up an LLM provider in Settings to use the scout.")
        if not scheduler().run_now("scout"):
            return {"started": False, "message": "The scout is already running."}
        return {"started": True, "message": "LLM scout started. Suggested boards are verified before they are added."}


def _settings_payload(http, data):
    from . import llm, regions, services, settings
    current = settings.load(data)
    providers = [{"id": p.id, "label": p.label, "auth": p.auth, "key_env": p.key_env, "default_model": p.default_model,
                  "needs_base_url": p.needs_base_url, "docs_url": p.docs_url, "notes": p.notes}
                 for p in llm.PROVIDERS.values()]
    try:
        codex = services.codex_auth(data).status()
    except Exception as exc:
        codex = {"logged_in": False, "account": None, "expires_at": None, "error": str(exc)[:200]}
    sched = http.state.scheduler
    return {"version": __version__, "timezone": settings.timezone_name(),
            "llm": current["llm"] | {"configured": services.llm_configured(data)},
            "providers": providers, "secrets": settings.secret_status(data), "codex": codex,
            "region": current["region"], "region_options": regions.catalog(),
            "schedule": current["schedule"], "schedule_status": sched.status() if sched else None}


def _install_settings_routes(http, db, data):
    from . import llm, services, settings

    @http.get("/api/settings")
    def get_settings():
        return _settings_payload(http, data)

    @http.put("/api/settings")
    def put_settings(body: SettingsUpdate):
        try:
            settings.save(body.model_dump(exclude_none=True), data)
        except settings.SettingsError as exc:
            raise HTTPException(400, str(exc)) from exc
        return _settings_payload(http, data)

    @http.put("/api/settings/secrets")
    def put_secret(body: SecretUpdate):
        try:
            settings.set_api_key(body.provider, body.api_key, data)
        except settings.SettingsError as exc:
            raise HTTPException(400, str(exc)) from exc
        return _settings_payload(http, data)

    @http.get("/api/llm/models")
    def models(provider: str = Query(..., max_length=40), base_url: str = Query("", max_length=300)):
        spec = llm.PROVIDERS.get(provider)
        if spec is None:
            raise HTTPException(400, "Unknown LLM provider")
        current = settings.load(data)["llm"]
        url = base_url or (current["base_url"] if current["provider"] == provider else "") or None
        try:
            items = llm.list_models(provider, api_key=settings.api_key(provider, data), base_url=url,
                                    codex=services.codex_auth(data) if spec.auth == "oauth" else None)
            return {"items": items, "error": None}
        except Exception as exc:
            return {"items": [], "error": str(exc)[:300]}

    @http.post("/api/llm/test")
    def test_llm(body: dict | None = None):
        current = settings.load(data)["llm"]
        started = time.monotonic()
        result = {"ok": False, "provider": current["provider"] or None, "model": None, "latency_ms": None,
                  "reply": None, "error": None}
        try:
            client = services.llm_client(data, required=True)
            result["model"] = client.model
            # Generous limit: reasoning models spend part of it thinking before they answer.
            reply = client.complete("You are a connection test. Follow the instruction exactly.",
                                    "Reply with exactly: Career Radar OK", max_tokens=2000)
            result.update(ok=True, reply=reply.strip()[:200])
        except Exception as exc:
            result["error"] = str(exc)[:300]
        result["latency_ms"] = round((time.monotonic() - started) * 1000)
        return result

    @http.post("/api/llm/codex/login")
    def codex_login(body: dict | None = None):
        return services.codex_auth(data).start_login()

    @http.get("/api/llm/codex/login")
    def codex_login_state():
        return services.codex_auth(data).login_state()

    @http.delete("/api/llm/codex")
    def codex_logout():
        services.codex_auth(data).logout()
        return _settings_payload(http, data)


def _build_scheduler(http, db, data):
    from . import scheduler, services, settings

    def plan():
        schedule = settings.load(data)["schedule"]
        if not schedule["enabled"]:
            return {}
        return {"collect": schedule["collect_times"], "discovery": [schedule["discovery_time"]],
                "scout": [schedule["scout_time"]] if schedule["scout_enabled"] and services.llm_configured(data) else []}

    def record(name, result):
        core.set_meta(db, f"schedule_last_{name}", result)

    def collect_job():
        # Manual refreshes and scheduled runs share one lock so they never overlap.
        if not http.state.refresh_lock.acquire(blocking=False):
            return True, "skipped: a collection is already running"
        try:
            return services.run_collection_job(data, db)
        finally:
            http.state.refresh_lock.release()

    jobs = {"collect": collect_job, "discovery": lambda: services.run_discovery_job(data, db),
            "scout": lambda: services.run_scout_job(data, db)}
    sched = scheduler.Scheduler(jobs, plan, tz=settings.timezone_name(), on_result=record)
    for name in jobs:
        last = core.get_meta(db, f"schedule_last_{name}")
        if isinstance(last, dict):
            sched.remember(name, last)
    return sched


def create_app(db_path=None, refresh_callback=None, data_dir=None, start_scheduler=None):
    from . import settings
    data = Path(data_dir) if data_dir else (Path(db_path).parent if db_path and not os.environ.get("CAREER_RADAR_DATA")
                                            else config.data_dir())
    data.mkdir(parents=True, exist_ok=True)
    config.seed_data_dir(data)
    db = Path(db_path) if db_path else config.db_path()
    core.init_db(db)
    http = FastAPI(title="Career Radar", version=__version__, docs_url=None, redoc_url=None)
    http.state.db, http.state.data = db, data
    http.state.refresh_lock = threading.Lock()
    http.state.last_manual_refresh = 0.0
    http.state.settings_snapshot = lambda: settings.load(data)
    _install_middleware(http, os.environ.get("CAREER_RADAR_PASSWORD") or None,
                        _host_checker(os.environ.get("CAREER_RADAR_ALLOWED_HOSTS", "")))
    http.state.scheduler = _build_scheduler(http, db, data)
    _install_job_routes(http, db, data, refresh_callback)
    _install_profile_routes(http, db, data)
    _install_source_routes(http, db, data)
    _install_discovery_routes(http, db, data)
    _install_settings_routes(http, db, data)
    autostart = start_scheduler if start_scheduler is not None else (
        refresh_callback is None and os.environ.get("CAREER_RADAR_SCHEDULER", "1") != "0")

    @asynccontextmanager
    async def lifespan(_app):
        if autostart:
            http.state.scheduler.start()
        try:
            yield
        finally:
            http.state.scheduler.stop()

    http.router.lifespan_context = lifespan

    @http.get("/")
    def index():
        file = config.STATIC / "index.html"
        if not file.is_file():
            raise HTTPException(503, "Frontend not installed")
        return FileResponse(file)

    http.mount("/static", StaticFiles(directory=config.STATIC, check_dir=False), name="static")
    return http


def __getattr__(name):
    # ``uvicorn career_radar.app:app`` builds the app lazily, so importing this module (tests,
    # tooling) never creates a data directory or starts the scheduler as a side effect.
    if name == "app":
        globals()["app"] = create_app()
        return globals()["app"]
    raise AttributeError(name)
