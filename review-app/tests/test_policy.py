import pytest

from review_app.policy import GRADES, Policy, PolicyError, grade_rank, load_policy, parse_policy


def test_no_file_means_recommend_only_everywhere(tmp_path):
    book = load_policy(tmp_path / "missing.toml")
    assert book.for_repo("octo/app") == Policy()
    assert book.defaults.mode == "recommend"
    assert book.auto_repos() == []


def test_repository_tables_build_on_the_defaults():
    book = parse_policy("""
        [defaults]
        merge_min_score = 92
        reviewers = ["alice"]

        [repos."Octo/App"]
        mode = "auto"
        merge_method = "rebase"
    """)
    app = book.for_repo("octo/app")   # names are matched ignoring case
    assert (app.mode, app.merge_method, app.merge_min_score, app.reviewers) == (
        "auto", "rebase", 92, ("alice",))
    assert book.for_repo("octo/other").mode == "recommend"
    assert book.auto_repos() == ["octo/app"]


def test_the_example_file_is_valid():
    from pathlib import Path
    example = Path(__file__).parents[1] / "review-app.example.toml"
    book = load_policy(example)
    assert book.defaults == Policy()


@pytest.mark.parametrize(("text", "problem"), [
    ('[defaults]\nmode = "auto"', "turn auto-merge on per repository"),
    ("[defaults]\nmerge_min_scor = 90", "merge_min_scor"),
    ('[repo."octo/app"]\nmode = "auto"', "unknown table or key 'repo'"),
    ('[repos."not a repo"]\nmode = "auto"', "not an owner/name repository name"),
    ('[repos."octo/app"]\nmode = "yolo"', "mode"),
    ("[defaults]\nmerge_min_confidence = 2", "merge_min_confidence"),
    ("[defaults]\nblock_below_score = 95", "block_below_score can't be above merge_min_score"),
    ('[defaults]\nblock_below_grade = "A+"', "block_below_grade can't be better"),
    ('[defaults]\nmerge_method = "fast-forward"', "merge_method"),
    ('[defaults]\nreviewers = ["not a login!"]', "reviewers"),
    ("[defaults\n", "not valid TOML"),
    ('[repos."octo/app"]\nmode = "auto"\n[repos."OCTO/APP"]\nmode = "auto"', "listed twice"),
])
def test_problems_are_errors(text, problem):
    with pytest.raises(PolicyError) as error:
        parse_policy(text, source="review-app.toml")
    assert problem in str(error.value)
    assert str(error.value).startswith("review-app.toml is invalid")


def test_every_problem_is_reported_at_once():
    with pytest.raises(PolicyError) as error:
        parse_policy('[defaults]\nmode = "auto"\nfoo = 1\n[repos."x y"]\n')
    assert len(error.value.problems) >= 3


def test_grades_rank_best_first():
    assert GRADES[0] == "A+" and GRADES[-1] == "F"
    assert grade_rank("A+") > grade_rank("A-") > grade_rank("B+") > grade_rank("F") > grade_rank("?")


def test_base_branches_default_to_the_default_branch():
    assert Policy().allows_base("main", "main")
    assert not Policy().allows_base("dev", "main")
    assert Policy(base_branches=("dev",)).allows_base("dev", "main")
    assert not Policy(base_branches=("dev",)).allows_base("main", "main")


def test_review_paths_match_like_gitignore():
    policy = Policy(review_paths=(".github/**", "CODEOWNERS", "requirements*.txt", "infra/"))
    hits = policy.review_path_hits([
        ".github/workflows/ci.yml", "docs/CODEOWNERS", "requirements-dev.txt",
        "infra/main.tf", "src/github/client.py", "src/app.py",
    ])
    assert hits == [".github/workflows/ci.yml", "docs/CODEOWNERS", "requirements-dev.txt",
                    "infra/main.tf"]
