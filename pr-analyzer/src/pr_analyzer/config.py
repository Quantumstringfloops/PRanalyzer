from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file =".env",
        env_file_encoding= "utf-8",
        env_ignore_empty=True
    )

    github_token : str = "test_token"
    max_diff_lines: int = 3000
    llm_provider: str = "openai"      # "openai" | "anthropic" | "ollama"
    llm_model: str = "gpt-4o-mini"
    llm_confidence_threshold: float = 0.5
    openai_api_key:str
    



settings = Settings()