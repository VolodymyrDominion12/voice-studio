"""API-роутер: пресети синтезу.

GET    /api/v1/presets          — список
POST   /api/v1/presets          — створити
PATCH  /api/v1/presets/{id}     — змінити
DELETE /api/v1/presets/{id}     — видалити
POST   /api/v1/presets/{id}/apply — застосувати емоцію до всіх блоків документа

Таблиця `presets` була в моделі з початку, але без API (знахідка 16.6).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session

from app.api.errors import to_http_error
from app.db import get_session
from app.models import Preset
from app.services import presets as presets_service
from app.services.library.errors import DocumentNotFoundError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/presets", tags=["presets"])


class PresetCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    engine_id: str = ""
    voice_id: str = ""
    emotion: str = "neutral"
    intensity: float = Field(default=0.5, ge=0.0, le=1.0)
    options: dict = Field(default_factory=dict)


class PresetUpdate(BaseModel):
    name: str | None = None
    engine_id: str | None = None
    voice_id: str | None = None
    emotion: str | None = None
    intensity: float | None = Field(default=None, ge=0.0, le=1.0)
    options: dict | None = None


class PresetRead(BaseModel):
    id: int
    name: str
    engine_id: str
    voice_id: str
    emotion: str
    intensity: float
    options_json: dict


class PresetApply(BaseModel):
    document_id: int


def _read(preset: Preset) -> PresetRead:
    return PresetRead(
        id=preset.id or 0,
        name=preset.name,
        engine_id=preset.engine_id,
        voice_id=preset.voice_id,
        emotion=preset.emotion,
        intensity=preset.intensity,
        options_json=preset.options_json or {},
    )


@router.get("", response_model=list[PresetRead])
def list_presets(session: Session = Depends(get_session)):
    """Усі пресети за назвою."""
    return [_read(preset) for preset in presets_service.list_presets(session)]


@router.post("", response_model=PresetRead, status_code=201)
def create_preset(payload: PresetCreate, session: Session = Depends(get_session)):
    """Створити пресет."""
    try:
        preset = presets_service.create_preset(
            session,
            name=payload.name,
            engine_id=payload.engine_id,
            voice_id=payload.voice_id,
            emotion=payload.emotion,
            intensity=payload.intensity,
            options=payload.options,
        )
    except presets_service.PresetNameTakenError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _read(preset)


@router.patch("/{preset_id}", response_model=PresetRead)
def update_preset(
    preset_id: int, payload: PresetUpdate, session: Session = Depends(get_session)
):
    """Змінити пресет (наприклад, перейменувати)."""
    try:
        preset = presets_service.update_preset(
            session, preset_id, payload.model_dump(exclude_unset=True)
        )
    except presets_service.PresetNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except presets_service.PresetNameTakenError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _read(preset)


@router.delete("/{preset_id}", status_code=204)
def delete_preset(preset_id: int, session: Session = Depends(get_session)):
    """Видалити пресет."""
    try:
        presets_service.delete_preset(session, preset_id)
    except presets_service.PresetNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{preset_id}/apply")
def apply_preset(
    preset_id: int, payload: PresetApply, session: Session = Depends(get_session)
):
    """Застосувати емоцію та інтенсивність пресета до всіх озвучуваних блоків."""
    try:
        changed = presets_service.apply_to_blocks(session, payload.document_id, preset_id)
    except presets_service.PresetNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DocumentNotFoundError as exc:
        raise to_http_error(exc) from exc
    return {"preset_id": preset_id, "document_id": payload.document_id, "blocks_changed": changed}
