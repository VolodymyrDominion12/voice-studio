"""API-роутер: рушії та голоси.

GET /api/v1/engines               — каталог із capabilities
GET /api/v1/engines/{id}/voices   — голоси конкретного рушія

Каталог живе в `app/services/engines.py` — спільний із HTML-інтерфейсом.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from app.services import engines as engines_service
from app.services.engines import EngineNotFoundError

router = APIRouter(prefix="/api/v1/engines", tags=["engines"])


@router.get("", response_class=JSONResponse)
async def list_engines():
    """Перелік рушіїв із їхніми capabilities (включно з недоступними)."""
    return JSONResponse({"engines": engines_service.list_engines()})


@router.get("/{engine_id}/voices")
async def get_engine_voices(engine_id: str):
    """Голоси конкретного рушія."""
    try:
        voices = engines_service.voices_of(engine_id)
    except EngineNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return JSONResponse({"engine_id": engine_id, "voices": voices})
