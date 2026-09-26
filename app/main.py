"""Voice Studio — точка входу FastAPI.

СТАТУС: каркас. Тут свідомо реалізовано лише те, що потрібно, щоб застосунок
запускався і був готовий до наповнення: налаштування, healthcheck (на нього
посилається docker/Dockerfile.app), опис можливостей рушіїв.

Решта ендпоінтів з docs/ARCHITECTURE.md (розділ 4) додається на етапі 1 —
див. docs/PLAN.md.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.config import get_settings

logger = logging.getLogger("voice_studio")

__version__ = "0.1.0"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.ensure_dirs()
    logger.info("Voice Studio %s — data_dir=%s", __version__, settings.data_dir)
    logger.info("TTS-шлюз: %s", settings.tts_base_url)
    yield
    logger.info("Зупинка Voice Studio")


app = FastAPI(
    title="Voice Studio",
    description="Локальне озвучення текстів із виразом на відкритих TTS-моделях",
    version=__version__,
    lifespan=lifespan,
)


@app.get("/api/v1/health", tags=["service"])
async def health() -> dict[str, object]:
    """Healthcheck. Використовується Docker HEALTHCHECK і фронтендом."""
    settings = get_settings()
    return {
        "status": "ok",
        "version": __version__,
        "data_dir": str(settings.data_dir),
        "data_dir_writable": settings.data_dir.exists(),
        "tts_base_url": settings.tts_base_url,
        "default_engine": settings.default_engine,
    }


@app.get("/api/v1/engines", tags=["engines"])
async def engines() -> JSONResponse:
    """Перелік доступних рушіїв із їхніми можливостями.

    СТАТУС: заглушка з реальними фактами про рушії (з docs/RESEARCH.md), але
    без підключення самих адаптерів. Дозволяє фронтенду будувати UI вже зараз
    і коректно деградувати, коли рушій не вміє емоцій (ADR-001, ADR-004).

    `available: false` означає, що адаптер ще не реалізовано, а не що рушій
    поганий. Реальна перевірка доступності додається разом із адаптерами.
    """
    catalogue = [
        {
            "id": "openai_compat",
            "label": "OpenAI-сумісний шлюз (Speaches)",
            "available": False,
            "capabilities": {
                "emotions": False,
                "emotion_tags": False,
                "voice_cloning": False,
                "speed": True,
                "streaming": True,
                "max_chars": 500,
                "sample_rate": 22050,
            },
            "voices": [
                {"id": "uk_UA-tetiana-high", "lang": "uk_UA", "gender": "f"},
                {"id": "uk_UA-mykyta-high", "lang": "uk_UA", "gender": "m"},
                {"id": "uk_UA-oleksa-high", "lang": "uk_UA", "gender": "m"},
                {"id": "uk_UA-lada-x_low", "lang": "uk_UA", "gender": "f"},
                {"id": "uk_UA-ukrainian_tts-medium", "lang": "uk_UA", "gender": "multi"},
            ],
        },
        {
            "id": "ukrainian_tts",
            "label": "ukrainian-tts (ESPNet, автоматичний наголос)",
            "available": False,
            "capabilities": {
                "emotions": False,
                "emotion_tags": False,
                "voice_cloning": False,
                "speed": True,
                "streaming": False,
                "max_chars": 400,
                "sample_rate": 22050,
            },
            "voices": [
                {"id": v, "lang": "uk_UA"}
                for v in ("tetiana", "mykyta", "lada", "dmytro", "oleksa")
            ],
        },
        {
            "id": "zonos2",
            "label": "ZONOS2 (Zyphra, 7.6B MoE, CPU через zonos2.cpp)",
            "available": False,
            "capabilities": {
                "emotions": True,
                "emotion_tags": False,
                "voice_cloning": True,
                "speed": True,
                "streaming": True,
                "max_chars": 300,
                "sample_rate": 44100,
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
                "emotions": True,
                "emotion_tags": False,
                "voice_cloning": True,
                "speed": True,
                "streaming": False,
                "max_chars": 300,
                "sample_rate": 24000,
            },
            "voices": [],
            "note": "23 мови, української серед них немає",
        },
    ]
    return JSONResponse({"engines": catalogue})


@app.get("/api/v1/emotions", tags=["expression"])
async def emotions() -> dict[str, object]:
    """Профілі емоцій шару виразу (ADR-004).

    СТАТУС: значення з docs/ARCHITECTURE.md, розділ 2.4. На етапі 2 переїдуть
    у app/services/expression/profiles.py і читатимуться звідти.
    """
    profiles = {
        "neutral": {"speed": 1.00, "pitch": 0.0, "energy_db": 0.0, "pause_after_ms": 400},
        "warm": {"speed": 0.96, "pitch": -0.5, "energy_db": 0.0, "pause_after_ms": 500},
        "serious": {"speed": 0.94, "pitch": -1.0, "energy_db": -1.0, "pause_after_ms": 650},
        "joyful": {"speed": 1.08, "pitch": 1.5, "energy_db": 1.0, "pause_after_ms": 350},
        "excited": {"speed": 1.15, "pitch": 2.0, "energy_db": 2.0, "pause_after_ms": 250},
        "sad": {"speed": 0.88, "pitch": -1.5, "energy_db": -2.0, "pause_after_ms": 900},
        "tense": {"speed": 1.05, "pitch": 1.0, "energy_db": 0.0, "pause_after_ms": 180},
        "questioning": {"speed": 1.02, "pitch": 1.0, "energy_db": 0.0, "pause_after_ms": 450},
    }
    return {
        "profiles": profiles,
        "intensity_scales_delta_from_neutral": True,
        "note": (
            "Це керована просодія, а не акторська гра моделі. "
            "Паузи й темп рушія — без артефактів; зсув тону типово вимкнено."
        ),
    }
