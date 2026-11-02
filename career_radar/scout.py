"""Optional LLM scout: asks the user's LLM for Canadian employers' public job-board links.

Replaces the external research agents of the original deployment. The model only *suggests*
boards; nothing is trusted until discovery verifies it with a live public-API probe. Suggestions
are written as lead files in ``<data>/leads/`` (the same format discovery already reads), so a
bad or hallucinated answer costs one failed probe at most.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from . import config, discover

MAX_KNOWN_IN_PROMPT = 400
SYSTEM = (
    "You help a job-search tool find employers' public job boards. Reply with JSON only. "
    "Suggest real organisations that hire technical students, interns, co-ops, new graduates or "
    "junior staff in Canada, and the exact URL of their public job board when it is hosted on one of "
    "these systems: Greenhouse (job-boards.greenhouse.io/<board>), Lever (jobs.lever.co/<board>), "
    "Ashby (jobs.ashbyhq.com/<board>), Workday (<tenant>.wd<N>.myworkdayjobs.com/<site>), "
    "SmartRecruiters (jobs.smartrecruiters.com/<Company>), Workable (apply.workable.com/<account>), "
    "Recruitee (<account>.recruitee.com) or BambooHR (<account>.bamboohr.com/careers). "
    "Only include a URL you are confident exists; if you know the employer but not the board, set "
    "board_url to its careers page and ats to \"other\". Never invent slugs. Do not repeat boards from "
    "the known list."
)


def _prompt(theme, known, count):
    sample = "\n".join(known[:MAX_KNOWN_IN_PROMPT])
    return (
        f"Theme: {theme}\n\n"
        f"Suggest up to {count} employers for this theme with Canadian technical roles.\n"
        "Return a JSON object: {\"leads\": [{\"company\": str, \"board_url\": str, "
        "\"ats\": \"greenhouse|lever|ashby|workday|smartrecruiters|workable|recruitee|bamboohr|other\", "
        "\"canada_evidence\": str (city/province or office, short), \"notes\": str (short)}]}\n\n"
        f"Already known boards (skip these):\n{sample}\n"
    )


def _clean_leads(reply):
    rows = reply.get("leads") if isinstance(reply, dict) else reply if isinstance(reply, list) else []
    leads = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        company = re.sub(r"\s+", " ", str(row.get("company") or "")).strip()[:120]
        url = str(row.get("board_url") or "").strip()[:300]
        if not company or not re.match(r"^https://[^\s<>\"']+$", url):
            continue
        leads.append({"company": company, "board_url": url, "ats": str(row.get("ats") or "other").lower()[:20],
                      "canada_evidence": str(row.get("canada_evidence") or "")[:120],
                      "notes": str(row.get("notes") or "")[:200]})
    return leads


def run(client, *, root=None, themes=3, per_theme=12):
    """Ask ``client`` about the next ``themes`` focus themes; write one lead file per run."""
    root = Path(root or config.data_dir())
    plan = discover.focus(max(1, min(int(themes), 6)), root=root)
    known_file = Path(plan["known_boards_file"])
    known = [line for line in known_file.read_text().splitlines() if line and not line.startswith("unsupported system:")]
    results, errors = [], []
    for theme in plan["themes"]:
        try:
            reply = client.complete_json(SYSTEM, _prompt(theme, known, per_theme), max_tokens=3000)
            leads = _clean_leads(reply)
            results.append({"theme": theme, "leads": leads})
        except Exception as exc:  # One failed theme must not lose the others.
            errors.append(f"{theme[:60]}: {str(exc)[:200]}")
    stamp = datetime.now(timezone.utc)
    leads = [dict(lead, theme=r["theme"][:120]) for r in results for lead in r["leads"]]
    supported = [lead for lead in leads if discover.parse_urls(lead["board_url"])]
    path = None
    if leads:
        path = config.leads_dir(root) / f"scout-{stamp.strftime('%Y%m%dT%H%M%SZ')}.json"
        config.write_json(path, {"generated_at": stamp.isoformat(), "method": "llm-scout", "leads": leads})
    if errors and not results:
        raise RuntimeError("LLM scout failed: " + "; ".join(errors[:3]))
    return {"finished_at": stamp.isoformat(), "themes": [t[:120] for t in plan["themes"]],
            "lead_count": len(supported), "suggestions": len(leads), "lead_file": str(path) if path else None,
            "errors": errors}
