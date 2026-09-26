"""TTS-рушій: базовий інтерфейс і типи даних.

Усі конкретні адаптери реалізують TTSEngine (Protocol). Це дозволяє:
- замінювати рушій без змін у решті застосунку (ADR-001);
- шару виразу знати, що брати на себе, а що рушій вміє сам (ADR-004).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass
class EngineCapabilities:
    """Що вміє даний рушій. Шар виразу адаптується відповідно."""
    emotions: bool       = False   # нативна гра емоцій у моделі
    emotion_tags: bool   = False   # підтримка тегів у тексті (<happy>…)
    voice_cloning: bool  = False
    speed: bool          = True    # керування темпом
    streaming: bool      = False   # підтримка потокового синтезу
    max_chars: int       = 400     # максимум символів у запиті
    sample_rate: int     = 22050   # Hz


@dataclass
class Voice:
    id: str
    lang: str = ""
    gender: str = ""    # "f" | "m" | "multi" | ""
    name: str = ""


@dataclass
class SynthRequest:
    """Запит до рушія на синтез одного сегмента."""
    text: str
    voice_id: str       = ""
    speed: float        = 1.0
    pitch_semitones: float = 0.0   # передається лише якщо enable_pitch_shift
    engine_hints: dict  = field(default_factory=dict)  # length_scale, noise_scale…
    output_path: Path | None = None  # якщо None — рушій сам вибирає


@runtime_checkable
class TTSEngine(Protocol):
    """Спільний інтерфейс усіх TTS-адаптерів."""

    id: str

    def capabilities(self) -> EngineCapabilities:
        """Оголосити можливості рушія."""
        ...

    def voices(self) -> list[Voice]:
        """Повернути список доступних голосів."""
        ...

    def synthesize(self, req: SynthRequest) -> Path:
        """Синтезувати текст і повернути шлях до WAV-файлу на диску."""
        ...
