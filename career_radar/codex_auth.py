"""ChatGPT sign-in for the ``codex`` LLM provider: device-code login and token refresh.

Uses the device-code login of the open-source Codex CLI (``codex login --device-auth``):

1. ``POST {ISSUER}/api/accounts/deviceauth/usercode`` returns a one-time ``user_code``;
2. the user opens ``{ISSUER}/codex/device`` in any browser, on any device, and enters it;
3. a daemon thread polls ``/api/accounts/deviceauth/token`` until OpenAI issues an authorization
   code, then exchanges it at ``/oauth/token`` for access, refresh and id tokens.

The tokens are kept in one private (0600) JSON file. Access tokens are refreshed shortly before
they expire. Refresh tokens are single-use, so refreshes of one token file are serialized and
re-read the file under the lock: a refresh that finds the pair already rotated adopts it instead
of replaying a spent refresh token. Keep one ``CodexAuth`` per token file for the app's lifetime
(instances on the same file share the lock and the login state anyway).
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

from . import __version__, config
from .llm import LLMError

CLIENT_ID = os.environ.get("CODEX_OAUTH_CLIENT_ID", "app_EMoamEEZ73f0CkXaXp7hrann")  # public client id of the open-source Codex CLI
ISSUER = "https://auth.openai.com"
USERCODE_URL = f"{ISSUER}/api/accounts/deviceauth/usercode"
DEVICE_TOKEN_URL = f"{ISSUER}/api/accounts/deviceauth/token"
TOKEN_URL = f"{ISSUER}/oauth/token"
VERIFICATION_URL = f"{ISSUER}/codex/device"
REDIRECT_URI = f"{ISSUER}/deviceauth/callback"
AUTH_CLAIM = "https://api.openai.com/auth"
PROFILE_CLAIM = "https://api.openai.com/profile"
RESIDENCY_HEADER = "x-openai-internal-codex-residency"
LOGIN_TIMEOUT = 15 * 60
MIN_POLL_INTERVAL = 3
MAX_POLL_FAILURES = 5
REFRESH_SKEW = 300
HTTP_TIMEOUT = 20.0
NOT_SIGNED_IN = "not signed in with ChatGPT; sign in in Settings first"
EXPIRED = "ChatGPT sign-in expired; sign in again"
RATE_LIMITED = "OpenAI is rate-limiting sign-in requests; try again in a few minutes"
_REJECTED_CODES = {"invalid_grant", "invalid_token", "refresh_token_reused", "refresh_token_expired",
                   "refresh_token_invalidated"}
_HEADERS = {"Accept": "application/json", "User-Agent": f"CareerRadar/{__version__}"}


class _LoginError(Exception):
    """A sign-in step failed; the message is shown to the user."""


class _Shared:
    """Locks and login state shared by every CodexAuth on one token file."""

    def __init__(self) -> None:
        self.refresh_lock = threading.Lock()  # token refresh/save/delete
        self.start_lock = threading.Lock()  # one start_login at a time
        self.state_lock = threading.Lock()  # login state below
        self.login: dict = {"status": "idle"}
        self.attempt = 0  # bumped by every new login and logout; a poller from an older attempt stops
        self.deadline = 0.0
        self.thread: threading.Thread | None = None


_shared: dict[str, _Shared] = {}
_shared_guard = threading.Lock()


def _shared_for(path: Path) -> _Shared:
    with _shared_guard:
        return _shared.setdefault(os.path.abspath(path), _Shared())


class CodexAuth:
    def __init__(self, path: Path, *, transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], Any] = time.sleep, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self._transport = transport
        self._sleep = sleep
        self._clock = clock
        self._shared = _shared_for(self.path)

    # -- public API ---------------------------------------------------------------------------

    def status(self) -> dict:
        tokens = self._read()
        access, refresh = tokens.get("access_token"), tokens.get("refresh_token")
        exp = _exp(access)
        logged_in = bool(refresh) or (bool(access) and (exp is None or exp > self._clock()))
        if not logged_in:
            return {"logged_in": False, "account": None, "expires_at": None}
        email = _claims(tokens.get("id_token")).get("email")
        if not isinstance(email, str) or not email:
            profile = _claims(access).get(PROFILE_CLAIM)
            email = profile.get("email") if isinstance(profile, dict) else None
        return {"logged_in": True, "account": email if isinstance(email, str) and email else None,
                "expires_at": _iso(exp) if exp is not None else None}

    def start_login(self) -> dict:
        shared = self._shared
        with shared.start_lock:
            with shared.state_lock:
                if shared.login.get("status") == "pending" and self._clock() < shared.deadline:
                    return dict(shared.login)
            try:
                device = self._request_user_code()
            except _LoginError as exc:
                with shared.state_lock:
                    shared.attempt += 1
                    shared.login = {"status": "error", "error": str(exc)}
                    return dict(shared.login)
            with shared.state_lock:
                shared.attempt += 1
                attempt = shared.attempt
                shared.deadline = deadline = self._clock() + LOGIN_TIMEOUT
                shared.login = {"status": "pending", "user_code": device["user_code"],
                                "verification_url": VERIFICATION_URL, "expires_at": _iso(deadline)}
                state = dict(shared.login)
            shared.thread = threading.Thread(target=self._run_login, args=(attempt, device, deadline),
                                             name="codex-login", daemon=True)
            shared.thread.start()
            return state

    def login_state(self) -> dict:
        with self._shared.state_lock:
            return dict(self._shared.login)

    def access_token(self, force_refresh: bool = False) -> tuple[str, str | None, dict]:
        """``(access_token, account_id, extra_headers)``, refreshed when it expires within 5 minutes."""
        tokens = self._read()
        if not tokens.get("access_token") and not tokens.get("refresh_token"):
            raise LLMError(NOT_SIGNED_IN)
        if force_refresh or not tokens.get("access_token") or self._expiring(tokens.get("access_token")):
            tokens = self._refresh(tokens, force=force_refresh)
        return _credentials(tokens)

    def logout(self) -> None:
        shared = self._shared
        with shared.state_lock:
            shared.attempt += 1
            shared.deadline = 0.0
            shared.login = {"status": "idle"}
        with shared.refresh_lock:
            self.path.unlink(missing_ok=True)

    # -- device login -------------------------------------------------------------------------

    def _request_user_code(self) -> dict:
        try:
            status, data, headers = self._post(USERCODE_URL, json_body={"client_id": CLIENT_ID})
        except httpx.HTTPError:
            raise _LoginError("could not reach auth.openai.com; check this server's internet connection") from None
        if status == 429:
            raise _LoginError(_rate_limited(headers))
        if status != 200 or not isinstance(data, dict):
            raise _LoginError(f"could not start ChatGPT sign-in (HTTP {status})")
        user_code = data.get("user_code") or data.get("usercode")
        device_auth_id = data.get("device_auth_id")
        if not (isinstance(user_code, str) and user_code and isinstance(device_auth_id, str) and device_auth_id):
            raise _LoginError("OpenAI sent an incomplete sign-in code")
        return {"device_auth_id": device_auth_id, "user_code": user_code,
                "interval": max(MIN_POLL_INTERVAL, _int(data.get("interval"), 5))}

    def _run_login(self, attempt: int, device: dict, deadline: float) -> None:
        try:
            code = self._poll_for_code(attempt, device, deadline)
            if code is None:
                return
            tokens = self._exchange(code)
            with self._shared.refresh_lock:
                if not self._current(attempt):
                    return
                self._save(tokens)
            self._finish(attempt, {"status": "complete"})
        except _LoginError as exc:
            self._finish(attempt, {"status": "error", "error": str(exc)})
        except Exception:  # a crashed poller must not leave the UI waiting on "pending"
            self._finish(attempt, {"status": "error", "error": "ChatGPT sign-in failed unexpectedly"})

    def _poll_for_code(self, attempt: int, device: dict, deadline: float) -> dict | None:
        """Wait for the user to approve the code; ``None`` when this login was cancelled or replaced."""
        interval, failures = device["interval"], 0  # transport failures in a row
        body = {"device_auth_id": device["device_auth_id"], "user_code": device["user_code"]}
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise _LoginError("Sign-in timed out")
            self._sleep(min(interval, remaining))
            if not self._current(attempt):
                return None
            try:
                status, data, headers = self._post(DEVICE_TOKEN_URL, json_body=body)
            except httpx.HTTPError:
                failures += 1
                if failures >= MAX_POLL_FAILURES:
                    raise _LoginError("lost contact with auth.openai.com while waiting for sign-in") from None
                continue
            failures = 0
            if status == 200 and isinstance(data, dict):
                return data
            if status in (403, 404):  # the user has not approved the code yet
                continue
            raise _LoginError(_rate_limited(headers) if status == 429 else f"ChatGPT sign-in failed (HTTP {status})")

    def _exchange(self, data: dict) -> dict:
        code, verifier = data.get("authorization_code"), data.get("code_verifier")
        if not (isinstance(code, str) and code and isinstance(verifier, str) and verifier):
            raise _LoginError("OpenAI sent an incomplete sign-in response")
        try:
            status, tokens, headers = self._post(TOKEN_URL, form={
                "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI,
                "client_id": CLIENT_ID, "code_verifier": verifier})
        except httpx.HTTPError:
            raise _LoginError("could not reach auth.openai.com to finish sign-in") from None
        if status == 429:
            raise _LoginError(_rate_limited(headers))
        if status != 200 or not isinstance(tokens, dict) or not _nonempty(tokens.get("access_token")):
            raise _LoginError(f"ChatGPT sign-in could not be completed (HTTP {status})")
        return tokens

    def _current(self, attempt: int) -> bool:
        with self._shared.state_lock:
            return self._shared.attempt == attempt

    def _finish(self, attempt: int, state: dict) -> None:
        with self._shared.state_lock:
            if self._shared.attempt == attempt:
                self._shared.login = state

    # -- tokens -------------------------------------------------------------------------------

    def _refresh(self, seen: dict, *, force: bool) -> dict:
        with self._shared.refresh_lock:
            stored = self._read()
            stored_access, stored_refresh = stored.get("access_token"), stored.get("refresh_token")
            if not stored_access and not stored_refresh:
                raise LLMError(NOT_SIGNED_IN)  # signed out meanwhile
            if stored_refresh != seen.get("refresh_token") and stored_access and not self._expiring(stored_access):
                return stored  # rotated by a concurrent refresh: adopt it, never replay the spent token
            if not stored_refresh:
                return self._still_valid(stored, force, EXPIRED)
            try:
                status, data, headers = self._post(TOKEN_URL, form={
                    "grant_type": "refresh_token", "refresh_token": stored_refresh, "client_id": CLIENT_ID})
            except httpx.HTTPError:
                return self._still_valid(stored, force, "could not reach auth.openai.com to renew the ChatGPT sign-in")
            if status == 200 and isinstance(data, dict) and _nonempty(data.get("access_token")):
                return self._save({"access_token": data["access_token"],
                                   "refresh_token": data.get("refresh_token") or stored_refresh,
                                   "id_token": data.get("id_token") or stored.get("id_token")})
            if status == 401 or (status == 400 and _error_code(data) in _REJECTED_CODES):
                current = self._read()  # another process may have rotated the pair meanwhile
                if current.get("access_token") and current.get("refresh_token") not in (None, stored_refresh):
                    return current
                # The refresh token is dead for good (expired, revoked or already used): forget it, so
                # status() shows signed out and later calls stop retrying it.
                self.path.unlink(missing_ok=True)
                raise LLMError(EXPIRED)
            if status == 429:
                return self._still_valid(stored, force, _rate_limited(headers))
            return self._still_valid(stored, force, f"could not renew the ChatGPT sign-in (HTTP {status})")

    def _still_valid(self, stored: dict, force: bool, message: str) -> dict:
        """After a transient refresh failure, keep using an access token that has not expired yet."""
        exp = _exp(stored.get("access_token"))
        if not force and exp is not None and exp > self._clock():
            return stored
        raise LLMError(message)

    def _expiring(self, token: Any) -> bool:
        exp = _exp(token)
        return exp is not None and exp - self._clock() <= REFRESH_SKEW

    def _save(self, tokens: dict) -> dict:
        access, id_token = tokens["access_token"], tokens.get("id_token") or None
        record = {"access_token": access, "refresh_token": tokens.get("refresh_token") or None, "id_token": id_token,
                  "account_id": _account_id(access, id_token), "obtained_at": _iso(self._clock())}
        config.write_json(self.path, record, private=True)
        return record

    def _read(self) -> dict:
        data = config.read_json(self.path, None)
        return data if isinstance(data, dict) else {}

    def _post(self, url: str, *, json_body: dict | None = None, form: dict | None = None) -> tuple[int, Any, httpx.Headers]:
        with httpx.Client(transport=self._transport, timeout=HTTP_TIMEOUT, follow_redirects=False) as client:
            response = client.post(url, json=json_body, data=form, headers=_HEADERS)
        try:
            data = response.json()
        except ValueError:
            data = None
        return response.status_code, data, response.headers


# -- helpers ----------------------------------------------------------------------------------

def _claims(token: Any) -> dict:
    """The payload of a JWT. Not verified: it is only read for routing hints and display."""
    if not isinstance(token, str) or token.count(".") < 2:
        return {}
    payload = token.split(".")[1]
    try:
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _auth_claims(token: Any) -> dict:
    claims = _claims(token).get(AUTH_CLAIM)
    return claims if isinstance(claims, dict) else {}


def _exp(token: Any) -> float | None:
    exp = _claims(token).get("exp")
    return float(exp) if isinstance(exp, (int, float)) and not isinstance(exp, bool) else None


def _account_id(*tokens: Any) -> str | None:
    for token in tokens:
        account_id = _auth_claims(token).get("chatgpt_account_id")
        if _nonempty(account_id):
            return account_id
    return None


def _credentials(tokens: dict) -> tuple[str, str | None, dict]:
    access = tokens.get("access_token")
    if not _nonempty(access):
        raise LLMError(EXPIRED)
    account_id = _account_id(access, tokens.get("id_token")) or tokens.get("account_id")
    extra = {}
    for auth in (_auth_claims(access), _auth_claims(tokens.get("id_token"))):
        residency = auth.get("chatgpt_data_residency") or auth.get("chatgpt_compute_residency")
        if _nonempty(residency) and residency.strip():
            extra[RESIDENCY_HEADER] = residency.strip()
            break
    return access, account_id if _nonempty(account_id) else None, extra


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _int(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="seconds")


def _error_code(data: Any) -> str | None:
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        error = error.get("code") or error.get("type")
    return error if isinstance(error, str) else None


def _rate_limited(headers: httpx.Headers) -> str:
    seconds = _int((headers.get("retry-after") or "").strip(), -1)
    return f"{RATE_LIMITED} (OpenAI asks to wait {seconds} seconds)" if seconds >= 0 else RATE_LIMITED
