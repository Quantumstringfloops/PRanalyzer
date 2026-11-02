import json
from types import SimpleNamespace

import httpx
import pytest
from unittest.mock import patch
from pr_analyzer.exceptions import ConfigError, LLMError
from pr_analyzer.llm import engine as engine_module
from pr_analyzer.llm.engine import LLMEngine
from pr_analyzer.llm.prompts import build_prompt
from pr_analyzer.models import PRSummary, FileChange, Suggestion

# ── fake PR ───────────────────────────────────────────────────────────────────
def make_pr() -> PRSummary:
    return PRSummary(
        number=1,
        title="Add login feature",
        author="neil",
        repo="neil/test-repo",
        base_branch="main",
        head_branch="feature/login",
        total_additions=20,
        total_deletions=5,
        is_draft=False,
        files=[
            FileChange(
                filename="auth.py",
                status="modified",
                additions=20,
                deletions=5,
                patch="@@ -1,1 +1,1 @@\n+password = 'abc123'",
            )
        ]
    )

# ── fake LLM response ─────────────────────────────────────────────────────────
MOCK_RESPONSE = '''[
    {
        "filename": "auth.py",
        "line": 1,
        "severity": "HIGH",
        "category": "security",
        "title": "Hardcoded password",
        "description": "Password is hardcoded. Use environment variables.",
        "confidence": 0.9
    },
    {
        "filename": "auth.py",
        "line": 5,
        "severity": "LOW",
        "category": "style",
        "title": "Missing docstring",
        "description": "Function has no docstring.",
        "confidence": 0.3
    }
]'''

engine = LLMEngine()

@pytest.fixture
def llm_settings(monkeypatch):
    fake = SimpleNamespace(
        llm_provider="ollama",
        llm_model="",
        max_diff_lines=3000,
        llm_confidence_threshold=0.5,
        llm_timeout_seconds=5,
        llm_max_tokens=1024,
        ollama_url="http://ollama.test",
        openai_api_key="sk-test",
        anthropic_api_key="ant-test",
    )
    monkeypatch.setattr(engine_module, "settings", fake)
    return fake

def analyze_raw(raw: str, pr: PRSummary | None = None) -> list[Suggestion]:
    with patch.object(engine, "call_ollama", return_value=raw):
        return engine.analyze(pr or make_pr(), existing_findings=[])

# ── test: mock openai call and validate parsing ───────────────────────────────
def test_analyze_parses_mock_response():
    with patch.object(engine, "call_ollama", return_value=MOCK_RESPONSE):
        with patch("pr_analyzer.llm.engine.settings") as mock_settings:
            mock_settings.llm_provider = "ollama"
            mock_settings.llm_model = "llama3.2"
            mock_settings.max_diff_lines = 3000
            mock_settings.llm_confidence_threshold = 0.5

            results = engine.analyze(make_pr(), existing_findings=[])

    # only high confidence suggestion passes — low confidence (0.3) filtered out
    assert len(results) == 1
    assert results[0].title == "Hardcoded password"
    assert results[0].source == "llm"

# ── test: low confidence filtered ────────────────────────────────────────────
def test_low_confidence_filtered():
    with patch.object(engine, "call_ollama", return_value=MOCK_RESPONSE):
        with patch("pr_analyzer.llm.engine.settings") as mock_settings:
            mock_settings.llm_provider = "ollama"
            mock_settings.llm_model = "llama3.2"
            mock_settings.max_diff_lines = 3000
            mock_settings.llm_confidence_threshold = 0.5

            results = engine.analyze(make_pr(), existing_findings=[])

    titles = [r.title for r in results]
    assert "Missing docstring" not in titles

# ── test: duplicate filtered ──────────────────────────────────────────────────
def test_duplicate_filtered():
    existing = [Suggestion(
        filename="auth.py",
        line=1,
        severity="HIGH",
        category="security",
        title="Hardcoded password",
        description="already found",
        source="static"
    )]

    with patch.object(engine, "call_ollama", return_value=MOCK_RESPONSE):
        with patch("pr_analyzer.llm.engine.settings") as mock_settings:
            mock_settings.llm_provider = "ollama"
            mock_settings.llm_model = "llama3.2"
            mock_settings.max_diff_lines = 3000
            mock_settings.llm_confidence_threshold = 0.5

            results = engine.analyze(make_pr(), existing_findings=existing)

    assert results == []

# ── test: broken JSON returns empty list ──────────────────────────────────────
def test_broken_json_returns_empty():
    with patch.object(engine, "call_ollama", return_value="not valid json at all"):
        with patch("pr_analyzer.llm.engine.settings") as mock_settings:
            mock_settings.llm_provider = "ollama"
            mock_settings.llm_model = "llama3.2"
            mock_settings.max_diff_lines = 3000
            mock_settings.llm_confidence_threshold = 0.5

            results = engine.analyze(make_pr(), existing_findings=[])

    assert results == []

# ── test: clean diff returns empty list ──────────────────────────────────────
def test_clean_diff_no_suggestions():
    with patch.object(engine, "call_ollama", return_value="[]"):
        with patch("pr_analyzer.llm.engine.settings") as mock_settings:
            mock_settings.llm_provider = "ollama"
            mock_settings.llm_model = "llama3.2"
            mock_settings.max_diff_lines = 3000
            mock_settings.llm_confidence_threshold = 0.5

            results = engine.analyze(make_pr(), existing_findings=[])

    assert results == []

# ── response parsing and normalisation ───────────────────────────────────────
def test_prompt_schema_response_is_normalised(llm_settings):
    raw = json.dumps({"suggestions": [{
        "filename": "auth.py", "line": 1, "severity": "high", "category": "Security",
        "title": "Hardcoded password", "description": "Use an env var.", "confidence": 0.9,
    }]})

    [result] = analyze_raw(raw)

    assert (result.filename, result.line, result.severity, result.category) == ("auth.py", 1, "HIGH", "security")

def test_older_field_names_still_accepted(llm_settings):
    # the old prompt asked for "file" and "line_start"; the parser required "filename" and "line"
    raw = json.dumps([{"file": "auth.py", "line_start": 1, "severity": "medium", "category": "style",
                       "title": "Naming", "description": "d", "confidence": 0.8}])
    [result] = analyze_raw(raw)
    assert (result.filename, result.line, result.severity) == ("auth.py", 1, "MEDIUM")

def test_fenced_json_is_parsed(llm_settings):
    raw = '```json\n{"suggestions": [{"filename": "auth.py", "line": 1, "severity": "LOW", ' \
          '"category": "style", "title": "t", "description": "d", "confidence": 0.7}]}\n```'
    assert len(analyze_raw(raw)) == 1

@pytest.mark.parametrize("raw", ["{}", '{"summary": "looks fine"}', "", "Sure! Here you go."])
def test_responses_without_suggestions_return_nothing(llm_settings, raw):
    # '{}' used to raise IndexError
    assert analyze_raw(raw) == []

def test_suggestion_for_file_not_in_pr_dropped(llm_settings):
    raw = json.dumps([{"filename": "invented.py", "line": 1, "severity": "HIGH", "category": "security",
                       "title": "t", "description": "d", "confidence": 0.9}])
    assert analyze_raw(raw) == []

def test_unknown_category_and_severity_mapped(llm_settings):
    raw = json.dumps([{"filename": "auth.py", "line": 1, "severity": "warning", "category": "performance",
                       "title": "Slow loop", "description": "d", "confidence": 0.9}])
    [result] = analyze_raw(raw)
    assert (result.severity, result.category) == ("MEDIUM", "complexity")

def test_missing_confidence_dropped(llm_settings):
    raw = json.dumps([{"filename": "auth.py", "line": 1, "severity": "LOW", "category": "style",
                       "title": "t", "description": "d"}])
    assert analyze_raw(raw) == []

def test_threshold_is_inclusive(llm_settings):
    raw = json.dumps([{"filename": "auth.py", "line": 1, "severity": "LOW", "category": "style",
                       "title": "t", "description": "d", "confidence": 0.5}])
    assert len(analyze_raw(raw)) == 1

def test_unknown_provider_raises_config_error(llm_settings):
    llm_settings.llm_provider = "gemini"
    with pytest.raises(ConfigError):
        engine.analyze(make_pr(), existing_findings=[])

# ── prompt ────────────────────────────────────────────────────────────────────
def test_prompt_includes_every_file_with_line_numbers():
    # the diff used to be overwritten in a loop, so only the last file reached the model
    pr = make_pr()
    pr.files.append(FileChange(filename="views.py", status="added", additions=1, deletions=0,
                               patch="@@ -0,0 +1,1 @@\n+render()"))

    prompt = build_prompt(pr, [], max_diff_lines=3000)

    assert "--- auth.py ---" in prompt and "--- views.py ---" in prompt
    assert "    1 +render()" in prompt

def test_prompt_truncation_is_marked():
    pr = make_pr()
    pr.files[0].patch = "@@ -0,0 +1,50 @@\n" + "\n".join(f"+line {i}" for i in range(50))
    prompt = build_prompt(pr, [], max_diff_lines=10)
    assert "[diff truncated:" in prompt

# ── providers ─────────────────────────────────────────────────────────────────
def test_ollama_returns_message_content(llm_settings):
    # the reply variable used to be shadowed, so a valid reply always came back as "[]"
    requests_seen = []

    def handler(request):
        requests_seen.append(json.loads(request.content))
        return httpx.Response(200, json={"message": {"role": "assistant", "content": '{"suggestions": []}'}})

    reply = LLMEngine(transport=httpx.MockTransport(handler)).call_ollama("prompt")

    assert reply == '{"suggestions": []}'
    assert requests_seen[0]["stream"] is False
    assert requests_seen[0]["format"] == "json"
    assert requests_seen[0]["model"] == "llama3.2"

def test_ollama_unreachable_raises_llm_error(llm_settings):
    def handler(request):
        raise httpx.ConnectError("connection refused")

    with pytest.raises(LLMError):
        LLMEngine(transport=httpx.MockTransport(handler)).call_ollama("prompt")

def test_ollama_http_error_raises_llm_error(llm_settings):
    transport = httpx.MockTransport(lambda request: httpx.Response(404, json={"error": "model not found"}))
    with pytest.raises(LLMError, match="404"):
        LLMEngine(transport=transport).call_ollama("prompt")

def test_openai_error_raises_llm_error(llm_settings, monkeypatch):
    # any non-network error used to end in UnboundLocalError
    openai = pytest.importorskip("openai")

    class FailingClient:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        def create(self, **kwargs):
            raise openai.OpenAIError("invalid api key")

    monkeypatch.setattr(openai, "OpenAI", FailingClient)
    with pytest.raises(LLMError, match="invalid api key"):
        engine.call_openai("prompt")

def test_openai_missing_key_raises_llm_error(llm_settings):
    pytest.importorskip("openai")
    llm_settings.openai_api_key = ""
    with pytest.raises(LLMError, match="OPENAI_API_KEY"):
        engine.call_openai("prompt")

def test_anthropic_joins_text_blocks(llm_settings, monkeypatch):
    anthropic = pytest.importorskip("anthropic")
    seen = {}

    class FakeClient:
        def __init__(self, **kwargs):
            seen["client"] = kwargs
            self.messages = SimpleNamespace(create=self.create)

        def create(self, **kwargs):
            seen["request"] = kwargs
            return SimpleNamespace(stop_reason="end_turn", content=[
                SimpleNamespace(type="text", text='{"suggestions": '),
                SimpleNamespace(type="text", text="[]}"),
            ])

    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)

    assert engine.call_anthropic("prompt") == '{"suggestions": []}'
    # the key comes from settings (.env), not only from an exported environment variable
    assert seen["client"]["api_key"] == "ant-test"
    assert seen["request"]["model"] == "claude-haiku-5-5"

def test_anthropic_error_raises_llm_error(llm_settings, monkeypatch):
    anthropic = pytest.importorskip("anthropic")

    class FailingClient:
        def __init__(self, **kwargs):
            self.messages = SimpleNamespace(create=self.create)

        def create(self, **kwargs):
            raise anthropic.AnthropicError("overloaded")

    monkeypatch.setattr(anthropic, "Anthropic", FailingClient)
    with pytest.raises(LLMError, match="overloaded"):
        engine.call_anthropic("prompt")
