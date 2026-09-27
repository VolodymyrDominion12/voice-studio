"""API-роутер: рушії та голоси.

GET /api/v1/engines               — каталог із capabilities
GET /api/v1/engines/{id}/voices   — голоси конкретного рушія
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from app.services.tts.openai_compat import get_openai_compat_engine

router = APIRouter(prefix="/api/v1/engines", tags=["engines"])

# Реєстр усіх відомих рушіїв (статичний + активні адаптери)
# available=True → адаптер реалізовано; available=False → тільки в каталозі
_ENGINE_CATALOGUE: list[dict] = [
    {
        "id": "openai_compat",
        "label": "OpenAI-сумісний шлюз (Speaches)",
        "available": True,   # адаптер реалізовано
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
            {"id": "uk_UA-tetiana-high",        "lang": "uk_UA", "gender": "f"},
            {"id": "uk_UA-mykyta-high",          "lang": "uk_UA", "gender": "m"},
            {"id": "uk_UA-oleksa-high",          "lang": "uk_UA", "gender": "m"},
            {"id": "uk_UA-lada-x_low",           "lang": "uk_UA", "gender": "f"},
            {"id": "uk_UA-ukrainian_tts-medium", "lang": "uk_UA", "gender": "multi"},
        ],
    },
    {
        "id": "ukrainian_tts",
        "label": "ukrainian-tts (ESPNet, автоматичний наголос)",
        "available": False,  # адаптер планується в Етапі 3
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

_ENGINE_INDEX: dict[str, dict] = {e["id"]: e for e in _ENGINE_CATALOGUE}


@router.get("", response_class=JSONResponse)
async def list_engines():
    """Перелік доступних рушіїв із їхніми capabilities."""
    return JSONResponse({"engines": _ENGINE_CATALOGUE})


@router.get("/{engine_id}/voices")
async def get_engine_voices(engine_id: str):
    """Голоси конкретного рушія."""
    entry = _ENGINE_INDEX.get(engine_id)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Рушій {engine_id!r} не знайдено")

    # Для активних рушіїв — питаємо адаптер (може бути актуальніше ніж каталог)
    if engine_id == "openai_compat" and entry["available"]:
        engine = get_openai_compat_engine()
        voices = [
            {"id": v.id, "lang": v.lang, "gender": v.gender, "name": v.name}
            for v in engine.voices()
        ]
        return JSONResponse({"engine_id": engine_id, "voices": voices})

    return JSONResponse({"engine_id": engine_id, "voices": entry["voices"]})
