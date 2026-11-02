"""LLM providers for Career Radar: one small synchronous client over plain httpx (no SDKs).

``LLMClient(provider, model).complete(system, user)`` talks to seven providers:

* ``openai`` and ``openrouter`` (Chat Completions), ``anthropic`` (Messages) and
  ``openai-compatible`` (Ollama, LM Studio, vLLM… at a user-supplied base URL);
* ``opencode-zen`` and ``opencode-go``: OpenCode's public model catalog says which wire format and
  endpoint each model uses (Chat Completions, Anthropic Messages, OpenAI Responses or Gemini
  generateContent). The catalog is cached for an hour; a model it does not list (or a catalog
  that cannot be fetched) falls back to Chat Completions at the provider's base URL;
* ``codex``: the user's ChatGPT plan through the Codex backend (streamed Responses API), using the
  OAuth tokens kept by :class:`career_radar.codex_auth.CodexAuth`.

Failures raise :class:`LLMError` with a short message that is safe to show in the UI: it never
contains an API key or token. Every request takes an injectable ``httpx`` transport, so the tests
run offline.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterable
from urllib.parse import quote, urlsplit

import httpx

from . import __version__

if TYPE_CHECKING:  # pragma: no cover - typing only; codex_auth imports this module
    from .codex_auth import CodexAuth


class LLMError(RuntimeError):
    """An LLM call failed; the message is user-presentable and never contains a key or token."""


@dataclass(frozen=True)
class Provider:
    id: str
    label: str
    auth: str  # 'api_key' | 'optional_key' | 'oauth'
    key_env: str | None
    default_model: str
    base_url: str
    needs_base_url: bool
    docs_url: str
    notes: str = ""


# Insertion order is the order the settings UI lists them in.
PROVIDERS: dict[str, Provider] = {spec.id: spec for spec in (
    Provider("openai", "OpenAI API", "api_key", "OPENAI_API_KEY", "gpt-5.4-mini", "https://api.openai.com/v1", False,
             "https://platform.openai.com/api-keys", "Pay-as-you-go API key from the OpenAI platform."),
    Provider("anthropic", "Claude API", "api_key", "ANTHROPIC_API_KEY", "claude-haiku-4-5", "https://api.anthropic.com/v1",
             False, "https://console.anthropic.com/settings/keys", "Pay-as-you-go API key from the Anthropic Console."),
    Provider("openrouter", "OpenRouter", "api_key", "OPENROUTER_API_KEY", "openai/gpt-5.4-mini",
             "https://openrouter.ai/api/v1", False, "https://openrouter.ai/keys",
             "One key for models from many vendors."),
    Provider("opencode-zen", "OpenCode Zen (pay-as-you-go)", "api_key", "OPENCODE_ZEN_API_KEY", "claude-haiku-4-5",
             "https://opencode.ai/zen/v1", False, "https://opencode.ai/auth",
             "Pay-as-you-go OpenCode key; each model is called the way OpenCode's public Zen catalog lists it."),
    Provider("opencode-go", "OpenCode Go (subscription)", "api_key", "OPENCODE_GO_API_KEY", "deepseek-v4.1-flash",
             "https://opencode.ai/zen/go/v1", False, "https://opencode.ai/auth",
             "OpenCode Go subscription key; each model is called the way OpenCode's public Go catalog lists it."),
    Provider("codex", "ChatGPT (Codex sign-in)", "oauth", None, "gpt-5.4-mini", "https://chatgpt.com/backend-api/codex",
             False, "https://chatgpt.com/codex",
             "Uses your ChatGPT plan via the same device sign-in as the Codex CLI; no API key."),
    Provider("openai-compatible", "Custom OpenAI-compatible (Ollama, LM Studio, vLLM...)", "optional_key",
             "OPENAI_COMPATIBLE_API_KEY", "", "", True, "https://ollama.com",
             "Base URL of the server's OpenAI-compatible API, e.g. http://host.docker.internal:11434/v1 for Ollama "
             "on the Docker host. The API key is optional."),
)}

MAX_RESPONSE_BYTES = 16 * 1024 * 1024
ERROR_TEXT_LIMIT = 300
# Reasoning/thinking models spend hidden tokens out of the same output limit; extra room keeps the
# visible answer from being starved. The limit is a ceiling, not a charge. Set to 0 to disable.
REASONING_HEADROOM = 4000
CATALOG_TTL = 3600.0
CATALOG_RETRY = 300.0  # a failed catalog fetch is not retried for routing within this window
CATALOG_TIMEOUT = 15.0
LIST_TIMEOUT = 30.0
JSON_NUDGE = "Reply with only valid JSON."
ANTHROPIC_VERSION = "2023-06-01"
OPENROUTER_HEADERS = {"HTTP-Referer": "https://github.com/career-radar/career-radar", "X-Title": "Career Radar"}
CODEX_ORIGINATOR = "codex_cli_rs"
CODEX_USER_AGENT = "codex_cli_rs/0.0.0 (Career Radar)"
CODEX_MODEL_CLIENT_VERSIONS = ("99.0.0", "0.0.0")
CODEX_FALLBACK_MODELS = ("gpt-5.4-mini", "gpt-5.4", "gpt-5.5", "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol")
CODEX_DEFAULT_INSTRUCTIONS = "You are a helpful assistant."
CODEX_REJECTED = "ChatGPT sign-in expired or rejected"

_OPENCODE = ("opencode-zen", "opencode-go")
_WIRE_FORMATS = ("chat-completions", "messages", "responses", "generate-content")
_REASONING_PREFIXES = ("gpt-5", "o1", "o3", "o4")
_OPENAI_CHAT_PREFIXES = ("gpt-", "o1", "o3", "o4", "chatgpt-")
# Not chat-completions models: audio/image/embedding families, legacy completions, Responses-only models.
_OPENAI_NOT_CHAT = ("embedding", "tts", "whisper", "dall-e", "image", "audio", "realtime", "transcribe", "moderation",
                    "instruct", "codex", "-pro", "deep-research")
_HEADERS = {"Accept": "application/json", "User-Agent": f"CareerRadar/{__version__}"}
_NO_CODEX = "sign in with ChatGPT in Settings first"
_THINK = re.compile(r"\s*<think>.*?</think>", re.S | re.I)
_FENCE = re.compile(r"```[^\n`]*\n?(.*?)```", re.S)
_JSON_START = re.compile(r'\{\s*["}]|\[\s*(?:[\[{"\]\-0-9]|true\b|false\b|null\b)')
_JSON_SPECIAL = re.compile(r'[\\"{}\[\]]')
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")
_MAX_JSON_CANDIDATES = 200

# provider id -> (monotonic stamp, {model id: route} or None after a failed fetch)
_catalog_cache: dict[str, tuple[float, dict[str, dict] | None]] = {}
_clock = time.monotonic  # tests patch this to age the cache


def _clear_catalog_cache() -> None:
    """Forget the cached OpenCode catalogs."""
    _catalog_cache.clear()


class LLMClient:
    """One configured model. ``complete`` returns reply text, ``complete_json`` parsed JSON.

    ``api_key`` falls back to the provider's ``key_env`` environment variable. ``base_url`` is only
    used by providers that need one (``openai-compatible``); keys for the fixed providers are only
    ever sent to that provider's own hosts.
    """

    def __init__(self, provider: str, model: str | None = None, *, api_key: str | None = None,
                 base_url: str | None = None, codex: CodexAuth | None = None,
                 transport: httpx.BaseTransport | None = None, timeout: float = 120.0):
        spec = _provider(provider)
        self.spec = spec
        self.provider = spec.id
        self.label = spec.label
        self.base_url = _base_url(spec, base_url)
        self.model = (model or "").strip() or spec.default_model
        if not self.model:
            raise LLMError("choose a model")
        self._api_key = _api_key(spec, api_key)
        if spec.auth == "api_key" and not self._api_key:
            raise LLMError(_missing_key(spec))
        if spec.auth == "oauth" and codex is None:
            raise LLMError(_NO_CODEX)
        self._codex = codex
        self._transport = transport
        self.timeout = float(timeout)

    def __repr__(self) -> str:
        return f"LLMClient(provider={self.provider!r}, model={self.model!r})"

    def complete(self, system: str, user: str, *, max_tokens: int = 1500, json_mode: bool = False) -> str:
        system, user = (system or "").strip(), user or ""
        if json_mode and "json" not in f"{system}\n{user}".lower():
            system = f"{system}\n\n{JSON_NUDGE}".strip()
        max_tokens = max(1, int(max_tokens))
        if self.provider == "codex":
            text, cut = self._codex_complete(system, user)
        elif self.provider == "anthropic":
            text, cut = self._messages(self.base_url, system, user, max_tokens, bearer=False)
        elif self.provider in _OPENCODE:
            text, cut = self._opencode(system, user, max_tokens)
        else:
            text, cut = self._chat(self.base_url, system, user, max_tokens, json_mode=json_mode)
        text = _clean_reply(text)
        if not text:
            raise LLMError("empty reply (the model used its whole token budget before answering)" if cut
                           else "empty reply")
        return text

    def complete_json(self, system: str, user: str, *, max_tokens: int = 2500) -> dict | list:
        """Parsed JSON reply; one retry with a nudge when the first reply has no valid JSON.

        Does not use ``json_mode``: OpenAI's json_object mode cannot return a top-level array.
        """
        if "json" not in f"{system}\n{user}".lower():
            system = f"{(system or '').strip()}\n\n{JSON_NUDGE}".strip()
        reply = self.complete(system, user, max_tokens=max_tokens)
        try:
            return extract_json(reply)
        except ValueError:
            pass
        reply = self.complete(system, f"{user}\n\n{JSON_NUDGE}", max_tokens=max_tokens)
        try:
            return extract_json(reply)
        except ValueError:
            raise LLMError(f"{self.label} did not return valid JSON") from None

    # -- wire formats -------------------------------------------------------------------------

    def _chat(self, base_url: str, system: str, user: str, max_tokens: int, *, json_mode: bool = False,
              temperature: bool = True, thinking: bool = False) -> tuple[str, bool]:
        reasoning = _is_reasoning_model(self.model)
        budget = _token_budget(self.model, max_tokens, thinking)
        body: dict[str, Any] = {"model": self.model, "messages": _chat_messages(system, user)}
        if self.provider == "openai" and reasoning:
            body["max_completion_tokens"] = budget
        else:
            body["max_tokens"] = budget
            if temperature and not reasoning:
                body["temperature"] = 0.2
        if json_mode and self.provider == "openai":
            body["response_format"] = {"type": "json_object"}
        headers = self._bearer()
        if self.provider == "openrouter":
            headers.update(OPENROUTER_HEADERS)
        return _chat_reply(self._post(f"{base_url}/chat/completions", headers, body), self.label)

    def _messages(self, base_url: str, system: str, user: str, max_tokens: int, *, bearer: bool) -> tuple[str, bool]:
        # No thinking headroom: extended thinking is opt-in in the Messages API and is not requested.
        headers = {"x-api-key": self._api_key or "", "anthropic-version": ANTHROPIC_VERSION,
                   "content-type": "application/json"}
        if bearer:
            headers.update(self._bearer())
        body: dict[str, Any] = {"model": self.model, "max_tokens": max_tokens}
        if system:
            body["system"] = system
        body["messages"] = [{"role": "user", "content": user}]
        return _anthropic_reply(self._post(f"{base_url}/messages", headers, body), self.label)

    def _responses(self, base_url: str, system: str, user: str, max_tokens: int, *,
                   thinking: bool = False) -> tuple[str, bool]:
        body: dict[str, Any] = {"model": self.model}
        if system:
            body["instructions"] = system
        body.update(input=[{"role": "user", "content": [{"type": "input_text", "text": user}]}],
                    max_output_tokens=_token_budget(self.model, max_tokens, thinking), store=False)
        return _responses_reply(self._post(f"{base_url}/responses", self._bearer(), body), self.label)

    def _generate_content(self, base_url: str, system: str, user: str, max_tokens: int, *,
                          thinking: bool = False) -> tuple[str, bool]:
        body: dict[str, Any] = {}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        body.update(contents=[{"role": "user", "parts": [{"text": user}]}],
                    generationConfig={"maxOutputTokens": _token_budget(self.model, max_tokens, thinking)})
        headers = {"x-goog-api-key": self._api_key or "", **self._bearer()}
        url = f"{base_url}/models/{quote(self.model, safe='')}:generateContent"
        return _gemini_reply(self._post(url, headers, body), self.label)

    def _opencode(self, system: str, user: str, max_tokens: int) -> tuple[str, bool]:
        route = (_opencode_catalog(self.spec, self._transport) or {}).get(self.model) or {}
        wire, base, thinking = route.get("wire"), route.get("base_url"), route.get("reasoning", False)
        if wire == "messages":
            return self._messages(base, system, user, max_tokens, bearer=True)
        if wire == "responses":
            return self._responses(base, system, user, max_tokens, thinking=thinking)
        if wire == "generate-content":
            return self._generate_content(base, system, user, max_tokens, thinking=thinking)
        if wire == "chat-completions":
            return self._chat(base, system, user, max_tokens, temperature=route.get("temperature", False),
                              thinking=thinking)
        return self._chat(self.base_url, system, user, max_tokens, temperature=False)

    def _codex_complete(self, system: str, user: str) -> tuple[str, bool]:
        body = {"model": self.model, "instructions": system or CODEX_DEFAULT_INSTRUCTIONS,
                "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": user}]}],
                "store": False, "stream": True}
        result = self._codex_stream(self._codex.access_token(), body)
        if result is None:  # 401: the access token was revoked or rotated elsewhere; refresh once
            result = self._codex_stream(self._codex.access_token(force_refresh=True), body)
            if result is None:
                raise LLMError(CODEX_REJECTED)
        return result

    def _codex_stream(self, creds: tuple, body: dict) -> tuple[str, bool] | None:
        """POST one streamed Codex request; ``None`` on HTTP 401."""
        secrets = (creds[0],)
        deadline = time.monotonic() + self.timeout
        try:
            with _client(self._transport, self.timeout) as client, client.stream(
                    "POST", f"{self.base_url}/responses", headers=_codex_headers(creds, stream=True),
                    json=body) as response:
                if response.status_code == 401:
                    return None
                if not 200 <= response.status_code < 300:
                    raise _status_error(self.spec, response.status_code, _read_body(response, self.spec, deadline),
                                        secrets)
                return _codex_events(response.iter_lines(), self.label, deadline, secrets)
        except httpx.TimeoutException:
            raise LLMError(f"{self.label} timed out") from None
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise LLMError(f"could not reach {self.label}: {_describe(exc, secrets)}") from None

    # -- plumbing -----------------------------------------------------------------------------

    def _bearer(self) -> dict[str, str]:
        return _bearer(self._api_key)

    def _post(self, url: str, headers: dict, body: dict) -> Any:
        hint = ""
        if self.provider == "openai-compatible" and not self.base_url.endswith("/v1"):
            hint = " (OpenAI-compatible base URLs usually end in /v1)"
        return _request_json(self.spec, "POST", url, transport=self._transport, timeout=self.timeout,
                             headers={**_HEADERS, **headers}, json_body=body,
                             secrets=(self._api_key,) if self._api_key else (), not_found_hint=hint)


def list_models(provider: str, *, api_key: str | None = None, base_url: str | None = None,
                codex: CodexAuth | None = None, transport: httpx.BaseTransport | None = None) -> list[dict]:
    """``[{"id", "label"}]`` for the provider's selectable models; raises LLMError when they cannot be listed."""
    spec = _provider(provider)
    key = _api_key(spec, api_key)
    secrets = (key,) if key else ()
    if spec.id in _OPENCODE:
        catalog = _opencode_catalog(spec, transport, strict=True) or {}
        return _sorted_models((model_id, route["name"]) for model_id, route in catalog.items())
    if spec.id == "codex":
        return _codex_models(spec, codex, transport)
    if spec.id == "openrouter":  # public list; no key needed
        data = _get(spec, f"{spec.base_url}/models", transport=transport)
        return _sorted_models((item.get("id"), item.get("name")) for item in _model_entries(spec, data))
    if spec.id == "openai-compatible":
        root = _base_url(spec, base_url)
        data = _get(spec, f"{root}/models", transport=transport, headers=_bearer(key), secrets=secrets)
        return _sorted_models((item.get("id"), item.get("name")) for item in _model_entries(spec, data))
    if not key:
        raise LLMError(_missing_key(spec))
    if spec.id == "anthropic":  # the API lists newest first, which is the useful order
        data = _get(spec, f"{spec.base_url}/models", transport=transport, params={"limit": 100}, secrets=secrets,
                    headers={"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION})
        return _models((item.get("id"), item.get("display_name")) for item in _model_entries(spec, data))
    data = _get(spec, f"{spec.base_url}/models", transport=transport, headers=_bearer(key), secrets=secrets)
    ids = [item.get("id") for item in _model_entries(spec, data)]
    return _sorted_models((model_id, model_id) for model_id in ids if _openai_chat_model(model_id))


def extract_json(text: str) -> dict | list:
    """The JSON object/array in an LLM reply, tolerating ``` fences, leading prose and <think> blocks.

    Takes the first balanced ``{...}`` or ``[...]`` that parses (fenced blocks first; brackets inside
    strings are respected; trailing commas are forgiven). A reply whose first JSON value never closes
    is treated as truncated: ValueError, rather than returning a fragment of it.
    """
    if not isinstance(text, str):
        raise ValueError("expected text")
    cleaned = _clean_reply(text)
    for block in _FENCE.findall(cleaned):
        value = _scan_json(block)
        if value is not None:
            return value
    value = _scan_json(cleaned)
    if value is None:
        raise ValueError("no complete JSON object or array found")
    return value


# -- helpers ----------------------------------------------------------------------------------

def _provider(provider: Any) -> Provider:
    spec = PROVIDERS.get(provider) if isinstance(provider, str) else None
    if spec is None:
        raise LLMError(f"unknown LLM provider {provider!r}")
    return spec


def _api_key(spec: Provider, api_key: str | None) -> str | None:
    if spec.auth == "oauth":
        return None
    key = (api_key or "").strip() or (os.environ.get(spec.key_env or "") or "").strip()
    return key or None


def _missing_key(spec: Provider) -> str:
    return f"no API key for {spec.label}; add one in Settings or set {spec.key_env}"


def _base_url(spec: Provider, base_url: str | None) -> str:
    if not spec.needs_base_url:
        return spec.base_url
    value = (base_url or "").strip().rstrip("/")
    if not value:
        raise LLMError(f"{spec.label} needs a base URL, e.g. http://localhost:11434/v1")
    try:
        parts = urlsplit(value)
        valid = parts.scheme in ("http", "https") and bool(parts.hostname)
    except ValueError:
        valid = False
    if not valid:
        raise LLMError("the base URL must start with http:// or https://, e.g. http://localhost:11434/v1")
    return value


def _bearer(key: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"} if key else {}


def _is_reasoning_model(model: str) -> bool:
    return model.strip().lower().rsplit("/", 1)[-1].startswith(_REASONING_PREFIXES)


def _token_budget(model: str, max_tokens: int, thinking: bool = False) -> int:
    """Output-token ceiling: reasoning models get REASONING_HEADROOM on top for their hidden thinking."""
    name = model.strip().lower().rsplit("/", 1)[-1]
    thinking = thinking or name.startswith(_REASONING_PREFIXES) or name.startswith("gemini")
    return max_tokens + REASONING_HEADROOM if thinking else max_tokens


def _openai_chat_model(model_id: Any) -> bool:
    if not isinstance(model_id, str):
        return False
    lowered = model_id.lower()
    return lowered.startswith(_OPENAI_CHAT_PREFIXES) and not any(word in lowered for word in _OPENAI_NOT_CHAT)


def _chat_messages(system: str, user: str) -> list[dict]:
    return ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": user}]


def _clean_reply(text: Any) -> str:
    text = text if isinstance(text, str) else ""
    while match := _THINK.match(text):  # reasoning some local models inline before the answer
        text = text[match.end():]
    return text.strip()


def _client(transport: httpx.BaseTransport | None, timeout: float) -> httpx.Client:
    return httpx.Client(transport=transport, timeout=httpx.Timeout(timeout, connect=min(timeout, 15.0)),
                        follow_redirects=False)  # never replay key headers to a redirect target


def _send(spec: Provider, method: str, url: str, *, transport: httpx.BaseTransport | None, timeout: float,
          headers: dict | None = None, json_body: Any = None, params: dict | None = None,
          secrets: Iterable[str] = ()) -> tuple[int, bytes]:
    """One request -> ``(status, body)``; the body is read under a size cap and an overall deadline."""
    deadline = time.monotonic() + timeout
    try:
        with _client(transport, timeout) as client, client.stream(
                method, url, headers=headers, json=json_body, params=params) as response:
            return response.status_code, _read_body(response, spec, deadline)
    except httpx.TimeoutException:
        raise LLMError(f"{spec.label} timed out") from None
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        raise LLMError(f"could not reach {spec.label}: {_describe(exc, secrets)}") from None


def _read_body(response: httpx.Response, spec: Provider, deadline: float) -> bytes:
    body = bytearray()
    for chunk in response.iter_bytes():
        body += chunk
        if len(body) > MAX_RESPONSE_BYTES:
            raise LLMError(f"{spec.label} sent an oversized reply")
        if time.monotonic() > deadline:
            raise LLMError(f"{spec.label} timed out")
    return bytes(body)


def _request_json(spec: Provider, method: str, url: str, *, transport: httpx.BaseTransport | None, timeout: float,
                  headers: dict | None = None, json_body: Any = None, params: dict | None = None,
                  secrets: Iterable[str] = (), not_found_hint: str = "") -> Any:
    secrets = tuple(secrets)
    status, raw = _send(spec, method, url, transport=transport, timeout=timeout, headers=headers,
                        json_body=json_body, params=params, secrets=secrets)
    if not 200 <= status < 300:
        raise _status_error(spec, status, raw, secrets, hint=not_found_hint if status == 404 else "")
    try:
        return json.loads(raw)
    except ValueError:
        raise LLMError(f"{spec.label} sent a reply that is not JSON") from None


def _get(spec: Provider, url: str, *, transport: httpx.BaseTransport | None, headers: dict | None = None,
         params: dict | None = None, secrets: Iterable[str] = ()) -> Any:
    return _request_json(spec, "GET", url, transport=transport, timeout=LIST_TIMEOUT,
                         headers={**_HEADERS, **(headers or {})}, params=params, secrets=secrets)


def _status_error(spec: Provider, status: int, body: bytes, secrets: Iterable[str] = (), *, hint: str = "") -> LLMError:
    detail = _scrub(_error_text(body), secrets)
    if status in (401, 403):
        lead = CODEX_REJECTED if spec.auth == "oauth" else f"API key rejected by {spec.label}"
        # A 401 body can quote a masked key; a 403 body usually explains (region, model access, scopes).
        return LLMError(f"{lead}: {detail}" if status == 403 and detail else lead)
    suffix = f": {detail}" if detail else ""
    if status == 429:
        return LLMError(f"rate limited by {spec.label}{suffix}")
    return LLMError(f"{spec.label} returned HTTP {status}{suffix}{hint}")


def _error_text(body: bytes) -> str:
    """The provider's error message from a JSON, HTML or plain-text error body (≤ 300 chars)."""
    text = body.decode("utf-8", "replace").strip()
    if not text:
        return ""
    try:
        found = _find_message(json.loads(text))
    except ValueError:
        found = ""
    if not found:
        found = text
        if text.startswith("<"):
            found = re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(script|style)\b.*?</\1\s*>", " ", text))
    found = " ".join(found.split())
    return found if len(found) <= ERROR_TEXT_LIMIT else found[:ERROR_TEXT_LIMIT - 3] + "..."


def _find_message(data: Any, depth: int = 0) -> str:
    if depth > 4:
        return ""
    if isinstance(data, str):
        return data.strip()
    if isinstance(data, list):
        return next((found for item in data[:3] if (found := _find_message(item, depth + 1))), "")
    if isinstance(data, dict):
        for key in ("message", "error", "detail", "error_description", "msg", "title"):
            if key in data and (found := _find_message(data[key], depth + 1)):
                return found
    return ""


def _scrub(text: str, secrets: Iterable[str] = ()) -> str:
    for secret in secrets:
        if secret and len(secret) >= 4:
            text = text.replace(secret, "[redacted]")
    return text


def _describe(exc: Exception, secrets: Iterable[str] = ()) -> str:
    text = " ".join(str(exc).split()) or type(exc).__name__
    return _scrub(text, secrets)[:200]


# -- reply parsing ----------------------------------------------------------------------------

def _chat_reply(data: Any, label: str) -> tuple[str, bool]:
    if not isinstance(data, dict):
        raise LLMError(f"{label} sent an unexpected reply")
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        detail = _find_message(data.get("error"))  # some gateways answer 200 with an error object
        if detail:
            raise LLMError(f"{label} error: {detail[:ERROR_TEXT_LIMIT]}")
        return "", False
    choice = choices[0]
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    content = message.get("content")  # reasoning / reasoning_content fields are deliberately ignored
    if isinstance(content, list):
        text = "".join(part["text"] for part in content if isinstance(part, dict)
                       and part.get("type", "text") in ("text", "output_text") and isinstance(part.get("text"), str))
    else:
        text = content if isinstance(content, str) else ""
    if not text.strip() and choice.get("error"):
        raise LLMError(f"{label} error: {_find_message(choice['error'])[:ERROR_TEXT_LIMIT]}")
    return text, choice.get("finish_reason") == "length"


def _anthropic_reply(data: Any, label: str) -> tuple[str, bool]:
    if not isinstance(data, dict):
        raise LLMError(f"{label} sent an unexpected reply")
    blocks = data.get("content") if isinstance(data.get("content"), list) else []
    text = "".join(block["text"] for block in blocks if isinstance(block, dict)
                   and block.get("type") == "text" and isinstance(block.get("text"), str))
    return text, data.get("stop_reason") == "max_tokens"


def _responses_text(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    text = data.get("output_text")
    if isinstance(text, list):
        text = "".join(part for part in text if isinstance(part, str))
    if isinstance(text, str) and text.strip():
        return text
    parts = []
    for item in data.get("output") if isinstance(data.get("output"), list) else []:
        content = item.get("content") if isinstance(item, dict) else None
        for part in content if isinstance(content, list) else []:
            if isinstance(part, dict) and part.get("type") == "output_text" and isinstance(part.get("text"), str):
                parts.append(part["text"])
    return "".join(parts)


def _responses_reply(data: Any, label: str) -> tuple[str, bool]:
    if not isinstance(data, dict):
        raise LLMError(f"{label} sent an unexpected reply")
    text = _responses_text(data)
    if not text.strip() and data.get("error"):
        raise LLMError(f"{label} error: {_find_message(data['error'])[:ERROR_TEXT_LIMIT]}")
    return text, data.get("status") == "incomplete"


def _gemini_reply(data: Any, label: str) -> tuple[str, bool]:
    if not isinstance(data, dict):
        raise LLMError(f"{label} sent an unexpected reply")
    candidates = data.get("candidates")
    if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict):
        feedback = data.get("promptFeedback")
        reason = feedback.get("blockReason") if isinstance(feedback, dict) else None
        raise LLMError(f"{label} blocked the request ({reason})" if reason else f"{label} sent no reply")
    candidate = candidates[0]
    content = candidate.get("content") if isinstance(candidate.get("content"), dict) else {}
    parts = content.get("parts") if isinstance(content.get("parts"), list) else []
    text = "".join(part["text"] for part in parts if isinstance(part, dict)
                   and not part.get("thought") and isinstance(part.get("text"), str))
    return text, candidate.get("finishReason") == "MAX_TOKENS"


def _codex_events(lines: Iterable[str], label: str, deadline: float, secrets: Iterable[str]) -> tuple[str, bool]:
    """Read a Codex Responses SSE stream up to its terminal event."""
    parts: list[str] = []
    event, data, size = "", [], 0
    for line in lines:
        size += len(line) + 1
        if size > MAX_RESPONSE_BYTES:
            raise LLMError(f"{label} sent an oversized reply")
        if time.monotonic() > deadline:
            raise LLMError(f"{label} timed out")
        if not line:
            if data and (done := _codex_event(event, "\n".join(data), parts, label, secrets)):
                return done
            event, data = "", []
        elif not line.startswith(":"):
            field, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if field == "event":
                event = value
            elif field == "data":
                data.append(value)
    if data and (done := _codex_event(event, "\n".join(data), parts, label, secrets)):
        return done
    raise LLMError(f"{label} stream ended before the reply was complete")


def _codex_event(event: str, payload: str, parts: list[str], label: str,
                 secrets: Iterable[str]) -> tuple[str, bool] | None:
    if payload.strip() == "[DONE]":
        return "".join(parts), False
    try:
        message = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(message, dict):
        return None
    kind = message.get("type") or event
    response = message.get("response") if isinstance(message.get("response"), dict) else {}
    if kind == "response.output_text.delta":
        if isinstance(message.get("delta"), str):
            parts.append(message["delta"])
    elif kind == "response.completed":
        return "".join(parts) or _responses_text(response), False
    elif kind == "response.incomplete":
        text = "".join(parts) or _responses_text(response)
        if not text.strip():
            details = response.get("incomplete_details")
            reason = details.get("reason") if isinstance(details, dict) else None
            raise LLMError(f"{label} stopped before answering ({reason or 'incomplete'})")
        return text, True
    elif kind in ("response.failed", "error"):
        detail = _find_message(response.get("error") if kind == "response.failed" else message) or "request failed"
        raise LLMError(f"{label} error: {_scrub(' '.join(detail.split()), secrets)[:ERROR_TEXT_LIMIT]}")
    return None


def _codex_headers(creds: tuple, *, stream: bool) -> dict[str, str]:
    token, account_id, extra = creds
    headers = {"Authorization": f"Bearer {token}", "originator": CODEX_ORIGINATOR, "User-Agent": CODEX_USER_AGENT,
               "Accept": "text/event-stream" if stream else "application/json"}
    if account_id:
        headers["ChatGPT-Account-ID"] = account_id
    headers.update({name: value for name, value in (extra or {}).items() if isinstance(value, str) and value})
    if stream:
        headers["session_id"] = str(uuid.uuid4())
    return headers


# -- model lists and the OpenCode catalog -----------------------------------------------------

def _models(pairs: Iterable[tuple[Any, Any]]) -> list[dict]:
    seen, items = set(), []
    for model_id, label in pairs:
        if not isinstance(model_id, str) or not model_id.strip() or model_id.strip() in seen:
            continue
        model_id = model_id.strip()
        seen.add(model_id)
        items.append({"id": model_id, "label": label.strip() if isinstance(label, str) and label.strip() else model_id})
    return items


def _sorted_models(pairs: Iterable[tuple[Any, Any]]) -> list[dict]:
    return sorted(_models(pairs), key=lambda item: (item["label"].lower(), item["id"]))


def _model_entries(spec: Provider, data: Any) -> list[dict]:
    entries = None
    if isinstance(data, dict):
        entries = data.get("data") if isinstance(data.get("data"), list) else data.get("models")
    if not isinstance(entries, list):
        raise LLMError(f"{spec.label} sent an unexpected model list")
    return [entry for entry in entries if isinstance(entry, dict)]


def _codex_models(spec: Provider, codex: CodexAuth | None, transport: httpx.BaseTransport | None) -> list[dict]:
    if codex is None:
        raise LLMError(_NO_CODEX)
    creds, refreshed = codex.access_token(), False
    for version in CODEX_MODEL_CLIENT_VERSIONS:
        while True:
            try:
                status, raw = _send(spec, "GET", f"{spec.base_url}/models", transport=transport, timeout=LIST_TIMEOUT,
                                    headers=_codex_headers(creds, stream=False), params={"client_version": version},
                                    secrets=(creds[0],))
            except LLMError:
                status, raw = 0, b""
            if status == 401 and not refreshed:
                creds, refreshed = codex.access_token(force_refresh=True), True
                continue
            break
        items = _codex_catalog(raw) if status == 200 else []
        if items:
            return items
    return [{"id": slug, "label": slug} for slug in CODEX_FALLBACK_MODELS]


def _codex_catalog(raw: bytes) -> list[dict]:
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    entries = data.get("models") if isinstance(data, dict) else None
    rows = []
    for index, entry in enumerate(entries if isinstance(entries, list) else []):
        if not isinstance(entry, dict) or not isinstance(entry.get("slug"), str):
            continue
        visibility = entry.get("visibility")
        if isinstance(visibility, str) and visibility.strip().lower() in ("hide", "hidden"):
            continue
        priority = entry.get("priority")
        rank = priority if isinstance(priority, (int, float)) and not isinstance(priority, bool) else 10_000
        rows.append((rank, index, entry["slug"], entry.get("display_name")))
    rows.sort(key=lambda row: (row[0], row[1]))
    return _models((slug, label) for _, _, slug, label in rows)


def _opencode_catalog(spec: Provider, transport: httpx.BaseTransport | None, *,
                      strict: bool = False) -> dict[str, dict] | None:
    """``{model id: {"name", "wire", "base_url", "temperature"}}`` from OpenCode's public catalog.

    Cached per provider for CATALOG_TTL. For routing, a failed fetch is remembered for CATALOG_RETRY
    (callers fall back to Chat Completions) and a stale catalog keeps serving; ``strict`` (model
    listing) ignores the remembered failure and raises when there is nothing to show.
    """
    now = _clock()
    cached = _catalog_cache.get(spec.id)
    if cached:
        stamp, catalog = cached
        if catalog is not None and now - stamp < CATALOG_TTL:
            return catalog
        if catalog is None and not strict and now - stamp < CATALOG_RETRY:
            return None
    try:
        catalog = _fetch_opencode_catalog(spec, transport)
    except LLMError:
        stale = cached[1] if cached else None
        _catalog_cache[spec.id] = (now - CATALOG_TTL + CATALOG_RETRY, stale) if stale else (now, None)
        if stale is None and strict:
            raise
        return stale
    _catalog_cache[spec.id] = (now, catalog)
    return catalog


def _fetch_opencode_catalog(spec: Provider, transport: httpx.BaseTransport | None) -> dict[str, dict]:
    data = _request_json(spec, "GET", f"{spec.base_url}/models", transport=transport, timeout=CATALOG_TIMEOUT,
                         headers=dict(_HEADERS))
    catalog: dict[str, dict] = {}
    for entry in _model_entries(spec, data):
        model_id = entry.get("id")
        if not isinstance(model_id, str) or not model_id.strip():
            continue
        name = entry.get("name")
        capabilities = entry.get("capabilities") if isinstance(entry.get("capabilities"), dict) else {}
        reasoning = capabilities.get("reasoning")
        wire, base = _catalog_route(entry.get("api")) or (None, None)
        catalog[model_id.strip()] = {"name": name.strip() if isinstance(name, str) and name.strip() else model_id.strip(),
                                     "wire": wire, "base_url": base,
                                     "temperature": capabilities.get("temperature") is True,
                                     "reasoning": isinstance(reasoning, dict) and reasoning.get("supported") is True}
    if not catalog:
        raise LLMError(f"{spec.label} model catalog is empty")
    return catalog


def _catalog_route(api: Any) -> tuple[str, str] | None:
    """The (wire format, base URL) of a catalog entry; only https URLs on opencode.ai get the key."""
    if not isinstance(api, dict):
        return None
    for wire, info in api.items():
        url = info.get("base_url") if isinstance(info, dict) else None
        if wire in _WIRE_FORMATS and isinstance(url, str) and _is_opencode_url(url.strip()):
            return wire, url.strip().rstrip("/")
    return None


def _is_opencode_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
        host, port = parts.hostname or "", parts.port
    except ValueError:
        return False
    return (parts.scheme == "https" and parts.username is None and port in (None, 443)
            and (host == "opencode.ai" or host.endswith(".opencode.ai")))


# -- JSON extraction --------------------------------------------------------------------------

def _scan_json(text: str) -> dict | list | None:
    position = 0
    for _ in range(_MAX_JSON_CANDIDATES):
        match = _JSON_START.search(text, position)
        if match is None:
            return None
        start = match.start()
        end = _balanced_end(text, start)
        if end is None:  # the first JSON value never closes: a truncated reply, not a fragment source
            return None
        if end > start:
            candidate = text[start:end]
            for attempt in (candidate, _TRAILING_COMMA.sub(r"\1", candidate)):
                try:
                    value = json.loads(attempt)
                except ValueError:
                    continue
                if isinstance(value, (dict, list)):
                    return value
        position = start + 1
    return None


def _balanced_end(text: str, start: int) -> int | None:
    """Index after the bracket closing ``text[start]`` (strings respected); ``start`` on a mismatch,
    ``None`` when the text ends first."""
    stack: list[str] = []
    in_string, skip = False, -1
    for match in _JSON_SPECIAL.finditer(text, start):
        index, char = match.start(), match.group()
        if index < skip:
            continue
        if in_string:
            if char == "\\":
                skip = index + 2
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "{[":
            stack.append("}" if char == "{" else "]")
        elif char in "}]":
            if not stack or stack.pop() != char:
                return start
            if not stack:
                return index + 1
    return None
