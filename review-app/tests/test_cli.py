from pathlib import Path

import pytest
from conftest import FakeAccess, FakeAnalyzer, FakeGateway, make_pull
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pr_analyzer.config import Settings as AnalyzerSettings
from typer.testing import CliRunner

from review_app import cli
from review_app.db import Store
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
