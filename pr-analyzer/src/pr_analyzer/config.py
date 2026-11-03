from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        # other tools often keep their keys in the same .env; don't refuse to start because of them
        extra="ignore",
    )

    # GitHub: the token is optional for public repos (unauthenticated = 60 requests/hour)
    github_token: str = ""
    github_timeout_seconds: int = Field(default=30, gt=0)

    # LLM review
    llm_provider: str = "ollama"      # "ollama" | "openai" | "anthropic"
    llm_model: str = ""               # empty = the provider's default model
    llm_confidence_threshold: float = Field(default=0.5, ge=0, le=1)
    llm_timeout_seconds: float = Field(default=120, gt=0)
    llm_max_tokens: int = Field(default=4096, gt=0)
    max_diff_lines: int = Field(default=3000, gt=0)   # diff lines sent to the LLM
    ollama_url: str = "http://localhost:11434"
    openai_api_key: str = ""
    anthropic_api_key: str = ""

    # external scanners (skipped with a warning when the tool isn't installed)
    semgrep_config: str = "p/owasp-top-ten"
    semgrep_timeout_seconds: int = Field(default=300, gt=0)
    pip_audit_timeout_seconds: int = Field(default=300, gt=0)

    log_level: str = "WARNING"


settings = Settings()
