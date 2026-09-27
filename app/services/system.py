"""Стан системи: те, що показує healthcheck і сторінка «Система».

Одна функція на дві поверхні (`/api/v1/health` і `/settings`), щоб вони не
розійшлися (docs/FRONTEND.md, розд. 2).
"""

from __future__ import annotations

import os
from typing import Any

from app.config import get_settings
from app.version import __version__


def health_snapshot() -> dict[str, Any]:
    """Знімок стану застосунку для UI і healthcheck.

    Свідомо НЕ пробує TTS-шлюз: перевірка мережі на кожному запиті сторінки
    була б і повільною, і оманливою. Окремий пробник — у планах
    (docs/FRONTEND.md, знахідка 16.1).
    """
    settings = get_settings()
    return {
        "status": "ok",
        "version": __version__,
        "data_dir": str(settings.data_dir),
        "data_dir_writable": settings.data_dir.is_dir() and os.access(settings.data_dir, os.W_OK),
        "tts_base_url": settings.tts_base_url,
        "default_engine": settings.default_engine,
        "default_voice": settings.default_voice,
        "default_emotion": settings.default_emotion,
        "synth_concurrency": settings.synth_concurrency,
        "target_lufs": settings.target_lufs,
        "target_sample_rate": settings.target_sample_rate,
        "segment_max_chars": settings.segment_max_chars,
        "max_upload_mb": settings.max_upload_mb,
        "allowed_extensions": settings.allowed_extensions,
        "enable_pitch_shift": settings.enable_pitch_shift,
    }
