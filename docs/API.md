# Career Radar HTTP API

All endpoints are served by the same FastAPI app that serves the web UI. Responses are JSON
unless noted. Every write (`POST`, `PUT`, `PATCH`, `DELETE`) must send
`Content-Type: application/json` (the resume upload sends `application/pdf`) from the same
origin; anything else is rejected (415/403). Errors use FastAPI's `{"detail": "..."}` shape.

When `CAREER_RADAR_PASSWORD` is set, every route requires HTTP Basic auth (any username).

## Jobs (unchanged from the original board)

| Method | Path | Notes |
|---|---|---|
| GET | `/api/health` | `{ok, service, time}` |
| GET | `/api/jobs` | query: `q, level, category, location, status, sort=priority\|newest\|deadline, deadline_days, page, page_size<=250, priority_only, include_closed` → `{items, total, page, page_size, pages}` |
| GET | `/api/jobs/{id}` | one job |
| PATCH | `/api/jobs/{id}` | `{status: new\|saved\|applied\|dismissed}` |
| GET | `/api/stats` | totals (see below) |
| GET | `/api/deadlines` | `{items}` closing within 30 days |
| GET | `/api/meta` | static app description |
| POST | `/api/refresh` | start a collection now → `{started, message}` |
| GET | `/api/export.csv` | CSV download |

Each job item has the original fields (`id, title, company, location, url, level, category, tags[],
priority_score, priority_reasons[], eligibility_notes[], deadline, posted_at, first_seen, last_seen,
active, status, sources[], verification, description, ...`).

`priority_score` and `priority_reasons` are **profile-aware**: when a profile exists they are the
match score (0–100) and its reasons, computed at request time. Extra field on every job:

```json
"match": {"score": 72, "reasons": ["Matches your interest: embedded", "3 of your skills: C++, Python, Linux"],
          "terms": ["embedded", "c++", "python", "linux"], "priority": true}
```

`/api/stats` keeps its old keys (`total, new_today, priority, closing_soon, saved, applied, sources_ok,
sources_total, last_refresh, next_refresh, timezone, categories, levels, location_counts,
collection_running`) and adds `profile_set: bool` and `priority_terms: [str]` (the profile's
interests; empty without a profile). `priority` counts jobs whose `match.priority` is true.

## Profile and resume

Profile object (every key always present; strings may be empty, lists may be empty):

```json
{
  "name": "", "email": "", "location": "", "headline": "",
  "graduation": "",                      // free text the user confirmed, e.g. "2027-04"; never assumed
  "education": [{"school": "", "degree": "", "field": "", "graduation": ""}],
  "experience": [{"title": "", "company": "", "start": "", "end": ""}],
  "skills": ["Python", "C++"],
  "interests": ["embedded", "aerospace"], // boost terms for ranking ("priority matches")
  "preferred_locations": ["Toronto", "Remote"],
  "levels": ["internship", "co_op", "new_grad", "entry_level"], // preferred role levels
  "exclude_keywords": ["sales"],          // demote titles containing these
  "links": ["https://github.com/example"],
  "source": "manual|resume|resume+llm",
  "updated_at": "2026-11-01T12:00:00+00:00"
}
```

| Method | Path | Body / result |
|---|---|---|
| GET | `/api/profile` | `{profile, resume}`; `resume` is `null` or `{uploaded_at, filename, pages, chars, method: "heuristic"\|"llm", pdf: {title, author, creator, producer, created}, warnings: []}` |
| PUT | `/api/profile` | body: profile fields (partial allowed) → `{profile}` |
| DELETE | `/api/profile` | reset profile and resume data → `{profile, resume: null}` |
| POST | `/api/profile/resume?llm=auto\|on\|off&filename=cv.pdf` | raw PDF body, `Content-Type: application/pdf`, ≤ 8 MB → `{profile, resume, extracted, warnings}`; the extracted fields are merged into the saved profile (lists are unioned) |
| DELETE | `/api/profile/resume` | delete stored resume text/metadata; profile stays → `{resume: null}` |

`llm=auto` uses the configured LLM when one is set up, else heuristics only. `on` fails with 400
when no LLM is configured. LLM failures fall back to heuristics with a warning.

## Sources and boards

Source item (registry entry merged with its last health check):

```json
{
  "id": "greenhouse-acme", "name": "Acme", "type": "greenhouse", "url": "https://job-boards.greenhouse.io/acme",
  "origin": "builtin|discovered|user", "enabled": true, "removable": true,
  "keywords": "", "coverage": "…", "notes": null,
  "status": "ok|partial|error|blocked|disabled|pending", "last_checked": "iso|null",
  "raw_count": 12, "matched_count": 3, "error": null
}
```

| Method | Path | Body / result |
|---|---|---|
| GET | `/api/sources` | `{items: [source], last_refresh, counts: {total, enabled, by_origin: {...}, by_type: {...}}, types: [{type, label, kind: "board"\|"search"}]}` |
| POST | `/api/sources/check` | `{urls: ["https://jobs.lever.co/acme", ...]}` (≤ 10) → `{candidates: [{key, type, name, url, status, raw, canada, tech, matched, sample_titles, error, monitored_id}], unsupported: ["url", ...]}` — live read-only probe; `status` is `verified\|no_canada_tech\|not_found\|error\|excluded\|already_monitored` |
| POST | `/api/sources` | board: `{url, name?}`; search: `{type: "linkedin"\|"jobbank", keywords, name?}` → `{source}` (201). 409 when already monitored, 400 when the URL is not a supported board |
| PATCH | `/api/sources/{id}` | `{enabled?, name?, keywords?}` (`name`/`keywords` only for `user` sources) → `{source}` |
| DELETE | `/api/sources/{id}` | `user`: deleted; `discovered`: removed and never re-added by discovery; `builtin`: 400 (disable it instead) → `{removed: id}` |

Supported board URL shapes: Greenhouse (`boards.greenhouse.io/x`, `job-boards.greenhouse.io/x`),
Lever (`jobs.lever.co/x`), Ashby (`jobs.ashbyhq.com/x`), Workday (`x.wd3.myworkdayjobs.com/Site`),
SmartRecruiters (`jobs.smartrecruiters.com/X`), Workable (`apply.workable.com/x`, `x.workable.com`),
Recruitee (`x.recruitee.com`), BambooHR (`x.bamboohr.com/careers`).

## Discovery

| Method | Path | Result |
|---|---|---|
| GET | `/api/discovery` | `{discovered_sources, curated_sources, candidates_checked, by_status, running, last_run: summary\|null, scout: {available, enabled, running, last_run: summary\|null}}` |
| POST | `/api/discovery/run` | `{started, message}` — harvest board links from collected jobs and lead files, probe, re-verify |
| POST | `/api/discovery/scout` | `{started, message}` — LLM scout (400 without a configured LLM) |

## Settings and LLM

| Method | Path | Body / result |
|---|---|---|
| GET | `/api/settings` | see below |
| PUT | `/api/settings` | `{llm?: {provider, model, base_url}, schedule?: {...}}` (partial) → same as GET |
| PUT | `/api/settings/secrets` | `{provider: "openai", api_key: "..."}`; `api_key: null` clears it → same as GET |
| GET | `/api/llm/models?provider=openai` | `{items: [{id, label}], error: null\|str}` |
| POST | `/api/llm/test` | `{}` → `{ok, provider, model, latency_ms, reply\|null, error\|null}` |
| POST | `/api/llm/codex/login` | start ChatGPT/Codex device login → `{status: "pending", user_code, verification_url, expires_at}` |
| GET | `/api/llm/codex/login` | `{status: "idle"\|"pending"\|"complete"\|"error", user_code?, verification_url?, error?}` |
| DELETE | `/api/llm/codex` | sign out (delete stored tokens) → same as GET settings |

`GET /api/settings`:

```json
{
  "version": "2.0.0",
  "timezone": "America/Toronto",
  "llm": {"provider": "openrouter", "model": "openai/gpt-5.4-mini", "base_url": "", "configured": true},
  "providers": [{"id": "openai", "label": "OpenAI API", "auth": "api_key", "key_env": "OPENAI_API_KEY",
                 "default_model": "gpt-5.4-mini", "needs_base_url": false, "docs_url": "https://platform.openai.com/api-keys"}],
  "secrets": {"openai": {"set": true, "source": "env|file", "hint": "…a1b2"}},
  "codex": {"logged_in": false, "account": null, "expires_at": null},
  "schedule": {"enabled": true, "collect_times": ["08:00", "18:00"], "discovery_time": "04:30",
               "scout_enabled": false, "scout_time": "01:30", "scout_themes_per_run": 3,
               "collection_budget_minutes": 40, "discovery_budget_minutes": 15},
  "schedule_status": {"running": true, "jobs": {"collect": {"next_run": "iso", "last_run": "iso|null", "last_ok": true, "last_summary": "…"},
                                                "discovery": {...}, "scout": {...}}}
}
```

Provider ids: `openai`, `anthropic`, `openrouter`, `opencode-zen`, `opencode-go`, `codex`
(ChatGPT sign-in, no key), `openai-compatible` (custom base URL, e.g. Ollama/LM Studio; key optional).
API keys are write-only: the API never returns them, only `set/source/hint`.
