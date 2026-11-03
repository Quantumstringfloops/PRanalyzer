import json
import re

import httpx
import structlog

from pr_analyzer.config import settings
from pr_analyzer.exceptions import ConfigError, LLMError
from pr_analyzer.llm.prompts import CATEGORIES, SEVERITIES, SYSTEM_PROMPT, build_prompt
from pr_analyzer.models import PRSummary, Suggestion

log = structlog.get_logger(__name__)

# used when LLM_MODEL is empty
DEFAULT_MODELS = {
    "ollama": "llama3.2",
    "openai": "gpt-4o-mini",
    "anthropic": "claude-haiku-5-5",
}

# models don't always stick to the requested vocabulary; map common variants onto it
SEVERITY_ALIASES = {
    "INFO": "LOW", "MINOR": "LOW", "WARNING": "MEDIUM", "MODERATE": "MEDIUM",
    "MAJOR": "HIGH", "ERROR": "HIGH", "BLOCKER": "CRITICAL",
}
CATEGORY_ALIASES = {
    "performance": "complexity", "maintainability": "complexity", "readability": "style",
    "docs": "documentation", "doc": "documentation", "test": "testing", "tests": "testing",
}
# keys a model might wrap its list of suggestions in
WRAPPER_KEYS = ("suggestions", "issues", "findings", "results", "comments")


class LLMEngine:

    def __init__(self, transport: httpx.BaseTransport | None = None):
        self.transport = transport   # lets tests swap in a fake HTTP layer for Ollama

    def analyze(self, pr: PRSummary, existing_findings: list[Suggestion]) -> list[Suggestion]:
        # can take any provider
        provider = settings.llm_provider.strip().lower()
        callers = {
            "openai": self.call_openai,
            "anthropic": self.call_anthropic,
            "ollama": self.call_ollama,
        }
        if provider not in callers:
            raise ConfigError(
                f"Unknown LLM_PROVIDER {settings.llm_provider!r}: use ollama, openai or anthropic"
            )

        prompt = build_prompt(pr, existing_findings, settings.max_diff_lines)
        raw = callers[provider](prompt)

        items = self.parse_response(raw)
        suggestions = [s for s in (self.to_suggestion(item, pr) for item in items) if s is not None]
        suggestions = self.filter_confidence(suggestions)
        suggestions = self.filter_duplicates(suggestions, existing_findings)

        log.info(
            "llm analysis complete",
            provider=provider,
            model=self.model_name(provider),
            returned=len(items),
            kept=len(suggestions),
        )
        return suggestions

    @staticmethod
    def model_name(provider: str) -> str:
        return settings.llm_model or DEFAULT_MODELS[provider]

    def call_openai(self, prompt: str) -> str:
        try:
            import openai
        except ImportError as e:
            raise LLMError("LLM_PROVIDER=openai needs the openai package: "
                           "pip install 'pranalyzer[openai]'") from e
        if not settings.openai_api_key:
            raise LLMError("OPENAI_API_KEY is not set")

        client = openai.OpenAI(
            api_key=settings.openai_api_key,
            timeout=settings.llm_timeout_seconds,
            max_retries=2,
        )
        try:
            response = client.chat.completions.create(
                model=self.model_name("openai"),
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
            )
        except openai.OpenAIError as e:
            raise LLMError(f"OpenAI request failed: {e}") from e

        if not response.choices:
            return ""
        return response.choices[0].message.content or ""

    def call_anthropic(self, prompt: str) -> str:
        try:
            import anthropic
        except ImportError as e:
            raise LLMError("LLM_PROVIDER=anthropic needs the anthropic package: "
                           "pip install 'pranalyzer[anthropic]'") from e
        if not settings.anthropic_api_key:
            raise LLMError("ANTHROPIC_API_KEY is not set")

        client = anthropic.Anthropic(
            api_key=settings.anthropic_api_key,
            timeout=settings.llm_timeout_seconds,
            max_retries=2,
        )
        try:
            response = client.messages.create(
                model=self.model_name("anthropic"),
                max_tokens=settings.llm_max_tokens,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.AnthropicError as e:
            raise LLMError(f"Anthropic request failed: {e}") from e

        if response.stop_reason == "max_tokens":
            log.warning("llm_output_truncated", hint="raise LLM_MAX_TOKENS")
        return "".join(
            getattr(block, "text", "") for block in response.content
            if getattr(block, "type", None) == "text"
        )

    def call_ollama(self, prompt: str) -> str:
        url = settings.ollama_url.rstrip("/") + "/api/chat"
        payload = {
            "model": self.model_name("ollama"),
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "format": "json",   # Ollama constrains the reply to valid JSON
        }
        try:
            with httpx.Client(timeout=settings.llm_timeout_seconds, transport=self.transport) as client:
                response = client.post(url, json=payload)
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as e:
            raise LLMError(f"Ollama returned HTTP {e.response.status_code}: "
                           f"{e.response.text[:200]}") from e
        except httpx.HTTPError as e:
            raise LLMError(f"Could not reach Ollama at {url}: {e}") from e
        except ValueError as e:
            raise LLMError("Ollama returned a response that is not JSON") from e

        if not isinstance(data, dict):
            return ""
        return (data.get("message") or {}).get("content", "")

    def parse_response(self, raw: str) -> list[dict]:
        text = (raw or "").strip()
        # tolerate ```json fences even though the prompt asks for bare JSON
        fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
        if fenced:
            text = fenced.group(1)

        data = self._load_json(text)
        if isinstance(data, dict):
            for key in WRAPPER_KEYS:
                if isinstance(data.get(key), list):
                    data = data[key]
                    break
            else:
                data = [data] if "title" in data else []   # a single bare suggestion

        if not isinstance(data, list):
            if text:
                log.warning("failed to parse LLM response", preview=text[:200])
            return []
        return [item for item in data if isinstance(item, dict)]

    @staticmethod
    def _load_json(text: str):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # fall back to the first JSON object or array embedded in surrounding prose
        candidates = sorted(
            (text.find(opening), closing)
            for opening, closing in (("{", "}"), ("[", "]"))
            if text.find(opening) != -1
        )
        for start, closing in candidates:
            end = text.rfind(closing)
            if end > start:
                try:
                    return json.loads(text[start:end + 1])
                except json.JSONDecodeError:
                    continue
        return None

    def to_suggestion(self, item: dict, pr: PRSummary) -> Suggestion | None:
        filename = self._match_filename(item.get("filename") or item.get("file") or item.get("path"), pr)
        title = str(item.get("title") or "").strip()
        if filename is None or not title:
            return None   # a file that isn't in the PR, or an empty suggestion

        try:
            confidence = min(max(float(item["confidence"]), 0.0), 1.0)
        except (KeyError, TypeError, ValueError):
            return None

        severity = str(item.get("severity") or "").strip().upper()
        severity = SEVERITY_ALIASES.get(severity, severity)
        if severity not in SEVERITIES:
            severity = "LOW"

        category = str(item.get("category") or "").strip().lower()
        category = CATEGORY_ALIASES.get(category, category)
        if category not in CATEGORIES:
            category = "style"

        line = item.get("line", item.get("line_start"))
        try:
            line = int(line) if line is not None else None
        except (TypeError, ValueError):
            line = None
        if line is not None and line < 1:
            line = None

        return Suggestion(
            filename=filename,
            severity=severity,
            category=category,
            title=title[:200],
            description=str(item.get("description") or "").strip()[:2000],
            line=line,
            source="llm",
            confidence=confidence,
        )

    @staticmethod
    def _match_filename(name, pr: PRSummary) -> str | None:
        if not name:
            return None
        name = str(name).strip().removeprefix("./").lstrip("/")
        paths = {f.filename for f in pr.files}
        if name in paths:
            return name
        # models sometimes shorten the path; accept it only when it's unambiguous
        matches = [path for path in paths if path.endswith("/" + name)]
        return matches[0] if len(matches) == 1 else None

    def filter_confidence(self, suggestions: list[Suggestion]) -> list[Suggestion]:
        threshold = settings.llm_confidence_threshold
        kept = [s for s in suggestions if s.confidence >= threshold]
        if len(kept) < len(suggestions):
            log.info("low_confidence_dropped", dropped=len(suggestions) - len(kept), threshold=threshold)
        return kept

    def filter_duplicates(self, suggestions: list[Suggestion], existing: list[Suggestion]) -> list[Suggestion]:
        existing_keys = {(e.filename, e.line, e.category) for e in existing}
        return [s for s in suggestions if (s.filename, s.line, s.category) not in existing_keys]
