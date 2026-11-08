"""Settings from the environment or a .env file in the directory the app runs from.

pr-analyzer reads its own settings (LLM_MODEL, NVIDIA_API_KEY, SEMGREP_CONFIG and the rest) from
the same place. Secrets are SecretStr, so they never show up in a repr or a log line."""
from pathlib import Path

from pr_analyzer.config import Settings as AnalyzerSettings
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

MEBIBYTE = 1024 * 1024
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

    @property
    def webhook_secret(self) -> bytes:
        return self.github_webhook_secret.get_secret_value().encode()

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
