"""Налаштування застосунку.

Усі значення читаються зі змінних середовища з префіксом, див. `.env.example`.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Шляхи ────────────────────────────────────────────────────────────
    data_dir: Path = Field(default=PROJECT_ROOT / "data")
    model_dir: Path = Field(default=PROJECT_ROOT / "data" / "models")

    # ── TTS-шлюз ─────────────────────────────────────────────────────────
    tts_base_url: str = "http://127.0.0.1:8001/v1"
    tts_api_key: str = "not-needed"
    tts_model: str = "tts-1"

    # ── Рушій за замовчуванням ───────────────────────────────────────────
    default_engine: str = "openai_compat"
    default_voice: str = "uk_UA-tetiana-high"
    default_emotion: str = "neutral"

    # ── Синтез ───────────────────────────────────────────────────────────
    synth_concurrency: int = Field(default=2, ge=1, le=16)
    target_lufs: float = -18.0
    target_sample_rate: int = 24000
    segment_max_chars: int = Field(default=300, ge=40, le=2000)

    # ── Пост-обробка ─────────────────────────────────────────────────────
    # Зсув тону найлегше звучить штучно — типово вимкнено (ADR-004).
    enable_pitch_shift: bool = False

    # ── Завантаження ─────────────────────────────────────────────────────
    max_upload_mb: int = 50
    allowed_extensions: str = ".txt,.md,.markdown,.pdf,.docx,.epub,.html,.htm"

    # ── Зовнішній API (ет. 3, вимкнено) ──────────────────────────────────
    remote_tts_enabled: bool = False
    remote_tts_base_url: str = ""
    remote_tts_api_key: str = ""

    @field_validator("data_dir", "model_dir", mode="after")
    @classmethod
    def _resolve(cls, v: Path) -> Path:
        return v if v.is_absolute() else (PROJECT_ROOT / v).resolve()

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def renders_dir(self) -> Path:
        return self.data_dir / "renders"

    @property
    def voices_dir(self) -> Path:
        return self.data_dir / "voices"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "voice_studio.sqlite3"

    @property
    def allowed_suffixes(self) -> set[str]:
        return {
            s.strip().lower()
            for s in self.allowed_extensions.split(",")
            if s.strip()
        }

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.model_dir, self.uploads_dir,
                  self.renders_dir, self.voices_dir):
            d.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
