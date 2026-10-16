from pydantic import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file =".env",
        env_file_encoding= "utf-8"
    )

    github_token : str
    max_diff_lines: int = 3000



settings = Settings()