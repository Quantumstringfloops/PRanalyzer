import pytest

from pr_analyzer.analyzer.ast_analyzer import Thresholds
from pr_analyzer.exceptions import ConfigError
from pr_analyzer.project_config import (
    RULE_IDS,
    ProjectConfig,
    config_findings,
    glob_regex,
    load_project_config,
    parse_project_config,
    read_project_config,
    severity_rank,
)

FULL = """
disable = ["magic-number", "llm"]
ignore_paths = ["docs/**", "*.min.js"]
block_on = ["critical"]
inline_min_severity = "high"

[thresholds]
complexity_medium = 10
complexity_high = 20
max_file_lines = 800
max_line_length = 100
"""

def problems(text: str) -> str:
    with pytest.raises(ConfigError) as excinfo:
        parse_project_config(text)
    return str(excinfo.value)

# ── parsing ───────────────────────────────────────────────────────────────────
def test_empty_file_means_defaults():
    assert parse_project_config("") == ProjectConfig()

def test_defaults_keep_the_built_in_behaviour():
    config = ProjectConfig()
    assert config.block_on == {"CRITICAL", "HIGH"}
    assert config.inline_min_severity == "MEDIUM"
    assert config.thresholds == Thresholds()
    assert (config.disable, config.ignore_paths) == (frozenset(), ())

def test_every_setting_is_read():
    config = parse_project_config(FULL)

    assert config.disable == {"magic-number", "llm"}
    assert config.ignore_paths == ("docs/**", "*.min.js")
    assert config.block_on == {"CRITICAL"}          # severities may be written in any case
    assert config.inline_min_severity == "HIGH"
    assert config.thresholds == Thresholds(
        complexity_medium=10, complexity_high=20, max_file_lines=800, max_line_length=100
    )

def test_unset_thresholds_keep_their_defaults():
    config = parse_project_config("[thresholds]\nmax_line_length = 88")
    assert config.thresholds == Thresholds(max_line_length=88)

def test_empty_block_on_never_blocks():
    assert parse_project_config("block_on = []").block_on == frozenset()

def test_raw_bytes_are_accepted():
    # the GitHub API hands the base branch's file over as bytes
    assert parse_project_config(b'disable = ["todo-comment"]').disable == {"todo-comment"}

def test_every_check_has_an_id_to_disable():
    stages = {"complexity", "file-length", "semgrep", "dependency-audit", "llm", "config"}
    assert stages | {"long-line", "magic-number", "hardcoded-password", "sql-injection"} <= RULE_IDS
    assert parse_project_config(f"disable = {sorted(RULE_IDS)}").disable == RULE_IDS

# ── validation ────────────────────────────────────────────────────────────────
def test_invalid_toml_names_the_position():
    message = problems('disable = ["llm"]\nblock_on = = ["HIGH"]')
    assert "is not valid TOML" in message and "line 2" in message

def test_unknown_key_suggests_the_right_one():
    assert "unknown key 'disabel' (did you mean 'disable'?)" in problems('disabel = ["llm"]')

def test_unknown_rule_id_suggests_the_right_one():
    message = problems('disable = ["magic-numbers"]')
    assert "disable: unknown rule id 'magic-numbers' (did you mean 'magic-number'?)" in message

def test_rule_id_in_another_style_finds_its_match():
    assert "did you mean 'magic-number'" in problems('disable = ["MAGIC_NUMBER"]')

def test_unknown_rule_id_without_a_close_match_lists_them_all():
    message = problems('disable = ["zzz"]')
    assert "use one of:" in message and "todo-comment" in message and "semgrep" in message

def test_bad_severities_are_named():
    message = problems('block_on = ["HIGH", "SEVERE"]\ninline_min_severity = "INFO"')
    assert "block_on: 'SEVERE' is not a severity; use one of LOW, MEDIUM, HIGH, CRITICAL" in message
    assert "inline_min_severity: 'INFO' is not a severity" in message

@pytest.mark.parametrize("text, expected", [
    ('disable = "llm"', "disable must be a list of strings"),
    ("ignore_paths = [1]", "ignore_paths must be a list of strings"),
    ('ignore_paths = ["", "/"]', "ignore_paths: '' is not a path pattern"),
    ("block_on = 'HIGH'", "block_on must be a list of strings"),
    ("thresholds = 5", "thresholds must be a table"),
    ("[thresholds]\nmax_file_lines = 0", "thresholds.max_file_lines must be a whole number above 0"),
    ("[thresholds]\nmax_file_lines = true", "thresholds.max_file_lines must be a whole number above 0, not true"),
    ('[thresholds]\nmax_line_length = "120"', "thresholds.max_line_length must be a whole number"),
    ("[thresholds]\nmax_line_length = 99.5", "thresholds.max_line_length must be a whole number"),
    ("[thresholds]\nmax_lines = 10", "thresholds: unknown key 'max_lines'"),
    ("[thresholds]\ncomplexity_medium = 40", "complexity_medium (40) must not be above complexity_high (30)"),
])
def test_bad_values_are_explained(text, expected):
    assert expected in problems(text)

def test_every_problem_is_reported_at_once():
    message = problems('disabel = []\nblock_on = ["SEVERE"]\n[thresholds]\nmax_file_lines = -1')
    assert message.count("\n  - ") == 3

def test_message_names_the_file_it_came_from():
    with pytest.raises(ConfigError, match=r"\.pr-analyzer\.toml on main is invalid"):
        parse_project_config("x = 1", source=".pr-analyzer.toml on main")

# ── reading without stopping ──────────────────────────────────────────────────
def test_read_returns_the_settings_and_no_problems():
    assert read_project_config(b'disable = ["llm"]') == (ProjectConfig(disable=frozenset({"llm"})), [])

def test_read_falls_back_to_the_defaults_and_names_every_problem():
    # a broken file on the base branch used to stop every run; the caller now warns instead
    config, problems = read_project_config('disabel = []\nblock_on = ["SEVERE"]')
    assert config == ProjectConfig()
    assert problems == [
        "unknown key 'disabel' (did you mean 'disable'?)",
        "block_on: 'SEVERE' is not a severity; use one of LOW, MEDIUM, HIGH, CRITICAL",
    ]

def test_read_applies_no_part_of_an_invalid_file():
    # the valid half of a broken file isn't what the project chose either
    config, _ = read_project_config('block_on = ["CRITICAL"]\ndisable = ["magic-numbers"]')
    assert config == ProjectConfig()

@pytest.mark.parametrize("raw, problem", [
    (b"\xff\xfe", "the file is not UTF-8 text"),
    ('disable = ["llm"', "the file is not valid TOML: "),
])
def test_read_names_files_that_cannot_be_parsed(raw, problem):
    config, problems = read_project_config(raw)
    assert config == ProjectConfig()
    assert [p[:len(problem)] for p in problems] == [problem]

# ── a PR's own copy ───────────────────────────────────────────────────────────
def test_pr_copy_problems_are_blocking_findings():
    # a typo used to merge unnoticed, then stop every later run
    [finding] = config_findings('disable = ["magic-numbers"]\n')

    problem = "disable: unknown rule id 'magic-numbers' (did you mean 'magic-number'?)"
    assert finding.title == f"Invalid config: {problem}"
    assert (finding.filename, finding.severity, finding.rule_id) == (".pr-analyzer.toml", "HIGH", "config")
    assert (finding.line, finding.source, finding.category) == (None, "static", "style")
    assert problem in finding.description and "never applied" in finding.description

def test_pr_copy_gets_one_finding_per_problem():
    findings = config_findings('disabel = []\n[thresholds]\nmax_file_lines = 0')
    assert [f.title for f in findings] == [
        "Invalid config: unknown key 'disabel' (did you mean 'disable'?)",
        "Invalid config: thresholds.max_file_lines must be a whole number above 0, not 0",
    ]

def test_valid_pr_copy_has_no_findings():
    assert config_findings('disable = ["magic-number"]\nignore_paths = ["docs/**"]') == []

def test_pr_copy_that_is_not_utf8_is_reported():
    # GitHub hands a PR's files over as text, with U+FFFD for bytes that aren't UTF-8; once
    # merged, the same file read as bytes from the base branch is rejected
    [finding] = config_findings('# caf\ufffd\ndisable = ["llm"]')
    assert "probably not UTF-8 text" in finding.title

# ── ignore_paths ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("pattern, path, expected", [
    ("docs/**", "docs/guide/intro.md", True),
    ("docs/**", "src/docs/intro.md", False),         # an inner / anchors it to the repo root
    ("*.min.js", "static/js/app.min.js", True),      # no / means any directory
    ("*.min.js", "app.js", False),
    ("vendor/", "lib/vendor/a.js", True),
    ("vendor/", "vendor", False),                     # a trailing / matches directories only
    ("migrations", "app/migrations/0001_initial.py", True),   # a directory covers its files
    ("/setup.py", "setup.py", True),                  # a leading / anchors it too
    ("/setup.py", "pkg/setup.py", False),
    ("tests/data/*.json", "tests/data/a.json", True),
    ("tests/data/*.json", "tests/data/sub/a.json", False),   # * stays inside one directory
    ("**/fixtures/**", "tests/unit/fixtures/a.txt", True),
    ("**/fixtures/**", "fixtures/a.txt", True),
    ("src/?.py", "src/a.py", True),
    ("src/?.py", "src/ab.py", False),
    ("a+b(1).py", "lib/a+b(1).py", True),             # regex characters are taken literally
    ("a/**/b", "a/b", True),                           # /**/ is any number of directories...
    ("a/**/b", "a/x/y/b", True),
    ("docs/**/", "docs/sub/a.md", True),               # ...and with a trailing /, only them
    ("docs/**/", "docs/a.md", False),
    ("***/x", "a/b/x", True),                          # a longer run of asterisks is a ** too
])
def test_glob_matching(pattern, path, expected):
    assert bool(glob_regex(pattern).fullmatch(path)) is expected

@pytest.mark.parametrize("pattern, path, expected", [
    # as in git, ** crosses directories only as a whole path segment; within a name it is a *.
    # It used to cross them anywhere, ignoring files that a .gitignore line wouldn't.
    ("foo**bar", "foo_bar", True),
    ("foo**bar", "foo/x/bar", False),
    ("docs/**.md", "docs/a.md", True),
    ("docs/**.md", "docs/a/b.md", False),
    ("a/**b", "a/xb", True),
    ("a/**b", "a/x/b", False),
    ("**.min.js", "static/js/app.min.js", True),       # no / means any directory, as before
    ("build**/", "out/build-tmp/a.o", True),           # build* for directories
    ("build**/", "build-tmp", False),
])
def test_double_star_inside_a_name_is_a_single_star(pattern, path, expected):
    assert bool(glob_regex(pattern).fullmatch(path)) is expected

def test_ignores_checks_every_pattern():
    config = ProjectConfig(ignore_paths=("docs/**", "*.lock"))
    assert config.ignores("poetry.lock") and config.ignores("docs/a.md")
    assert not config.ignores("src/app.py")

@pytest.mark.parametrize("pattern, expected", [
    # each used to be read literally and match nothing (or the wrong files), without an error
    ("!docs/keep.md", "'!docs/keep.md' uses negation (a leading !), which isn't supported"),
    ("*.py[co]", "'*.py[co]' uses [ or ] (a character class in git), which isn't supported"),
    ("[Dd]ocs/", "'[Dd]ocs/' uses [ or ]"),
    ("docs]", "'docs]' uses [ or ]"),
    ("docs\\api", "'docs\\\\api' uses a backslash, which isn't supported"),
    ("\\#notes.md", "'\\\\#notes.md' uses a backslash"),
    ("foo**/bar", "'foo**/bar' has ** inside a name before a /, which isn't supported"),
])
def test_unsupported_gitignore_syntax_is_rejected(pattern, expected):
    toml = f"ignore_paths = ['docs/**', '{pattern}']"
    assert f"ignore_paths: {expected}" in problems(toml)

def test_unsupported_patterns_say_what_to_write_instead():
    message = problems("ignore_paths = ['!a', 'b[12]', 'c\\d', 'e**/f']")
    assert "ignore_paths can only leave files out" in message
    assert "list each name, or use ? for any one character" in message
    assert "/ separates directories" in message
    assert "write foo*/bar for one directory, or foo*/**/bar for any number" in message
    assert message.count("\n  - ") == 4

@pytest.mark.parametrize("pattern", [
    "docs/**", "**/fixtures/**", "/setup.py", "vendor/", "*.min.js", "**.min.js", "foo**bar",
    "src/?.py", "a/**/b", "**", "build**/", "docs/!draft.md", "#notes.md", "a b.txt",
])
def test_supported_patterns_are_accepted(pattern):
    # a ! after the start and a # are plain characters; only a leading ! means negation. And
    # build**/ ends in the trailing / of a directory pattern, not in a ** before a /
    assert parse_project_config(f"ignore_paths = ['{pattern}']").ignore_paths == (pattern,)

# ── severities ────────────────────────────────────────────────────────────────
def test_severity_rank_orders_severities_in_any_case():
    assert severity_rank("low") < severity_rank("MEDIUM") < severity_rank("High") < severity_rank("CRITICAL")
    assert severity_rank("MEDUIM") == -1

# ── --config files ────────────────────────────────────────────────────────────
def test_local_file_is_loaded(tmp_path):
    path = tmp_path / "settings.toml"
    path.write_text('disable = ["todo-comment"]\n', encoding="utf-8")
    assert load_project_config(path).disable == {"todo-comment"}

def test_missing_local_file_is_an_error(tmp_path):
    # named on the command line, so its absence is a mistake, unlike the base-branch lookup
    with pytest.raises(ConfigError, match="Can't read"):
        load_project_config(tmp_path / "missing.toml")

def test_local_file_that_is_not_utf8_is_an_error(tmp_path):
    path = tmp_path / "settings.toml"
    path.write_bytes(b"\xff\xfe")
    with pytest.raises(ConfigError, match="is not UTF-8 text"):
        load_project_config(path)
