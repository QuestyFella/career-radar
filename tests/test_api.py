"""API tests use a disposable SQLite DB and synthetic fixture only."""
import asyncio
import csv
import io
from datetime import datetime, timedelta, timezone
import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect
from career_radar import core
from career_radar.app import create_app


def plain(job):
    """API job without the request-time profile ``match`` field (not stored in the DB)."""
    return {k: v for k, v in job.items() if k != "match"}


def client(tmp_path, title="Junior Software Engineer", **job_fields):
    db = tmp_path / "api-test.db"
    core.upsert_jobs(db, [{"source_id":"fixture", "source":"Fixture", "source_type":"test", "source_job_id":"1", "title":title,
        "company":"Test Company", "location":"Toronto, Canada", "url":"https://example.com/jobs/1", "description":"0-2 years experience; CS degree", "verification":"direct", **job_fields}])
    return TestClient(create_app(db_path=db, refresh_callback=lambda: None))


def test_health_and_real_counts(tmp_path):
    c = client(tmp_path)
    assert c.get("/api/health").json()["ok"] is True
    assert c.get("/api/stats").json()["total"] == 1
    assert c.get("/api/jobs").json()["total"] == 1


def test_persistence_and_detail(tmp_path):
    c = client(tmp_path)
    job = plain(c.get("/api/jobs").json()["items"][0])
    assert c.patch(f"/api/jobs/{job['id']}", json={"status":"saved"}).json()["status"] == "saved"
    assert c.get(f"/api/jobs/{job['id']}").json()["status"] == "saved"
    assert c.get("/api/jobs?status=saved").json()["total"] == 1
    assert c.get("/api/jobs?status=applied").json()["total"] == 0


def test_validation(tmp_path):
    c = client(tmp_path)
    job = plain(c.get("/api/jobs").json()["items"][0])
    assert c.patch(f"/api/jobs/{job['id']}", json={"status":"hacked"}).status_code == 422
    assert c.get("/api/jobs?page_size=1000000").status_code == 422
    assert c.get("/api/jobs?sort=garbage").status_code == 422
    assert c.get("/api/jobs/not-a-real-job").status_code == 404


def streamed_patch(c, job_id, chunks, headers=None):
    """ASGITransport preserves chunks; TestClient may eagerly join them."""
    async def send():
        async def body():
            for chunk in chunks:
                yield chunk

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=c.app), base_url="http://testserver") as http:
            return await http.patch(f"/api/jobs/{job_id}", content=body(),
                                    headers={"Content-Type": "application/json", **(headers or {})})

    return asyncio.run(send())


def test_chunked_patch_rejects_over_limit_before_reading_remainder(tmp_path):
    c = client(tmp_path)
    job = plain(c.get("/api/jobs").json()["items"][0])
    payload = b'{"status":"saved"}'
    consumed = []

    def chunks():
        for chunk in (payload, b" " * (8192 - len(payload)), b" ", b" " * 100):
            consumed.append(chunk)
            yield chunk

    response = streamed_patch(c, job["id"], chunks())
    assert "content-length" not in response.request.headers
    assert response.request.headers["transfer-encoding"] == "chunked"
    assert response.status_code == 413
    assert response.json() == {"detail": "Request too large"}
    assert len(consumed) == 3
    assert core.get_job(c.app.state.db, job["id"]) == job


@pytest.mark.parametrize("size", [32, 8192], ids=["small", "at-limit"])
def test_chunked_patch_replays_valid_json(tmp_path, size):
    c = client(tmp_path)
    job = plain(c.get("/api/jobs").json()["items"][0])
    payload = b'{"status":"saved"}'
    chunks = [payload[:7], b"", payload[7:], b" " * (size - len(payload))]
    response = streamed_patch(c, job["id"], chunks, {"Content-Type": "application/json; charset=utf-8"})
    assert "content-length" not in response.request.headers
    assert response.status_code == 200
    assert response.json()["status"] == "saved"
    assert c.get(f"/api/jobs/{job['id']}").json()["status"] == "saved"


@pytest.mark.parametrize("size, expected", [(8192, 200), (8193, 413)])
def test_patch_content_length_boundary(tmp_path, size, expected):
    c = client(tmp_path)
    job = plain(c.get("/api/jobs").json()["items"][0])
    payload = b'{"status":"saved"}'
    response = c.patch(f"/api/jobs/{job['id']}", content=payload + b" " * (size - len(payload)),
                       headers={"Content-Type": "application/json"})
    assert response.request.headers["content-length"] == str(size)
    assert response.status_code == expected
    assert core.get_job(c.app.state.db, job["id"])["status"] == ("saved" if expected == 200 else "new")


@pytest.mark.parametrize("content_length", ["not-a-number", "-1", "+18", "1_8", "1.5", ""])
def test_patch_invalid_content_length(tmp_path, content_length):
    c = client(tmp_path)
    job = plain(c.get("/api/jobs").json()["items"][0])
    response = c.patch(f"/api/jobs/{job['id']}", json={"status": "saved"},
                       headers={"Content-Length": content_length})
    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid content length"}
    assert core.get_job(c.app.state.db, job["id"]) == job


def test_patch_underreported_content_length_cannot_bypass_limit(tmp_path):
    c = client(tmp_path)
    job = plain(c.get("/api/jobs").json()["items"][0])
    response = streamed_patch(c, job["id"], [b'{"status":"saved"}', b" " * 8192],
                              {"Content-Length": "1"})
    assert response.status_code == 413
    assert core.get_job(c.app.state.db, job["id"]) == job


def test_chunked_patch_invalid_json_still_returns_validation_error(tmp_path):
    c = client(tmp_path)
    job = plain(c.get("/api/jobs").json()["items"][0])
    response = streamed_patch(c, job["id"], [b'{"status":', b"invalid}"])
    assert response.status_code == 422
    assert core.get_job(c.app.state.db, job["id"]) == job


def test_interrupted_patch_body_does_not_raise_server_error(tmp_path):
    c = client(tmp_path)
    job = plain(c.get("/api/jobs").json()["items"][0])

    def chunks():
        yield b'{"status":'
        raise ClientDisconnect()

    response = streamed_patch(c, job["id"], chunks())
    assert response.status_code == 400
    assert core.get_job(c.app.state.db, job["id"]) == job


def test_cross_site_writes_rejected(tmp_path):
    c = client(tmp_path)
    job = plain(c.get("/api/jobs").json()["items"][0])
    assert c.patch(f"/api/jobs/{job['id']}", json={"status":"applied"}, headers={"Origin":"https://evil.example"}).status_code == 403
    assert c.post("/api/refresh", content="", headers={"Content-Type":"application/x-www-form-urlencoded"}).status_code == 415
    assert c.get("/api/health", headers={"Host":"evil.example"}).status_code == 400


def test_security_headers_and_no_file_exposure(tmp_path):
    c = client(tmp_path)
    r = c.get("/api/health")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert c.get("/.env").status_code == 404
    assert c.get("/data/career.db").status_code == 404


def test_export_and_no_formula_injection(tmp_path):
    c = client(tmp_path, title="=Junior Software Engineer")
    r = c.get("/api/export.csv")
    assert r.status_code == 200 and "text/csv" in r.headers["content-type"]
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert rows[0]["title"].startswith("'=")
    assert rows[0]["company"] == "Test Company"


def test_deadlines_are_not_faked(tmp_path):
    c = client(tmp_path)
    assert c.get("/api/deadlines").json()["items"] == []
    assert c.get("/api/jobs").json()["items"][0]["deadline"] is None


@pytest.fixture(params=[
    ("2026-11-06", "date", "2026-11-07T04:59:59+00:00"),
    ("2026-11-06T17:00:00-05:00", "datetime", "2026-11-06T22:00:00+00:00"),
], ids=["date-only", "explicit-time"])
def expiring_job(tmp_path, monkeypatch, request):
    deadline, kind, cutoff = request.param
    collected_at = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(core, "utcnow", lambda: collected_at)
    c = client(tmp_path, deadline=deadline, deadline_evidence=f"Apply by {deadline}")
    job = plain(c.get("/api/jobs").json()["items"][0])
    job = core.set_status(c.app.state.db, job["id"], "saved")
    assert job["active"] is True
    assert job["deadline_kind"] == kind
    assert job["deadline"] == cutoff
    if kind == "date":
        assert job["deadline_date"] == deadline
    return c, job, datetime.fromisoformat(cutoff)


def database_snapshot(c):
    with core.connection(c.app.state.db) as conn:
        return tuple(conn.iterdump())


@pytest.mark.parametrize("seconds", [-1, 0, 1], ids=["before", "at-cutoff", "expired"])
def test_detail_uses_current_deadline_without_writing(expiring_job, monkeypatch, seconds):
    c, job, cutoff = expiring_job
    monkeypatch.setattr(core, "utcnow", lambda: cutoff + timedelta(seconds=seconds))
    before = database_snapshot(c)
    expected_active = seconds <= 0
    assert c.get("/api/jobs").json()["total"] == int(expected_active)
    response = c.get(f"/api/jobs/{job['id']}")
    assert response.status_code == 200
    assert plain(response.json()) == job | {"active": expected_active}
    assert database_snapshot(c) == before


def test_export_omits_expired_rows_without_writing(expiring_job, monkeypatch):
    c, job, cutoff = expiring_job
    additions = [
        job | {"source_job_id": name, "title": f"Junior Software Engineer {name}",
               "url": f"https://example.com/jobs/{name}", "deadline": deadline, "is_closed": closed,
               "deadline_evidence": f"Apply by {deadline}" if deadline else None}
        for name, deadline, closed in [
            ("ongoing", None, False),
            ("future", core.iso(cutoff + timedelta(days=2)), False),
            ("source-closed", None, True),
            ("dismissed", None, False),
        ]
    ]
    core.upsert_jobs(c.app.state.db, additions)
    for stored in core.all_jobs(c.app.state.db):
        if stored["source_job_id"] in {"future", "dismissed"}:
            core.set_status(c.app.state.db, stored["id"],
                            "applied" if stored["source_job_id"] == "future" else "dismissed")
    monkeypatch.setattr(core, "utcnow", lambda: cutoff + timedelta(seconds=1))
    before = database_snapshot(c)
    assert core.get_job(c.app.state.db, job["id"])["active"] is True
    response = c.get("/api/export.csv")
    assert response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert {row["title"]: row["status"] for row in rows} == {
        "Junior Software Engineer ongoing": "new",
        "Junior Software Engineer future": "applied",
    }
    assert database_snapshot(c) == before


def test_include_closed_retains_expired_jobs_without_writing(expiring_job, monkeypatch):
    c, job, cutoff = expiring_job
    monkeypatch.setattr(core, "utcnow", lambda: cutoff + timedelta(seconds=1))
    before = database_snapshot(c)
    assert c.get("/api/jobs").json()["total"] == 0
    response = c.get("/api/jobs", params={"include_closed": True, "status": "saved", "page_size": 1})
    assert response.status_code == 200
    body = response.json()
    body["items"] = [plain(item) for item in body["items"]]
    assert body == {"items": [job | {"active": False}], "total": 1, "page": 1, "page_size": 1, "pages": 1}
    assert database_snapshot(c) == before


def test_status_patch_on_expired_job_only_persists_user_status(expiring_job, monkeypatch):
    c, job, cutoff = expiring_job
    monkeypatch.setattr(core, "utcnow", lambda: cutoff + timedelta(seconds=1))
    response = c.patch(f"/api/jobs/{job['id']}", json={"status": "applied"})
    assert response.status_code == 200
    assert plain(response.json()) == job | {"status": "applied", "active": False}
    assert core.get_job(c.app.state.db, job["id"]) == job | {"status": "applied"}


def test_current_deadline_does_not_reactivate_source_closed_job(expiring_job):
    c, job, cutoff = expiring_job
    core.upsert_jobs(c.app.state.db, [job | {"is_closed": True}])
    before = database_snapshot(c)
    assert c.get(f"/api/jobs/{job['id']}").json()["active"] is False
    assert list(csv.DictReader(io.StringIO(c.get("/api/export.csv").text))) == []
    assert c.get("/api/jobs", params={"include_closed": True}).json()["total"] == 1
    assert database_snapshot(c) == before

