"""Offline tests for career_radar.llm: request shapes, reply parsing, routing and error mapping."""
import json
import uuid

import httpx
import pytest

from career_radar import llm
from career_radar.llm import PROVIDERS, LLMClient, LLMError, extract_json, list_models

KEY = "sk-test-SECRET-0123456789"
HEADROOM = llm.REASONING_HEADROOM
OPENAI_CHAT = "https://api.openai.com/v1/chat/completions"
ZEN_CATALOG = "https://opencode.ai/zen/v1/models"
GO_CATALOG = "https://opencode.ai/zen/go/v1/models"
CODEX_URL = "https://chatgpt.com/backend-api/codex/responses"
CODEX_MODELS = "https://chatgpt.com/backend-api/codex/models"


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    for spec in PROVIDERS.values():
        if spec.key_env:
            monkeypatch.delenv(spec.key_env, raising=False)
    llm._clear_catalog_cache()
    yield
    llm._clear_catalog_cache()


class Router:
    """MockTransport answering by (method, URL without query); records every request."""

    def __init__(self, routes):
        self.routes = routes
        self.requests = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request):
        request.read()
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


def fail(exc_type, message="boom"):
    def raise_(request):
        raise exc_type(message, request=request)
    return raise_


def chat_json(text, finish="stop", **message):
    return {"choices": [{"index": 0, "message": {"role": "assistant", "content": text, **message},
                         "finish_reason": finish}]}


def body(request):
    return json.loads(request.content)


def openai_client(router, **kwargs):
    return LLMClient("openai", kwargs.pop("model", None), api_key=KEY, transport=router.transport, **kwargs)


# -- registry and constructor -----------------------------------------------------------------

def test_provider_registry_order_and_fields():
    assert list(PROVIDERS) == ["openai", "anthropic", "openrouter", "opencode-zen", "opencode-go", "codex",
                               "openai-compatible"]
    assert {p.id: p.auth for p in PROVIDERS.values()} == {
        "openai": "api_key", "anthropic": "api_key", "openrouter": "api_key", "opencode-zen": "api_key",
        "opencode-go": "api_key", "codex": "oauth", "openai-compatible": "optional_key"}
    assert {p.id: p.key_env for p in PROVIDERS.values()} == {
        "openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "openrouter": "OPENROUTER_API_KEY",
        "opencode-zen": "OPENCODE_ZEN_API_KEY", "opencode-go": "OPENCODE_GO_API_KEY", "codex": None,
        "openai-compatible": "OPENAI_COMPATIBLE_API_KEY"}
    assert {p.id: p.default_model for p in PROVIDERS.values()} == {
        "openai": "gpt-5.4-mini", "anthropic": "claude-haiku-4-5", "openrouter": "openai/gpt-5.4-mini",
        "opencode-zen": "claude-haiku-4-5", "opencode-go": "deepseek-v4.1-flash", "codex": "gpt-5.4-mini",
        "openai-compatible": ""}
    assert [p.id for p in PROVIDERS.values() if p.needs_base_url] == ["openai-compatible"]
    assert all(p.docs_url.startswith("https://") and p.label for p in PROVIDERS.values())
    assert "host.docker.internal:11434/v1" in PROVIDERS["openai-compatible"].notes


def test_constructor_validation(monkeypatch):
    with pytest.raises(LLMError, match="unknown"):
        LLMClient("nope")
    with pytest.raises(LLMError, match="OPENAI_API_KEY"):
        LLMClient("openai")
    with pytest.raises(LLMError, match="base URL"):
        LLMClient("openai-compatible", "llama3.2:1b")
    with pytest.raises(LLMError, match="http"):
        LLMClient("openai-compatible", "llama3.2:1b", base_url="ftp://example.com/v1")
    with pytest.raises(LLMError, match="choose a model"):
        LLMClient("openai-compatible", base_url="http://localhost:11434/v1")
    with pytest.raises(LLMError, match="ChatGPT"):
        LLMClient("codex")
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    client = LLMClient("anthropic")
    assert (client.provider, client.model) == ("anthropic", "claude-haiku-4-5")
    assert KEY not in repr(client) and KEY not in str(vars(client).get("model"))
    assert LLMClient("openai", "gpt-4.1", api_key=KEY).model == "gpt-4.1"
    local = LLMClient("openai-compatible", "llama3.2:1b", base_url="http://localhost:11434/v1/")
    assert local.base_url == "http://localhost:11434/v1"


# -- request shapes -----------------------------------------------------------------------------

def test_openai_reasoning_model_request_shape_and_reply():
    router = Router({("POST", OPENAI_CHAT): respond(json=chat_json("Hi there"))})
    assert openai_client(router).complete("Be brief.", "Say hi") == "Hi there"
    request = router.requests[0]
    assert request.headers["authorization"] == f"Bearer {KEY}"
    sent = body(request)
    assert sent["model"] == "gpt-5.4-mini"
    assert sent["messages"] == [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "Say hi"}]
    assert sent["max_completion_tokens"] == 1500 + HEADROOM
    assert "max_tokens" not in sent and "temperature" not in sent and "response_format" not in sent


@pytest.mark.parametrize("model,reasoning", [
    ("gpt-5.4", True), ("gpt-5", True), ("o1-mini", True), ("o3", True), ("o4-mini", True),
    ("gpt-4.1-mini", False), ("gpt-4o", False), ("chatgpt-4o-latest", False)])
def test_openai_token_parameter_switch(model, reasoning):
    router = Router({("POST", OPENAI_CHAT): respond(json=chat_json("ok"))})
    openai_client(router, model=model).complete("s", "u", max_tokens=700)
    sent = body(router.requests[0])
    if reasoning:
        assert sent["max_completion_tokens"] == 700 + HEADROOM
        assert "temperature" not in sent and "max_tokens" not in sent
    else:
        assert sent["max_tokens"] == 700 and sent["temperature"] == 0.2
        assert "max_completion_tokens" not in sent


def test_openai_json_mode_sets_response_format_and_mentions_json():
    router = Router({("POST", OPENAI_CHAT): respond(json=chat_json('{"a": 1}'))})
    openai_client(router).complete("Be brief.", "List colours", json_mode=True)
    sent = body(router.requests[0])
    assert sent["response_format"] == {"type": "json_object"}
    assert "JSON" in sent["messages"][0]["content"]  # OpenAI rejects json_object without the word


def test_chat_reply_joins_content_parts_and_ignores_reasoning_fields():
    message = {"reasoning_content": "secret chain of thought", "reasoning": "more thoughts"}
    reply = chat_json([{"type": "text", "text": "Hello "}, {"type": "text", "text": "world"}], **message)
    router = Router({("POST", OPENAI_CHAT): respond(json=reply)})
    assert openai_client(router).complete("s", "u") == "Hello world"


def test_anthropic_request_shape_and_text_blocks():
    reply = {"content": [{"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": "Hello"},
                         {"type": "tool_use", "id": "t1", "name": "x", "input": {}}, {"type": "text", "text": " there"}],
             "stop_reason": "end_turn"}
    router = Router({("POST", "https://api.anthropic.com/v1/messages"): respond(json=reply)})
    client = LLMClient("anthropic", api_key=KEY, transport=router.transport)
    assert client.complete("sys", "hi", max_tokens=321) == "Hello there"
    request = router.requests[0]
    assert request.headers["x-api-key"] == KEY
    assert request.headers["anthropic-version"] == "2023-06-01"
    assert request.headers["content-type"] == "application/json"
    assert "authorization" not in request.headers
    assert body(request) == {"model": "claude-haiku-4-5", "max_tokens": 321, "system": "sys",
                             "messages": [{"role": "user", "content": "hi"}]}


def test_openrouter_headers_and_max_tokens():
    router = Router({("POST", "https://openrouter.ai/api/v1/chat/completions"): respond(json=chat_json("ok"))})
    LLMClient("openrouter", api_key=KEY, transport=router.transport).complete("s", "u", max_tokens=100)
    request = router.requests[0]
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert request.headers["http-referer"] == "https://github.com/career-radar/career-radar"
    assert request.headers["x-title"] == "Career Radar"
    sent = body(request)
    assert sent["model"] == "openai/gpt-5.4-mini"
    assert sent["max_tokens"] == 100 + HEADROOM  # vendor-prefixed reasoning model: still max_tokens here
    assert "max_completion_tokens" not in sent and "temperature" not in sent


def test_openai_compatible_without_key_sends_no_authorization():
    reply = chat_json("<think>pondering</think>\npong", reasoning_content="thinking...")
    router = Router({("POST", "http://localhost:11434/v1/chat/completions"): respond(json=reply)})
    client = LLMClient("openai-compatible", "llama3.2:1b", base_url="http://localhost:11434/v1/",
                       transport=router.transport)
    assert client.complete("", "ping", max_tokens=50) == "pong"
    request = router.requests[0]
    assert "authorization" not in request.headers
    assert body(request) == {"model": "llama3.2:1b", "messages": [{"role": "user", "content": "ping"}],
                             "max_tokens": 50, "temperature": 0.2}


def test_openai_compatible_key_from_environment(monkeypatch):
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", KEY)
    router = Router({("POST", "https://llm.internal.example/v1/chat/completions"): respond(json=chat_json("ok"))})
    LLMClient("openai-compatible", "qwen", base_url="https://llm.internal.example/v1",
              transport=router.transport).complete("s", "u")
    assert router.requests[0].headers["authorization"] == f"Bearer {KEY}"


# -- error mapping ------------------------------------------------------------------------------

def raised(router, **kwargs):
    with pytest.raises(LLMError) as info:
        openai_client(router, **kwargs).complete("s", "u")
    message = str(info.value)
    assert KEY not in message
    return message


def test_401_maps_to_rejected_key_without_leaking_it():
    router = Router({("POST", OPENAI_CHAT): respond(401, json={"error": {"message": f"Incorrect API key: {KEY}"}})})
    assert raised(router) == "API key rejected by OpenAI API"


def test_403_maps_to_rejected_key():
    router = Router({("POST", OPENAI_CHAT): respond(403, json={"error": {"message": "Region not supported"}})})
    assert raised(router).startswith("API key rejected by OpenAI API")


def test_429_maps_to_rate_limited():
    router = Router({("POST", OPENAI_CHAT): respond(429, json={"error": {"message": "Slow down"}})})
    assert raised(router).startswith("rate limited by OpenAI API")


def test_500_includes_status_and_truncated_provider_message():
    long_message = "Upstream exploded. " * 40 + KEY
    router = Router({("POST", OPENAI_CHAT): respond(500, json={"error": {"message": long_message}})})
    message = raised(router)
    assert "500" in message and "Upstream exploded." in message
    assert len(message.split(": ", 1)[1]) <= 300


def test_error_text_from_html_and_scrubbed_echoed_key():
    html = "<html><head><style>h1{color:red}</style></head><body><h1>Bad gateway</h1></body></html>"
    message = raised(Router({("POST", OPENAI_CHAT): respond(502, text=html)}))
    assert "502" in message and "Bad gateway" in message and "<" not in message and "color" not in message
    echoed = Router({("POST", OPENAI_CHAT): respond(400, json={"error": {"message": f"key {KEY} is malformed"}})})
    assert "[redacted]" in raised(echoed)


def test_timeout_and_connection_errors():
    assert raised(Router({("POST", OPENAI_CHAT): fail(httpx.ReadTimeout)})) == "OpenAI API timed out"
    assert raised(Router({("POST", OPENAI_CHAT): fail(httpx.ConnectError, "[Errno 111] refused")})).startswith(
        "could not reach OpenAI API")


def test_redirects_are_not_followed():
    router = Router({("POST", OPENAI_CHAT): respond(307, headers={"Location": "https://evil.example/collect"})})
    assert "307" in raised(router)
    assert len(router.requests) == 1


@pytest.mark.parametrize("reply", [chat_json(""), chat_json(None), chat_json("   "), {"choices": []},
                                   chat_json("", finish="length")])
def test_empty_reply(reply):
    assert raised(Router({("POST", OPENAI_CHAT): respond(json=reply)})).startswith("empty reply")


def test_non_json_success_body():
    assert "not JSON" in raised(Router({("POST", OPENAI_CHAT): respond(text="<html>hello</html>")}))


# -- JSON extraction ----------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ('{"a": 1}', {"a": 1}),
    ('```json\n{"a": [1, 2]}\n```', {"a": [1, 2]}),
    ('Here you go:\n```\n[{"b": true}]\n```\nDone.', [{"b": True}]),
    ('Sure! Result:\n{"title": "Dev {Jr}", "tags": ["a]b"]}\nLet me know.', {"title": "Dev {Jr}", "tags": ["a]b"]}),
    ('Escaped: {"q": "say \\"}\\" now", "n": {"m": {}}}', {"q": 'say "}" now', "n": {"m": {}}}),
    ("Items: [1, 2, 3] done", [1, 2, 3]),
    ('[{"id": 1}, {"id": 2}]', [{"id": 1}, {"id": 2}]),
    ('<think>{"draft": 1}</think>{"final": 2}', {"final": 2}),
    ('Use {name} placeholders. Answer: {"ok": true}', {"ok": True}),
    ('{"a": 1, "b": [2,],}', {"a": 1, "b": [2]}),
])
def test_extract_json(text, expected):
    assert extract_json(text) == expected


@pytest.mark.parametrize("text", ["no json here", "", '{"a": [1, 2', "{oops}", "[x]", '{"a": 1]'])
def test_extract_json_failures(text):
    with pytest.raises(ValueError):
        extract_json(text)


def test_complete_json_retries_once_with_nudge():
    router = Router({("POST", OPENAI_CHAT): seq(respond(json=chat_json("I think it is a junior role")),
                                                respond(json=chat_json('```json\n{"ok": true}\n```')))})
    assert openai_client(router).complete_json("Return JSON.", "Classify this") == {"ok": True}
    first, second = (body(request) for request in router.requests)
    assert "response_format" not in first  # json_object mode would forbid top-level arrays
    assert first["max_completion_tokens"] == 2500 + HEADROOM
    assert first["messages"][-1]["content"] == "Classify this"
    assert second["messages"][-1]["content"].endswith("Reply with only valid JSON.")


def test_complete_json_returns_arrays_and_mentions_json():
    router = Router({("POST", OPENAI_CHAT): respond(json=chat_json('Sure: [{"theme": "co-op"}]'))})
    assert openai_client(router).complete_json("List themes.", "Go") == [{"theme": "co-op"}]
    assert "JSON" in body(router.requests[0])["messages"][0]["content"]


def test_complete_json_gives_up_after_one_retry():
    router = Router({("POST", OPENAI_CHAT): seq(respond(json=chat_json("nope")), respond(json=chat_json("still no")))})
    with pytest.raises(LLMError, match="valid JSON"):
        openai_client(router).complete_json("Return JSON.", "x")
    assert len(router.requests) == 2


# -- OpenCode catalog routing -------------------------------------------------------------------

CATALOG = {"object": "list", "data": [
    {"id": "claude-haiku-4-5", "name": "Claude Haiku 4.5",
     "api": {"messages": {"base_url": "https://opencode.ai/inference/anthropic/v1", "sdk": {}}}},
    {"id": "deepseek-v4.1-flash", "name": "DeepSeek V4.1 Flash", "capabilities": {"reasoning": {"supported": True}},
     "api": {"chat-completions": {"base_url": "https://opencode.ai/inference/openai/v1"}}},
    {"id": "gpt-5.4-mini", "name": "GPT-5.4 Mini", "api": {"responses": {"base_url": "https://opencode.ai/inference/openai/v1"}}},
    {"id": "gemini-3.5-flash", "name": "Gemini 3.5 Flash",
     "api": {"generate-content": {"base_url": "https://opencode.ai/inference/google/v1beta"}}},
    {"id": "big-pickle", "name": "Big Pickle", "capabilities": {"temperature": True},
     "api": {"chat-completions": {"base_url": "https://opencode.ai/inference/openai/v1"}}},
]}


def zen(routes, catalog_status=200, catalog=CATALOG, catalog_url=ZEN_CATALOG):
    return Router({("GET", catalog_url): respond(catalog_status, json=catalog), **routes})


def zen_client(router, model=None, provider="opencode-zen"):
    return LLMClient(provider, model, api_key=KEY, transport=router.transport)


def test_opencode_messages_route():
    reply = {"content": [{"type": "text", "text": "claude says hi"}], "stop_reason": "end_turn"}
    router = zen({("POST", "https://opencode.ai/inference/anthropic/v1/messages"): respond(json=reply)})
    assert zen_client(router).complete("sys", "hi", max_tokens=200) == "claude says hi"
    catalog_request, request = router.requests
    assert "authorization" not in catalog_request.headers and "x-api-key" not in catalog_request.headers
    assert request.headers["x-api-key"] == KEY
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert request.headers["anthropic-version"] == "2023-06-01"
    assert body(request) == {"model": "claude-haiku-4-5", "max_tokens": 200, "system": "sys",
                             "messages": [{"role": "user", "content": "hi"}]}


def test_opencode_chat_completions_route():
    router = zen({("POST", "https://opencode.ai/inference/openai/v1/chat/completions"): respond(json=chat_json("deep"))})
    assert zen_client(router, "deepseek-v4.1-flash").complete("sys", "hi", max_tokens=200) == "deep"
    request = router.requests[1]
    assert request.headers["authorization"] == f"Bearer {KEY}"
    sent = body(request)
    assert sent["model"] == "deepseek-v4.1-flash" and sent["max_tokens"] == 200 + HEADROOM
    assert "temperature" not in sent  # the catalog does not advertise temperature support
    zen_client(router, "big-pickle").complete("sys", "hi", max_tokens=200)
    assert body(router.requests[-1])["temperature"] == 0.2 and body(router.requests[-1])["max_tokens"] == 200


def test_opencode_responses_route():
    url = "https://opencode.ai/inference/openai/v1/responses"
    output = {"output": [{"type": "reasoning", "summary": []}, {"type": "message", "content": [
        {"type": "output_text", "text": "part one, "}, {"type": "output_text", "text": "part two"}]}]}
    router = zen({("POST", url): seq(respond(json={"output_text": "from responses"}), respond(json=output))})
    client = zen_client(router, "gpt-5.4-mini")
    assert client.complete("sys", "hi", max_tokens=200) == "from responses"
    request = router.requests[1]
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert body(request) == {"model": "gpt-5.4-mini", "instructions": "sys",
                             "input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
                             "max_output_tokens": 200 + HEADROOM, "store": False}
    assert client.complete("sys", "hi") == "part one, part two"


def test_opencode_generate_content_route():
    url = "https://opencode.ai/inference/google/v1beta/models/gemini-3.5-flash:generateContent"
    reply = {"candidates": [{"content": {"role": "model", "parts": [
        {"text": "thinking...", "thought": True}, {"text": "Hel"}, {"text": "lo"}]}, "finishReason": "STOP"}]}
    router = zen({("POST", url): respond(json=reply)})
    assert zen_client(router, "gemini-3.5-flash").complete("sys", "hi", max_tokens=200) == "Hello"
    request = router.requests[1]
    assert request.headers["x-goog-api-key"] == KEY
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert body(request) == {"systemInstruction": {"parts": [{"text": "sys"}]},
                             "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
                             "generationConfig": {"maxOutputTokens": 200 + HEADROOM}}


def test_opencode_falls_back_to_chat_completions_when_catalog_fails():
    fallback = "https://opencode.ai/zen/v1/chat/completions"
    router = zen({("POST", fallback): respond(json=chat_json("fallback ok"))}, catalog_status=500)
    client = zen_client(router)
    assert client.complete("s", "u") == "fallback ok"
    assert client.complete("s", "u") == "fallback ok"
    assert len(router.hits(ZEN_CATALOG)) == 1  # a failed fetch is not retried on every call
    assert body(router.hits(fallback)[0])["model"] == "claude-haiku-4-5"
    assert router.hits(fallback)[0].headers["authorization"] == f"Bearer {KEY}"


def test_opencode_model_missing_from_catalog_falls_back():
    router = zen({("POST", "https://opencode.ai/zen/v1/chat/completions"): respond(json=chat_json("ok"))})
    assert zen_client(router, "brand-new-model").complete("s", "u") == "ok"


def test_opencode_catalog_never_routes_the_key_off_opencode():
    catalog = {"data": [{"id": "claude-haiku-4-5", "name": "x", "api": {"messages": {"base_url": "https://evil.example/v1"}}}]}
    router = zen({("POST", "https://opencode.ai/zen/v1/chat/completions"): respond(json=chat_json("safe"))},
                 catalog=catalog)
    assert zen_client(router).complete("s", "u") == "safe"
    assert not [request for request in router.requests if request.url.host == "evil.example"]


def test_opencode_catalog_cached_for_an_hour(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(llm, "_clock", lambda: now[0])
    router = zen({("POST", "https://opencode.ai/inference/anthropic/v1/messages"):
                  respond(json={"content": [{"type": "text", "text": "ok"}]})})
    client = zen_client(router)
    client.complete("s", "u")
    client.complete("s", "u")
    assert len(router.hits(ZEN_CATALOG)) == 1
    now[0] += 3599
    client.complete("s", "u")
    assert len(router.hits(ZEN_CATALOG)) == 1
    now[0] += 2
    client.complete("s", "u")
    assert len(router.hits(ZEN_CATALOG)) == 2
    llm._clear_catalog_cache()
    client.complete("s", "u")
    assert len(router.hits(ZEN_CATALOG)) == 3


def test_opencode_go_uses_its_own_catalog_and_fallback():
    catalog = {"data": [{"id": "deepseek-v4.1-flash", "name": "DeepSeek V4.1 Flash",
                         "api": {"chat-completions": {"base_url": "https://opencode.ai/inference/go/openai/v1"}}}]}
    routed = "https://opencode.ai/inference/go/openai/v1/chat/completions"
    router = zen({("POST", routed): respond(json=chat_json("go"))}, catalog=catalog, catalog_url=GO_CATALOG)
    assert zen_client(router, provider="opencode-go").complete("s", "u") == "go"
    assert body(router.hits(routed)[0])["model"] == "deepseek-v4.1-flash"
    llm._clear_catalog_cache()
    fallback = "https://opencode.ai/zen/go/v1/chat/completions"
    down = zen({("POST", fallback): respond(json=chat_json("go fallback"))}, catalog_status=404, catalog_url=GO_CATALOG)
    assert zen_client(down, provider="opencode-go").complete("s", "u") == "go fallback"


def test_opencode_list_models_uses_catalog_names():
    router = zen({})
    assert list_models("opencode-zen", transport=router.transport) == [
        {"id": "big-pickle", "label": "Big Pickle"}, {"id": "claude-haiku-4-5", "label": "Claude Haiku 4.5"},
        {"id": "deepseek-v4.1-flash", "label": "DeepSeek V4.1 Flash"},
        {"id": "gemini-3.5-flash", "label": "Gemini 3.5 Flash"}, {"id": "gpt-5.4-mini", "label": "GPT-5.4 Mini"}]
    with pytest.raises(LLMError):
        llm._clear_catalog_cache()
        list_models("opencode-go", transport=zen({}, catalog_status=500, catalog_url=GO_CATALOG).transport)


# -- Codex (ChatGPT sign-in) --------------------------------------------------------------------

class FakeCodex:
    def __init__(self):
        self.calls = []

    def access_token(self, force_refresh=False):
        self.calls.append(force_refresh)
        if force_refresh:
            return "token-2", "acct-2", {}
        return "token-1", "acct-1", {"x-openai-internal-codex-residency": "us"}


def sse(*events):
    return "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events).encode()


def stream(*events):
    return lambda request: httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse(*events))


def delta(text):
    return {"type": "response.output_text.delta", "delta": text}


COMPLETED = {"type": "response.completed", "response": {"id": "r1", "status": "completed", "output": []}}


def codex_client(router, codex=None, **kwargs):
    return LLMClient("codex", codex=codex or FakeCodex(), transport=router.transport, **kwargs)


def test_codex_streams_deltas_with_codex_headers_and_body():
    router = Router({("POST", CODEX_URL): stream({"type": "response.created", "response": {"id": "r1"}},
                                                 delta("Hello"), delta(", world"),
                                                 {"type": "response.output_text.done", "text": "Hello, world"}, COMPLETED)})
    codex = FakeCodex()
    client = codex_client(router, codex)
    assert client.model == "gpt-5.4-mini"
    assert client.complete("Be brief.", "Greet", max_tokens=99) == "Hello, world"
    headers = router.requests[0].headers
    assert headers["authorization"] == "Bearer token-1"
    assert headers["chatgpt-account-id"] == "acct-1"
    assert headers["x-openai-internal-codex-residency"] == "us"
    assert headers["originator"] == "codex_cli_rs"
    assert headers["user-agent"] == "codex_cli_rs/0.0.0 (Career Radar)"
    assert headers["accept"] == "text/event-stream"
    uuid.UUID(headers["session_id"])
    assert body(router.requests[0]) == {
        "model": "gpt-5.4-mini", "instructions": "Be brief.",
        "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Greet"}]}],
        "store": False, "stream": True}
    assert codex.calls == [False]


def test_codex_uses_completed_output_when_no_deltas():
    completed = {"type": "response.completed", "response": {"output": [
        {"type": "reasoning", "summary": []},
        {"type": "message", "content": [{"type": "output_text", "text": "Final answer"}]}]}}
    assert codex_client(Router({("POST", CODEX_URL): stream(completed)})).complete("s", "u") == "Final answer"


@pytest.mark.parametrize("event,expected", [
    ({"type": "response.failed", "response": {"error": {"code": "server_error", "message": "The model is overloaded"}}},
     "The model is overloaded"),
    ({"type": "error", "code": "rate_limit_exceeded", "message": "Rate limit reached for gpt-5.4-mini"},
     "Rate limit reached")])
def test_codex_failure_events_raise(event, expected):
    router = Router({("POST", CODEX_URL): stream(delta("partial"), event)})
    with pytest.raises(LLMError, match=expected):
        codex_client(router).complete("s", "u")


def test_codex_stream_without_terminal_event_raises():
    with pytest.raises(LLMError, match="ended"):
        codex_client(Router({("POST", CODEX_URL): stream(delta("cut off"))})).complete("s", "u")


def test_codex_401_forces_one_refresh_then_retries():
    router = Router({("POST", CODEX_URL): seq(respond(401, json={"detail": "token expired"}),
                                              stream(delta("ok"), COMPLETED))})
    codex = FakeCodex()
    assert codex_client(router, codex).complete("s", "u") == "ok"
    assert codex.calls == [False, True]
    assert [r.headers["authorization"] for r in router.requests] == ["Bearer token-1", "Bearer token-2"]
    assert router.requests[1].headers["chatgpt-account-id"] == "acct-2"


def test_codex_second_401_reports_rejected_sign_in():
    router = Router({("POST", CODEX_URL): respond(401, json={"detail": "nope"})})
    codex = FakeCodex()
    with pytest.raises(LLMError, match="ChatGPT sign-in expired or rejected"):
        codex_client(router, codex).complete("s", "u")
    assert codex.calls == [False, True] and len(router.requests) == 2


def test_codex_429_is_rate_limited():
    router = Router({("POST", CODEX_URL): respond(429, json={"error": {"message": "The usage limit has been reached"}})})
    with pytest.raises(LLMError, match="rate limited by ChatGPT") as info:
        codex_client(router).complete("s", "u")
    assert "usage limit" in str(info.value) and "token-1" not in str(info.value)


def test_codex_list_models_uses_catalog_and_auth_headers():
    catalog = {"models": [{"slug": "gpt-5.5", "display_name": "GPT-5.5", "priority": 2},
                          {"slug": "gpt-5.4-mini", "display_name": "GPT-5.4 Mini", "priority": 1},
                          {"slug": "internal-only", "visibility": "hide"}]}
    router = Router({("GET", CODEX_MODELS): respond(json=catalog)})
    models = list_models("codex", codex=FakeCodex(), transport=router.transport)
    assert models == [{"id": "gpt-5.4-mini", "label": "GPT-5.4 Mini"}, {"id": "gpt-5.5", "label": "GPT-5.5"}]
    request = router.requests[0]
    assert request.url.params["client_version"] == "99.0.0"
    assert request.headers["authorization"] == "Bearer token-1"
    assert request.headers["chatgpt-account-id"] == "acct-1"


def test_codex_list_models_retries_old_client_version_then_static_fallback():
    router = Router({("GET", CODEX_MODELS): seq(respond(json={"models": []}),
                                                respond(json={"models": [{"slug": "gpt-5.4", "display_name": "GPT-5.4"}]}))})
    assert list_models("codex", codex=FakeCodex(), transport=router.transport) == [{"id": "gpt-5.4", "label": "GPT-5.4"}]
    assert [r.url.params["client_version"] for r in router.requests] == ["99.0.0", "0.0.0"]
    down = Router({("GET", CODEX_MODELS): respond(500, text="oops")})
    ids = [item["id"] for item in list_models("codex", codex=FakeCodex(), transport=down.transport)]
    assert ids == ["gpt-5.4-mini", "gpt-5.4", "gpt-5.5", "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"]
    assert len(down.requests) == 2


# -- model lists --------------------------------------------------------------------------------

def test_openai_list_models_filters_to_chat_models():
    ids = ["gpt-5.4-mini", "gpt-4.1", "o3", "o4-mini", "chatgpt-4o-latest", "text-embedding-3-large", "tts-1",
           "whisper-1", "dall-e-3", "gpt-image-1", "gpt-4o-audio-preview", "gpt-4o-realtime-preview",
           "gpt-4o-transcribe", "omni-moderation-latest", "babbage-002", "gpt-4o-mini-tts", "text-moderation-latest"]
    router = Router({("GET", "https://api.openai.com/v1/models"): respond(json={"data": [{"id": i} for i in ids]})})
    models = list_models("openai", api_key=KEY, transport=router.transport)
    assert [m["id"] for m in models] == ["chatgpt-4o-latest", "gpt-4.1", "gpt-5.4-mini", "o3", "o4-mini"]
    assert all(m["label"] == m["id"] for m in models)
    assert router.requests[0].headers["authorization"] == f"Bearer {KEY}"


def test_anthropic_list_models():
    data = {"data": [{"id": "claude-sonnet-4-5", "display_name": "Claude Sonnet 4.5"},
                     {"id": "claude-haiku-4-5", "display_name": "Claude Haiku 4.5"}]}
    router = Router({("GET", "https://api.anthropic.com/v1/models"): respond(json=data)})
    assert list_models("anthropic", api_key=KEY, transport=router.transport) == [
        {"id": "claude-sonnet-4-5", "label": "Claude Sonnet 4.5"}, {"id": "claude-haiku-4-5", "label": "Claude Haiku 4.5"}]
    request = router.requests[0]
    assert request.url.params["limit"] == "100"
    assert request.headers["x-api-key"] == KEY and request.headers["anthropic-version"] == "2023-06-01"


def test_openrouter_list_models_is_public():
    data = {"data": [{"id": "openai/gpt-5.4-mini", "name": "OpenAI: GPT-5.4 Mini"},
                     {"id": "anthropic/claude-haiku-4.5", "name": "Anthropic: Claude Haiku 4.5"}]}
    router = Router({("GET", "https://openrouter.ai/api/v1/models"): respond(json=data)})
    assert list_models("openrouter", transport=router.transport) == [
        {"id": "anthropic/claude-haiku-4.5", "label": "Anthropic: Claude Haiku 4.5"},
        {"id": "openai/gpt-5.4-mini", "label": "OpenAI: GPT-5.4 Mini"}]
    assert "authorization" not in router.requests[0].headers


def test_openai_compatible_list_models():
    data = {"object": "list", "data": [{"id": "llama3.2:1b"}, {"id": "tinyllama:latest"}, {"id": "llama3.2:latest"}]}
    router = Router({("GET", "http://localhost:11434/v1/models"): respond(json=data)})
    models = list_models("openai-compatible", base_url="http://localhost:11434/v1/", transport=router.transport)
    assert [m["id"] for m in models] == ["llama3.2:1b", "llama3.2:latest", "tinyllama:latest"]
    assert "authorization" not in router.requests[0].headers


def test_list_models_errors():
    with pytest.raises(LLMError, match="unknown"):
        list_models("nope")
    with pytest.raises(LLMError, match="OPENAI_API_KEY"):
        list_models("openai")
    with pytest.raises(LLMError, match="base URL"):
        list_models("openai-compatible")
    with pytest.raises(LLMError, match="ChatGPT"):
        list_models("codex")
    rejected = Router({("GET", "https://api.openai.com/v1/models"): respond(401, json={"error": {"message": KEY}})})
    with pytest.raises(LLMError, match="API key rejected by OpenAI API") as info:
        list_models("openai", api_key=KEY, transport=rejected.transport)
    assert KEY not in str(info.value)
