"""Склейка аудіо-сегментів і нормалізація гучності.

Базовий цикл без зовнішніх бінарників (ADR-006):
  soundfile — читання/запис WAV
  lameenc   — кодування MP3
  pyloudnorm — EBU R128 нормалізація до цільового LUFS
  numpy     — операції з масивами

Ланцюг:
  WAV-сегменти → ресемплінг → пауза між сегментами → LUFS → MP3
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

# Ці бібліотеки є в базових залежностях (pyproject.toml)
try:
    import soundfile as sf
except ImportError as exc:
    raise ImportError("soundfile не встановлено: uv sync") from exc

try:
    import pyloudnorm as pyln
except ImportError as exc:
    raise ImportError("pyloudnorm не встановлено: uv sync") from exc

try:
    import lameenc
except ImportError as exc:
    raise ImportError("lameenc не встановлено: uv sync") from exc


def _silence(samples: int, channels: int = 1) -> np.ndarray:
    """Масив тиші заданого розміру."""
    return np.zeros((samples, channels) if channels > 1 else (samples,), dtype=np.float32)


def _ms_to_samples(ms: int, sample_rate: int) -> int:
    return int(sample_rate * ms / 1000)


def load_wav(path: Path) -> tuple[np.ndarray, int]:
    """Завантажити WAV і повернути (audio_float32, sample_rate)."""
    audio, sr = sf.read(str(path), dtype="float32", always_2d=False)
    # Конвертувати стерео → моно (усереднення каналів)
    if audio.ndim == 2:
        audio = audio.mean(axis=1)
    return audio, sr


def _resample_if_needed(audio: np.ndarray, src_sr: int, target_sr: int) -> np.ndarray:
    """Проста ресемпляція через numpy (для точних потреб краще resampy/soxr)."""
    if src_sr == target_sr:
        return audio
    ratio = target_sr / src_sr
    target_len = int(len(audio) * ratio)
    return np.interp(
        np.linspace(0, len(audio) - 1, target_len),
        np.arange(len(audio)),
        audio,
    ).astype(np.float32)


def assemble_segments(
    segment_paths: list[Path],
    pause_ms_between: int = 400,
    target_sample_rate: int = 24000,
    target_lufs: float = -18.0,
    pauses_ms: list[int] | None = None,
    energies_db: list[float] | None = None,
) -> np.ndarray:
    """Склеїти WAV-сегменти з паузами і нормалізувати гучність.

    Args:
        segment_paths:       Впорядкований список WAV-файлів.
        pause_ms_between:    Пауза за замовчуванням (мс), якщо немає по-сегментних.
        target_sample_rate:  Цільова частота дискретизації (Гц).
        target_lufs:         Цільова інтегральна гучність (EBU R128).
        pauses_ms:           Пауза ПІСЛЯ кожного сегмента з ProsodyPlan. Це те,
                             що робить «сумно» сумним: у профілю `sad` пауза
                             900 мс проти 250 мс у `excited`. Без цього емоція
                             впливала б лише на темп.
        energies_db:         Корекція гучності кожного сегмента (дБ) за профілем
                             емоції. Застосовується ДО глобальної нормалізації,
                             тож відносна динаміка між сегментами зберігається —
                             саме тому нормалізація тут глобальна, а не
                             по-сегментна (ADR-004).

    Returns:
        Нормалізований float32-масив готового аудіо.
    """
    if not segment_paths:
        raise ValueError("Список сегментів порожній")

    if pauses_ms is not None and len(pauses_ms) != len(segment_paths):
        raise ValueError(
            f"pauses_ms має містити стільки ж значень, скільки сегментів "
            f"({len(pauses_ms)} проти {len(segment_paths)})"
        )
    if energies_db is not None and len(energies_db) != len(segment_paths):
        raise ValueError(
            f"energies_db має містити стільки ж значень, скільки сегментів "
            f"({len(energies_db)} проти {len(segment_paths)})"
        )

    parts: list[np.ndarray] = []
    default_pause_samples = _ms_to_samples(pause_ms_between, target_sample_rate)

    for i, path in enumerate(segment_paths):
        if not path.exists():
            logger.warning("Сегмент не знайдено, пропускаємо: %s", path)
            continue

        audio, sr = load_wav(path)
        audio = _resample_if_needed(audio, sr, target_sample_rate)

        if energies_db is not None and energies_db[i]:
            gain = 10 ** (energies_db[i] / 20)
            audio = (audio * gain).astype(np.float32)

        parts.append(audio)

        if i < len(segment_paths) - 1:
            pause = (
                _ms_to_samples(pauses_ms[i], target_sample_rate)
                if pauses_ms is not None
                else default_pause_samples
            )
            parts.append(_silence(max(0, pause)))

    if not parts:
        raise ValueError("Жоден сегмент не завантажено")

    combined = np.concatenate(parts)
    combined = _normalize_lufs(combined, target_sample_rate, target_lufs)
    return combined


def _normalize_lufs(
    audio: np.ndarray, sample_rate: int, target_lufs: float
) -> np.ndarray:
    """EBU R128 (LUFS) нормалізація через pyloudnorm."""
    meter = pyln.Meter(sample_rate)
    current_lufs = meter.integrated_loudness(audio)

    if not np.isfinite(current_lufs):
        logger.warning("Неможливо виміряти LUFS (тихий/порожній файл), пропускаємо нормалізацію")
        return audio

    gain_db = target_lufs - current_lufs
    # Захист від кліпінгу: обмежимо підсилення
    if gain_db > 30:
        logger.warning("Аномально великий LUFS gain: %.1f dB, обрізаємо до 30 dB", gain_db)
        gain_db = 30.0

    normalized = pyln.normalize.loudness(audio, current_lufs, target_lufs)
    logger.debug(
        "LUFS: %.1f → %.1f (gain %.1f dB)",
        current_lufs, target_lufs, gain_db,
    )
    return normalized.astype(np.float32)


def export_wav(audio: np.ndarray, sample_rate: int, output_path: Path) -> Path:
    """Зберегти float32-масив як WAV PCM 16-bit."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(output_path), audio, sample_rate, subtype="PCM_16")
    logger.info("WAV збережено: %s (%.1f с)", output_path.name, len(audio) / sample_rate)
    return output_path


def export_mp3(
    audio: np.ndarray,
    sample_rate: int,
    output_path: Path,
    bitrate: int = 192,
) -> Path:
    """Кодувати float32-масив у MP3 через lameenc."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # lameenc приймає int16 PCM
    pcm16 = (audio * 32767).clip(-32768, 32767).astype(np.int16)

    encoder = lameenc.Encoder()
    encoder.set_bit_rate(bitrate)
    encoder.set_in_sample_rate(sample_rate)
    encoder.set_channels(1)
    encoder.set_quality(2)  # 2 = близько до найкращої якості

    mp3_data = encoder.encode(pcm16.tobytes())
    mp3_data += encoder.flush()

    output_path.write_bytes(mp3_data)
    logger.info("MP3 збережено: %s (%d KB)", output_path.name, len(mp3_data) // 1024)
    return output_path


def build_audio(
    segment_paths: list[Path],
    output_dir: Path,
    base_name: str,
    pause_ms: int = 400,
    target_sample_rate: int = 24000,
    target_lufs: float = -18.0,
    formats: tuple[str, ...] = ("mp3",),
    pauses_ms: list[int] | None = None,
    energies_db: list[float] | None = None,
) -> dict[str, Path]:
    """Повний цикл збірки: сегменти → нормалізований WAV → MP3.

    `pauses_ms` і `energies_db` — по-сегментні значення з ProsodyPlan; якщо не
    передані, використовується однакова пауза `pause_ms` (поведінка до F4).

    Returns:
        dict: {"mp3": Path, "wav": Path} — ті формати, що запитані.
    """
    audio = assemble_segments(
        segment_paths,
        pause_ms_between=pause_ms,
        target_sample_rate=target_sample_rate,
        target_lufs=target_lufs,
        pauses_ms=pauses_ms,
        energies_db=energies_db,
    )
    results: dict[str, Path] = {}

    if "wav" in formats:
        results["wav"] = export_wav(audio, target_sample_rate, output_dir / f"{base_name}.wav")

    if "mp3" in formats:
        results["mp3"] = export_mp3(audio, target_sample_rate, output_dir / f"{base_name}.mp3")

    return results
