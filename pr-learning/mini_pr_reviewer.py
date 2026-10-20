import json
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Any, Optional

# Configure basic logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("MiniPRReviewer")

# --- 1. Data Models ---

@dataclass
class Suggestion:
    file: str
    line: int
    category: str
    severity: str  # CRITICAL, HIGH, MEDIUM, LOW
    message: str
    confidence: float

@dataclass
class QualityReport:
    overall_score: float
    letter_grade: str
    passed: bool
    dimension_scores: Dict[str, float]
    summary: str


# --- 2. LLM Engine Simulation ---

class MockLLMEngine:
    """Simulates multi-provider LLM responses (OpenAI, Anthropic, Ollama)."""
    
    def __init__(self, provider: str = "openai", max_diff_lines: int = 100):
        self.provider = provider
        self.max_diff_lines = max_diff_lines

    def _truncate_diff(self, diff: str) -> str:
        lines = diff.splitlines()
        if len(lines) > self.max_diff_lines:
            logger.info(f"Truncating diff from {len(lines)} to {self.max_diff_lines} lines.")
            return "\n".join(lines[:self.max_diff_lines])
        return diff

    def analyze(self, pr_title: str, diff: str, static_findings: List[Dict]) -> List[Suggestion]:
        truncated_diff = self._truncate_diff(diff)
        
        # In a real app, you would branch here based on self.provider:
        # - OpenAI: client.chat.completions.create(..., response_format={"type": "json_object"})
        # - Anthropic: client.messages.create(system=..., messages=...)
        # - Ollama: httpx.post("http://localhost:11434/api/chat", json=...)
        
        logger.info(f"Calling provider [{self.provider}] with model. Context built successfully.")
        
        # Simulating a JSON response from the LLM
        mock_raw_json = json.dumps({
            "suggestions": [
                {
                    "file": "auth.py",
                    "line": 42,
                    "category": "security",
                    "severity": "CRITICAL",
                    "message": "Hardcoded API secret detected.",
                    "confidence": 0.95
                },
                {
                    "file": "utils.py",
                    "line": 15,
                    "category": "style",
                    "severity": "LOW",
                    "message": "Unused import statement.",
                    "confidence": 0.60  # Low confidence, should be filtered if threshold is 0.7
                },
                {
                    "file": "auth.py",
                    "line": 42,
                    "category": "security",
                    "severity": "CRITICAL",
                    "message": "Duplicate finding already caught by static analyzer.",
                    "confidence": 0.90
                }
            ]
        })

        try:
            parsed_data = json.loads(mock_raw_json)
            raw_suggestions = parsed_data.get("suggestions", [])
        except json.JSONDecodeError as e:
            logger.error(f"JSON parse error: {e}")
            return []
        except Exception as e:
            logger.error(f"API or network error handled gracefully: {e}")
            return []

        # Convert to objects
        suggestions = [Suggestion(**s) for s in raw_suggestions]
        logger.info(f"Received {len(suggestions)} raw suggestions from LLM.")
        return suggestions


# --- 3. Filtering & Quality Scorer ---

class QualityScorer:
    SEVERITY_PENALTIES = {
        "CRITICAL": 40,
        "HIGH": 20,
        "MEDIUM": 8,
        "LOW": 3
    }
    
    DIMENSION_WEIGHTS = {
        "security": 0.35,
        "complexity": 0.20,
        "style": 0.15,
        "documentation": 0.15,
        "testing": 0.15
    }

    def __init__(self, confidence_threshold: float = 0.7, block_threshold: int = 40):
        self.confidence_threshold = confidence_threshold
        self.block_threshold = block_threshold

    def filter_suggestions(self, suggestions: List[Suggestion], existing_findings: List[Dict]) -> List[Suggestion]:
        filtered = []
        for s in suggestions:
            # 1. Filter by confidence threshold
            if s.confidence < self.confidence_threshold:
                logger.debug(f"Filtered out low confidence ({s.confidence}) suggestion: {s.message}")
                continue
            
            # 2. Filter duplicates against static findings
            is_duplicate = any(
                f.get("file") == s.file and f.get("line") == s.line and f.get("category") == s.category
                for f in existing_findings
            )
            if is_duplicate:
                logger.debug(f"Filtered out duplicate suggestion: {s.message}")
                continue
                
            filtered.append(s)
        
        logger.info(f"Filtered down to {len(filtered)} valid suggestions.")
        return filtered

    def compute_score(self, suggestions: List[Suggestion]) -> QualityReport:
        # Start dimensions at 100 points each
        dim_scores = {dim: 100.0 for dim in self.DIMENSION_WEIGHTS.keys()}
        
        critical_count = 0
        high_count = 0
        max_penalty_applied = 0.0

        for s in suggestions:
            if s.severity == "CRITICAL":
                critical_count += 1
            elif s.severity == "HIGH":
                high_count += 1

            base_penalty = self.SEVERITY_PENALTIES.get(s.severity, 5)
            # Multiply penalty by confidence score
            effective_penalty = base_penalty * s.confidence
            max_penalty_applied = max(max_penalty_applied, effective_penalty)

            # Apply penalty to appropriate dimension (default to style/security mapping)
            dim = s.category if s.category in dim_scores else "style"
            dim_scores[dim] = max(0.0, dim_scores[dim] - effective_penalty)

        # Compute weighted overall score
        overall = sum(dim_scores[dim] * weight for dim, weight in self.DIMENSION_WEIGHTS.items())
        
        # Determine letter grade
        if overall >= 95: grade = "A+"
        elif overall >= 90: grade = "A"
        elif overall >= 80: grade = "B"
        elif overall >= 70: grade = "C"
        elif overall >= 60: grade = "D"
        else: grade = "F"

        # Check block condition
        passed = max_penalty_applied < self.block_threshold and critical_count == 0

        # One-line summary
        if critical_count > 0:
            summary = f"Failed: {critical_count} critical finding(s) detected."
        elif high_count > 0:
            summary = f"Warning: {high_count} high severity finding(s) detected."
        else:
            summary = "Clean pass! Code looks pristine."

        return QualityReport(
            overall_score=round(overall, 2),
            letter_grade=grade,
            passed=passed,
            dimension_scores=dim_scores,
            summary=summary
        )


# --- 4. Execution Example & Tests ---

if __name__ == "__main__":
    print("=== Running Mini PR Reviewer Pipeline ===")
    
    sample_diff = "+ def authenticate():\n+     api_key = 'sk_live_12345'\n+     return True"
    static_findings = [{"file": "auth.py", "line": 42, "category": "security"}]

    # 1. Initialize Engine & Scorer
    engine = MockLLMEngine(provider="openai")
    scorer = QualityScorer(confidence_threshold=0.7, block_threshold=30)

    # 2. Analyze PR via LLM
    raw_suggestions = engine.analyze(pr_title="Fix auth bug", diff=sample_diff, static_findings=static_findings)

    # 3. Filter Suggestions
    valid_suggestions = scorer.filter_suggestions(raw_suggestions, existing_findings=static_findings)

    # 4. Compute Score & Report
    report = scorer.compute_score(valid_suggestions)

    print("\n--- Quality Report Result ---")
    print(f"Overall Score : {report.overall_score}")
    print(f"Letter Grade  : {report.letter_grade}")
    print(f"Pipeline Pass : {report.passed}")
    print(f"Summary       : {report.summary}")
    print(f"Dimensions    : {report.dimension_scores}")