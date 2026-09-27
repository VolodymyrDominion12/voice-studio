"""Voice Studio — точка входу FastAPI.

Реалізовано:
  - healthcheck (використовується Docker HEALTHCHECK)
  - /api/v1/engines  — каталог рушіїв
  - /api/v1/emotions — профілі емоцій
  - /api/v1/documents — завантаження, редагування, нормалізація, видалення
  - /api/v1/jobs    — синтез, SSE-прогрес, завантаження
  - /api/v1/preview — швидкий синтез блоку
  - HTML-інтерфейс (app/ui, ADR-008): робоча стола, документ, система
  - asyncio-воркер запускається разом із процесом
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.documents import router as documents_router
from app.api.engines import router as engines_router
from app.api.jobs import router as jobs_router
from app.api.presets import router as presets_router
from app.api.preview import router as preview_router
from app.config import get_settings
from app.db import create_db_and_tables
from app.services.expression.profiles import all_profiles_dict
from app.services.system import health_snapshot, probe_tts_gateway
from app.ui.router import router as ui_router
from app.version import __version__

logger = logging.getLogger("voice_studio")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.ensure_dirs()

    # Ініціалізуємо БД
    create_db_and_tables()

    # Скидаємо чергу воркера (потрібно для тестів, де кожен TestClient
    # має свій event loop; у production — нешкідливо)
    from app.worker.queue import requeue_incomplete_jobs, reset_queue, worker_loop
    reset_queue()

    # Запускаємо фоновий воркер
    worker_task = asyncio.create_task(worker_loop())

    # Повертаємо в чергу завдання, обірвані попереднім процесом: черга живе
    # лише в памʼяті, тож без цього кроку вони не виконались би ніколи
    # (docs/FRONTEND.md, знахідка 16.2). Помилка тут не має валити старт.
    try:
        requeue_incomplete_jobs()
    except Exception:  # pragma: no cover — захист від несподіванок на старті
        logger.exception("Не вдалося повернути незавершені завдання в чергу")

    logger.info("Voice Studio %s — data_dir=%s", __version__, settings.data_dir)
    logger.info("TTS-шлюз: %s", settings.tts_base_url)
    yield

    # Зупинка
    worker_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await worker_task
    logger.info("Зупинка Voice Studio")


app = FastAPI(
    title="Voice Studio",
    description="Локальне озвучення текстів із виразом на відкритих TTS-моделях",
    version=__version__,
    lifespan=lifespan,
)

app.include_router(documents_router)
app.include_router(engines_router)
app.include_router(jobs_router)
app.include_router(preview_router)
app.include_router(presets_router)
app.include_router(ui_router)

# Статика інтерфейсу (css; htmx/Alpine зʼявляться у фазі F1)
_settings = get_settings()
_settings.static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(_settings.static_dir)), name="static")


# ── Базові ендпоінти ───────────────────────────────────────────────────────────

@app.get("/api/v1/health", tags=["service"])
async def health() -> dict[str, object]:
    """Healthcheck. Використовується Docker HEALTHCHECK і фронтендом.

    Той самий знімок, що й на сторінці «Система» (app/services/system.py),
    щоб JSON-API й інтерфейс не розходились.
    """
    return health_snapshot()


@app.get("/api/v1/health/tts", tags=["service"])
async def health_tts() -> dict[str, object]:
    """Перевірка TTS-шлюзу: живий + чи встановлено сконфігуровану модель.

    Окремий маршрут, бо це мережева перевірка: `/health` має відповідати
    миттєво, а цей — може чекати до таймауту.
    """
    return await asyncio.to_thread(probe_tts_gateway)


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
