"""Пресети: збережені набори параметрів синтезу.

«Аудіокнига, теплий голос» = рушій + голос + емоція + інтенсивність.
Таблиця `presets` існувала в моделі з самого початку, але API до неї не було
(docs/FRONTEND.md, знахідка 16.6) — тож і скористатися пресетами було нічим.
"""

from __future__ import annotations

import logging

from sqlmodel import Session, select

from app.models import Block, Preset

logger = logging.getLogger(__name__)


class PresetNotFoundError(LookupError):
    """Пресета з таким id немає (API: 404)."""


class PresetNameTakenError(ValueError):
    """Пресет із такою назвою вже є (API: 409)."""


def list_presets(session: Session) -> list[Preset]:
    return list(session.exec(select(Preset).order_by(Preset.name)).all())


def get_preset(session: Session, preset_id: int) -> Preset:
    preset = session.get(Preset, preset_id)
    if not preset:
        raise PresetNotFoundError(f"Пресет {preset_id} не знайдено")
    return preset


def create_preset(
    session: Session,
    *,
    name: str,
    engine_id: str = "",
    voice_id: str = "",
    emotion: str = "neutral",
    intensity: float = 0.5,
    options: dict | None = None,
) -> Preset:
    """Створити пресет. Назва унікальна — інакше їх неможливо розрізнити в списку."""
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("Назва пресета не може бути порожньою")

    existing = session.exec(select(Preset).where(Preset.name == clean_name)).first()
    if existing:
        raise PresetNameTakenError(f"Пресет із назвою {clean_name!r} уже є")

    preset = Preset(
        name=clean_name,
        engine_id=engine_id,
        voice_id=voice_id,
        emotion=emotion,
        intensity=intensity,
        options_json=options or {},
    )
    session.add(preset)
    session.commit()
    session.refresh(preset)
    logger.info("Пресет %s створено: %s", preset.id, clean_name)
    return preset


def update_preset(session: Session, preset_id: int, changes: dict) -> Preset:
    preset = get_preset(session, preset_id)

    new_name = changes.get("name")
    if new_name is not None:
        clean_name = str(new_name).strip()
        if not clean_name:
            raise ValueError("Назва пресета не може бути порожньою")
        duplicate = session.exec(
            select(Preset).where(Preset.name == clean_name, Preset.id != preset_id)
        ).first()
        if duplicate:
            raise PresetNameTakenError(f"Пресет із назвою {clean_name!r} уже є")
        changes["name"] = clean_name

    for key, value in changes.items():
        setattr(preset, key, value)
    session.add(preset)
    session.commit()
    session.refresh(preset)
    return preset


def delete_preset(session: Session, preset_id: int) -> None:
    preset = get_preset(session, preset_id)
    session.delete(preset)
    session.commit()
    logger.info("Пресет %s видалено", preset_id)


def apply_to_blocks(session: Session, document_id: int, preset_id: int) -> int:
    """Застосувати емоцію та інтенсивність пресета до всіх озвучуваних блоків.

    Повертає кількість змінених блоків. Це явна дія користувача, а не побічний
    ефект вибору пресета: мовчки переписувати розмітку всієї книги неприпустимо.
    """
    from app.services.library.blocks import block_effective_text

    preset = get_preset(session, preset_id)
    blocks = session.exec(select(Block).where(Block.document_id == document_id)).all()

    changed = 0
    for block in blocks:
        if not block.speak or not block_effective_text(block).strip():
            continue
        block.emotion = preset.emotion
        block.intensity = preset.intensity
        session.add(block)
        changed += 1

    session.commit()
    logger.info("Пресет %s застосовано до %d блоків документа %s", preset_id, changed, document_id)
    return changed
