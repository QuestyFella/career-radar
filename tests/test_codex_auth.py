"""Offline tests for career_radar.codex_auth: device-code login, token storage and refresh."""
import base64
import json
import os
import stat
import threading
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs

import httpx
import pytest

from career_radar import codex_auth
from career_radar.codex_auth import CodexAuth
from career_radar.llm import LLMClient, LLMError

NOW = 1_800_000_000.0
ISSUER = "https://auth.openai.com"
USERCODE = f"{ISSUER}/api/accounts/deviceauth/usercode"
POLL = f"{ISSUER}/api/accounts/deviceauth/token"
TOKEN = f"{ISSUER}/oauth/token"
CODEX_URL = "https://chatgpt.com/backend-api/codex/responses"
DEVICE = {"device_auth_id": "dev-1", "user_code": "ABCD-EFGH", "interval": "5"}
CODE = {"authorization_code": "auth-code", "code_challenge": "challenge", "code_verifier": "verifier"}


def jwt(claims):
    def part(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()
    return f"{part({'alg': 'RS256', 'typ': 'JWT'})}.{part(claims)}.c2lnbmF0dXJl"


def access(exp=NOW + 3600, tag="a1", account="acct-123", **auth_claims):
    auth = {"chatgpt_plan_type": "plus", **auth_claims}
    if account:
        auth["chatgpt_account_id"] = account
    return jwt({"exp": exp, "jti": tag, "https://api.openai.com/auth": auth})


def id_token(email="user@example.com"):
    return jwt({"email": email, "exp": NOW + 3600, "https://api.openai.com/auth": {"chatgpt_account_id": "acct-123"}})


def iso(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="seconds")


class Router:
    """MockTransport answering by (method, URL without query); records every request."""

    def __init__(self, routes):
        self.routes, self.requests = routes, []
        self.lock = threading.Lock()
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request):
        request.read()
        with self.lock:
            self.requests.append(request)
        answer = self.routes.get((request.method, str(request.url).split("?")[0]))
        if answer is None:
            return httpx.Response(599, text=f"unexpected {request.method} {request.url}")
        return answer(request)

    def hits(self, url):
        return [request for request in self.requests if str(request.url).split("?")[0] == url]


def respond(status=200, **kwargs):
    return lambda request: httpx.Response(status, **kwargs)


def seq(*answers):
    pending = list(answers)
    return lambda request: pending.pop(0)(request)


def form(request):
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    return {key: values[0] for key, values in parse_qs(request.content.decode()).items()}


@pytest.fixture
def token_path(tmp_path):
    return tmp_path / "data" / "codex_tokens.json"


def make(path, routes=None, *, sleep=None, clock=None):
    router = Router(routes if routes is not None else {})
    auth = CodexAuth(path, transport=router.transport, sleep=sleep or (lambda seconds: None),
                     clock=clock or (lambda: NOW))
    return auth, router


def store(path, **tokens):
    codex_auth.config.write_json(path, tokens, private=True)


def wait(auth):
    thread = auth._shared.thread
    if thread is not None:
        thread.join(5)
        assert not thread.is_alive()
    return auth.login_state()


def login_routes(overrides=None):
    tokens = {"access_token": access(), "refresh_token": "rt-1", "id_token": id_token(), "expires_in": 3600}
    return {("POST", USERCODE): respond(json=DEVICE), ("POST", POLL): respond(json=CODE),
            ("POST", TOKEN): respond(json=tokens), **(overrides or {})}


# -- device login -------------------------------------------------------------------------------

def test_constants():
    assert codex_auth.ISSUER == "https://auth.openai.com"
    assert codex_auth.CLIENT_ID == os.environ.get("CODEX_OAUTH_CLIENT_ID", "app_EMoamEEZ73f0CkXaXp7hrann")


def test_signed_out_state(token_path):
    auth, router = make(token_path)
    assert auth.status() == {"logged_in": False, "account": None, "expires_at": None}
    assert auth.login_state() == {"status": "idle"}
    with pytest.raises(LLMError, match="not signed in"):
        auth.access_token()
    assert not router.requests


def test_device_login_happy_path(token_path):
    sleeps = []
    tokens = {"access_token": access(), "refresh_token": "rt-1", "id_token": id_token(), "expires_in": 3600}
    auth, router = make(token_path, login_routes({
        ("POST", POLL): seq(respond(403, json={"error": "deviceauth_authorization_pending"}), respond(404),
                            respond(json=CODE)),
        ("POST", TOKEN): respond(json=tokens)}), sleep=sleeps.append)
    state = auth.start_login()
    assert state == {"status": "pending", "user_code": "ABCD-EFGH",
                     "verification_url": "https://auth.openai.com/codex/device", "expires_at": iso(NOW + 900)}
    assert wait(auth) == {"status": "complete"}
    assert sleeps == [5, 5, 5]
    assert json.loads(router.hits(USERCODE)[0].content) == {"client_id": codex_auth.CLIENT_ID}
    assert [json.loads(r.content) for r in router.hits(POLL)] == [{"device_auth_id": "dev-1", "user_code": "ABCD-EFGH"}] * 3
    assert form(router.hits(TOKEN)[0]) == {
        "grant_type": "authorization_code", "code": "auth-code", "client_id": codex_auth.CLIENT_ID,
        "redirect_uri": "https://auth.openai.com/deviceauth/callback", "code_verifier": "verifier"}
    assert json.loads(token_path.read_text()) == {
        "access_token": tokens["access_token"], "refresh_token": "rt-1", "id_token": tokens["id_token"],
        "account_id": "acct-123", "obtained_at": iso(NOW)}
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
    assert auth.status() == {"logged_in": True, "account": "user@example.com", "expires_at": iso(NOW + 3600)}
    assert auth.access_token() == (tokens["access_token"], "acct-123", {})
    assert len(router.hits(TOKEN)) == 1  # a fresh token is not refreshed


@pytest.mark.parametrize("interval,expected", [("1", 3), (0, 3), (None, 5), ("7", 7), ("junk", 5)])
def test_poll_interval_has_a_three_second_floor(token_path, interval, expected):
    sleeps = []
    device = {"device_auth_id": "dev-1", "user_code": "ABCD-EFGH"}
    if interval is not None:
        device["interval"] = interval
    auth, _ = make(token_path, login_routes({("POST", USERCODE): respond(json=device)}), sleep=sleeps.append)
    auth.start_login()
    assert wait(auth) == {"status": "complete"}
    assert sleeps == [expected]


def test_poller_survives_a_transient_network_error(token_path):
    def blip(request):
        raise httpx.ConnectError("connection reset", request=request)
    auth, router = make(token_path, login_routes({("POST", POLL): seq(blip, respond(json=CODE))}))
    auth.start_login()
    assert wait(auth) == {"status": "complete"}
    assert len(router.hits(POLL)) == 2


def test_start_login_while_pending_returns_the_same_login(token_path):
    gate = threading.Event()
    auth, router = make(token_path, login_routes({("POST", POLL): respond(403)}), sleep=lambda s: gate.wait(5))
    first = auth.start_login()
    assert first["status"] == "pending"
    assert auth.start_login() == first
    assert CodexAuth(token_path).login_state() == first  # instances on one file share the login
    assert len(router.hits(USERCODE)) == 1
    auth.logout()  # cancels the pending login
    gate.set()
    assert wait(auth) == {"status": "idle"}
    assert not router.hits(POLL) and not token_path.exists()


@pytest.mark.parametrize("headers,suffix", [({"Retry-After": "42"}, " (OpenAI asks to wait 42 seconds)"), ({}, "")])
def test_usercode_429_reports_rate_limiting(token_path, headers, suffix):
    auth, router = make(token_path, login_routes({("POST", USERCODE): respond(429, headers=headers,
                                                                                 json={"error": "slow down"})}))
    state = auth.start_login()
    assert state == {"status": "error",
                     "error": "OpenAI is rate-limiting sign-in requests; try again in a few minutes" + suffix}
    assert auth.login_state() == state
    assert auth._shared.thread is None and not router.hits(POLL)
    router.routes[("POST", USERCODE)] = respond(json=DEVICE)  # the limit lifted: a new attempt starts
    assert auth.start_login()["status"] == "pending"
    assert wait(auth) == {"status": "complete"}


def test_login_times_out_after_15_minutes(token_path):
    now = [NOW]

    def sleep(seconds):
        now[0] += seconds
    auth, router = make(token_path, login_routes({
        ("POST", USERCODE): respond(json={**DEVICE, "interval": "60"}), ("POST", POLL): respond(403)}),
        sleep=sleep, clock=lambda: now[0])
    auth.start_login()
    assert wait(auth) == {"status": "error", "error": "Sign-in timed out"}
    assert len(router.hits(POLL)) == 15
    assert not router.hits(TOKEN) and not token_path.exists()


@pytest.mark.parametrize("status,expected", [
    (500, "ChatGPT sign-in failed (HTTP 500)"), (400, "ChatGPT sign-in failed (HTTP 400)"),
    (429, "OpenAI is rate-limiting sign-in requests; try again in a few minutes")])
def test_unexpected_poll_status_is_an_error(token_path, status, expected):
    auth, router = make(token_path, login_routes({("POST", POLL): respond(status, json={"error": "x"})}))
    auth.start_login()
    assert wait(auth) == {"status": "error", "error": expected}
    assert len(router.hits(POLL)) == 1 and not token_path.exists()


def test_failed_code_exchange_is_an_error(token_path):
    auth, _ = make(token_path, login_routes({("POST", TOKEN): respond(400, json={"error": "invalid_grant"})}))
    auth.start_login()
    assert wait(auth) == {"status": "error", "error": "ChatGPT sign-in could not be completed (HTTP 400)"}
    assert not token_path.exists()


# -- tokens -------------------------------------------------------------------------------------

def test_status_reads_email_and_expiry(token_path):
    store(token_path, access_token=access(exp=NOW + 600), refresh_token="rt-1", id_token=id_token("me@example.org"))
    auth, _ = make(token_path)
    assert auth.status() == {"logged_in": True, "account": "me@example.org", "expires_at": iso(NOW + 600)}
    store(token_path, access_token=access(exp=NOW + 600), refresh_token="rt-1")
    assert auth.status()["account"] is None
    store(token_path, access_token=access(exp=NOW - 1))  # expired and nothing to renew it with
    assert auth.status()["logged_in"] is False


def test_account_and_residency_come_from_the_jwt(token_path):
    token = access(account=None, chatgpt_compute_residency="eu")
    store(token_path, access_token=token, refresh_token="rt-1", id_token=id_token())
    auth, _ = make(token_path)
    assert auth.access_token() == (token, "acct-123", {"x-openai-internal-codex-residency": "eu"})
    data_residency = access(chatgpt_data_residency="us", chatgpt_compute_residency="eu")
    store(token_path, access_token=data_residency, refresh_token="rt-1")
    assert auth.access_token()[2] == {"x-openai-internal-codex-residency": "us"}
    store(token_path, access_token="opaque-token", refresh_token="rt-1", account_id="acct-stored")
    assert auth.access_token() == ("opaque-token", "acct-stored", {})


def test_expiring_token_is_refreshed_and_saved(token_path):
    new = access(exp=NOW + 7200, tag="new")
    store(token_path, access_token=access(exp=NOW + 120, tag="old"), refresh_token="rt-1", id_token=id_token(),
          account_id="acct-123", obtained_at=iso(NOW - 3000))
    auth, router = make(token_path, {("POST", TOKEN): respond(json={"access_token": new,
                                                                      "id_token": id_token("new@example.com")})})
    assert auth.access_token() == (new, "acct-123", {})
    assert form(router.hits(TOKEN)[0]) == {"grant_type": "refresh_token", "refresh_token": "rt-1",
                                           "client_id": codex_auth.CLIENT_ID}
    saved = json.loads(token_path.read_text())
    assert saved["access_token"] == new and saved["refresh_token"] == "rt-1"  # none returned: keep the old one
    assert saved["obtained_at"] == iso(NOW)
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
    assert auth.status()["account"] == "new@example.com"
    auth.access_token()
    assert len(router.hits(TOKEN)) == 1


def test_valid_token_is_only_refreshed_when_forced(token_path):
    old, new = access(tag="old"), access(tag="new")
    store(token_path, access_token=old, refresh_token="rt-1")
    auth, router = make(token_path, {("POST", TOKEN): respond(json={"access_token": new, "refresh_token": "rt-2"})})
    assert auth.access_token()[0] == old and not router.requests
    assert auth.access_token(force_refresh=True)[0] == new
    assert json.loads(token_path.read_text())["refresh_token"] == "rt-2"


def test_refresh_adopts_a_pair_rotated_by_another_process(token_path, monkeypatch):
    rotated = access(tag="rotated")
    store(token_path, access_token=rotated, refresh_token="rt-2", account_id="acct-123")
    auth, router = make(token_path, {("POST", TOKEN): respond(500)})
    stale = {"access_token": access(exp=NOW + 60, tag="stale"), "refresh_token": "rt-1"}
    real_read, reads = auth._read, []

    def read():  # the first read sees the file as it was before another process refreshed it
        reads.append(1)
        return dict(stale) if len(reads) == 1 else real_read()
    monkeypatch.setattr(auth, "_read", read)
    assert auth.access_token()[0] == rotated
    assert not router.requests  # the spent refresh token was never replayed


def test_concurrent_refreshes_spend_the_refresh_token_once(token_path):
    store(token_path, access_token=access(exp=NOW + 60, tag="old"), refresh_token="rt-1")
    new, spent = access(tag="new"), []

    def refresh(request):
        spent.append(form(request)["refresh_token"])
        time.sleep(0.05)
        return httpx.Response(200, json={"access_token": new, "refresh_token": "rt-2"})
    router = Router({("POST", TOKEN): refresh})
    results = []

    def worker():
        results.append(CodexAuth(token_path, transport=router.transport, clock=lambda: NOW).access_token()[0])
    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert spent == ["rt-1"]
    assert results == [new] * 4


@pytest.mark.parametrize("status,payload", [
    (400, {"error": "invalid_grant", "error_description": "Refresh token expired"}),
    (400, {"error": {"code": "refresh_token_reused", "message": "Already used"}}),
    (401, {"error": {"code": "refresh_token_expired"}})])
def test_rejected_refresh_signs_out(token_path, status, payload):
    store(token_path, access_token=access(exp=NOW - 10), refresh_token="rt-1", id_token=id_token())
    auth, _ = make(token_path, {("POST", TOKEN): respond(status, json=payload)})
    with pytest.raises(LLMError, match="^ChatGPT sign-in expired; sign in again$"):
        auth.access_token()
    assert not token_path.exists()
    assert auth.status()["logged_in"] is False


def test_transient_refresh_failure_keeps_an_unexpired_token(token_path):
    old = access(exp=NOW + 100, tag="old")
    store(token_path, access_token=old, refresh_token="rt-1")
    auth, router = make(token_path, {("POST", TOKEN): respond(503, text="unavailable")})
    assert auth.access_token()[0] == old
    with pytest.raises(LLMError, match="HTTP 503") as info:
        auth.access_token(force_refresh=True)
    assert old not in str(info.value) and "rt-1" not in str(info.value)
    router.routes[("POST", TOKEN)] = respond(429, headers={"Retry-After": "30"})
    with pytest.raises(LLMError, match="rate-limiting"):
        auth.access_token(force_refresh=True)
    assert token_path.exists()
    store(token_path, access_token=access(exp=NOW - 1), refresh_token="rt-1")
    with pytest.raises(LLMError, match="rate-limiting"):
        auth.access_token()
    assert token_path.exists()  # a throttled refresh is not a sign-out


def test_logout_deletes_tokens(token_path):
    store(token_path, access_token=access(), refresh_token="rt-1", id_token=id_token())
    auth, _ = make(token_path)
    assert auth.status()["logged_in"] is True
    auth.logout()
    assert not token_path.exists()
    assert auth.status() == {"logged_in": False, "account": None, "expires_at": None}
    assert auth.login_state() == {"status": "idle"}
    with pytest.raises(LLMError, match="not signed in"):
        auth.access_token()
    auth.logout()  # idempotent


# -- wiring into the codex provider ------------------------------------------------------------

def test_llm_client_refreshes_the_sign_in_after_a_401(token_path):
    old, new = access(tag="old"), access(tag="new")
    store(token_path, access_token=old, refresh_token="rt-1", id_token=id_token())
    stream = ('event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"pong"}\n\n'
              'event: response.completed\ndata: {"type":"response.completed","response":{"output":[]}}\n\n')
    router = Router({
        ("POST", CODEX_URL): seq(respond(401, json={"detail": "Unauthorized"}),
                                 respond(200, text=stream, headers={"content-type": "text/event-stream"})),
        ("POST", TOKEN): respond(json={"access_token": new, "refresh_token": "rt-2"})})
    auth = CodexAuth(token_path, transport=router.transport, clock=lambda: NOW)
    assert LLMClient("codex", codex=auth, transport=router.transport).complete("Be brief.", "ping") == "pong"
    calls = router.hits(CODEX_URL)
    assert [r.headers["authorization"] for r in calls] == [f"Bearer {old}", f"Bearer {new}"]
    assert all(r.headers["chatgpt-account-id"] == "acct-123" for r in calls)
    assert json.loads(token_path.read_text())["refresh_token"] == "rt-2"


def test_llm_client_reports_a_missing_sign_in(token_path):
    client = LLMClient("codex", codex=CodexAuth(token_path, transport=Router({}).transport))
    with pytest.raises(LLMError, match="not signed in"):
        client.complete("s", "u")
