from pathlib import Path

import pytest
from conftest import FakeAccess, FakeAnalyzer, FakeGateway, make_pull, merged_pull
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from github import GithubException
from pr_analyzer.config import Settings as AnalyzerSettings
from typer.testing import CliRunner

from review_app import cli
from review_app.db import Store
from review_app.outcomes import CheckResult, CommitInfo
from review_app.settings import AppSettings, use_nvidia_by_default

runner = CliRunner()


@pytest.fixture
def served(monkeypatch):
    calls = []
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs)))
    return calls


@pytest.fixture
def app_credentials(monkeypatch, tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = tmp_path / "app.pem"
    pem.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                      serialization.PrivateFormat.TraditionalOpenSSL,
                                      serialization.NoEncryption()))
    monkeypatch.setenv("GITHUB_APP_ID", "1234")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_PATH", str(pem))
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "s3cret")


def test_the_dashboard_only_listens_on_loopback(served):
    result = runner.invoke(cli.app, ["dashboard", "--host", "0.0.0.0"])
    assert result.exit_code == 1
    assert "only listens on a loopback address" in result.output
    assert served == []


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_the_dashboard_starts_on_loopback(served, host):
    result = runner.invoke(cli.app, ["dashboard", "--host", host, "--port", "9001"])
    assert result.exit_code == 0, result.output
    assert served[0][1] == {"host": host, "port": 9001, "log_level": "warning"}


def test_the_dashboard_shows_its_settings_but_no_secret(served, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon-key-never-shown")
    monkeypatch.setenv("REVIEW_APP_ADMIN_EMAILS", "admin@example.com")
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "webhook-secret-never-shown")
    assert runner.invoke(cli.app, ["dashboard"]).exit_code == 0
    client = TestClient(served[0][0], base_url="http://localhost:8001", follow_redirects=False)
    # with login on, the page needs a session; read it the way the dashboard renders it instead
    settings = cli.dashboard_settings(AppSettings())
    assert ("Dashboard login", "Supabase Auth", "SUPABASE_URL and REVIEW_APP_ADMIN_EMAILS") in settings
    assert "never-shown" not in repr(settings)
    assert client.get("/config").status_code == 303


def test_a_half_set_up_login_stops_the_dashboard(served, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
    result = runner.invoke(cli.app, ["dashboard"])
    assert result.exit_code == 1
    assert "only partly set up" in result.output and "SUPABASE_ANON_KEY is not set" in result.output
    assert served == []


def test_with_login_the_dashboard_may_listen_beyond_loopback(served, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon")
    monkeypatch.setenv("REVIEW_APP_ADMIN_EMAILS", "admin@example.com")
    monkeypatch.setenv("REVIEW_APP_DASHBOARD_HOSTS", "review.example.com")
    result = runner.invoke(cli.app, ["dashboard", "--host", "0.0.0.0"])
    assert result.exit_code == 0, result.output
    assert "sign-in required" in result.output
    app, options = served[0]
    assert options["host"] == "0.0.0.0"
    assert {"/auth/login", "/auth/logout", "/auth/refresh"} <= {
        route.path for route in app.routes if hasattr(route, "path")}
    check = runner.invoke(cli.app, ["check-config"])
    assert ("ok  Dashboard login: Supabase Auth at proj.supabase.co; 1 admin address; "
            "served on review.example.com") in check.output


def test_the_webhook_server_needs_the_app_credentials(served):
    result = runner.invoke(cli.app, ["webhook"])
    assert result.exit_code == 1
    assert "GITHUB_APP_ID is not set" in result.output
    assert "GITHUB_WEBHOOK_SECRET is not set" in result.output
    assert served == []


def test_the_webhook_server_starts_with_them(served, app_credentials, monkeypatch):
    monkeypatch.setenv("REVIEW_APP_WORKERS", "2")
    result = runner.invoke(cli.app, ["webhook", "--port", "9000"])
    assert result.exit_code == 0, result.output
    app, options = served[0]
    assert options == {"host": "127.0.0.1", "port": 9000, "log_level": "info"}
    assert [route.path for route in app.routes if hasattr(route, "path")] >= ["/webhook"]


def test_an_invalid_policy_stops_everything(served, app_credentials):
    Path("review-app.toml").write_text('[defaults]\nmode = "auto"\n')
    for command in (["webhook"], ["dashboard"], ["check-config"]):
        result = runner.invoke(cli.app, command)
        assert result.exit_code == 1, command
        assert "turn auto-merge on per repository" in result.output
    assert served == []


def test_check_config_shows_each_repository(app_credentials):
    Path("review-app.toml").write_text('[repos."octo/app"]\nmode = "auto"\n')
    result = runner.invoke(cli.app, ["check-config"])
    assert result.exit_code == 0, result.output
    assert "ok  GitHub App: app 1234" in result.output
    assert "!!  semgrep: not installed" in result.output
    assert "auto-merge on for octo/app" in result.output
    assert "octo/app: {" in result.output


def test_analyze_needs_a_pr_url():
    result = runner.invoke(cli.app, ["analyze", "https://github.com/octo/app/issues/7"])
    assert result.exit_code == 1
    assert "Expected https://github.com/<owner>/<repo>/pull/<number>" in result.output


def test_analyze_decides_but_never_merges(monkeypatch):
    # even for a repository whose policy says auto
    Path("review-app.toml").write_text('[repos."octo/app"]\nmode = "auto"\n')
    gateway = FakeGateway(make_pull())
    analyzer = FakeAnalyzer()
    monkeypatch.setattr(cli, "PrAnalyzer", lambda: analyzer)
    monkeypatch.setattr(cli, "TokenAccess", lambda token: FakeAccess(gateway, checks=False))

    result = runner.invoke(cli.app, ["analyze", "https://github.com/octo/app/pull/7"])

    assert result.exit_code == 0, result.output
    assert "Decision: merge" in result.output
    assert "mode recommend, outcome merge_recommended" in result.output
    assert gateway.names() == ["pull"]          # no check run, no merge
    assert analyzer.calls[0]["post_review"] is False
    with Store("review-app.db").read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0   # never queued


def test_outcome_looks_once_and_prints_it(monkeypatch):
    # even where the policy has outcome tracking off: asking for it is enough
    Path("review-app.toml").write_text("[defaults]\ntrack_outcomes = false\n")
    gateway = FakeGateway(
        merged_pull(), checks=[CheckResult("test", "failed"), CheckResult("lint", "passed")],
        commits=[CommitInfo("f" * 40, "fix: the helper")], files={"f" * 40: ["src/app.py"]})
    monkeypatch.setattr(cli, "TokenAccess", lambda token: FakeAccess(gateway, checks=False))

    result = runner.invoke(cli.app, ["outcome", "https://github.com/octo/app/pull/7"])

    assert result.exit_code == 0, result.output
    assert "❌ CI is failing on the merge commit" in result.output
    assert "CI on ddddddd: failing (failed: test)" in result.output
    assert "fffffff fix: the helper (src/app.py)" in result.output
    assert "review-app's call: no decision: no decision recorded" in result.output
    assert "comment" not in gateway.names()          # not without --comment
    with Store("review-app.db").read() as conn:     # one look, nothing queued to come back
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_outcome_comments_when_asked(monkeypatch):
    gateway = FakeGateway(merged_pull(), checks=[CheckResult("test", "passed")])
    monkeypatch.setattr(cli, "TokenAccess", lambda token: FakeAccess(gateway, checks=False))
    result = runner.invoke(cli.app, ["outcome", "https://github.com/octo/app/pull/7",
                                     "--comment"])
    assert result.exit_code == 0, result.output
    # merged on 8 November, more than the 7-day watch ago: the verdict is final
    assert "✅ Held up: no CI failure, revert or follow-up fix  (final)" in result.output
    assert "### review-app: after the merge" in gateway.comments[900]
    assert "Comment: https://github.com/octo/app/pull/7#issuecomment-900" in result.output


def test_outcome_fails_when_the_comment_cant_be_posted(monkeypatch):
    # in GitHub Actions a red run is the only sign of it: the database is thrown away
    refused = GithubException(403, {"message": "Resource not accessible by integration"},
                              None)
    gateway = FakeGateway(merged_pull(), checks=[CheckResult("test", "passed")],
                          comment_error=refused)
    monkeypatch.setattr(cli, "TokenAccess", lambda token: FakeAccess(gateway, checks=False))
    result = runner.invoke(cli.app, ["outcome", "https://github.com/octo/app/pull/7",
                                     "--comment"])
    assert result.exit_code == 1
    assert "✅ Held up" in result.output                 # the outcome is still printed
    assert "Couldn't post the comment on octo/app#7" in result.output
    assert "Resource not accessible by integration" in result.output


def test_outcome_of_a_pr_that_was_not_merged(monkeypatch):
    gateway = FakeGateway(make_pull(state="closed"))
    monkeypatch.setattr(cli, "TokenAccess", lambda token: FakeAccess(gateway, checks=False))
    result = runner.invoke(cli.app, ["outcome", "https://github.com/octo/app/pull/7"])
    assert result.exit_code == 1
    assert "may not be merged" in result.output


def test_audit_verify():
    store = Store("review-app.db")
    store.init()
    store.audit("cli", "decision.made", verdict="merge")
    result = runner.invoke(cli.app, ["audit-verify"])
    assert result.exit_code == 0
    assert "intact: 1 entries" in result.output
    with store.read() as conn:
        conn.execute("DROP TRIGGER audit_log_no_update")
        conn.execute("UPDATE audit_log SET details = '{}'")
    result = runner.invoke(cli.app, ["audit-verify"])
    assert result.exit_code == 1
    assert "breaks at entry 1" in result.output


def test_nvidia_is_the_default_provider(monkeypatch):
    fresh = AnalyzerSettings(_env_file=None)
    use_nvidia_by_default(fresh)
    assert fresh.llm_provider == "nvidia"

    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    chosen = AnalyzerSettings(_env_file=None)
    use_nvidia_by_default(chosen)
    assert chosen.llm_provider == "ollama"


def test_app_problems(tmp_path):
    assert AppSettings().app_problems() == ["GITHUB_APP_ID is not set",
                                            "GITHUB_APP_PRIVATE_KEY_PATH is not set",
                                            "GITHUB_WEBHOOK_SECRET is not set"]
    missing = AppSettings(github_app_id=1, github_app_private_key_path=tmp_path / "nope.pem",
                          github_webhook_secret="x")
    expected = f"GITHUB_APP_PRIVATE_KEY_PATH: no file at {tmp_path / 'nope.pem'}"
    assert missing.app_problems() == [expected]


def test_secrets_stay_out_of_reprs():
    settings = AppSettings(github_webhook_secret="hunter2")
    assert "hunter2" not in repr(settings)
    assert settings.webhook_secret == b"hunter2"
