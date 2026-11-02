"""Source discovery (discover.py): lead harvesting, live-probe verification, the
discovered registry, re-checks and scout focus rotation. Synthetic fixtures only; the
imported autouse guard forbids real DNS and sockets."""
from __future__ import annotations

import fcntl
import json
from datetime import timedelta

import pytest

from career_radar import collect
from career_radar import config
from career_radar import core
from career_radar import discover
from career_radar import extras
from test_extras import no_live_network  # noqa: F401 - autouse network guard


def row(title, location, board="acme", company=None, **extra):
    return {"title": title, "company": company or "Placeholder", "location": location,
            "url": f"https://job-boards.greenhouse.io/{board}/jobs/{abs(hash((title, location))) % 10**8}",
            "source_job_id": f"{board}-{title}", "description": "Pursuing a computer science degree.", **extra}


class FakeFetcher:
    def __init__(self, boards):
        self.boards, self.calls = boards, []

    def __call__(self, source):
        ident = source.get("board") or source.get("company_id") or source.get("site")
        self.calls.append((source["type"], ident))
        outcome = self.boards.get((source["type"], ident))
        if outcome is None:
            raise extras.SourceError(f"HTTP error 404: {source['url']}; no bypass attempted")
        if isinstance(outcome, Exception):
            raise outcome
        # Real Greenhouse/Lever/Ashby/Workday adapters stamp the source's company/name.
        return [dict(r, company=source.get("company") or source["name"]) if r["company"] == "Placeholder" else dict(r)
                for r in outcome]


@pytest.fixture()
def root(tmp_path, monkeypatch):
    (tmp_path / "sources.json").write_text(json.dumps({"sources": [
        {"id": "greenhouse-known", "name": "Known", "type": "greenhouse", "board": "known",
         "url": "https://job-boards.greenhouse.io/known"}]}))
    (tmp_path / "extra_sources.json").write_text(json.dumps({"sources": []}))
    (tmp_path / "leads").mkdir(parents=True)
    monkeypatch.setattr(config, "BUILTIN_REGISTRY", tmp_path)
    monkeypatch.setenv("CAREER_RADAR_DATA", str(tmp_path))
    monkeypatch.setattr(extras, "_json_response", lambda *a, **k: {"name": "Acme Robotics"})
    monkeypatch.setattr(discover, "_page_title", lambda url: None)
    return tmp_path


LEADS = [
    {"company": "Acme", "board_url": "https://job-boards.greenhouse.io/acme", "canada_evidence": "Toronto, ON"},
    {"company": "US Only Co", "board_url": "https://jobs.lever.co/usonly", "canada_evidence": ""},
    {"company": "Gone Inc", "board_url": "https://jobs.ashbyhq.com/gone"},
    {"company": "Maple Bank", "evidence_url": "https://maple.wd3.myworkdayjobs.com/en-US/Maple_Careers/job/Toronto/Dev-Intern_R1"},
    {"company": "Ledger Co", "board_url": "https://jobs.smartrecruiters.com/LedgerCo"},
    {"company": "Known", "board_url": "https://job-boards.greenhouse.io/known"},
    {"company": "MDA Space", "ats": "other", "board_url": "https://recruiting.ultipro.ca/MAC5000MCDW/JobBoard/x"},
]
BOARDS = {
    ("greenhouse", "acme"): [row("Software Developer Intern", "Toronto, Ontario, Canada"), row("Accountant", "Toronto, ON")],
    ("lever", "usonly"): [row("Software Engineer Intern", "New York, NY", board="usonly")],
    ("workday", "Maple_Careers"): [row("Developer Co-op", "Toronto, Ontario", board="maple", country_hint="CA")],
    ("smartrecruiters", "LedgerCo"): [row("Accountant", "Calgary, AB", board="ledger", company="Ledger Company Ltd")],
}


def write_leads(root, rows=LEADS, name="scout.json"):
    (root / "leads" / name).write_text(json.dumps(rows))


def registry(root):
    return json.loads((root / collect.DISCOVERED_FILE).read_text())["sources"]


def test_parse_urls_recognizes_every_supported_board_shape():
    text = " ".join([
        "https://job-boards.greenhouse.io/ada18/jobs/123", "https://boards.greenhouse.io/embed/job_board?for=cohere",
        "https://boards-api.greenhouse.io/v1/boards/wealthsimple/jobs", "https://jobs.lever.co/PocketHealth/abc",
        "https://jobs.eu.lever.co/euco", "https://api.lever.co/v0/postings/apico?mode=json",
        "https://jobs.ashbyhq.com/1password/xyz", "https://cae.wd3.myworkdayjobs.com/en-US/career/job/x",
        "https://td.wd3.myworkdayjobs.com/TD_Bank_Careers", "https://wd5.myworkdaysite.com/en-US/recruiting/bell/BellCareers/job/1",
        "https://jobs.smartrecruiters.com/Ubisoft2/7440001", "https://apply.workable.com/borrowell/j/ABC123/",
        "https://medmehealth.recruitee.com/o/dev", "https://avidbots.bamboohr.com/careers/12",
        "https://allen-vanguard.workable.com",
    ])
    keys = {hit["key"] for hit in discover.parse_urls(text)}
    assert keys == {"greenhouse:ada18", "greenhouse:cohere", "greenhouse:wealthsimple", "lever:pockethealth",
                    "lever:eu:euco", "lever:apico", "ashby:1password", "workday:cae:career",
                    "workday:td:td_bank_careers", "workday:bell:bellcareers", "smartrecruiters:ubisoft2",
                    "workable:borrowell", "recruitee:medmehealth", "bamboohr:avidbots", "workable:allen-vanguard"}


def test_parse_urls_ignores_reserved_paths_and_marketing_hosts():
    text = ("https://job-boards.greenhouse.io/embed/job_app https://jobs.lever.co/ https://www.bamboohr.com/pricing "
            "https://apply.workable.com/j/ABC123 https://cae.wd3.myworkdayjobs.com/wday/cxs/cae/jobs "
            "https://soti.wd3.myworkdayjobs.com/en-US/Referral_Job_Posting_Site/job/1 "
            "https://bmo.wd3.myworkdayjobs.com/Internal_Careers https://www.recruitee.com/blog site:jobs.lever.co Toronto "
            "https://www.workable.com/pricing https://help.workable.com/hc/en-us https://apply.workable.com/")
    assert discover.parse_urls(text) == []


def test_lever_and_ashby_names_come_from_the_board_page_title(root, monkeypatch):
    monkeypatch.setattr(discover, "_page_title", lambda url: "Acceldata" if "acceldata" in url else None)
    write_leads(root, [{"url": "https://jobs.lever.co/acceldata/1", "title": "Wrong Name - Data Engineer Intern"}])
    fetcher = FakeFetcher({("lever", "acceldata"): [row("Data Engineer Intern", "Toronto, Ontario, Canada")]})
    discover.run([root / "leads"], root=root, fetcher=fetcher)
    assert registry(root)[0]["name"] == "Acceldata"


def test_run_adds_only_verified_canadian_technical_boards(root):
    write_leads(root)
    fetcher = FakeFetcher(BOARDS)
    summary = discover.run([root / "leads"], root=root, fetcher=fetcher)

    sources = {s["type"]: s for s in registry(root)}
    assert set(sources) == {"greenhouse", "workday"}
    assert summary["added_count"] == 2 and summary["already_monitored"] == 1
    assert summary["by_status"] == {"verified": 2, "no_canada_tech": 2, "not_found": 1}
    assert ("greenhouse", "known") not in fetcher.calls  # curated boards are never re-probed
    acme, maple = sources["greenhouse"], sources["workday"]
    assert acme["name"] == acme["company"] == "Acme Robotics"  # board metadata name wins
    assert acme["url"] == "https://job-boards.greenhouse.io/acme" and acme["board"] == "acme"
    assert acme["discovered"] is True and acme["probe_canada_tech_count"] == 1
    assert maple["name"] == "Maple Bank" and maple["country"] == "Canada" and maple["canada_only"] is True
    assert maple["url"] == "https://maple.wd3.myworkdayjobs.com/Maple_Careers" and maple["tenant"] == "maple"
    assert maple["max_pages"] == discover.CAPS["workday"]["max_pages"]
    unsupported = json.loads((root / "discovery" / "unsupported-leads.json").read_text())
    assert "mda space" in unsupported


def test_probe_sources_omit_company_so_adapters_report_the_api_name(root):
    write_leads(root, [{"board_url": "https://jobs.smartrecruiters.com/Ledger2"}])
    seen = []

    def fetcher(source):
        seen.append(source)
        return [row("Data Analyst Intern", "Calgary, AB", company="Ledger Company Ltd", country_hint="CA")]

    discover.run([root / "leads"], root=root, fetcher=fetcher)
    assert "company" not in seen[0] and seen[0]["max_pages"] == discover.PROBE_CAPS["smartrecruiters"]["max_pages"]
    (added,) = registry(root)
    assert added["name"] == added["company"] == "Ledger Company Ltd"
    assert added["detail_limit"] == discover.CAPS["smartrecruiters"]["detail_limit"]


def test_lead_names_and_alternate_spellings(root):
    write_leads(root, [
        {"url": "https://jobs.lever.co/PocketHealth/1", "title": "PocketHealth - Software Engineer Intern"},
        {"url": "https://jobs.lever.co/pockethealth/2", "title": "PocketHealth - Data Engineer"},
        {"url": "https://jobs.lever.co/pockethealth/3", "title": "PocketHealth - QA Analyst"},
    ])
    fetcher = FakeFetcher({("lever", "PocketHealth"): [row("Software Engineer Intern", "Toronto, Ontario, Canada")]})
    discover.run([root / "leads"], root=root, fetcher=fetcher)
    assert fetcher.calls == [("lever", "pockethealth"), ("lever", "PocketHealth")]
    (added,) = registry(root)
    assert added["name"] == "PocketHealth" and added["board"] == "PocketHealth"


def test_collect_loads_discovered_boards_after_curated_ones(root):
    write_leads(root)
    discover.run([root / "leads"], root=root, fetcher=FakeFetcher(BOARDS))
    loaded = collect.load_registry()
    assert [s["id"] for s in loaded][:1] == ["greenhouse-known"]
    assert {s["id"] for s in loaded[1:]} == {s["id"] for s in registry(root)}
    for source in loaded[1:]:
        assert all(source.get(k) for k in ("id", "name", "type", "url"))


def test_recently_checked_leads_are_skipped_then_requeued_when_due(root, monkeypatch):
    write_leads(root)
    first = FakeFetcher(BOARDS)
    discover.run([root / "leads"], root=root, fetcher=first)
    again = FakeFetcher(BOARDS)
    summary = discover.run([root / "leads"], root=root, fetcher=again)
    assert again.calls == [] and summary["recently_checked"] == 3 and summary["already_monitored"] == 3

    (root / "leads" / "scout.json").unlink()
    later = discover._now() + timedelta(days=discover.RECHECK_DAYS["no_canada_tech"] + 1)
    monkeypatch.setattr(discover, "_now", lambda: later)
    hiring = {**BOARDS, ("lever", "usonly"): [row("Firmware Co-op", "Ottawa, ON", board="usonly")]}
    summary = discover.run([root / "leads"], root=root, fetcher=FakeFetcher(hiring))
    assert summary["requeued_from_cache"] == 2  # usonly + ledger; not-found boards wait longer
    assert {s["type"] for s in registry(root)} == {"greenhouse", "workday", "lever"}


def test_failing_discovered_board_is_rechecked_and_dropped(root):
    write_leads(root)
    discover.run([root / "leads"], root=root, fetcher=FakeFetcher(BOARDS))
    acme = next(s for s in registry(root) if s["type"] == "greenhouse")
    db = root / "data" / "career.db"
    core.init_db(db)
    core.record_source(db, acme, status="error", error="HTTP 404 from public ATS endpoint")
    broken = dict(BOARDS)
    del broken[("greenhouse", "acme")]
    for attempt in range(1, discover.DROP_AFTER + 1):
        summary = discover.run([], db=db, root=root, fetcher=FakeFetcher(broken))
        assert summary["rechecked"] == 1
        names = {s["id"] for s in registry(root)}
        assert (acme["id"] in names) == (attempt < discover.DROP_AFTER)
    assert summary["dropped"] == ["Acme Robotics"]


def test_recovered_board_resets_failure_count(root):
    write_leads(root)
    discover.run([root / "leads"], root=root, fetcher=FakeFetcher(BOARDS))
    acme = next(s for s in registry(root) if s["type"] == "greenhouse")
    db = root / "data" / "career.db"
    core.init_db(db)
    core.record_source(db, acme, status="error", error="Network error")
    discover.run([], db=db, root=root, fetcher=FakeFetcher({}))
    assert next(s for s in registry(root) if s["id"] == acme["id"])["recheck_failures"] == 1
    discover.run([], db=db, root=root, fetcher=FakeFetcher(BOARDS))
    assert next(s for s in registry(root) if s["id"] == acme["id"])["recheck_failures"] == 0


def test_database_links_become_leads(root):
    db = root / "data" / "career.db"
    core.init_db(db)
    job = row("Embedded Software Intern", "Ottawa, Ontario, Canada", company="Kanata Chips")
    job["url"] = "https://jobs.lever.co/kanatachips/0b5b?utm_source=Simplify"
    with core.connection(db) as conn:
        conn.execute("INSERT INTO observations VALUES(?,?,?,?)", ("simplify", "1", "job1", json.dumps(job)))
    fetcher = FakeFetcher({("lever", "kanatachips"): [row("Embedded Software Intern", "Ottawa, Ontario, Canada")]})
    summary = discover.run([], db=db, root=root, fetcher=fetcher)
    assert summary["db_links"] == 1
    (added,) = registry(root)
    assert added["name"] == "Kanata Chips" and added["lead_origins"] == ["board-db"]


def test_time_budget_and_concurrent_run(root):
    write_leads(root)
    summary = discover.run([root / "leads"], root=root, fetcher=FakeFetcher(BOARDS), max_seconds=0)
    assert summary["probed"] == 0 and summary["not_probed_time_budget"] == 5
    lock = (root / "discovery" / "discover.lock").open("a+")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert discover.run([], root=root, fetcher=FakeFetcher({}))["skipped"] is True
    finally:
        lock.close()


def test_aggregator_and_crowd_work_boards_are_never_added(root):
    write_leads(root, [{"company": "Jobgether", "board_url": "https://jobs.lever.co/jobgether"},
                       {"company": "Mindrift", "board_url": "https://apply.workable.com/toloka-ai/"}])
    fetcher = FakeFetcher({("lever", "jobgether"): [row("Software Engineer Intern", "Toronto, ON", board="jobgether")],
                           ("workable", "toloka-ai"): [row("Data Engineer Intern", "Toronto, ON", board="toloka-ai")]})
    summary = discover.run([root / "leads"], root=root, fetcher=fetcher)
    assert summary["by_status"] == {"excluded": 2} and fetcher.calls == [] and summary["added_count"] == 0


def test_ats_sandbox_tenants_are_excluded(root):
    """Live case: an ATS-integration vendor's SmartRecruiters tenant listing 'TestE2E002',
    'Test - Kombo Disposition', 'dsa' ... beside one plausible title."""
    write_leads(root, [{"board_url": "https://jobs.smartrecruiters.com/sandbox"},
                       {"board_url": "https://jobs.smartrecruiters.com/realco"}])
    sandbox = [row(t, "Toronto, ON", board="sandbox") for t in
               ("TestE2ESmartRecruitersCA", "TestE2E002", "Test - Kombo Disposition", "dsa", "Best Backend Engineer")]
    real = [row(t, "Toronto, ON", board="realco") for t in
            ("Software Developer Intern", "QA Test Analyst", "Test Automation Developer, New Grad")]
    summary = discover.run([root / "leads"], root=root,
                           fetcher=FakeFetcher({("smartrecruiters", "sandbox"): sandbox, ("smartrecruiters", "realco"): real}))
    assert summary["by_status"] == {"excluded": 1, "verified": 1}
    assert [s["company_id"] for s in registry(root)] == ["realco"]


def test_generic_titles_alone_do_not_qualify_a_board(root):
    """Retail 'Associate' / bank 'Analyst' titles pass the cheap gate but are not technical
    by themselves; such a board qualifies only if a posting is actually accepted."""
    write_leads(root, [{"company": "Shop", "board_url": "https://jobs.lever.co/shop"},
                       {"company": "Bank", "board_url": "https://jobs.lever.co/bank"}])
    shop = [dict(row("Store Associate", "Toronto, ON", board="shop"), description="Retail floor role.")]
    bank = [dict(row("Analyst", "Toronto, ON", board="bank"), description="Computer science students. Entry level role.")]
    discover.run([root / "leads"], root=root,
                 fetcher=FakeFetcher({("lever", "shop"): shop, ("lever", "bank"): bank}))
    assert [s["board"] for s in registry(root)] == ["bank"]


def test_reverify_drops_boards_that_no_longer_qualify(root, monkeypatch):
    write_leads(root)
    discover.run([root / "leads"], root=root, fetcher=FakeFetcher(BOARDS))
    assert {s["type"] for s in registry(root)} == {"greenhouse", "workday"}
    retail = {**BOARDS, ("greenhouse", "acme"): [dict(row("Store Associate", "Toronto, ON"), description="Retail floor role.")]}
    calls = FakeFetcher(retail)
    summary = discover.run([], root=root, fetcher=calls)
    assert summary["reverified"] == 0 and calls.calls == []  # verified boards rest for 30 days
    summary = discover.run([], root=root, fetcher=FakeFetcher(retail), reverify=True)
    assert summary["reverified"] == 2 and summary["dropped"] == ["Acme Robotics"]
    assert {s["type"] for s in registry(root)} == {"workday"}
    later = discover._now() + timedelta(days=discover.RECHECK_DAYS["verified"] + 1)
    monkeypatch.setattr(discover, "_now", lambda: later)
    summary = discover.run([], root=root, fetcher=FakeFetcher(BOARDS))
    assert summary["reverified"] == 1 and summary["requeued_from_cache"] >= 1
    assert {s["type"] for s in registry(root)} == {"greenhouse", "workday"}  # hiring again: back


def test_focus_rotates_themes_and_lists_known_boards(root):
    (root / "discovery").mkdir(parents=True, exist_ok=True)
    (root / "discovery" / "unsupported-leads.json").write_text(json.dumps(
        {"mda space": {"company": "MDA Space", "board_url": "https://recruiting.ultipro.ca/MAC5000MCDW/JobBoard/x"}}))
    first = discover.focus(4, root=root)
    second = discover.focus(4, root=root)
    assert first["themes"] == discover.FOCUS[:4] and second["themes"] == discover.FOCUS[4:8]
    known = (root / "discovery" / "known-boards.txt").read_text()
    assert "https://job-boards.greenhouse.io/known" in known and "greenhouse:known" in known
    assert "unsupported system: MDA Space https://recruiting.ultipro.ca/MAC5000MCDW/JobBoard/x" in known
    assert first["known_unsupported_companies"] == 1
    assert first["leads_dir"] == str(root / "leads")
