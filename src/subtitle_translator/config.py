from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="SUBTITLE_TRANSLATOR_",
        extra="ignore",
    )

    host: str = "127.0.0.1"
    port: int = 8012
    llm_base_url: str = "http://127.0.0.1:8080/v1"
    llm_api_key: str = "local"
    llm_model: str = "qwen3.8-27b"
    batch_size: int = 20
    timeout_seconds: float = 180


@lru_cache
def get_settings() -> Settings:
    return Settings()

