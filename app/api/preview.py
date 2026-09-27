"""API-роутер: швидкий прев'ю синтезу одного фрагмента.

POST /api/v1/preview — синтез тексту → WAV у відповідь.
Критично для UX: дозволяє підібрати голос за секунди (ARCHITECTURE.md, розд. 4).

Логіка — у `app/services/preview.py` (спільна з HTML-інтерфейсом).
Службові заголовки відповіді:
  * `X-Preview-Chars` — скільки символів було синтезовано;
  * `X-Preview-Requested-Chars` — скільки просили;
  * `X-Preview-Truncated` — `true`, якщо текст обрізано до ліміту рушія;
  * `X-Preview-Cached` — `true`, якщо файл узято з кешу.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.services.preview import (
    PreviewEngineUnsupportedError,
    PreviewUnavailableError,
    synthesize_preview_async,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/preview", tags=["preview"])


class PreviewRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=5000)
    voice_id: str = "uk_UA-tetiana-high"
    emotion: str = "neutral"
    intensity: float = Field(default=0.5, ge=0.0, le=1.0)
    engine_id: str | None = None


@router.post("")
async def preview_synthesis(payload: PreviewRequest):
    """Синтезувати короткий фрагмент і повернути WAV."""
    try:
        result = await synthesize_preview_async(
            text=payload.text,
            voice_id=payload.voice_id,
            emotion=payload.emotion,
            intensity=payload.intensity,
            engine_id=payload.engine_id,
        )
    except PreviewEngineUnsupportedError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    except PreviewUnavailableError as exc:
        raise HTTPException(status_code=503, detail=f"TTS-шлюз недоступний: {exc}") from exc

    return FileResponse(
        path=str(result.path),
        media_type="audio/wav",
        filename="preview.wav",
        headers={
            "X-Preview-Chars": str(result.effective_chars),
            "X-Preview-Requested-Chars": str(result.requested_chars),
            "X-Preview-Truncated": str(result.truncated).lower(),
            "X-Preview-Cached": str(result.cached).lower(),
            "Cache-Control": "no-store",
        },
    )
