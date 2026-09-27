"""Стан системи: те, що показує healthcheck і сторінка «Система».

Одна функція на дві поверхні (`/api/v1/health` і `/settings`), щоб те,
що бачить користувач, не розходилося з тим, що віддає API.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx

from app.config import get_settings
from app.version import __version__

logger = logging.getLogger(__name__)


def health_snapshot() -> dict[str, Any]:
    """Знімок стану застосунку для UI і healthcheck.

    Свідомо НЕ пробує TTS-шлюз: перевірка мережі на кожному запиті сторінки
    була б і повільною, і оманливою. Для перевірки шлюзу є окремий
    `probe_tts_gateway()` і маршрут `GET /api/v1/health/tts`.
    """
    settings = get_settings()
    return {
        "status": "ok",
        "version": __version__,
        "data_dir": str(settings.data_dir),
        "data_dir_writable": settings.data_dir.is_dir() and os.access(settings.data_dir, os.W_OK),
        "tts_base_url": settings.tts_base_url,
        "tts_model": settings.tts_model,
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


def probe_tts_gateway(timeout: float = 3.0) -> dict[str, Any]:
    """Перевірити TTS-шлюз: чи живий і чи встановлено потрібну модель.

    Навіщо саме так: найдорожча помилка в цьому застосунку — створити завдання
    на 20 хвилин і дізнатися через три сегменти, що шлюз відповідає `404 Model
    ... is not installed`. Тому пробник перевіряє не лише доступність, а й
    наявність **сконфігурованої моделі** серед установлених, і повертає готову
    підказку, що саме виправити.

    Повертає словник, а не кидає винятки: недоступний шлюз — це стан, а не збій.
    """
    settings = get_settings()
    base = settings.tts_base_url.rstrip("/")
    url = f"{base}/models"

    result: dict[str, Any] = {
        "url": url,
        "configured_model": settings.tts_model,
        "reachable": False,
        "latency_ms": None,
        "models": [],
        "model_installed": None,   # None = невідомо (не змогли перевірити)
        "hint": "",
    }

    started = time.perf_counter()
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(url, headers={"Authorization": f"Bearer {settings.tts_api_key}"})
        result["latency_ms"] = round((time.perf_counter() - started) * 1000)
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as exc:
        result["hint"] = (
            f"Шлюз відповів {exc.response.status_code} на {url}. "
            "Перевірте, що TTS_BASE_URL вказує на /v1 OpenAI-сумісного сервера."
        )
        return result
    except httpx.RequestError as exc:
        result["hint"] = (
            f"Шлюз недоступний за {base}: {exc}. "
            "Підніміть контейнер: docker compose -f docker/docker-compose.yml up -d tts"
        )
        return result
    except ValueError:
        result["hint"] = f"Шлюз відповів не-JSON на {url} — це не OpenAI-сумісний сервер."
        return result

    result["reachable"] = True
    models = payload.get("data") if isinstance(payload, dict) else None
    ids = [item.get("id", "") for item in models or [] if isinstance(item, dict)]
    result["models"] = ids

    configured = settings.tts_model
    result["model_installed"] = configured in ids if ids else None

    if result["model_installed"] is False:
        result["hint"] = (
            f"Шлюз живий, але моделі {configured!r} у ньому немає. "
            f"Установлені: {', '.join(ids) if ids else '—'}. "
            f"Задайте TTS_MODEL у .env (наприклад {ids[0]!r}) і перезапустіть застосунок."
        )
    elif not ids:
        result["hint"] = (
            "Шлюз живий, але не повідомляє жодної моделі — синтез замовкне на першому ж запиті."
        )

    return result


def tts_ready_for_synthesis() -> tuple[bool, str]:
    """Чи можна запускати синтез: (можна, причина-якщо-ні).

    Використовується перед створенням завдання. Якщо перевірити не вдалося
    (наприклад, шлюз не віддає список моделей), НЕ блокуємо роботу: краще
    спробувати й показати реальну помилку, ніж заборонити через непевність.
    """
    probe = probe_tts_gateway()
    if not probe["reachable"]:
        return False, probe["hint"]
    if probe["model_installed"] is False:
        return False, probe["hint"]
    return True, ""

