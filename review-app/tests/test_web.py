"""The two web apps: the webhook receiver over HTTP, and the read-only dashboard."""
import json
import re
import threading
from pathlib import Path

import pytest
from conftest import (
    NEW_SHA,
    SHA,
    FakeAccess,
    FakeAnalyzer,
    FakeGateway,
    make_finding,
    make_outcome,
    make_pull,
    make_result,
)
from fastapi.testclient import TestClient
from pr_analyzer.models import FileChange
from pr_analyzer.security.scanner import DANGEROUS_PATTERNS, SecurityScanner
from test_webhooks import pr_payload

import review_app
from review_app import web
from review_app.db import Store
from review_app.policy import Policy, PolicyBook
from review_app.settings import AppSettings
from review_app.web import SECURITY_HEADERS, create_dashboard_app, create_webhook_app, host_name
from review_app.webhooks import WebhookHandler, sign
from review_app.worker import Worker

SECRET = "s3cret"


def webhook_client(store, queue, secret=SECRET, limit=1_000_000, workers=()):
    settings = AppSettings(github_webhook_secret=secret, review_app_max_webhook_bytes=limit)
    return TestClient(create_webhook_app(settings, WebhookHandler(store, queue, 42), workers))


def deliver(client, payload=None, *, event="pull_request", delivery="d-1", body=None,
            secret=SECRET, **headers):
    body = json.dumps(payload or pr_payload()).encode() if body is None else body
    sent = {"content-type": "application/json", "x-github-event": event,
            "x-github-delivery": delivery, "x-hub-signature-256": sign(secret.encode(), body)}
    sent.update({k.replace("_", "-"): v for k, v in headers.items()})
    return client.post("/webhook", content=body, headers=sent)


def queued(store) -> int:
    with store.read() as conn:
        return conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]


# ── webhook receiver ─────────────────────────────────────────────────────────────────────
def test_a_signed_delivery_queues_an_analysis(store, queue):
    response = deliver(webhook_client(store, queue))
    assert response.status_code == 202
    assert response.json()["message"] == "analysis queued"
    assert queued(store) == 1


@pytest.mark.parametrize("signature", [None, "sha256=" + "0" * 64, "garbage"])
def test_an_unsigned_or_forged_delivery_is_refused(store, queue, signature):
    body = json.dumps(pr_payload()).encode()
    headers = {"content-type": "application/json", "x-github-event": "pull_request",
               "x-github-delivery": "d-1"}
    if signature:
        headers["x-hub-signature-256"] = signature
    response = webhook_client(store, queue).post("/webhook", content=body, headers=headers)
    assert response.status_code == 401
    assert queued(store) == 0


def test_a_delivery_signed_with_another_secret_is_refused(store, queue):
    assert deliver(webhook_client(store, queue), secret="guess").status_code == 401


def test_without_a_secret_nothing_is_trusted(store, queue):
    response = deliver(webhook_client(store, queue, secret=""), secret="")
    assert response.status_code == 503
    assert queued(store) == 0


def test_only_json_deliveries(store, queue):
    response = deliver(webhook_client(store, queue),
                       content_type="application/x-www-form-urlencoded")
    assert response.status_code == 415


def test_an_oversized_delivery_is_refused_unread(store, queue):
    client = webhook_client(store, queue, limit=100)
    assert deliver(client).status_code == 413
    assert queued(store) == 0


@pytest.mark.parametrize(("body", "headers"), [
    (b"not json", {}),
    (b"[1, 2]", {}),
    (None, {"x_github_event": ""}),
    (None, {"x_github_delivery": "x" * 101}),
    (json.dumps({"action": "opened"}).encode(), {}),   # signed, but no pull_request
])
def test_bad_deliveries_are_rejected(store, queue, body, headers):
    response = deliver(webhook_client(store, queue), body=body, **headers)
    assert response.status_code == 400
    assert queued(store) == 0


def test_the_worker_runs_with_the_server(store, queue):
    started, stopped = threading.Event(), threading.Event()

    def worker(stop):
        started.set()
        stop.wait(5)
        stopped.set()

    with webhook_client(store, queue, workers=[worker]) as client:
        assert started.wait(5)
        assert client.get("/healthz").json() == {"ok": True}
    assert stopped.wait(5)


# ── dashboard ────────────────────────────────────────────────────────────────────────────
def dashboard(store, policies=None, health=None, host="localhost:8001") -> TestClient:
    app = create_dashboard_app(Store(store.path, readonly=True), policies or PolicyBook(), health)
    return TestClient(app, base_url=f"http://{host}")


def run_one_pr(store, queue, title="Add a helper", outcome=None, sha=SHA):
    queue.enqueue("analyze", "octo/app", 7, sha, 11)
    gateway = FakeGateway(make_pull(title=title, head_sha=sha))
    Worker(store, queue, PolicyBook(), FakeAnalyzer(outcome), FakeAccess(gateway)).run_once()


def stat_counts(page) -> dict[str, int]:
    """The overview's count per verdict, by the tone each one is drawn in (the PRs-graded tile,
    toneless, left out)."""
    found = re.findall(r'<div class="stat tilt tone-(\w+)">.*?<span class="stat-value">(\d+)</span>',
                       page.text, flags=re.DOTALL)
    return {tone: int(n) for tone, n in found if tone != "none"}


def test_an_empty_dashboard(store):
    page = dashboard(store, health=[("semgrep", False, "not installed")]).get("/")
    assert page.status_code == 200
    assert "No repositories yet" in page.text
    assert "not installed" in page.text


def test_the_overview_lists_repositories_and_decisions(store, queue):
    run_one_pr(store, queue)
    page = dashboard(store, PolicyBook(repos={"octo/app": Policy(mode="auto")})).get("/")
    assert 'href="/repos/octo/app"' in page.text
    assert "octo/app#7" in page.text
    assert "Merge" in page.text and "Merge recommended" in page.text
    assert "Auto-merge on for octo/app" in page.text
    assert stat_counts(page) == {"good": 1, "warning": 0, "critical": 0}


def test_the_overview_counts_each_prs_latest_decision(store, queue):
    blocked = make_outcome(make_result(suggestions=[make_finding("CRITICAL")]))
    run_one_pr(store, queue, outcome=blocked)
    run_one_pr(store, queue, outcome=make_outcome(make_result(head_sha=NEW_SHA)), sha=NEW_SHA)
    page = dashboard(store).get("/")
    # both decisions are listed, but only the newer one counts
    assert page.text.count('<li class="row">') == 2
    assert stat_counts(page) == {"good": 1, "warning": 0, "critical": 0}
    assert "1 graded" in page.text
    assert "Recommend-only" in page.text


def test_pr_text_is_escaped(store, queue):
    evil = "<script>alert(1)</script>"
    outcome = make_outcome(make_result(warnings=[f"Skipped 1 file(s): {evil}.js"]))
    run_one_pr(store, queue, title=evil, outcome=outcome)
    client = dashboard(store)
    for path in ("/", "/repos/octo/app", "/prs/1", "/audit"):
        page = client.get(path)
        assert evil not in page.text, path
    assert "&lt;script&gt;" in client.get("/prs/1").text


@pytest.mark.parametrize("host", ["localhost:8001", "127.0.0.1:8001", "[::1]:8001", "LOCALHOST"])
def test_localhost_names_are_served(store, host):
    assert dashboard(store, host=host).get("/").status_code == 200


@pytest.mark.parametrize("host", ["evil.example", "evil.example:8001", "192.168.1.5:8001",
                                  "localhost.evil.example"])
def test_other_host_names_are_refused(store, host):
    # a DNS-rebinding page would arrive with its own host name
    assert dashboard(store, host=host).get("/").status_code == 403


def test_the_dashboard_is_read_only(store):
    client = dashboard(store)
    for method in ("post", "put", "delete", "patch"):
        assert getattr(client, method)("/").status_code == 405


def test_security_headers(store):
    response = dashboard(store).get("/")
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value
    assert "script-src" not in response.headers["content-security-policy"]


def test_the_only_outside_source_is_the_font(store):
    # the preset's Oxanium comes from Google Fonts; nothing else leaves the dashboard
    policy = dict(directive.strip().split(" ", 1)
                  for directive in SECURITY_HEADERS["Content-Security-Policy"].split(";"))
    assert policy["default-src"] == "'none'"
    assert policy["style-src"] == "'self' https://fonts.googleapis.com"
    assert policy["font-src"] == "https://fonts.gstatic.com"
    assert "unsafe" not in SECURITY_HEADERS["Content-Security-Policy"]
    page = dashboard(store).get("/").text
    sheets = re.findall(r'<link rel="stylesheet" href="([^"]+)">', page)
    assert sheets == ["https://fonts.googleapis.com/css2?family=Oxanium:wght@300..800&amp;display=swap",
                      "/static/style.css"]


def current_theme(page: str) -> list[str]:
    return re.findall(r'<a href="/theme/(\w+)\?next=[^"]*" title="[^"]+" class="on" '
                      r'aria-current="true">', page)


def test_the_theme_switch(store):
    client = dashboard(store)
    page = client.get("/").text
    assert '<html lang="en">' in page and current_theme(page) == ["system"]
    assert '<meta name="color-scheme" content="light dark">' in page

    response = client.get("/theme/dark", params={"next": "/audit?repo=octo/app"},
                          follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (303, "/audit?repo=octo/app")
    cookie = response.headers["set-cookie"].lower()
    assert "review_app_theme=dark" in cookie and "httponly" in cookie and "samesite=lax" in cookie
    page = client.get("/audit").text
    assert '<html lang="en" data-theme="dark">' in page and current_theme(page) == ["dark"]
    assert '<meta name="color-scheme" content="dark">' in page
    assert 'href="/theme/light?next=/audit"' in page          # each link comes back here

    client.get("/theme/system", follow_redirects=False)       # forget the choice
    assert '<html lang="en">' in client.get("/").text


@pytest.mark.parametrize(("target", "expected"), [
    ("/prs/1", "/prs/1"), ("//evil.example", "/"), ("https://evil.example", "/"),
    ("/login?next=%2Fprs%2F1", "/login?next=%2Fprs%2F1"), ("/login\\evil", "/"),
])
def test_the_theme_switch_only_returns_to_pages_here(store, target, expected):
    response = dashboard(store).get("/theme/light", params={"next": target},
                                    follow_redirects=False)
    assert response.headers["location"] == expected


def test_unknown_themes(store):
    client = dashboard(store)
    assert client.get("/theme/neon").status_code == 404
    client.cookies.set("review_app_theme", "neon")             # a cookie set by hand
    assert current_theme(client.get("/").text) == ["system"]


def test_a_chosen_dark_theme_gets_every_dark_token():
    # the dark tokens are written twice: for a dark system, and for dark chosen with the switch
    css = (Path(review_app.__file__).parent / "static" / "style.css").read_text(encoding="utf-8")
    system = re.search(r':root:not\(\[data-theme="light"\]\) \{(.*?)\n  \}', css, re.DOTALL)
    chosen = re.search(r':root\[data-theme="dark"\] \{(.*?)\n\}', css, re.DOTALL)
    assert system and chosen

    def declarations(block: str) -> dict[str, str]:
        return dict(re.findall(r"^\s*([\w-]+): (.+);$", block, re.MULTILINE))

    assert declarations(system.group(1)) == declarations(chosen.group(1))
    assert len(declarations(chosen.group(1))) > 30


# ── findings across pull requests, and the pages around them ─────────────────────────────
def analyse_twice(store, queue):
    """PR 7 analysed at two commits: the first finds a hardcoded password, the second doesn't."""
    first = [make_finding("CRITICAL", source="security", title="Hardcoded password", line=4),
             make_finding("LOW", title="Magic number", line=9)]
    run_one_pr(store, queue, outcome=make_outcome(make_result(suggestions=first)))
    second = [make_finding("LOW", title="Magic number", line=11),
              make_finding("HIGH", source="llm", title="Token logged", line=20)]
    run_one_pr(store, queue, sha=NEW_SHA,
               outcome=make_outcome(make_result(suggestions=second, head_sha=NEW_SHA)))


def finding_rows(page: str) -> list[tuple[str, str]]:
    """(title, state) for each row of the findings table."""
    rows = re.findall(r'<a class="cell-link" href="/prs/\d+">([^<]+)</a>.*?'
                      r'<span class="state state-(\w+)">', page, flags=re.DOTALL)
    return [(title, state) for title, state in rows]


def test_the_findings_page(store, queue):
    analyse_twice(store, queue)
    page = dashboard(store).get("/findings").text
    # the open PR's current findings are pending; the password a later commit removed, resolved
    assert sorted(finding_rows(page)) == [("Hardcoded password", "resolved"),
                                          ("Magic number", "pending"), ("Token logged", "pending")]
    assert "Findings (3)" in page
    kpis = re.findall(r'<strong class="kpi-value">([^<]+)</strong>', page)
    assert kpis == ["2", "50%", "2"]      # 2 high-severity, 1 of them resolved, in 1 PR
    assert 'href="?days=7"' in page and 'class="on" aria-current="true">30 days' in page


def test_the_findings_page_filters_and_searches(store, queue):
    analyse_twice(store, queue)
    client = dashboard(store)
    assert finding_rows(client.get("/findings", params={"state": "resolved"}).text) == [
        ("Hardcoded password", "resolved")]
    assert finding_rows(client.get("/findings", params={"type": "llm"}).text) == [
        ("Token logged", "pending")]
    page = client.get("/findings", params={"severity": "low", "q": "magic"}).text
    assert finding_rows(page) == [("Magic number", "pending")]
    # each filter in use is a chip whose link drops just that filter
    assert 'href="/findings?days=30&amp;q=magic"' in page and "Severity: low" in page
    assert "No findings match" in client.get("/findings", params={"q": "nothing like it"}).text


def test_the_triage_board(store, queue):
    run_one_pr(store, queue)
    blocked = make_outcome(make_result(passed=False, suggestions=[make_finding("CRITICAL")]))
    queue.enqueue("analyze", "octo/app", 8, SHA, 11)
    gateway = FakeGateway(make_pull(number=8, title="Rewrite the session handling"))
    Worker(store, queue, PolicyBook(), FakeAnalyzer(blocked), FakeAccess(gateway)).run_once()
    page = dashboard(store).get("/triage").text
    lanes = dict(re.findall(r'<h2 id="lane-(\w+)">.*?</h2>\s*<span class="count">(\d+)</span>',
                            page, flags=re.DOTALL))
    assert lanes == {"needs_review": "0", "block": "1", "merge": "1", "done": "0"}
    assert "Rewrite the session handling" in page and 'href="/prs/2"' in page


def test_repositories_standards_and_map(store, queue):
    analyse_twice(store, queue)
    policies = PolicyBook(repos={"octo/app": Policy(mode="auto", merge_min_score=93)})
    client = dashboard(store, policies)
    repos = client.get("/repos").text
    assert 'href="/repos/octo/app"' in repos and "Open findings" in repos
    standards = client.get("/standards").text
    assert "Defaults" in standards and '[repos."octo/app"]' in standards
    assert "Can merge: 93 and up" in standards and "Can merge: 90 and up" in standards
    spots = client.get("/map").text
    assert 'class="hot-total num">2</strong>' in spots     # src/app.py: two open findings
    assert "app.py" in spots and "Hardcoded password" not in spots


def test_configurations_and_integrations(store):
    config = [("Database", "review-app.db", ""), ("Workers", "1", "one analysis at a time")]
    health = [("GitHub App", False, "GITHUB_APP_ID is not set"), ("semgrep", True, "/bin/semgrep")]
    app = create_dashboard_app(Store(store.path, readonly=True), PolicyBook(), health,
                               config=config)
    client = TestClient(app, base_url="http://localhost:8001")
    settings_page = client.get("/config").text
    assert "<code>review-app.db</code>" in settings_page and "one analysis at a time" in settings_page
    cards = client.get("/integrations").text
    states = re.findall(r'<h2 id="int-\d+">([^<]+)</h2>\s*<span class="tag[^"]*">([^<]+)</span>',
                        cards)
    assert states == [("GitHub App", "Needs setup"), ("NVIDIA NIM", "Not checked"),
                      ("Semgrep", "Ready"), ("pip-audit", "Not checked"),
                      ("Supabase Auth", "Not checked")]
    assert "No settings to show" in dashboard(store).get("/config").text


def test_search(store, queue):
    analyse_twice(store, queue)
    client = dashboard(store)
    page = client.get("/search", params={"q": "token"}).text
    assert "Token logged" in page and "Results for “token”" in page
    assert 'href="/prs/1"' in client.get("/search", params={"q": "#7"}).text
    assert 'href="/repos/octo/app"' in client.get("/search", params={"q": "octo/"}).text
    assert "Nothing found" in client.get("/search", params={"q": "100%_"}).text   # literal


def test_the_activity_chart(store, queue):
    run_one_pr(store, queue)
    page = dashboard(store).get("/").text
    assert page.count('<g class="pt') == 14                     # one hover target a day
    assert page.count('<g class="pt has last">') == 1           # today's decision
    assert "<strong>1</strong><span>decision in 14 days" in page
    assert re.search(r'<path class="line" d="M[\d. C-]+"/>', page)


def test_a_repository_page(store, queue):
    run_one_pr(store, queue)
    page = dashboard(store, PolicyBook(repos={"octo/app": Policy(mode="auto")})).get(
        "/repos/octo/app")
    assert page.status_code == 200
    assert "auto" in page.text and "#7" in page.text
    assert dashboard(store).get("/repos/octo/unknown").status_code == 404


def test_a_pull_request_page(store, queue):
    outcome = make_outcome(make_result(warnings=["LLM review skipped: timeout"]))
    run_one_pr(store, queue, outcome=outcome)
    page = dashboard(store).get("/prs/1")
    assert page.status_code == 200
    assert "Needs review" in page.text
    assert "The analysis is incomplete: pr-analyzer: LLM review skipped: timeout" in page.text
    assert "Confidence = size" in page.text
    assert "decision.made" in page.text
    assert dashboard(store).get("/prs/999").status_code == 404


def test_findings_are_grouped_by_severity_worst_first(store, queue):
    findings = [make_finding("LOW", title="Magic number", line=3),
                make_finding("CRITICAL", source="security", title="Hardcoded password"),
                make_finding("LOW", title="Magic number", line=9),
                make_finding("MEDIUM", title="Function too complex")]
    run_one_pr(store, queue, outcome=make_outcome(make_result(suggestions=findings)))
    page = dashboard(store).get("/prs/1").text
    groups = re.findall(r'<details class="sev-group tone-\w+"( open)?>\s*<summary>'
                        r'<span class="sev-name">(\w+)</span>', page)
    # the worst open, LOW folded away
    assert groups == [(" open", "Critical"), (" open", "Medium"), ("", "Low")]
    # the same finding in two places is listed once, with both lines
    assert page.count("Magic number") == 1
    assert "2 places" in page and "src/app.py:3, 9" in page


def test_the_audit_log_page(store, queue):
    run_one_pr(store, queue)
    client = dashboard(store)
    page = client.get("/audit")
    assert "Hash chain intact" in page.text
    assert "analysis.completed" in page.text
    filtered = client.get("/audit", params={"action": "decision."})
    assert "decision.made" in filtered.text and "analysis.completed" not in filtered.text
    assert "No entries match" in client.get("/audit", params={"action": "100%_"}).text
    assert "No entries match" in client.get("/audit", params={"repo": "octo/other"}).text


def test_audit_entries_link_to_their_pull_request(store, queue):
    run_one_pr(store, queue)
    store.audit("worker", "installation.created", installation=11)   # no PR to link to
    page = dashboard(store).get("/audit").text
    assert 'href="/prs/1">octo/app#7</a>' in page
    assert "Installation created" in page


def audit_ids(page) -> list[int]:
    return [int(n) for n in re.findall(r'<li class="event [^"]*" id="entry-(\d+)">', page.text)]


def test_the_audit_log_pages_back(store, monkeypatch):
    monkeypatch.setattr(web, "PAGE_SIZE", 2)
    for n in range(5):
        store.audit("worker", f"step.{n}")
    client = dashboard(store)

    first = client.get("/audit")
    assert audit_ids(first) == [5, 4]
    assert "before=4" in first.text
    second = client.get("/audit", params={"before": 4})
    assert audit_ids(second) == [3, 2]
    last = client.get("/audit", params={"before": 2})
    assert audit_ids(last) == [1]
    assert "Older entries" not in last.text


def test_audit_filters_combine(store):
    for repo, action in [("octo/app", "merge.attempted"), ("Octo/App", "merge.succeeded"),
                         ("octo/lib", "merge.attempted"), ("octo/app", "decision.made")]:
        store.audit("worker", action, repo, 7)
    client = dashboard(store)

    both = client.get("/audit", params={"repo": "octo/app", "action": "merge."})
    assert audit_ids(both) == [2, 1]        # the repository name is matched ignoring case
    assert audit_ids(client.get("/audit", params={"repo": "octo/app", "action": "merge.",
                                                  "before": 2})) == [1]
    assert audit_ids(client.get("/audit", params={"action": "merge_"})) == []   # _ is literal


def test_no_query_is_assembled_with_string_formatting():
    # the check pr-analyzer runs on every PR here flagged the audit page's f-string query (PR #10)
    rule = [p for p in DANGEROUS_PATTERNS if p["id"] == "sql-injection"]
    scanner = SecurityScanner()
    for path in sorted(Path(review_app.__file__).parent.glob("*.py")):
        lines = path.read_text(encoding="utf-8").split("\n")
        patch = f"@@ -0,0 +1,{len(lines)} @@\n" + "\n".join("+" + line for line in lines)
        added = FileChange(filename=f"review_app/{path.name}", status="added",
                           additions=len(lines), deletions=0, patch=patch)
        assert [(s.filename, s.line) for s in scanner.scan_dangerous(added, rule)] == []


def test_the_jobs_page_and_health(store, queue):
    queue.enqueue("analyze", "octo/app", 7, SHA, 11)
    client = dashboard(store)
    assert "octo/app#7" in client.get("/jobs").text
    assert client.get("/healthz").json()["jobs"]["queued"] == 1
    css = client.get("/static/style.css")
    assert css.headers["content-type"].startswith("text/css")
    assert client.get("/static/favicon.svg").headers["content-type"].startswith("image/svg+xml")


@pytest.mark.parametrize("path", ["/prs/999", "/prs/not-a-number", "/repos/octo/unknown",
                                  "/static/review-app.db", "/static/..%2Fweb.py", "/nowhere"])
def test_unknown_addresses_get_the_error_page(store, path):
    response = dashboard(store).get(path)
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert "Not found" in response.text
    assert response.headers["content-security-policy"] == SECURITY_HEADERS[
        "Content-Security-Policy"]


@pytest.mark.parametrize(("path", "current"), [
    ("/", ["/"]), ("/repos", ["/repos"]), ("/repos/octo/app", ["/repos"]), ("/map", ["/map"]),
    ("/standards", ["/standards"]), ("/triage", ["/triage"]), ("/prs/1", ["/triage"]),
    ("/findings", ["/findings"]), ("/audit", ["/audit"]), ("/jobs", ["/jobs"]),
    ("/config", ["/config"]), ("/integrations", ["/integrations"]), ("/search?q=x", []),
])
def test_the_nav_marks_the_current_section(store, queue, path, current):
    run_one_pr(store, queue)
    page = dashboard(store).get(path).text
    assert re.findall(r'<a href="([^"]+)" aria-current="page">', page) == current
    assert 'aria-disabled="true"' in page and "Workflows" in page     # coming soon, no link


def test_no_page_needs_what_the_csp_blocks(store, queue):
    # the CSP allows no script and no inline style; anything that needs one would silently break
    outcome = make_outcome(make_result(suggestions=[make_finding("HIGH")],
                                       warnings=["LLM review skipped: timeout"]))
    run_one_pr(store, queue, outcome=outcome)
    queue.enqueue("analyze", "octo/app", 8, SHA, 11)
    client = dashboard(store, health=[("semgrep", True, "/usr/bin/semgrep")])
    for path in ("/", "/repos/octo/app", "/prs/1", "/audit", "/jobs", "/prs/999", "/findings",
                 "/findings?severity=high&q=x", "/triage", "/repos", "/standards", "/map",
                 "/config", "/integrations", "/search?q=helper"):
        page = client.get(path).text
        assert "<script" not in page and "<style" not in page, path
        assert not re.search(r"\sstyle=", page), path
        assert 'href="/static/style.css"' in page, path


@pytest.mark.parametrize(("header", "name"), [
    ("localhost:8001", "localhost"), ("[::1]:8001", "[::1]"), ("127.0.0.1", "127.0.0.1"),
    ("Evil.Example:80", "evil.example"), ("", ""),
])
def test_host_name(header, name):
    assert host_name(header) == name
