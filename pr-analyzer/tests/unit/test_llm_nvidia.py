"""The NVIDIA provider: NVIDIA NIM's OpenAI-compatible chat API, hosted or self-hosted."""
import json
from unittest.mock import patch

import httpx
import pytest

from pr_analyzer.cache import open_cache
from pr_analyzer.config import settings
from pr_analyzer.exceptions import ConfigError, LLMError
from pr_analyzer.llm import engine as engine_module
from pr_analyzer.llm.engine import LLMEngine
from pr_analyzer.llm.prompts import SYSTEM_PROMPT
from pr_analyzer.models import FileChange, PRSummary
from pr_analyzer.pipeline import AnalysisPipeline

KEY = "nvapi-test-key-0123456789"
REPLY = '{"suggestions": []}'


@pytest.fixture
def nvidia(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "nvidia")
    monkeypatch.setattr(settings, "nvidia_api_key", KEY)
    monkeypatch.setattr(settings, "llm_max_tokens", 2048)
    waits: list[float] = []
    monkeypatch.setattr(engine_module, "sleep", waits.append)
    return waits


def completion(content: str, finish_reason: str = "stop", **message) -> dict:
    return {"choices": [{"index": 0, "finish_reason": finish_reason,
                         "message": {"role": "assistant", "content": content, **message}}]}


def engine_answering(*responses: httpx.Response, seen: list | None = None) -> LLMEngine:
    replies = iter(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return next(replies)

    return LLMEngine(transport=httpx.MockTransport(handler))


def make_pr() -> PRSummary:
    return PRSummary(
        number=7, title="Add a helper", author="neil", repo="neil/demo", base_branch="main",
        head_branch="feature", total_additions=1, total_deletions=0, is_draft=False,
        files=[FileChange(filename="app.py", status="modified", additions=1, deletions=0,
                          patch="@@ -1,1 +1,2 @@\n x = 1\n+y = 2")],
    )


def test_sends_an_openai_style_chat_request(nvidia):
    seen: list[httpx.Request] = []
    engine = engine_answering(httpx.Response(200, json=completion(REPLY)), seen=seen)

    assert engine.call_nvidia("the prompt") == REPLY

    request = seen[0]
    assert str(request.url) == "https://integrate.api.nvidia.com/v1/chat/completions"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    body = json.loads(request.content)
    assert body["model"] == "nvidia/nemotron-3-super-120b-a12b"
    assert body["messages"] == [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "the prompt"},
    ]
    assert body["max_tokens"] == 2048
    assert body["stream"] is False
    assert body["chat_template_kwargs"] == {"enable_thinking": False}


def test_model_and_thinking_come_from_the_settings(nvidia, monkeypatch):
    monkeypatch.setattr(settings, "llm_model", "deepseek-ai/deepseek-v4.1-flash")
    monkeypatch.setattr(settings, "nvidia_thinking", True)
    seen: list[httpx.Request] = []
    engine_answering(httpx.Response(200, json=completion(REPLY)), seen=seen).call_nvidia("p")

    body = json.loads(seen[0].content)
    assert body["model"] == "deepseek-ai/deepseek-v4.1-flash"
    assert body["chat_template_kwargs"] == {"enable_thinking": True}


@pytest.mark.parametrize("content", [
    "<think>Is y unused? {maybe}</think>\n" + REPLY,
    "the model's thoughts, with the opening tag in its prompt</think>" + REPLY,
])
def test_thinking_before_the_answer_is_dropped(nvidia, content):
    engine = engine_answering(httpx.Response(200, json=completion(content)))
    assert engine.call_nvidia("p") == REPLY


def test_reasoning_sent_apart_from_the_answer_is_ignored(nvidia):
    reply = completion(REPLY, reasoning_content="first, look at app.py {")
    assert engine_answering(httpx.Response(200, json=reply)).call_nvidia("p") == REPLY


def test_a_reply_cut_off_at_max_tokens_is_an_error(nvidia):
    # its JSON is broken, and used to pass as a review that found nothing
    engine = engine_answering(httpx.Response(200, json=completion('{"suggestions": [{"ti', "length")))
    with pytest.raises(LLMError, match="LLM_MAX_TOKENS"):
        engine.call_nvidia("p")


def test_a_reply_without_choices_is_empty(nvidia):
    assert engine_answering(httpx.Response(200, json={"choices": []})).call_nvidia("p") == ""


def test_the_hosted_api_needs_a_key(nvidia, monkeypatch):
    monkeypatch.setattr(settings, "nvidia_api_key", "")
    with pytest.raises(LLMError, match="NVIDIA_API_KEY"):
        engine_answering().call_nvidia("p")


def test_a_self_hosted_nim_needs_no_key(nvidia, monkeypatch):
    monkeypatch.setattr(settings, "nvidia_api_key", "")
    monkeypatch.setattr(settings, "nvidia_base_url", "http://nim.local:8000/v1/")
    seen: list[httpx.Request] = []
    engine = engine_answering(httpx.Response(200, json=completion(REPLY)), seen=seen)

    assert engine.call_nvidia("p") == REPLY
    assert str(seen[0].url) == "http://nim.local:8000/v1/chat/completions"
    assert "authorization" not in seen[0].headers


def test_a_rate_limit_is_waited_out_then_retried(nvidia):
    engine = engine_answering(
        httpx.Response(429, headers={"Retry-After": "3"}, json={"detail": "slow down"}),
        httpx.Response(200, json=completion(REPLY)),
    )
    assert engine.call_nvidia("p") == REPLY
    assert nvidia == [3.0]


def test_server_errors_are_retried_twice_then_reported(nvidia):
    engine = engine_answering(*[httpx.Response(503, text="overloaded")] * 3)
    with pytest.raises(LLMError, match="HTTP 503: overloaded"):
        engine.call_nvidia("p")
    assert nvidia == [1.0, 2.0]


def test_a_long_retry_after_is_not_waited_for(nvidia):
    engine = engine_answering(httpx.Response(429, headers={"Retry-After": "120"}, text="quota"))
    with pytest.raises(LLMError, match="HTTP 429"):
        engine.call_nvidia("p")
    assert nvidia == []


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_key_says_which_setting_to_check(nvidia, status):
    engine = engine_answering(httpx.Response(status, json={"detail": "Unauthorized"}))
    with pytest.raises(LLMError, match="NVIDIA_API_KEY"):
        engine.call_nvidia("p")


def test_an_unknown_model_is_reported_with_the_reply(nvidia):
    engine = engine_answering(httpx.Response(404, text="Function not found for account"))
    with pytest.raises(LLMError, match="HTTP 404: Function not found"):
        engine.call_nvidia("p")


def test_an_unreachable_api_is_an_llm_error(nvidia):
    def handler(request):
        raise httpx.ConnectError("connection refused")

    with pytest.raises(LLMError, match="Could not reach NVIDIA"):
        LLMEngine(transport=httpx.MockTransport(handler)).call_nvidia("p")


def test_a_reply_that_is_not_json_is_an_llm_error(nvidia):
    engine = engine_answering(httpx.Response(200, text="<html>gateway</html>"))
    with pytest.raises(LLMError, match="not JSON"):
        engine.call_nvidia("p")


def test_the_key_is_never_part_of_an_error(nvidia):
    engine = engine_answering(httpx.Response(500, text="boom"), httpx.Response(500, text="boom"),
                              httpx.Response(500, text="boom"))
    with pytest.raises(LLMError) as error:
        engine.call_nvidia("p")
    assert KEY not in str(error.value)


def test_analyze_uses_the_nvidia_provider(nvidia):
    reply = json.dumps({"suggestions": [{
        "filename": "app.py", "line": 2, "severity": "MEDIUM", "category": "style",
        "title": "Unused variable", "description": "y is never read.", "confidence": 0.9,
    }]})
    engine = LLMEngine()
    with patch.object(engine, "call_nvidia", return_value=reply) as call:
        found = engine.analyze(make_pr(), existing_findings=[])

    call.assert_called_once()
    assert [(s.title, s.source) for s in found] == [("Unused variable", "llm")]
    assert engine.warnings == []


def test_a_reply_that_cannot_be_read_is_a_warning(nvidia):
    # it reviewed nothing, so the report must not read as a clean AI review
    engine = LLMEngine()
    with patch.object(engine, "call_nvidia", return_value="Sorry, I can't help with that."):
        assert engine.analyze(make_pr(), existing_findings=[]) == []
    assert engine.warnings == [
        "1 of 1 LLM reply(ies) couldn't be read as JSON, so the files in them were not AI-reviewed"
    ]


def test_an_unknown_provider_lists_nvidia(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "gemini")
    with pytest.raises(ConfigError, match="anthropic or nvidia"):
        LLMEngine().analyze(make_pr(), existing_findings=[])


def test_cached_replies_depend_on_the_endpoint_and_thinking(nvidia, monkeypatch):
    engine = LLMEngine()
    hosted = engine.cache_key("nvidia", "p")
    monkeypatch.setattr(settings, "nvidia_thinking", True)
    thinking = engine.cache_key("nvidia", "p")
    monkeypatch.setattr(settings, "nvidia_base_url", "http://nim.local:8000/v1")
    self_hosted = engine.cache_key("nvidia", "p")

    assert len({hosted, thinking, self_hosted}) == 3
    assert all(KEY not in part for key in (hosted, thinking, self_hosted) for part in key)


def test_the_cache_never_stores_the_nvidia_key(nvidia):
    cache = open_cache()
    cache.put("contents", "demo", "file.py", value=f"token = '{KEY}'")
    cache.put("contents", "demo", "other.py", value="print('hello')")

    assert cache.get("contents", "demo", "file.py") is None
    assert cache.get("contents", "demo", "other.py") == "print('hello')"


def test_the_pipeline_can_run_with_another_github_token(monkeypatch):
    monkeypatch.setattr(settings, "github_token", "ghp_from_settings")
    default = AnalysisPipeline(use_cache=False)
    installation = AnalysisPipeline(use_cache=False, github_token="ghs_installation")
    try:
        assert default.github.token == "ghp_from_settings"
        assert installation.github.token == "ghs_installation"
    finally:
        default.close()
        installation.close()
