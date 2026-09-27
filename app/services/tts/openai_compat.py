"""Адаптер до OpenAI-сумісного TTS-шлюзу (Speaches, Kokoro-FastAPI, тощо).

Один адаптер закриває будь-який /v1/audio/speech сервер (ADR-005).
Синтез синхронний: POST → WAV-файл → шлях на диску.
"""

from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path

import httpx

from app.config import get_settings
from app.services.tts.base import EngineCapabilities, SynthRequest, Voice

logger = logging.getLogger(__name__)


class OpenAICompatEngine:
    """TTS-адаптер через /v1/audio/speech (OpenAI-формат)."""

    id = "openai_compat"

    def __init__(self) -> None:
        settings = get_settings()
        self._base_url = settings.tts_base_url.rstrip("/")
        self._api_key = settings.tts_api_key
        self._model = settings.tts_model
        self._timeout = 120.0

    def capabilities(self) -> EngineCapabilities:
        return EngineCapabilities(
            emotions=False,
            emotion_tags=False,
            voice_cloning=False,
            speed=True,
            streaming=True,
            max_chars=500,
            sample_rate=22050,
        )

    def voices(self) -> list[Voice]:
        """Повертаємо статичний список голосів (Piper-голоси Speaches).

        При бажанні можна запитати /v1/audio/voices динамічно.
        """
        return [
            Voice(id="uk_UA-tetiana-high",         lang="uk_UA", gender="f"),
            Voice(id="uk_UA-mykyta-high",           lang="uk_UA", gender="m"),
            Voice(id="uk_UA-oleksa-high",           lang="uk_UA", gender="m"),
            Voice(id="uk_UA-lada-x_low",            lang="uk_UA", gender="f"),
            Voice(id="uk_UA-ukrainian_tts-medium",  lang="uk_UA", gender="multi"),
        ]

    def synthesize(self, req: SynthRequest) -> Path:
        """POST /v1/audio/speech → WAV-файл на диску."""
        settings = get_settings()
        output_path = req.output_path or self._default_path(req, settings)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Якщо файл уже є — ідемпотентність (ADR-007)
        if output_path.exists() and output_path.stat().st_size > 0:
            logger.debug("Сегмент вже синтезовано: %s", output_path)
            return output_path

        payload = {
            "model": self._model,
            "input": req.text,
            "voice": req.voice_id or "uk_UA-tetiana-high",
            "response_format": "wav",
            "speed": req.speed,
        }

        logger.debug("TTS POST voice=%s chars=%d", payload["voice"], len(req.text))
        t0 = time.perf_counter()

        try:
            with httpx.Client(timeout=self._timeout) as client:
                resp = client.post(
                    f"{self._base_url}/audio/speech",
                    json=payload,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"TTS-шлюз повернув {exc.response.status_code}: {exc.response.text[:200]}"
            ) from exc
        except httpx.RequestError as exc:
            raise RuntimeError(f"TTS-шлюз недоступний: {exc}") from exc

        output_path.write_bytes(resp.content)
        elapsed = time.perf_counter() - t0
        logger.info(
            "Синтезовано %d символів за %.2f с → %s",
            len(req.text), elapsed, output_path.name,
        )
        return output_path

    @staticmethod
    def _default_path(req: SynthRequest, settings) -> Path:
        """Унікальний шлях на диску на основі хешу тексту."""
        text_hash = hashlib.md5(req.text.encode()).hexdigest()[:12]
        voice_slug = (req.voice_id or "default").replace("/", "_")
        filename = f"{voice_slug}_{text_hash}.wav"
        return settings.renders_dir / "segments" / filename


# Singleton — один клієнт на весь процес
_instance: OpenAICompatEngine | None = None


def get_openai_compat_engine() -> OpenAICompatEngine:
    global _instance
    if _instance is None:
        _instance = OpenAICompatEngine()
    return _instance
