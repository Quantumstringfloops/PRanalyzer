import pytest
from unittest.mock import patch, MagicMock
from pr_analyzer.llm.engine import LLMEngine
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

# ── test: mock openai call and validate parsing ───────────────────────────────
def test_analyze_parses_mock_response():
    with patch.object(engine, "_call_openai", return_value=MOCK_RESPONSE):
        with patch("pr_analyzer.llm.engine.settings") as mock_settings:
            mock_settings.llm_provider = "openai"
            mock_settings.llm_model = "gpt-4o-mini"
            mock_settings.max_diff_lines = 3000
            mock_settings.llm_confidence_threshold = 0.5

            results = engine.analyze(make_pr(), existing_findings=[])

    # only high confidence suggestion passes — low confidence (0.3) filtered out
    assert len(results) == 1
    assert results[0].title == "Hardcoded password"
    assert results[0].source == "llm"

# ── test: low confidence filtered ────────────────────────────────────────────
def test_low_confidence_filtered():
    with patch.object(engine, "_call_openai", return_value=MOCK_RESPONSE):
        with patch("pr_analyzer.llm.engine.settings") as mock_settings:
            mock_settings.llm_provider = "openai"
            mock_settings.llm_model = "gpt-4o-mini"
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

    with patch.object(engine, "_call_openai", return_value=MOCK_RESPONSE):
        with patch("pr_analyzer.llm.engine.settings") as mock_settings:
            mock_settings.llm_provider = "openai"
            mock_settings.llm_model = "gpt-4o-mini"
            mock_settings.max_diff_lines = 3000
            mock_settings.llm_confidence_threshold = 0.5

            results = engine.analyze(make_pr(), existing_findings=existing)

    assert results == []

# ── test: broken JSON returns empty list ──────────────────────────────────────
def test_broken_json_returns_empty():
    with patch.object(engine, "_call_openai", return_value="not valid json at all"):
        with patch("pr_analyzer.llm.engine.settings") as mock_settings:
            mock_settings.llm_provider = "openai"
            mock_settings.llm_model = "gpt-4o-mini"
            mock_settings.max_diff_lines = 3000
            mock_settings.llm_confidence_threshold = 0.5

            results = engine.analyze(make_pr(), existing_findings=[])

    assert results == []

# ── test: clean diff returns empty list ──────────────────────────────────────
def test_clean_diff_no_suggestions():
    with patch.object(engine, "_call_openai", return_value="[]"):
        with patch("pr_analyzer.llm.engine.settings") as mock_settings:
            mock_settings.llm_provider = "openai"
            mock_settings.llm_model = "gpt-4o-mini"
            mock_settings.max_diff_lines = 3000
            mock_settings.llm_confidence_threshold = 0.5

            results = engine.analyze(make_pr(), existing_findings=[])

    assert results == []