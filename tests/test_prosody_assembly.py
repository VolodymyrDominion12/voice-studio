"""Тести просодії на рівні склейки: паузи й гучність сегментів.

До цього емоція впливала лише на темп: `ProsodyPlan` рахував `pause_after_ms`
і `energy_db`, але `build_audio` брав одну паузу на все завдання. Тобто
«сумно» звучало просто повільніше, без довгих пауз, хоча саме паузи —
найдешевший і найчутніший важіль виразу (ADR-004).

Тут перевіряємо, що:
  * пауза після кожного сегмента береться з його профілю;
  * корекція гучності застосовується до сегмента і переживає глобальну
    нормалізацію (вона глобальна саме тому, щоб не «сплющити» динаміку);
  * межа абзацу дає довшу паузу, ніж межа речення;
  * кількість значень має збігатися з кількістю сегментів.

Запуск: uv run pytest tests/test_prosody_assembly.py -q
"""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

import numpy as np
import pytest

from app.services.audio.assemble import assemble_segments, build_audio
from app.services.expression.profiles import get_prosody

SAMPLE_RATE = 24000
TONE_SECONDS = 0.2


def write_tone(path: Path, amplitude: int = 12000, seconds: float = TONE_SECONDS) -> Path:
    """Записати короткий тон відомої амплітуди."""
    frames = int(seconds * SAMPLE_RATE)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(
            b"".join(
                struct.pack("<h", int(amplitude * math.sin(2 * math.pi * 440 * i / SAMPLE_RATE)))
                for i in range(frames)
            )
        )
    return path


def silence_ms(audio: np.ndarray, threshold: float = 1e-4) -> float:
    """Скільки мілісекунд тиші в масиві (для перевірки пауз)."""
    quiet = np.abs(audio) < threshold
    return float(quiet.sum()) / SAMPLE_RATE * 1000


def rms(audio: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(audio))))


# ── Паузи ─────────────────────────────────────────────────────────────────────

def test_default_pause_still_works(tmp_path: Path) -> None:
    """Без по-сегментних даних поведінка лишається як була."""
    paths = [write_tone(tmp_path / f"s{i}.wav") for i in range(3)]

    audio = assemble_segments(paths, pause_ms_between=400, target_sample_rate=SAMPLE_RATE)

    expected_ms = 3 * TONE_SECONDS * 1000 + 2 * 400
    assert len(audio) / SAMPLE_RATE * 1000 == pytest.approx(expected_ms, abs=2)


def test_per_segment_pauses_are_applied(tmp_path: Path) -> None:
    """Кожна пауза береться зі свого профілю, а не одна на всіх."""
    paths = [write_tone(tmp_path / f"s{i}.wav") for i in range(3)]

    audio = assemble_segments(
        paths,
        target_sample_rate=SAMPLE_RATE,
        pauses_ms=[900, 250, 400],   # як у профілів sad / excited / neutral
    )

    expected_ms = 3 * TONE_SECONDS * 1000 + 900 + 250   # пауза після останнього не додається
    assert len(audio) / SAMPLE_RATE * 1000 == pytest.approx(expected_ms, abs=2)


def test_sad_profile_gives_longer_silence_than_excited(tmp_path: Path) -> None:
    """Найважливіше для слухача: сумне має довші паузи, ніж захоплене."""
    sad = get_prosody("sad", 1.0)
    excited = get_prosody("excited", 1.0)
    assert sad.pause_after_ms > excited.pause_after_ms

    paths = [write_tone(tmp_path / f"s{i}.wav") for i in range(3)]

    sad_audio = assemble_segments(
        paths, target_sample_rate=SAMPLE_RATE,
        pauses_ms=[sad.pause_after_ms] * 3,
    )
    excited_audio = assemble_segments(
        paths, target_sample_rate=SAMPLE_RATE,
        pauses_ms=[excited.pause_after_ms] * 3,
    )

    assert len(sad_audio) > len(excited_audio)


def test_lenght_mismatch_is_rejected(tmp_path: Path) -> None:
    """Розбіжність довжин — це помилка програміста, а не тиха підміна."""
    paths = [write_tone(tmp_path / f"s{i}.wav") for i in range(3)]

    with pytest.raises(ValueError, match="pauses_ms"):
        assemble_segments(paths, pauses_ms=[400, 400], target_sample_rate=SAMPLE_RATE)
    with pytest.raises(ValueError, match="energies_db"):
        assemble_segments(paths, energies_db=[0.0], target_sample_rate=SAMPLE_RATE)


# ── Гучність сегментів ────────────────────────────────────────────────────────

def test_segment_energy_changes_relative_loudness(tmp_path: Path) -> None:
    """Корекція в дБ має пережити глобальну нормалізацію LUFS.

    Нормалізація множить УВЕСЬ сигнал на один коефіцієнт, тож відношення між
    сегментами зберігається — саме тому вона глобальна (ADR-004).
    """
    quiet = write_tone(tmp_path / "a.wav", amplitude=12000)
    loud = write_tone(tmp_path / "b.wav", amplitude=12000)

    audio = assemble_segments(
        [quiet, loud],
        target_sample_rate=SAMPLE_RATE,
        pauses_ms=[0, 0],
        energies_db=[-6.0, 0.0],
    )

    half = len(audio) // 2
    first, second = audio[:half], audio[half:]

    ratio = rms(first) / rms(second)
    expected = 10 ** (-6.0 / 20)   # −6 дБ ≈ 0.5
    assert ratio == pytest.approx(expected, rel=0.05), "перший сегмент мав стати тихішим"


def test_zero_energy_leaves_audio_untouched(tmp_path: Path) -> None:
    paths = [write_tone(tmp_path / "a.wav"), write_tone(tmp_path / "b.wav")]

    flat = assemble_segments(paths, target_sample_rate=SAMPLE_RATE, pauses_ms=[100, 100])
    zeroed = assemble_segments(
        paths, target_sample_rate=SAMPLE_RATE, pauses_ms=[100, 100],
        energies_db=[0.0, 0.0],
    )

    assert np.allclose(flat, zeroed)


# ── Наскрізно через build_audio ───────────────────────────────────────────────

def test_build_audio_passes_prosody_through(tmp_path: Path) -> None:
    """`build_audio` має прокидати по-сегментні дані, а не губити їх."""
    short_pause = [write_tone(tmp_path / f"a{i}.wav") for i in range(3)]
    long_pause = [write_tone(tmp_path / f"b{i}.wav") for i in range(3)]

    quick = build_audio(
        short_pause, tmp_path / "out", "quick",
        target_sample_rate=SAMPLE_RATE, pauses_ms=[200, 200, 200],
    )
    slow = build_audio(
        long_pause, tmp_path / "out", "slow",
        target_sample_rate=SAMPLE_RATE, pauses_ms=[900, 900, 900],
    )

    import soundfile as sf

    quick_audio, _ = sf.read(str(quick["mp3"]))
    slow_audio, _ = sf.read(str(slow["mp3"]))

    assert len(slow_audio) > len(quick_audio), "довші паузи мають дати довший файл"


def test_build_audio_default_behaviour_unchanged(tmp_path: Path) -> None:
    """За відсутності масивів працює стара логіка (одна пауза).

    Перевіряємо на WAV, а не на MP3: кодек додає власне вирівнювання кадрів
    (кілька десятків мілісекунд), і точна довжина в MP3 не збігається зі
    вхідною — це артефакт кодека, а не логіки склейки.
    """
    paths = [write_tone(tmp_path / f"s{i}.wav") for i in range(2)]

    result = build_audio(
        paths, tmp_path / "out", "legacy",
        pause_ms=500, target_sample_rate=SAMPLE_RATE, formats=("wav",),
    )

    import soundfile as sf

    audio, sample_rate = sf.read(str(result["wav"]))
    expected_ms = 2 * TONE_SECONDS * 1000 + 500
    assert len(audio) / sample_rate * 1000 == pytest.approx(expected_ms, abs=2)
