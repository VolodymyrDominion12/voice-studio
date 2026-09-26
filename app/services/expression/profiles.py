"""Профілі емоцій шару виразу.

Джерело правди для ProsodyPlan. Значення з docs/ARCHITECTURE.md, розділ 2.4.
Intensity ∈ [0, 1] масштабує відхилення від neutral.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Emotion(StrEnum):
    NEUTRAL      = "neutral"
    WARM         = "warm"
    SERIOUS      = "serious"
    JOYFUL       = "joyful"
    EXCITED      = "excited"
    SAD          = "sad"
    TENSE        = "tense"
    QUESTIONING  = "questioning"


@dataclass
class ProsodyPlan:
    """Набір просодичних параметрів для одного сегмента.

    Порядок застосування — від найменш руйнівного:
    1. engine_hints → параметри рушія (без артефактів)
    2. pause_before_ms / pause_after_ms → тиша при склейці
    3. energy_db → корекція гучності сегмента
    4. pitch_semitones → DSP (типово вимкнено, ADR-004)
    """
    speed: float           = 1.0    # множник темпу (length_scale у Piper)
    pitch_semitones: float = 0.0    # зсув тону; DSP-артефакти — вмикати обережно
    energy_db: float       = 0.0    # +/– dB для цього сегмента
    pause_before_ms: int   = 0
    pause_after_ms: int    = 400
    engine_hints: dict     = field(default_factory=dict)  # length_scale, noise_scale…


# ── Таблиця профілів ───────────────────────────────────────────────────────────
# Значення — відхилення від neutral при intensity=1.0.

_PROFILES: dict[str, ProsodyPlan] = {
    Emotion.NEUTRAL:     ProsodyPlan(speed=1.00, pitch_semitones=0.0,  energy_db=0.0,  pause_after_ms=400),
    Emotion.WARM:        ProsodyPlan(speed=0.96, pitch_semitones=-0.5, energy_db=0.0,  pause_after_ms=500),
    Emotion.SERIOUS:     ProsodyPlan(speed=0.94, pitch_semitones=-1.0, energy_db=-1.0, pause_after_ms=650),
    Emotion.JOYFUL:      ProsodyPlan(speed=1.08, pitch_semitones=1.5,  energy_db=1.0,  pause_after_ms=350),
    Emotion.EXCITED:     ProsodyPlan(speed=1.15, pitch_semitones=2.0,  energy_db=2.0,  pause_after_ms=250),
    Emotion.SAD:         ProsodyPlan(speed=0.88, pitch_semitones=-1.5, energy_db=-2.0, pause_after_ms=900),
    Emotion.TENSE:       ProsodyPlan(speed=1.05, pitch_semitones=1.0,  energy_db=0.0,  pause_after_ms=180),
    Emotion.QUESTIONING: ProsodyPlan(speed=1.02, pitch_semitones=1.0,  energy_db=0.0,  pause_after_ms=450),
}

# Нейтральна просодія — точка відліку для масштабування
_NEUTRAL = _PROFILES[Emotion.NEUTRAL]


def get_prosody(emotion: str, intensity: float = 1.0) -> ProsodyPlan:
    """Повернути ProsodyPlan для заданої емоції та інтенсивності.

    intensity ∈ [0, 1]: 0 = нейтрально, 1 = повне відхилення.
    """
    profile = _PROFILES.get(emotion, _NEUTRAL)
    if intensity == 1.0:
        return profile

    # Масштабуємо відхилення від neutral
    t = max(0.0, min(1.0, intensity))

    def lerp(base: float, target: float) -> float:
        return base + (target - base) * t

    def lerp_int(base: int, target: int) -> int:
        return round(base + (target - base) * t)

    return ProsodyPlan(
        speed=lerp(_NEUTRAL.speed, profile.speed),
        pitch_semitones=lerp(_NEUTRAL.pitch_semitones, profile.pitch_semitones),
        energy_db=lerp(_NEUTRAL.energy_db, profile.energy_db),
        pause_before_ms=lerp_int(_NEUTRAL.pause_before_ms, profile.pause_before_ms),
        pause_after_ms=lerp_int(_NEUTRAL.pause_after_ms, profile.pause_after_ms),
        engine_hints=profile.engine_hints.copy(),
    )


def all_profiles_dict() -> dict[str, dict]:
    """Серіалізована таблиця профілів — для /api/v1/emotions."""
    return {
        name: {
            "speed":         p.speed,
            "pitch":         p.pitch_semitones,
            "energy_db":     p.energy_db,
            "pause_after_ms": p.pause_after_ms,
        }
        for name, p in _PROFILES.items()
    }
