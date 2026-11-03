import json
import threading
from types import SimpleNamespace

import pytest

from pr_analyzer.cache import DiskCache
from pr_analyzer.config import settings
from pr_analyzer.diff import iter_new_lines
from pr_analyzer.exceptions import LLMError
from pr_analyzer.llm import engine as engine_module
from pr_analyzer.llm.chunking import plan_chunks, review_requests, split_patch
from pr_analyzer.llm.engine import LLMEngine
from pr_analyzer.llm.prompts import build_prompt
from pr_analyzer.models import FileChange, PRSummary, Suggestion


def added_file(name, lines=10):
    patch = f"@@ -0,0 +1,{lines} @@\n" + "\n".join(f"+line {i}" for i in range(1, lines + 1))
    return FileChange(filename=name, status="added", additions=lines, deletions=0, patch=patch)


def make_pr(*files):
    return PRSummary(number=1, title="Big change", author="neil", repo="neil/repo",
                     base_branch="main", head_branch="big", total_additions=100,
                     total_deletions=0, is_draft=False, files=list(files))


def finding(filename, title="Hardcoded password"):
    return Suggestion(filename=filename, severity="HIGH", category="security", title=title,
                      description="d", line=1)


def reply(*suggestions):
    return json.dumps({"suggestions": [
        {"filename": name, "line": line, "severity": "MEDIUM", "category": "style",
         "title": f"Remark on {name}", "description": "d", "confidence": 0.9}
        for name, line in suggestions
    ]})


# a patch with context, removed and added lines, and two hunks
MIXED = "\n".join([
    "@@ -10,6 +10,7 @@ def handler():",
    " keep = 1",
    "-old = 2",
    "+new = 2",
    "+extra = 3",
    " keep = 4",
    "\\ No newline at end of file",
    " keep = 5",
    "@@ -40,3 +41,4 @@ class Thing:",
    " a = 1",
    "+b = 2",
    " c = 3",
    "+d = 4",
])

# ── splitting a patch ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("patch", [MIXED, added_file("new.py", 50).patch])
@pytest.mark.parametrize("max_lines", [2, 3, 5, 8, 20])
def test_pieces_keep_every_line_and_its_number(patch, max_lines):
    pieces = split_patch(patch, max_lines)

    assert all(len(piece.splitlines()) <= max_lines for piece in pieces)
    numbered = [line for piece in pieces for line in iter_new_lines(piece)]
    assert numbered == list(iter_new_lines(patch))

def test_patch_is_cut_at_hunk_boundaries_when_hunks_fit():
    pieces = split_patch(MIXED, 9)
    assert [piece.splitlines()[0][:2] for piece in pieces] == ["@@", "@@"]
    assert len(pieces) == 2

def test_huge_new_file_continues_under_a_new_header():
    first, second, *_ = split_patch(added_file("new.py", 50).patch, 20)
    assert first.splitlines()[0] == "@@ -0,0 +1,50 @@"
    assert second.splitlines()[0] == "@@ -0 +20 @@"

# ── grouping files into requests ──────────────────────────────────────────────
def test_files_are_grouped_whole_up_to_the_line_budget():
    files = [added_file(f"f{i}.py", 10) for i in range(5)]   # 11 diff lines each

    chunks = plan_chunks(files, 25)

    assert [[f.filename for f in chunk] for chunk in chunks] == [
        ["f0.py", "f1.py"], ["f2.py", "f3.py"], ["f4.py"]]
    assert all(f is original for chunk in chunks for f in chunk for original in files
               if original.filename == f.filename)

def test_file_too_big_for_a_request_is_split_over_several():
    chunks = plan_chunks([added_file("big.py", 50), added_file("tiny.py", 1)], 20)

    # the last part of big.py has room left for the next file
    assert [[f.filename for f in chunk] for chunk in chunks] == [
        ["big.py"], ["big.py"], ["big.py", "tiny.py"]]
    assert all(sum(1 + len(f.patch.splitlines()) for f in chunk) <= 20 for chunk in chunks)

def test_files_without_a_patch_are_left_out():
    binary = FileChange(filename="logo.png", status="added", additions=0, deletions=0, patch=None)
    chunks = plan_chunks([binary, added_file("a.py", 30), added_file("b.py", 30)], 40)
    assert [[f.filename for f in chunk] for chunk in chunks] == [["a.py"], ["b.py"]]

# ── prompts ───────────────────────────────────────────────────────────────────
def test_small_pr_is_one_request_with_the_usual_prompt():
    pr = make_pr(added_file("a.py"), added_file("b.py"))
    findings = [finding("a.py")]

    [request] = review_requests(pr, findings, 3000)

    assert request.prompt == build_prompt(pr, findings, 3000)
    assert request.filenames == ["a.py", "b.py"]

def test_each_request_shows_only_its_files_and_their_findings():
    pr = make_pr(added_file("a.py", 30), added_file("b.py", 30))

    first, second = review_requests(pr, [finding("a.py", "A risk"), finding("b.py", "B risk")], 40)

    assert "--- a.py ---" in first.prompt and "--- b.py ---" not in first.prompt
    assert "A risk" in first.prompt and "B risk" not in first.prompt
    assert "--- b.py ---" in second.prompt and "A risk" not in second.prompt
    assert first.prompt.endswith("This is part 1 of 2 of the review of this pull request: the "
                                 "diff above covers 1 of its 2 changed files. The other parts "
                                 "are reviewed in separate requests.")

def test_part_of_a_split_file_says_so():
    requests = review_requests(make_pr(added_file("big.py", 50)), [], 20)
    assert len(requests) == 3
    assert all("(some of them only in part)" in r.prompt for r in requests)
    assert all(r.filenames == ["big.py"] for r in requests)

# ── reviewing in several requests ─────────────────────────────────────────────
@pytest.fixture
def llm(monkeypatch):
    """An engine whose provider calls are recorded and answered from `state.replies`."""
    fake_settings = SimpleNamespace(
        llm_provider="ollama", llm_model="", max_diff_lines=40, llm_confidence_threshold=0.5,
        llm_timeout_seconds=5, llm_max_tokens=1024, ollama_url="http://ollama.test",
        openai_api_key="", anthropic_api_key="",
    )
    monkeypatch.setattr(engine_module, "settings", fake_settings)
    monkeypatch.setattr(settings, "llm_token_budget", 0)
    monkeypatch.setattr(settings, "llm_max_concurrency", 1)
    state = SimpleNamespace(prompts=[], replies={}, settings=fake_settings)

    def call_ollama(self, prompt):
        state.prompts.append(prompt)
        for name, answer in state.replies.items():
            if f"--- {name} ---" in prompt:
                if isinstance(answer, Exception):
                    raise answer
                return answer
        return reply()

    monkeypatch.setattr(LLMEngine, "call_ollama", call_ollama)
    return state

def three_files():
    return make_pr(added_file("a.py", 30), added_file("b.py", 30), added_file("c.py", 30))

def test_big_pr_is_reviewed_in_several_requests_and_merged(llm):
    llm.replies = {name: reply((name, 2)) for name in ("a.py", "b.py", "c.py")}
    engine = LLMEngine()

    suggestions = engine.analyze(three_files(), [])

    assert len(llm.prompts) == 3
    assert [s.filename for s in suggestions] == ["a.py", "b.py", "c.py"]
    assert engine.warnings == []

def test_token_budget_skips_the_rest_and_says_how_many_files(llm):
    first_request = len(review_requests(three_files(), [], 40)[0].prompt)
    budget = (len(engine_module.SYSTEM_PROMPT) + first_request) // 4 + 10
    settings.llm_token_budget = budget
    engine = LLMEngine()

    engine.analyze(three_files(), [])

    assert len(llm.prompts) == 1
    assert engine.warnings == [
        f"2 of 3 file(s) were not AI-reviewed, or only in part: reviewing them would have gone "
        f"over LLM_TOKEN_BUDGET ({budget} tokens)"]

def test_later_failure_keeps_the_replies_already_in(llm):
    llm.replies = {"a.py": reply(("a.py", 2)), "b.py": LLMError("Ollama timed out")}
    engine = LLMEngine()

    suggestions = engine.analyze(three_files(), [])

    assert [s.filename for s in suggestions] == ["a.py"]
    assert len(llm.prompts) == 2   # nothing more is sent after a failure
    assert engine.warnings == ["2 of 3 file(s) were not AI-reviewed, or only in part: an LLM "
                               "request failed (Ollama timed out)"]

def test_failure_with_no_replies_raises_as_before(llm):
    llm.replies = {"a.py": LLMError("Could not reach Ollama")}
    with pytest.raises(LLMError, match="Could not reach Ollama"):
        LLMEngine().analyze(three_files(), [])

def test_same_remark_from_two_parts_of_a_file_is_kept_once(llm):
    llm.replies = {"big.py": reply(("big.py", None))}

    suggestions = LLMEngine().analyze(make_pr(added_file("big.py", 60)), [])

    assert len(llm.prompts) == 2
    assert len(suggestions) == 1

def test_requests_can_run_side_by_side_and_stay_in_order(llm, monkeypatch):
    settings.llm_max_concurrency = 3
    together = threading.Barrier(3, timeout=5)   # fails unless all three are in flight at once
    llm.replies = {name: reply((name, 2)) for name in ("a.py", "b.py", "c.py")}
    answer = LLMEngine.call_ollama

    def call_ollama(self, prompt):
        together.wait()
        return answer(self, prompt)

    monkeypatch.setattr(LLMEngine, "call_ollama", call_ollama)
    suggestions = LLMEngine().analyze(three_files(), [])

    assert [s.filename for s in suggestions] == ["a.py", "b.py", "c.py"]

# ── cached replies ────────────────────────────────────────────────────────────
def test_cached_reply_is_not_requested_again(llm, tmp_path):
    llm.replies = {name: reply((name, 2)) for name in ("a.py", "b.py", "c.py")}

    first = LLMEngine(cache=DiskCache(tmp_path)).analyze(three_files(), [])
    second = LLMEngine(cache=DiskCache(tmp_path)).analyze(three_files(), [])

    assert len(llm.prompts) == 3
    assert second == first

def test_cached_replies_cost_no_budget(llm, tmp_path):
    LLMEngine(cache=DiskCache(tmp_path)).analyze(three_files(), [])
    settings.llm_token_budget = 1
    engine = LLMEngine(cache=DiskCache(tmp_path))

    engine.analyze(three_files(), [])

    assert len(llm.prompts) == 3 and engine.warnings == []

def test_unparseable_reply_is_asked_again_next_time(llm, tmp_path):
    llm.replies = {"a.py": "Sorry, I can't help with that."}
    pr = make_pr(added_file("a.py"))

    LLMEngine(cache=DiskCache(tmp_path)).analyze(pr, [])
    LLMEngine(cache=DiskCache(tmp_path)).analyze(pr, [])

    assert len(llm.prompts) == 2

def test_failed_request_is_not_cached(llm, tmp_path):
    llm.replies = {"a.py": LLMError("Ollama returned HTTP 500")}
    pr = make_pr(added_file("a.py"))
    for _ in range(2):
        with pytest.raises(LLMError):
            LLMEngine(cache=DiskCache(tmp_path)).analyze(pr, [])
    assert len(llm.prompts) == 2

def test_another_model_does_not_reuse_a_reply(llm, tmp_path):
    pr = make_pr(added_file("a.py"))
    LLMEngine(cache=DiskCache(tmp_path)).analyze(pr, [])
    llm.settings.llm_model = "llama3.3"
    LLMEngine(cache=DiskCache(tmp_path)).analyze(pr, [])
    assert len(llm.prompts) == 2

def test_cache_key_leaves_the_api_keys_out(llm):
    llm.settings.openai_api_key = "sk-" + "k" * 40
    key = LLMEngine().cache_key("openai", "prompt")
    assert not any(llm.settings.openai_api_key in part for part in key)
