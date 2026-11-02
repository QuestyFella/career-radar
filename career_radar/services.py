"""Application services shared by the web API and the scheduler.

Builds the LLM client from settings, and runs collection, discovery and the LLM scout with
the configured budgets. Each run records a short summary in the database ``meta`` table so the
UI can show what happened, without relying on an external cron system.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

from . import config, core, settings

log = logging.getLogger("career-radar")
_codex_lock = threading.Lock()
_codex = {}


def codex_auth(data=None):
    """One shared ChatGPT sign-in manager per data directory."""
    from .codex_auth import CodexAuth
    path = Path(data or config.data_dir()) / "codex_auth.json"
    with _codex_lock:
        if str(path) not in _codex:
            _codex[str(path)] = CodexAuth(path)
        return _codex[str(path)]


def llm_client(data=None, *, provider=None, model=None, base_url=None, required=False):
    """The configured LLM client, or None when no provider is set up (unless ``required``)."""
    from . import llm
    chosen = settings.load(data)["llm"]
    provider = provider or chosen["provider"]
    if not provider:
        if required:
            raise llm.LLMError("No LLM provider is configured. Choose one in Settings.")
        return None
    spec = llm.PROVIDERS.get(provider)
    if spec is None:
        raise llm.LLMError(f"Unknown LLM provider: {provider}")
    kwargs = {"api_key": settings.api_key(provider, data), "base_url": base_url or chosen["base_url"] or None}
    if spec.auth == "oauth":
        kwargs["codex"] = codex_auth(data)
    try:
        return llm.LLMClient(provider, model or (chosen["model"] if provider == chosen["provider"] else None) or None, **kwargs)
    except llm.LLMError:
        if required:
            raise
        return None


def llm_configured(data=None):
    try:
        return llm_client(data) is not None
    except Exception:
        return False


def run_collection_job(data=None, db=None):
    from .collect import run_collection
    from .profile import load_profile
    current = settings.load(data)
    report = run_collection(db=db or config.db_path(), max_seconds=current["schedule"]["collection_budget_minutes"] * 60,
                            region=current["region"], levels=load_profile(data)["levels"])
    if report.get("skipped"):
        return True, report.get("reason")
    summary = (f"{report.get('new_jobs', 0)} new jobs; {report.get('sources_ok', 0)}/{report.get('sources_total', 0)} "
               f"sources OK in {round(report.get('duration_seconds', 0) / 60, 1)} min")
    if report.get("sources_failed"):
        summary += f"; {report['sources_failed']} failed"
    if report.get("sources_deferred"):
        summary += f"; {report['sources_deferred']} deferred to the next run"
    return bool(report.get("ok")), summary


def run_discovery_job(data=None, db=None, *, reverify=False):
    from . import discover
    data = Path(data or config.data_dir())
    budget = settings.load(data)["schedule"]["discovery_budget_minutes"]
    import io
    summary = discover.run([str(config.leads_dir(data))], db=db or config.db_path(), root=data,
                           max_seconds=budget * 60, log=io.StringIO(), reverify=reverify)
    core.set_meta(db or config.db_path(), "last_discovery", {k: v for k, v in summary.items() if k != "results"})
    if summary.get("skipped"):
        return True, summary.get("reason")
    added = summary.get("added_count", 0)
    names = ", ".join(a["name"] for a in summary.get("added", [])[:5])
    return True, (f"probed {summary.get('probed', 0)}, added {added}{(' (' + names + ')') if names else ''}, "
                  f"dropped {len(summary.get('dropped', []))}, {summary.get('discovered_total', 0)} discovered boards")


def run_scout_job(data=None, db=None):
    from . import scout
    data = Path(data or config.data_dir())
    client = llm_client(data, required=True)
    themes = settings.load(data)["schedule"]["scout_themes_per_run"]
    result = scout.run(client, root=data, themes=themes)
    core.set_meta(db or config.db_path(), "last_scout", result)
    if result.get("lead_count"):
        # Verify the new leads right away instead of waiting for the nightly discovery run.
        ok, discovery = run_discovery_job(data, db)
        return ok, f"{result['lead_count']} board leads from {len(result.get('themes', []))} themes; {discovery}"
    return True, f"no new board leads from {len(result.get('themes', []))} themes"
