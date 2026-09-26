from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="SUBTITLE_TRANSLATOR_",
        extra="ignore",
    )

    host: str = "127.0.0.1"
    port: int = 8012
    llm_provider: str = "deepseek"
    llm_base_url: str = "https://api.deepseek.com"
    llm_api_key: str = ""
    llm_model: str = "deepseek-flash"
    llm_thinking: bool = False
    llm_max_output_tokens: int = Field(default=4096, ge=1)
    llm_retry_attempts: int = Field(default=3, ge=1)
    llm_retry_backoff_seconds: float = Field(default=1, ge=0)
    batch_size: int = Field(default=20, ge=1)
    timeout_seconds: float = Field(default=300, gt=0)
    temperature: float = Field(default=0.1, ge=0, le=2)
    top_p: float = Field(default=0.9, gt=0, le=1)

    media_root: Path = Path("/home/simon/modeling-video")
    media_mount_source: str | None = r"Y:\模型制作视频"
    media_mount_type: str = "drvfs"
    media_mount_options: str = "rw"

    data_dir: Path = Path("/home/simon/subtitle-output")
    target_language: str = "zh-CN"
    glossary_path: Path = Path("config/glossary.json")
    export_srt: bool = True
    srt_line_width: int = Field(default=24, ge=8, le=80)
    srt_max_lines: int = Field(default=2, ge=1, le=4)
    quality_max_duration: float = Field(default=8.0, gt=0)
    quality_max_reading_speed: float = Field(default=12.0, gt=0)
    overwrite_unmanaged_srt: bool = False
    max_attempts: int = Field(default=3, ge=1)
    stale_lock_seconds: int = Field(default=3600, ge=1)
    profile_version: int = Field(default=1, ge=1)

    @field_validator("target_language")
    @classmethod
    def target_language_must_be_filename_safe(cls, value: str) -> str:
        if not value or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
            for character in value
        ):
            raise ValueError("target_language must contain only letters, digits, '.', '_' or '-'")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
