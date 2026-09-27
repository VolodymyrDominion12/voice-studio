"""API-роутер: швидкий прев'ю синтезу одного блоку.

POST /api/v1/preview — синтез одного текстового фрагмента → WAV у відповідь.
Критично для UX: дозволяє підібрати голос за секунди (ARCHITECTURE.md, розд. 4).
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.config import get_settings
from app.services.expression.profiles import get_prosody
from app.services.tts.base import SynthRequest
from app.services.tts.openai_compat import get_openai_compat_engine

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/preview", tags=["preview"])


class PreviewRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=500)
    voice_id: str = "uk_UA-tetiana-high"
    emotion: str = "neutral"
    intensity: float = Field(default=0.5, ge=0.0, le=1.0)
    engine_id: str = "openai_compat"


@router.post("")
async def preview_synthesis(payload: PreviewRequest):
    """Синтезувати короткий фрагмент і повернути WAV.

    Відповідь — бінарний WAV-файл (Content-Type: audio/wav).
    """
    settings = get_settings()
    engine = get_openai_compat_engine()
    caps = engine.capabilities()

    # Обрізаємо до ліміту рушія
    text = payload.text[: caps.max_chars]

    prosody = get_prosody(payload.emotion, payload.intensity)

    # Унікальний шлях для прев'ю
    import hashlib
    h = hashlib.md5(f"{text}{payload.voice_id}{payload.emotion}".encode()).hexdigest()[:10]
    preview_path = settings.renders_dir / "preview" / f"preview_{h}.wav"
    preview_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        result_path = await asyncio.to_thread(
            engine.synthesize,
            SynthRequest(
                text=text,
                voice_id=payload.voice_id,
                speed=prosody.speed,
                output_path=preview_path,
            ),
        )
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"TTS-шлюз недоступний: {exc}",
        ) from exc

    return FileResponse(
        path=str(result_path),
        media_type="audio/wav",
        filename="preview.wav",
    )
