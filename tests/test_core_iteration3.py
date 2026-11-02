"""Iteration-3 regressions: shared-URL identity across the negative ledger, and
Canadian-prohibition / degree-enumeration grammar. Synthetic rows and mocked
source I/O only; no live network, DNS, registry or production database."""
from datetime import datetime, timedelta, timezone
import json
import socket
import sqlite3

import httpx
import pytest
from career_radar import adapters
from career_radar import core
from career_radar import extras
NOW = datetime(2026, 10, 31, 14, tzinfo=timezone.utc)
URL = "https://jobs.example.org/posting"
TABLES = ("jobs", "aliases", "observations", "source_rejections", "sources", "reminders", "meta")
PATHS = ("greenhouse", "lever", "ashby", "jsonld", "workday")
STATUSES = ("saved", "applied", "dismissed")
REAL_CLIENT = httpx.Client
SENIOR = {"title": "Senior Firmware Developer"}


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Iteration-3 core fixtures forbid live network/DNS")
    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(core, "utcnow", lambda: NOW)


def raw(**changes):
    return dict(source_id="employer", source_job_id="A", source="Synthetic employer", source_type="fixture",
                company="Synthetic Aerospace", title="Junior Firmware Developer", location="Ottawa, ON, Canada",
                url=URL, description="Computer science. No prior experience required.",
                verification="direct", is_closed=False) | changes


COPY = raw(source_id="external-copy", source_job_id="COPY", verification="curated", source="External copy")


def snapshot(db):
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        return {"schema": [dict(r) for r in conn.execute(
                    "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name")],
                "tables": {t: [dict(r) for r in conn.execute("SELECT * FROM " + t + " ORDER BY 1,2")] for t in TABLES}}


def alias_job(db, source_id, source_job_id):
    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT job_id FROM aliases WHERE source_id=? AND source_job_id=?",
                           (source_id, source_job_id)).fetchone()
    return row[0] if row else None


def retain(db, job_id, status):
    core.set_status(db, job_id, status)
    core.set_meta(db, "pending_digest", [{"jobs": [{"id": job_id}], "new_ids": [job_id], "batch_id": "fixture"}])
    core.set_meta(db, "digest_announced_ids", [job_id])
    core.mark_reminders(db, [{"id": job_id, "deadline": "2026-11-03", "reminder_band": "3d"}], NOW)


def set_marker(db, marker):
    with sqlite3.connect(db) as conn:
        if marker is None:
            conn.execute("DELETE FROM meta WHERE key='identity_policy_version'")
        else:
            conn.execute("INSERT OR REPLACE INTO meta VALUES('identity_policy_version',?)", (json.dumps(marker),))


# --- N1: a rejected same-source requisition proves the URL is shared ---------

@pytest.mark.parametrize("status", STATUSES)
@pytest.mark.parametrize("marker", [None, 1, 2], ids=["missing", "stale-1", "current-2"])
@pytest.mark.parametrize("order", ["accepted-first", "rejected-first"])
@pytest.mark.parametrize("negative_key", ["B", None], ids=["native-B", "url-fallback"])
def test_rejected_same_source_requisition_blocks_cross_source_state_lending(tmp_path, status, marker, order, negative_key):
    db = tmp_path / "n1.db"
    negative = raw(source_job_id=negative_key, **SENIOR)
    if order == "rejected-first":
        core.upsert_jobs(db, [negative], NOW)
        first = core.upsert_jobs(db, [raw()], NOW)["new_ids"][0]
    else:
        first = core.upsert_jobs(db, [raw()], NOW)["new_ids"][0]
        core.upsert_jobs(db, [negative], NOW)
    retain(db, first, status)
    assert core.get_job(db, first)["active"], "A different same-source requisition is not evidence against A"
    set_marker(db, marker)
    result = core.upsert_jobs(db, [COPY], NOW)
    copy_id = alias_job(db, "external-copy", "COPY")
    assert result["new"] == 1 and result["updated"] == 0 and copy_id != first
    assert core.get_job(db, copy_id)["status"] == "new"
    original = core.get_job(db, first)
    assert original["status"] == status and original["active"]
    assert [s["name"] for s in original["sources"]] == ["Synthetic employer"]
    assert core.get_meta(db, "digest_announced_ids") == [first]
    assert len(snapshot(db)["tables"]["source_rejections"]) == 1, "Keep the authoritative negative evidence"
    again = core.upsert_jobs(db, [COPY], NOW + timedelta(hours=1))
    assert again["new"] == 0 and alias_job(db, "external-copy", "COPY") == copy_id


def test_rejection_ledger_and_positive_aliases_have_the_same_ambiguity_rule(tmp_path):
    db = tmp_path / "parity.db"
    first = core.upsert_jobs(db, [raw()], NOW)["new_ids"][0]
    second = core.upsert_jobs(db, [raw(source_job_id="B")], NOW)["new_ids"][0]
    third = core.upsert_jobs(db, [COPY], NOW)["new_ids"][0]
    assert len({first, second, third}) == 3


def test_copy_from_a_source_with_another_key_at_the_url_is_not_merged(tmp_path):
    db = tmp_path / "copy-source-conflict.db"
    first = core.upsert_jobs(db, [raw()], NOW)["new_ids"][0]
    core.set_status(db, first, "applied")
    core.upsert_jobs(db, [COPY | {"source_job_id": "OLD-COPY", **SENIOR}], NOW)
    result = core.upsert_jobs(db, [COPY], NOW)
    assert result["new"] == 1 and alias_job(db, "external-copy", "COPY") != first
    assert core.get_job(db, first)["status"] == "applied"


@pytest.mark.parametrize("negative_rank", ["curated", "public_listing", "direct"])
def test_unambiguous_cross_source_copy_still_merges_with_an_unrelated_negative(tmp_path, negative_rank):
    db = tmp_path / "still-merges.db"
    first = core.upsert_jobs(db, [raw()], NOW)["new_ids"][0]
    core.set_status(db, first, "saved")
    core.upsert_jobs(db, [raw(source_id="authority", source_job_id="NEG", verification=negative_rank, **SENIOR)], NOW)
    result = core.upsert_jobs(db, [COPY], NOW)
    assert result["new"] == 0 and result["updated"] == 1
    assert alias_job(db, "external-copy", "COPY") == first
    job, = core.all_jobs(db)
    assert job["status"] == "saved"
    assert job["active"] is (core.AUTHORITY[negative_rank] < core.AUTHORITY["direct"])


# --- N2: one URL-level rejection cannot target two known requisitions --------

ARRIVALS = {"negative-last": ("a", "b", "neg"), "negative-first": ("neg", "a", "b"), "negative-between": ("a", "neg", "b")}


@pytest.mark.parametrize("verification", ["curated", "direct"])
@pytest.mark.parametrize("arrival", list(ARRIVALS))
@pytest.mark.parametrize("batched", [False, True], ids=["separate-batches", "one-batch"])
def test_url_level_rejection_does_not_archive_either_of_two_distinct_requisitions(tmp_path, verification, arrival, batched):
    db = tmp_path / "n2.db"
    rows = {"a": raw(source_id="board", verification=verification),
            "b": raw(source_id="board", source_job_id="B", verification=verification),
            "neg": raw(source_id="authority", source_job_id="NEG", **SENIOR)}
    ordered = [rows[name] for name in ARRIVALS[arrival]]
    for batch in ([ordered] if batched else [[row] for row in ordered]):
        core.upsert_jobs(db, batch, NOW)
    jobs = core.all_jobs(db)
    assert len(jobs) == 2 and all(j["active"] for j in jobs)
    assert not any("authoritative source does not meet" in n for j in jobs for n in j["eligibility_notes"])
    assert all(core.SHARED_LINK_NOTE in j["eligibility_notes"] for j in jobs), "Keep unattributable evidence visible"
    assert len(snapshot(db)["tables"]["source_rejections"]) == 1
    after = snapshot(db)
    core.upsert_jobs(db, [], NOW)
    assert snapshot(db) == after


def test_same_source_bound_rejection_still_archives_only_its_own_requisition(tmp_path):
    db = tmp_path / "bound.db"
    core.upsert_jobs(db, [raw(source_id="board"), raw(source_id="board", source_job_id="B")], NOW)
    core.upsert_jobs(db, [raw(source_id="board", source_job_id="B", **SENIOR)], NOW)
    state = {j["source_job_id"]: j["active"] for j in core.all_jobs(db)}
    assert state == {"A": True, "B": False}


def test_moved_negative_releases_the_url_it_no_longer_describes(tmp_path):
    db = tmp_path / "moved-negative.db"
    first = core.upsert_jobs(db, [raw()], NOW)["new_ids"][0]
    negative = raw(source_id="authority", source_job_id="NEG", **SENIOR)
    core.upsert_jobs(db, [negative], NOW)
    assert not core.get_job(db, first)["active"]
    core.upsert_jobs(db, [negative | {"url": URL + "/moved"}], NOW + timedelta(hours=1))
    assert core.get_job(db, first)["active"]
    ledger, = snapshot(db)["tables"]["source_rejections"]
    assert ledger["url"] == URL + "/moved"


def test_accepted_correction_at_another_url_releases_the_old_negative(tmp_path):
    db = tmp_path / "corrected-negative.db"
    first = core.upsert_jobs(db, [raw()], NOW)["new_ids"][0]
    negative = raw(source_id="authority", source_job_id="NEG", **SENIOR)
    core.upsert_jobs(db, [negative], NOW)
    assert not core.get_job(db, first)["active"]
    core.upsert_jobs(db, [negative | {"title": "Junior Firmware Developer", "url": URL + "/moved"}], NOW)
    assert core.get_job(db, first)["active"]
    assert snapshot(db)["tables"]["source_rejections"] == []


# --- N3: explicit prohibitions of Canadian applicants are never positive -----

PROHIBITIONS = (
    "Remote — Canadian residents cannot apply",
    "Remote — not accepting Canadian applicants",
    "Remote — applications from Canadian residents are not accepted",
    "Remote — Canadians cannot apply",
    "Remote — Canadians are not eligible",
    "Remote — Canadian applicants will not be considered",
    "Remote — Canadian residents, unfortunately, cannot apply",
    "Remote — Canadian residents need not apply",
    "Remote — Canada-based candidates are ineligible",
    "Remote — applications from Canada are not accepted",
    "Remote — we are unable to hire in Canada",
    "Remote — we do not hire Canadian residents",
    "Remote — we are not considering candidates located in Canada",
    "Remote — no Canadian applicants",
    "Remote — open to US residents, not Canadian residents",
    "Remote — US only; Canadian residents may not apply",
    "Remote — not hiring in Canada at this time",
    "Remote — Canadian candidates can't apply",
    "Remote — Canadian residents are unable to apply",
    "Remote — Canada is not supported",
)
CANADIAN_POSITIVES = (
    "Remote — Canadian residents can apply", "Remote — accepting Canadian applicants",
    "Remote — open to Canadian residents", "Remote — Canada or United States", "Remote — Canada (no relocation)",
    "Toronto, ON (not remote)", "Remote — Canada only, US residents are not eligible",
    "Remote — Canada; visa sponsorship not available", "Remote — Canada (excluding Quebec)",
    "Remote — Canada, not US", "Remote — anywhere in Canada", "Remote — Canada; no US applicants",
    "Ottawa, ON — candidates must be able to work on-site", "Remote — no Canadian citizenship required",
    # A prohibition of non-Canadians is Canada-only eligibility, not a Canadian prohibition.
    "Remote — Canada; non-Canadian residents cannot apply", "Remote — Canada; non-Canadians cannot apply",
    "Remote — Canada; not accepting non-Canadian applicants",
    "Remote — Canada only; candidates not located in Canada cannot apply",
    "Remote — Canada; non-Canadian residents are not eligible",
)


@pytest.mark.parametrize("location", PROHIBITIONS)
@pytest.mark.parametrize("hint", [None, "CA"], ids=["no-hint", "CA-hint"])
def test_canadian_subject_before_or_after_a_prohibition_is_not_eligibility(location, hint):
    assert core.canadian_options(location) == []
    assert not core.canadian_location(location)
    assert core.normalize_job(raw(location=location, country_hint=hint), NOW) is None


@pytest.mark.parametrize("location", CANADIAN_POSITIVES)
def test_prohibition_grammar_keeps_genuine_canadian_options(location):
    job = core.normalize_job(raw(location=location), NOW)
    assert job is not None and job["active"]
    assert "United States" not in job["location"] and "US residents" not in job["location"]


# --- N4/N5: subject lists, enumerations and joint quantifiers ----------------

SUBJECT_LIST_ALTERNATIVES = (
    "A bachelor's degree in Electrical, Computer, or Software Engineering or a master's degree in Computer Science is required.",
    "A bachelor's degree in Computer Science, Mathematics, or a related field, or a master's degree in Computer Science is required.",
    "An undergraduate degree in Computer Science, Software Engineering or Mathematics, or a master's degree is required.",
    "Minimum qualifications: bachelor's degree in Computer Science, Computer Engineering, Electrical Engineering, or a related technical field, or a master's degree.",
    "Required: a bachelor's degree in a technical field such as Computer Science, Physics, or Mathematics, or a master's degree in Engineering.",
    "A bachelor's degree in Computer Science, Engineering, Physics, etc., or a master's degree is required.",
    "A bachelor's degree in a related discipline (e.g., Computer Science, Engineering) or a master's degree is required.",
    "A bachelor's degree (Computer Science, Engineering, or a related field) or a master's degree is required.",
    "A bachelor's degree in Computer Science and/or Mathematics, or a master's degree in Statistics, is required.",
    "A bachelor's degree in both Computer Science and Mathematics, or a master's degree, is required.",
    "Experience with both Python and C++, and a bachelor's or master's degree is required.",
    "A bachelor's degree, master's degree, or PhD in Computer Science is required.",
)
ADVANCED_REQUIREMENTS = (
    "Both degrees are required: a bachelor's degree in Computer Science, a master's degree in Electrical Engineering.",
    "Both degrees are mandatory: bachelor's degree in Computer Science, master's degree in Engineering.",
    "Both of these degrees are required: bachelor's degree in Computer Science / master's degree in Engineering.",
    "Both a bachelor's degree in Computer Science and a master's degree in Engineering are required.",
    "All of the following degrees are required: bachelor's degree in Computer Science, master's degree in Statistics, PhD in Mathematics.",
    "Two degrees are required: a bachelor's degree in Computer Science, a master's degree in Engineering.",
    "You must hold a bachelor's degree in Computer Science as well as a master's degree in Engineering.",
    "A bachelor's degree in Computer Science, Mathematics, or Physics and a master's degree are required.",
    "A bachelor's degree in Computer Science, Mathematics, or Physics is required; a master's degree is mandatory.",
    "Minimum qualifications: master's degree in Computer Science, Mathematics, or Statistics.",
    "We mentor bachelor's students in Computer Science, Mathematics, or Physics; a PhD is required.",
    "A PhD in Computer Science, Mathematics, or Physics is required, and bachelor's students may audit seminars.",
)


@pytest.mark.parametrize("description", SUBJECT_LIST_ALTERNATIVES)
def test_subject_lists_and_disjunctive_enumerations_keep_the_bachelor_route(description):
    assert core.advanced_degree_required(description) is False
    assert core.normalize_job(raw(description=description), NOW) is not None


@pytest.mark.parametrize("description", ADVANCED_REQUIREMENTS)
def test_joint_enumerations_and_unrelated_bachelor_mentions_remain_advanced(description):
    assert core.advanced_degree_required(description) is True
    assert core.normalize_job(raw(description=description), NOW) is None


# Abbreviated and generic credentials, from real collected postings (Tenstorrent, CAE).
ABBREVIATED_ALTERNATIVES = (
    "Who You Are Currently pursuing a BS, MS, or PhD in Comp Sci, Comp Eng, Physics/Math or a related field.",
    "Currently pursuing a BS, MS, or PhD in EE, ECE, CE, or CS.",
    "Currently pursuing a B.Sc., M.Sc. or Ph.D. in Physics or Computer Science.",
    "BEng or MEng in Electrical Engineering is required.",
    "BASc/MASc in Mechanical Engineering required.",
    "BS/MS in Computer Science required.",
    "Preferred Education Currently pursuing a university degree or master’s degree in industrial engineering.",
    "Proficiency with MS Office is required.",
    "MS Excel and MS Word skills are required; a bachelor's degree is an asset.",
    "Office in Boston, MA. A bachelor's degree is required.",
    "We require a B.S. Our research group includes PhD scientists.",
    "A B.Sc. (Computer Science) or M.Sc. is required.",
    "Hands-on LC-MS/MS experience is required.",
    "Experience with MS/MS instruments is required; a bachelor's degree is required.",
    "Experience with MS/MS and LC-MS is required; a bachelor's degree is required.",
)
ABBREVIATED_ADVANCED = (
    "Currently pursuing an MS or PhD in Computer Science.",
    "MS or PhD in Computer Science required.",
    "MS in Computer Science required.",
    "M.Sc. or Ph.D. in Physics is required.",
    "M.S. required.",
    "MS required; MS Office is an asset.",
    "Experience with MS/MS is required. M.S. degree required.",
    "Currently pursuing a MS in Computer Science; MS Office experience is an asset.",
    "We're looking for Masters or PhD students. Currently pursuing a Masters or PhD degree (required).",
    "Both degrees are required: BS in Computer Science, MS in Electrical Engineering.",
)


@pytest.mark.parametrize("description", ABBREVIATED_ALTERNATIVES)
def test_abbreviated_and_generic_bachelor_credentials_keep_the_bachelor_route(description):
    assert core.advanced_degree_required(description) is False
    assert core.normalize_job(raw(description=description), NOW) is not None


@pytest.mark.parametrize("description", ABBREVIATED_ADVANCED)
def test_abbreviated_advanced_only_requirements_remain_advanced(description):
    assert core.advanced_degree_required(description) is True
    assert core.normalize_job(raw(description=description), NOW) is None


# From a real CAE junior posting: an experience cap is not a minimum requirement.
@pytest.mark.parametrize("description", (
    "Minimum Qualifications Bachelor’s degree in engineering or computer science. Up to three years of software development experience.",
    "Less than 4 years of professional experience.",
    "No more than 5 years of industry experience.",
    "0 to 3 years of experience.",
))
def test_experience_caps_are_not_minimum_requirements(description):
    assert core.normalize_job(raw(description=description), NOW) is not None


@pytest.mark.parametrize("description", (
    "Minimum of 3 years of software development experience, up to 5 years preferred.",
    "At least three years of experience required.",
    "Up to date tooling. 4 years of embedded experience required.",
    # From a real Anvil (Ottawa) posting: a comma list sits between "years" and "experience".
    "Experience Minimum of 4+ years of software deployment, infrastructure, or systems integration experience, "
    "preferably in defence or related industry.",
))
def test_experience_minimums_remain_exclusions(description):
    assert core.normalize_job(raw(description=description), NOW) is None


def test_years_of_schooling_are_not_work_experience_minimums():
    description = "Requirements: completed 3 years of post-secondary education, including coursework and lab experience."
    assert core.normalize_job(raw(description=description), NOW) is not None


def test_unquantified_comma_list_stays_a_caveated_match_rather_than_an_exclusion():
    # Without "both"/"all"/"two" or an "and", a bare comma list is ambiguous; keep the
    # earlier alternative reading and the advanced-degree caveat instead of hiding it.
    description = "Required education: bachelor's degree in Computer Science, master's degree in Software Engineering."
    assert core.advanced_degree_required(description) is False
    job = core.normalize_job(raw(description=description), NOW)
    assert job is not None
    assert "Check whether an advanced degree is required or only preferred." in job["eligibility_notes"]


# --- The same grammar through the real adapter -> JSON -> core path ----------

class Wire(httpx.SyncByteStream):
    def __init__(self, body):
        self.body = body

    def __iter__(self):
        yield self.body


def source_row(monkeypatch, path, *, location="Toronto, ON, Canada", description=None):
    description = raw()["description"] if description is None else description
    title = raw()["title"]
    source = {"id": "fixture-" + path, "name": "Synthetic Aerospace", "type": path, "board": "fixture"}
    common = {"id": "17", "title": title}
    if path == "greenhouse":
        payload = {"jobs": [common | {"absolute_url": URL, "content": description, "location": {"name": location}}],
                   "meta": {"total": 1}}
    elif path == "lever":
        payload = [{"id": "17", "text": title, "hostedUrl": URL, "descriptionPlain": description,
                    "categories": {"location": location}}]
    elif path == "ashby":
        payload = {"jobs": [common | {"jobUrl": URL, "descriptionPlain": description, "location": location,
                                      "isListed": True}]}
    elif path == "jsonld":
        source["url"] = URL
        payload = {"@type": "JobPosting", "identifier": {"value": "17"}, "title": title, "description": description,
                   "url": URL, "jobLocation": {"address": location}, "hiringOrganization": {"name": source["name"]}}
    else:
        assert path == "workday"
        source.update(url="https://fixture.wd3.myworkdayjobs.com/Campus", tenant="fixture", site="Campus")
        index = {"total": 1, "jobPostings": [{"title": title, "externalPath": "/job/Fixture_17",
                                              "locationsText": location, "bulletFields": ["17"]}]}
        payload = {"jobPostingInfo": common | {"jobDescription": description, "location": location}}
    calls = []

    def handle(request):
        calls.append(str(request.url))
        value = index if path == "workday" and request.method == "POST" else payload
        body = json.dumps(value)
        if path == "jsonld":
            body = '<script type="application/ld+json">' + body + "</script>"
        return httpx.Response(200, stream=Wire(body.encode()))

    transport = httpx.MockTransport(handle)
    if path in ("greenhouse", "lever", "ashby"):
        monkeypatch.setattr(adapters.httpx, "Client", lambda **kw: REAL_CLIENT(**(kw | {"transport": transport})))
        rows = adapters.fetch_source(source)
    else:
        monkeypatch.setattr(extras, "_PublicTransport", lambda **kw: transport)
        rows = extras.fetch_source(source)
    assert len(rows) == 1 and len(calls) == (2 if path == "workday" else 1)
    return json.loads(json.dumps(rows[0]))


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("location", PROHIBITIONS[:3], ids=["cannot-apply", "not-accepting", "not-accepted"])
def test_actual_source_paths_reject_canadian_prohibitions(monkeypatch, path, location):
    assert core.normalize_job(source_row(monkeypatch, path, location=location), NOW) is None


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("description,accepted", [
    (SUBJECT_LIST_ALTERNATIVES[0], True), (SUBJECT_LIST_ALTERNATIVES[2], True),
    (ADVANCED_REQUIREMENTS[0], False), (ADVANCED_REQUIREMENTS[1], False),
    (ABBREVIATED_ALTERNATIVES[0], True), (ABBREVIATED_ADVANCED[1], False),
], ids=["subject-comma-or", "undergrad-comma", "both-required", "both-mandatory", "bs-ms-phd", "ms-or-phd"])
def test_actual_source_paths_apply_degree_enumeration_grammar(monkeypatch, path, description, accepted):
    job = core.normalize_job(source_row(monkeypatch, path, description=description), NOW)
    assert (job is not None) is accepted
