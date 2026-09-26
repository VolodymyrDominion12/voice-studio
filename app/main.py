"""Voice Studio — точка входу FastAPI.

Реалізовано:
  - healthcheck (використовується Docker HEALTHCHECK)
  - /api/v1/engines  — каталог рушіїв
  - /api/v1/emotions — профілі емоцій
  - /api/v1/documents — завантаження, редагування, нормалізація
  - /api/v1/jobs    — синтез, SSE-прогрес, завантаження
  - /api/v1/preview — швидкий синтез блоку
  - asyncio-воркер запускається разом із процесом
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.db import create_db_and_tables
from app.services.expression.profiles import all_profiles_dict

logger = logging.getLogger("voice_studio")

__version__ = "0.1.0"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.ensure_dirs()

    # Ініціалізуємо БД
    create_db_and_tables()

    # Скидаємо чергу воркера (потрібно для тестів, де кожен TestClient
    # має свій event loop; у production — нешкідливо)
    from app.worker.queue import reset_queue, worker_loop
    reset_queue()

    # Запускаємо фоновий воркер
    worker_task = asyncio.create_task(worker_loop())

    logger.info("Voice Studio %s — data_dir=%s", __version__, settings.data_dir)
    logger.info("TTS-шлюз: %s", settings.tts_base_url)
    yield

    # Зупинка
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass
    logger.info("Зупинка Voice Studio")


app = FastAPI(
    title="Voice Studio",
    description="Локальне озвучення текстів із виразом на відкритих TTS-моделях",
    version=__version__,
    lifespan=lifespan,
)

# ── Підключення роутерів ───────────────────────────────────────────────────────
from app.api.documents import router as documents_router
from app.api.engines import router as engines_router
from app.api.jobs import router as jobs_router
from app.api.preview import router as preview_router

app.include_router(documents_router)
app.include_router(engines_router)
app.include_router(jobs_router)
app.include_router(preview_router)


# ── Базові ендпоінти ───────────────────────────────────────────────────────────

@app.get("/api/v1/health", tags=["service"])
async def health() -> dict[str, object]:
    """Healthcheck. Використовується Docker HEALTHCHECK і фронтендом."""
    settings = get_settings()
    return {
        "status": "ok",
        "version": __version__,
        "data_dir": str(settings.data_dir),
        "data_dir_writable": settings.data_dir.is_dir() and os.access(settings.data_dir, os.W_OK),
        "tts_base_url": settings.tts_base_url,
        "default_engine": settings.default_engine,
    }


@app.get("/api/v1/emotions", tags=["expression"])
async def emotions() -> dict[str, object]:
    """Профілі емоцій шару виразу (ADR-004).

    Дані читаються з app/services/expression/profiles.py — єдина точка правди.
    """
    return {
        "profiles": all_profiles_dict(),
        "intensity_scales_delta_from_neutral": True,
        "note": (
            "Це керована просодія, а не акторська гра моделі. "
            "Паузи й темп рушія — без артефактів; зсув тону типово вимкнено."
        ),
    }
