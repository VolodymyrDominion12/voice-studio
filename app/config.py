"""Налаштування застосунку.

Усі значення читаються зі змінних середовища з префіксом, див. `.env.example`.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Шляхи ────────────────────────────────────────────────────────────
    # Два імені навмисно: `.env.example` і docker-compose роками документують
    # `VOICE_STUDIO_DATA_DIR`, а `Settings` (без env_prefix) читав лише
    # `DATA_DIR` — тож людина, яка переносила дані на інший диск (ризик R4 у
    # PLAN — найгостріший у проєкті), отримувала мовчазне ігнорування й
    # запис у теку репозиторію. Тепер працюють обидва імені; змінна
    # середовища має пріоритет над значенням із `.env`, як і для решти полів.
    data_dir: Path = Field(
        default=PROJECT_ROOT / "data",
        validation_alias=AliasChoices("DATA_DIR", "VOICE_STUDIO_DATA_DIR"),
    )
    model_dir: Path = Field(
        default=PROJECT_ROOT / "data" / "models",
        validation_alias=AliasChoices("MODEL_DIR", "VOICE_STUDIO_MODEL_DIR"),
    )

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

    # ── Збірка аудіокниги ────────────────────────────────────────────────
    # Які файли створювати за замовчуванням: mp3, wav, m4b (можна кілька
    # через кому). MP3 — найсумісніший, M4B — формат аудіокниги з розділами.
    output_formats: str = "mp3"
    m4b_bitrate_kbps: int = Field(default=64, ge=16, le=320)
    mp3_bitrate_kbps: int = Field(default=192, ge=32, le=320)
    audiobook_artist: str = "Voice Studio"

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

    @property
    def output_format_list(self) -> tuple[str, ...]:
        """`OUTPUT_FORMATS` як кортеж валідних імен форматів.

        Імпорт усередині методу: `app.services.audio.assemble` сам читає
        налаштування (бітрейти), і імпорт на рівні модуля замкнув би цикл
        config → assemble → config.
        """
        from app.services.audio.assemble import normalize_formats

        try:
            return normalize_formats(self.output_formats)
        except ValueError:
            # Друкарська помилка в .env не має валити застосунок: озвучення
            # має працювати, навіть якщо хтось написав OUTPUT_FORMATS=mp4.
            return ("mp3",)

    @property
    def templates_dir(self) -> Path:
        """Тека Jinja2-шаблонів (HTML-інтерфейс, ADR-008)."""
        return PROJECT_ROOT / "app" / "templates"

    @property
    def static_dir(self) -> Path:
        """Тека статики (css; згодом — htmx і Alpine)."""
        return PROJECT_ROOT / "app" / "static"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.model_dir, self.uploads_dir,
                  self.renders_dir, self.voices_dir):
            d.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
