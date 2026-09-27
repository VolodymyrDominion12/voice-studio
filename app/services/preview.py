"""Прев'ю: синтез одного фрагмента для швидкої перевірки голосу й емоції.

Критично для UX: дає змогу підібрати голос за секунди, не запускаючи синтез
цілого документа (ARCHITECTURE §4). Спільне для JSON-API (`/api/v1/preview`)
і для HTML-інтерфейсу (`POST /ui/blocks/{id}/preview`) — щоб обидві поверхні
поводились однаково.

Чесні межі, які поверхня зобовʼязана показати користувачу:
  * текст обрізається до `EngineCapabilities.max_chars` — і про це повертається
    явний прапорець `truncated`, а не мовчазне обрізання (знахідка 16.4);
  * паузи між сегментами тут НЕ застосовуються: вони вставляються під час
    склейки (`services/audio/assemble.py`), а прев'ю — один сегмент.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

from app.config import get_settings
from app.services.expression.profiles import get_prosody
from app.services.tts.base import SynthRequest
from app.services.tts.openai_compat import get_openai_compat_engine

logger = logging.getLogger(__name__)

# Рушій, для якого існує реалізований адаптер (решту — етап 3)
IMPLEMENTED_ENGINE_ID = "openai_compat"


class PreviewUnavailableError(RuntimeError):
    """TTS-шлюз недоступний (API: 503)."""


class PreviewEngineUnsupportedError(RuntimeError):
    """Прев'ю для цього рушія ще не реалізовано (API: 501)."""


@dataclass
class PreviewResult:
    """Результат прев'ю разом із тим, що саме було синтезовано."""
    path: Path
    text: str                 # фактично синтезований текст (може бути обрізаний)
    requested_chars: int      # скільки символів просив користувач
    max_chars: int            # ліміт рушія
    voice_id: str
    emotion: str
    intensity: float
    speed: float
    pause_after_ms: int
    cached: bool = False      # файл уже існував — синтез не виконувався

    @property
    def truncated(self) -> bool:
        return self.requested_chars > self.max_chars

    @property
    def effective_chars(self) -> int:
        return len(self.text)


def _preview_path(text: str, voice_id: str, emotion: str, intensity: float) -> Path:
    """Шлях кешу прев'ю.

    Хеш ураховує все, що впливає на звук: текст, голос, емоцію та
    інтенсивність (інтенсивність масштабує темп, тож без неї в хеші
    зміна повзунка віддавала б старий файл).
    """
    key = f"{text}|{voice_id}|{emotion}|{intensity:.2f}"
    digest = hashlib.md5(key.encode("utf-8")).hexdigest()[:12]
    preview_dir = get_settings().renders_dir / "preview"
    preview_dir.mkdir(parents=True, exist_ok=True)
    return preview_dir / f"preview_{digest}.wav"


def synthesize_preview(
    *,
    text: str,
    voice_id: str,
    emotion: str = "neutral",
    intensity: float = 0.5,
    engine_id: str | None = None,
) -> PreviewResult:
    """Синтезувати короткий фрагмент і повернути шлях до WAV.

    Блокуюча операція — викликати через `asyncio.to_thread` із асинхронного коду.
    """
    if engine_id and engine_id != IMPLEMENTED_ENGINE_ID:
        raise PreviewEngineUnsupportedError(
            f"Прев'ю для рушія {engine_id!r} ще не реалізовано "
            f"(працює лише {IMPLEMENTED_ENGINE_ID!r})."
        )

    engine = get_openai_compat_engine()
    capabilities = engine.capabilities()
    max_chars = capabilities.max_chars

    requested_chars = len(text)
    prepared = text[:max_chars]
    prosody = get_prosody(emotion, float(intensity))

    path = _preview_path(prepared, voice_id, emotion, float(intensity))
    cached = path.exists() and path.stat().st_size > 0

    if not cached:
        try:
            engine.synthesize(
                SynthRequest(
                    text=prepared,
                    voice_id=voice_id,
                    speed=prosody.speed,
                    output_path=path,
                )
            )
        except RuntimeError as exc:
            raise PreviewUnavailableError(str(exc)) from exc

    if requested_chars > max_chars:
        logger.info(
            "Прев'ю обрізано: %d із %d символів (ліміт рушія %s)",
            max_chars, requested_chars, engine.id,
        )

    return PreviewResult(
        path=path,
        text=prepared,
        requested_chars=requested_chars,
        max_chars=max_chars,
        voice_id=voice_id,
        emotion=emotion,
        intensity=float(intensity),
        speed=prosody.speed,
        pause_after_ms=prosody.pause_after_ms,
        cached=cached,
    )


async def synthesize_preview_async(**kwargs) -> PreviewResult:
    """Асинхронна обгортка: синтез не має блокувати event loop FastAPI."""
    return await asyncio.to_thread(synthesize_preview, **kwargs)


def engine_limits(engine_id: str | None = None) -> dict[str, object]:
    """Ліміти рушія для UI — щоб попереджати про обрізання ДО натискання."""
    engine = get_openai_compat_engine()
    capabilities = engine.capabilities()
    return {
        "id": engine.id,
        "max_chars": capabilities.max_chars,
        "emotions": capabilities.emotions,
        "voice_cloning": capabilities.voice_cloning,
        "speed": capabilities.speed,
        "sample_rate": capabilities.sample_rate,
    }
