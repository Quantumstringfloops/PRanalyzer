"""Settings from the environment or a .env file in the directory the app runs from.

pr-analyzer reads its own settings (LLM_MODEL, NVIDIA_API_KEY, SEMGREP_CONFIG and the rest) from
the same place. Secrets are SecretStr, so they never show up in a repr or a log line."""
import re
from pathlib import Path
from urllib.parse import urlsplit

from pr_analyzer.config import Settings as AnalyzerSettings
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from review_app.auth import Allowlist, Login, SupabaseAuth

MEBIBYTE = 1024 * 1024
# a host name as a Host header carries it, without the port: review.example.com, 10.0.0.5
HOST_NAME = re.compile(r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$")
# the AI review runs on NVIDIA-hosted models unless LLM_PROVIDER says otherwise
DEFAULT_LLM_PROVIDER = "nvidia"


class SettingsError(Exception):
    pass


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",   # pr-analyzer's settings share the file
    )

    # the GitHub App: its id, the private key it signs JWTs with, and the webhook secret
    github_app_id: int | None = Field(default=None, gt=0)
    github_app_private_key_path: Path | None = None
    github_webhook_secret: SecretStr = SecretStr("")

    review_app_db: Path = Path("review-app.db")
    review_app_policy: Path = Path("review-app.toml")
    # jobs worked on at once; each runs a whole pr-analyzer analysis
    review_app_workers: int = Field(default=1, ge=1, le=8)
    review_app_poll_seconds: float = Field(default=2.0, gt=0)
    review_app_max_webhook_bytes: int = Field(default=5 * MEBIBYTE, gt=0)
    review_app_skip_llm: bool = False

    # Dashboard login through Supabase Auth (README, "Dashboard login"). With none of these set
    # there is no login, and the dashboard answers on localhost only.
    supabase_url: str = ""                         # https://<project>.supabase.co
    supabase_anon_key: SecretStr = SecretStr("")    # the anon (publishable) key, never service_role
    supabase_jwt_secret: SecretStr = SecretStr("")  # only for projects on the legacy JWT secret
    review_app_admin_emails: str = ""     # who may sign in: addresses or @domain, comma-separated
    review_app_dashboard_hosts: str = ""  # Host names to answer besides localhost, once login is on

    @property
    def webhook_secret(self) -> bytes:
        return self.github_webhook_secret.get_secret_value().encode()

    @property
    def login_enabled(self) -> bool:
        """Login is on once any of its Supabase settings is; login_problems() says what's missing."""
        return bool(self.supabase_url or self.supabase_anon_key.get_secret_value())

    @property
    def dashboard_hosts(self) -> frozenset[str]:
        names = re.split(r"[,\s]+", self.review_app_dashboard_hosts.lower())
        return frozenset(name for name in names if name)

    def login_problems(self) -> list[str]:
        """What stops dashboard login from working, once any of it is set. Empty when it works,
        and when it's off."""
        if not self.login_enabled:
            # other host names without a login would put the dashboard on the network unguarded
            return ["REVIEW_APP_DASHBOARD_HOSTS needs dashboard login (SUPABASE_URL and the rest)"
                    ] if self.dashboard_hosts else []
        problems = []
        url = urlsplit(self.supabase_url)
        local = url.hostname in ("localhost", "127.0.0.1", "::1")
        if not self.supabase_url:
            problems.append("SUPABASE_URL is not set")
        elif not url.hostname or (url.scheme != "https" and not (url.scheme == "http" and local)):
            problems.append("SUPABASE_URL must be https://<project>.supabase.co "
                            "(plain http only for a local Supabase)")
        if not self.supabase_anon_key.get_secret_value():
            problems.append("SUPABASE_ANON_KEY is not set")
        if not Allowlist.parse(self.review_app_admin_emails):
            problems.append("REVIEW_APP_ADMIN_EMAILS is empty, so nobody could sign in")
        for name in self.dashboard_hosts:
            if not HOST_NAME.match(name):
                problems.append(f"REVIEW_APP_DASHBOARD_HOSTS: {name!r} isn't a host name")
        return problems

    def login(self) -> Login | None:
        """The dashboard's login, or None when it's off. Call login_problems() first."""
        if not self.login_enabled:
            return None
        auth = SupabaseAuth(self.supabase_url, self.supabase_anon_key.get_secret_value(),
                            self.supabase_jwt_secret.get_secret_value())
        return Login(auth=auth, allowlist=Allowlist.parse(self.review_app_admin_emails),
                     hosts=self.dashboard_hosts)

    def app_problems(self) -> list[str]:
        """What stops the app from acting as a GitHub App; empty when it can."""
        problems = []
        if self.github_app_id is None:
            problems.append("GITHUB_APP_ID is not set")
        if self.github_app_private_key_path is None:
            problems.append("GITHUB_APP_PRIVATE_KEY_PATH is not set")
        elif not self.github_app_private_key_path.is_file():
            problems.append(f"GITHUB_APP_PRIVATE_KEY_PATH: no file at "
                            f"{self.github_app_private_key_path}")
        if not self.webhook_secret:
            problems.append("GITHUB_WEBHOOK_SECRET is not set")
        return problems

    def private_key(self) -> str:
        if self.github_app_private_key_path is None:
            raise SettingsError("GITHUB_APP_PRIVATE_KEY_PATH is not set")
        try:
            return self.github_app_private_key_path.read_text(encoding="utf-8")
        except OSError as e:
            raise SettingsError(f"Can't read GITHUB_APP_PRIVATE_KEY_PATH: {e.strerror or e}") from e


def use_nvidia_by_default(analyzer: AnalyzerSettings) -> None:
    """Point pr-analyzer's AI review at NVIDIA unless LLM_PROVIDER was set explicitly."""
    if "llm_provider" not in analyzer.model_fields_set:
        analyzer.llm_provider = DEFAULT_LLM_PROVIDER
