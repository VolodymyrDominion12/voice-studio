"""Каталог TTS-рушіїв і голосів.

Джерело правди для JSON-API (`/api/v1/engines`) і HTML-інтерфейсу
(сторінка «Рушії та голоси», селектори в редакторі).

Ключова вимога ADR-001: **кожен рушій зобов'язаний оголосити `capabilities`**,
щоб шар виразу знав, що бере на себе, а що рушій уміє нативно. Для недоступних
рушіїв (`available: false`) у каталозі лишається пояснення, чому їх немає, —
UI показує це користувачу, а не ховає.
"""

from __future__ import annotations

import logging

from app.config import get_settings
from app.services.tts.openai_compat import get_openai_compat_engine

logger = logging.getLogger(__name__)

# ── Каталог ───────────────────────────────────────────────────────────────────
# available=True → адаптер реалізовано; False → рушій лише в каталозі.
_ENGINE_CATALOGUE: list[dict] = [
    {
        "id": "openai_compat",
        "label": "OpenAI-сумісний шлюз (Speaches)",
        "available": True,
        "capabilities": {
            "emotions":       False,
            "emotion_tags":   False,
            "voice_cloning":  False,
            "speed":          True,
            "streaming":      True,
            "max_chars":      500,
            "sample_rate":    22050,
        },
        "voices": [
            {"id": "uk_UA-tetiana-high",         "lang": "uk_UA", "gender": "f"},
            {"id": "uk_UA-mykyta-high",           "lang": "uk_UA", "gender": "m"},
            {"id": "uk_UA-oleksa-high",           "lang": "uk_UA", "gender": "m"},
            {"id": "uk_UA-lada-x_low",            "lang": "uk_UA", "gender": "f"},
            {"id": "uk_UA-ukrainian_tts-medium",  "lang": "uk_UA", "gender": "multi"},
        ],
    },
    {
        "id": "ukrainian_tts",
        "label": "ukrainian-tts (ESPNet, автоматичний наголос)",
        "available": False,
        "capabilities": {
            "emotions":      False,
            "emotion_tags":  False,
            "voice_cloning": False,
            "speed":         True,
            "streaming":     False,
            "max_chars":     400,
            "sample_rate":   22050,
        },
        "voices": [
            {"id": v, "lang": "uk_UA"}
            for v in ("tetiana", "mykyta", "lada", "dmytro", "oleksa")
        ],
        "note": "Адаптер планується в Етапі 3 (PLAN.md). MIT, прямий in-process інференс.",
    },
    {
        "id": "zonos2",
        "label": "ZONOS2 (Zyphra, 7.6B MoE, CPU через zonos2.cpp)",
        "available": False,
        "capabilities": {
            "emotions":      True,
            "emotion_tags":  False,
            "voice_cloning": True,
            "speed":         True,
            "streaming":     True,
            "max_chars":     300,
            "sample_rate":   44100,
        },
        "voices": [],
        "note": (
            "Apache-2.0, українська в Tier 3. Єдина знайдена відкрита модель "
            "з виразом + українською + клонуванням голосу. НЕ перевірено: "
            "якість української та швидкість на CPU — див. PLAN.md, етап 3.1."
        ),
    },
    {
        "id": "chatterbox",
        "label": "Chatterbox Multilingual V3 (ет. 3, англійська)",
        "available": False,
        "capabilities": {
            "emotions":      True,
            "emotion_tags":  False,
            "voice_cloning": True,
            "speed":         True,
            "streaming":     False,
            "max_chars":     300,
            "sample_rate":   24000,
        },
        "voices": [],
        "note": "23 мови, української серед них немає",
    },
]

_ENGINE_INDEX: dict[str, dict] = {engine["id"]: engine for engine in _ENGINE_CATALOGUE}


class EngineNotFoundError(LookupError):
    """Рушія з таким id немає в каталозі (API: 404)."""


# ── Читання каталогу ──────────────────────────────────────────────────────────

def list_engines() -> list[dict]:
    """Увесь каталог разом із недоступними рушіями."""
    return list(_ENGINE_CATALOGUE)


def get_engine(engine_id: str) -> dict:
    """Запис каталогу за id або EngineNotFoundError."""
    engine = _ENGINE_INDEX.get(engine_id)
    if not engine:
        raise EngineNotFoundError(f"Рушій {engine_id!r} не знайдено")
    return engine


def capabilities_of(engine_id: str) -> dict:
    """Оголошені можливості рушія."""
    return dict(get_engine(engine_id)["capabilities"])


def available_engines() -> list[dict]:
    """Лише рушії з реалізованим адаптером."""
    return [engine for engine in _ENGINE_CATALOGUE if engine.get("available")]


def unavailable_engines() -> list[dict]:
    """Рушії в каталозі без адаптера — UI показує їх із поясненням."""
    return [engine for engine in _ENGINE_CATALOGUE if not engine.get("available")]


def voices_of(engine_id: str) -> list[dict]:
    """Голоси рушія.

    Для рушіїв із живим адаптером питаємо адаптер: він може знати більше,
    ніж статичний каталог.
    """
    engine = get_engine(engine_id)
    if engine_id == "openai_compat" and engine.get("available"):
        return [
            {"id": voice.id, "lang": voice.lang, "gender": voice.gender, "name": voice.name}
            for voice in get_openai_compat_engine().voices()
        ]
    return list(engine.get("voices", []))


def default_engine_id() -> str:
    return get_settings().default_engine


def default_voice_id() -> str:
    return get_settings().default_voice


def pick_engine(engine_id: str | None) -> dict:
    """Обраний рушій або рушій за замовчуванням.

    Якщо в налаштуваннях указів рушій без адаптера — тихо повертаємось до
    першого доступного, бо інакше UI просто не мав би що показати.
    """
    if engine_id:
        try:
            candidate = get_engine(engine_id)
        except EngineNotFoundError:
            candidate = None
        if candidate and candidate.get("available"):
            return candidate

    configured = _ENGINE_INDEX.get(default_engine_id())
    if configured and configured.get("available"):
        return configured

    available = available_engines()
    if not available:
        raise EngineNotFoundError("Немає жодного рушія з реалізованим адаптером")
    return available[0]
