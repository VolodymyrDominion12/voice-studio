"""Склейка аудіо-сегментів, нормалізація гучності та експорт.

Базовий цикл без зовнішніх бінарників (ADR-006):
  soundfile  — читання/запис WAV
  lameenc    — кодування MP3
  pyloudnorm — EBU R128 нормалізація до цільового LUFS
  numpy      — операції з масивами
  mutagen    — ID3-теги MP3
  PyAV       — M4B/AAC із розділами (ADR-013). Це Python-колесо з
               вбудованими бібліотеками FFmpeg, а не виклик `ffmpeg`:
               зовнішнього бінарника базовий цикл так і не потребує.

Ланцюг:
  WAV-сегменти → ресемплінг → пауза між сегментами → LUFS → MP3/WAV/M4B → zip
"""

from __future__ import annotations

import logging
import tempfile
import zipfile
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

# Формати, у які реально вміє кодувати цей модуль. «zip» — не формат звуку, а
# упаковка: архів із файлами-розділами (PLAN, етап 4 «експорт у zip по розділах»).
VALID_FORMATS = ("mp3", "wav", "m4b")
VALID_OUTPUTS = (*VALID_FORMATS, "zip")

# Порядок, у якому обирається «головний» файл завдання, якщо запитано кілька
# форматів: MP3 — найсумісніший, M4B — для аудіокниг, WAV — найбільший.
_PRIMARY_ORDER = ("mp3", "m4b", "wav", "zip")

# Ці бібліотеки є в базових залежностях (pyproject.toml)
try:
    import soundfile as sf
except ImportError as exc:  # pragma: no cover - залежність базового набору
    raise ImportError("soundfile не встановлено: uv sync") from exc

try:
    import pyloudnorm as pyln
except ImportError as exc:  # pragma: no cover
    raise ImportError("pyloudnorm не встановлено: uv sync") from exc

try:
    import lameenc
except ImportError as exc:  # pragma: no cover
    raise ImportError("lameenc не встановлено: uv sync") from exc

try:
    from mutagen.id3 import (
        COMM,
        ID3,
        TALB,
        TCON,
        TDRC,
        TIT2,
        TPE1,
        TRCK,
        ID3NoHeaderError,
    )
except ImportError as exc:  # pragma: no cover
    raise ImportError("mutagen не встановлено: uv sync") from exc


@dataclass(frozen=True)
class SegmentTiming:
    """Де в готовому аудіо починається й закінчується сегмент (мс)."""

    index: int
    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class ChapterSpan:
    """Розділ у термінах сегментів — те, що відомо воркеру після синтезу.

    Межі віддаються індексами (включно), а не секундами, бо секунди залежать
    від тривалостей WAV-ів і від пауз; рахує їх `build_audio`, де ці дані вже є.
    """

    title: str
    first_segment: int
    last_segment: int


@dataclass(frozen=True)
class ChapterSlice:
    """Розділ у готовому аудіо: назва + межі в мілісекундах.

    `name` — ім'я файлу для експорту в zip. Його рахує шар бібліотеки
    (`library.chapters.chapter_file_name`), бо правило іменування — справа
    документа, а не аудіо; тут воно лише переноситься до архіву.
    """

    title: str
    start_ms: int
    end_ms: int
    name: str = ""

    @property
    def duration_ms(self) -> int:
        return max(0, self.end_ms - self.start_ms)


def normalize_formats(value: object, default: tuple[str, ...] = ("mp3",)) -> tuple[str, ...]:
    """Привести запит форматів до відомого набору.

    Приймає список, кортеж або рядок через кому (щоб те саме значення
    працювало і з `.env`, і з JSON-API). Невідомий формат — помилка, а не
    мовчазне ігнорування: інакше користувач чекав би M4B, а отримав MP3.
    """
    if value is None or value == "":
        return default
    if isinstance(value, str):
        items = [part for part in value.replace(";", ",").split(",")]
    elif isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
    else:
        raise ValueError(f"Не розумію список форматів: {value!r}")

    cleaned: list[str] = []
    for item in items:
        name = str(item).strip().lower().lstrip(".")
        if not name:
            continue
        if name not in VALID_OUTPUTS:
            raise ValueError(
                f"Невідомий формат {name!r}. Доступні: {', '.join(VALID_OUTPUTS)}"
            )
        if name not in cleaned:
            cleaned.append(name)

    return tuple(cleaned) or default


def primary_format(formats: tuple[str, ...] | list[str]) -> str:
    """Який із запитаних форматів стане головним файлом завдання."""
    for name in _PRIMARY_ORDER:
        if name in formats:
            return name
    return formats[0] if formats else "mp3"


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


def _validate_series(values: list | None, count: int, name: str) -> None:
    if values is not None and len(values) != count:
        raise ValueError(
            f"{name} має містити стільки ж значень, скільки сегментів "
            f"({len(values)} проти {count})"
        )


def _concat_with_timeline(
    segment_paths: list[Path],
    pause_ms_between: int,
    target_sample_rate: int,
    pauses_ms: list[int] | None,
    energies_db: list[float] | None,
) -> tuple[np.ndarray, list[SegmentTiming]]:
    """Склеїти сегменти й повернути масив разом із таймлайном.

    Таймлайн — це те, чого бракувало для розділів: щоб порізати готове аудіо на
    розділи (і розставити мітки в M4B), треба знати, з якої мілісекунди
    починається кожен сегмент. Рахуємо це ТУТ, бо саме тут відомі паузи, тож
    межі розділів не можуть розійтися з реальним звуком.
    """
    if not segment_paths:
        raise ValueError("Список сегментів порожній")

    _validate_series(pauses_ms, len(segment_paths), "pauses_ms")
    _validate_series(energies_db, len(segment_paths), "energies_db")

    parts: list[np.ndarray] = []
    timings: list[SegmentTiming] = []
    default_pause_samples = _ms_to_samples(pause_ms_between, target_sample_rate)
    cursor = 0  # позиція в семплах

    for i, path in enumerate(segment_paths):
        if not path.exists():
            logger.warning("Сегмент не знайдено, пропускаємо: %s", path)
            continue

        audio, sr = load_wav(path)
        audio = _resample_if_needed(audio, sr, target_sample_rate)

        if energies_db is not None and energies_db[i]:
            gain = 10 ** (energies_db[i] / 20)
            audio = (audio * gain).astype(np.float32)

        start_samples = cursor
        timings.append(
            SegmentTiming(
                index=i,
                start_ms=round(start_samples / target_sample_rate * 1000),
                end_ms=round((start_samples + len(audio)) / target_sample_rate * 1000),
            )
        )

        parts.append(audio)
        cursor += len(audio)

        if i < len(segment_paths) - 1:
            pause = (
                _ms_to_samples(pauses_ms[i], target_sample_rate)
                if pauses_ms is not None
                else default_pause_samples
            )
            pause = max(0, pause)
            parts.append(_silence(pause))
            cursor += pause

    if not parts:
        raise ValueError("Жоден сегмент не завантажено")

    return np.concatenate(parts), timings


def assemble_with_timeline(
    segment_paths: list[Path],
    pause_ms_between: int = 400,
    target_sample_rate: int = 24000,
    target_lufs: float = -18.0,
    pauses_ms: list[int] | None = None,
    energies_db: list[float] | None = None,
) -> tuple[np.ndarray, list[SegmentTiming]]:
    """Як `assemble_segments`, але ще й повертає таймлайн сегментів."""
    audio, timings = _concat_with_timeline(
        segment_paths,
        pause_ms_between,
        target_sample_rate,
        pauses_ms,
        energies_db,
    )
    return _normalize_lufs(audio, target_sample_rate, target_lufs), timings


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
    audio, _ = assemble_with_timeline(
        segment_paths,
        pause_ms_between=pause_ms_between,
        target_sample_rate=target_sample_rate,
        target_lufs=target_lufs,
        pauses_ms=pauses_ms,
        energies_db=energies_db,
    )
    return audio


def chapter_slices(
    spans: list[ChapterSpan],
    timings: list[SegmentTiming],
    names: dict[str, str] | None = None,
) -> list[ChapterSlice]:
    """Межі розділів у мілісекундах із таймлайну сегментів.

    Якщо файл сегмента зник із диска, відповідного таймлайну немає (він
    пропускається при склейці). Тоді межу беремо з найближчого наявного
    сегмента — розділ не зникає й не «з'їдає» сусіда, лишається неточність у
    кілька десятих секунди.

    `names` — назви файлів для zip: «заголовок розділу → ім'я».
    """
    by_index = {timing.index: timing for timing in timings}
    if not by_index:
        return []

    available = sorted(by_index)
    slices: list[ChapterSlice] = []

    def _at_or_after(index: int) -> SegmentTiming | None:
        for candidate in available:
            if candidate >= index:
                return by_index[candidate]
        return None

    def _at_or_before(index: int) -> SegmentTiming | None:
        for candidate in reversed(available):
            if candidate <= index:
                return by_index[candidate]
        return None

    for span in spans:
        start = _at_or_after(span.first_segment)
        end = _at_or_before(span.last_segment)
        if start is None or end is None:
            logger.warning("Розділ %r не має сегментів — пропускаємо", span.title)
            continue
        slices.append(
            ChapterSlice(
                title=span.title,
                start_ms=start.start_ms,
                end_ms=end.end_ms,
                name=(names or {}).get(span.title, ""),
            )
        )

    return slices


def _normalize_lufs(
    audio: np.ndarray, sample_rate: int, target_lufs: float
) -> np.ndarray:
    """EBU R128 (LUFS) нормалізація через pyloudnorm.

    pyloudnorm відмовляється міряти матеріал, коротший за вікно вимірювання
    (0.4 с), — а це не екзотика, а звичайний короткий блок («Так.») на початку
    документа. Доти такий документ валив завдання на етапі збірки з
    `ValueError: Audio must have length greater than the block size`. Коротке
    аудіо просто лишаємо як є: нормалізувати в ньому нічого, а ламати
    завдання через це — тим більше.
    """
    meter = pyln.Meter(sample_rate)
    try:
        current_lufs = meter.integrated_loudness(audio)
    except ValueError as exc:
        logger.info("Аудіо закоротке для вимірювання LUFS (%s) — лишаємо без нормалізації", exc)
        return audio

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


def _pcm16(audio: np.ndarray) -> bytes:
    """float32 → int16 PCM (формат, який приймає lameenc)."""
    return (audio * 32767).clip(-32768, 32767).astype(np.int16).tobytes()


def _encode_mp3_bytes(audio: np.ndarray, sample_rate: int, bitrate: int) -> bytes:
    encoder = lameenc.Encoder()
    encoder.set_bit_rate(bitrate)
    encoder.set_in_sample_rate(sample_rate)
    encoder.set_channels(1)
    encoder.set_quality(2)  # 2 = близько до найкращої якості
    data = encoder.encode(_pcm16(audio))
    data += encoder.flush()
    return bytes(data)


def write_mp3_tags(path: Path, tags: dict[str, str]) -> None:
    """Записати ID3v2-теги у готовий MP3 (PLAN, етап 2: «ID3-теги через mutagen»).

    Без цього аудіокнига в плейері — це «Невідомий виконавець / Невідомий
    альбом» і файл без назви, навіть якщо на диску він названий правильно:
    ім'я файлу плейери майже не читають, читають теги.
    """
    try:
        audio = ID3(str(path))
    except ID3NoHeaderError:
        audio = ID3()

    if title := tags.get("title"):
        audio.add(TIT2(encoding=3, text=title))
    if album := tags.get("album"):
        audio.add(TALB(encoding=3, text=album))
    if artist := tags.get("artist"):
        audio.add(TPE1(encoding=3, text=artist))
    if genre := tags.get("genre"):
        audio.add(TCON(encoding=3, text=genre))
    if date := tags.get("date"):
        audio.add(TDRC(encoding=3, text=date))
    if track := tags.get("track"):
        audio.add(TRCK(encoding=3, text=track))
    if comment := tags.get("comment"):
        audio.add(COMM(encoding=3, lang="ukr", desc="", text=comment))

    audio.save(str(path), v2_version=3)


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
    tags: dict[str, str] | None = None,
) -> Path:
    """Кодувати float32-масив у MP3 через lameenc (+ ID3-теги, якщо задані)."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    mp3_data = _encode_mp3_bytes(audio, sample_rate, bitrate)
    output_path.write_bytes(mp3_data)

    if tags:
        try:
            write_mp3_tags(output_path, tags)
        except Exception as exc:  # теги — не привід втратити аудіо
            logger.warning("Не вдалося записати ID3-теги у %s: %s", output_path.name, exc)

    logger.info("MP3 збережено: %s (%d KB)", output_path.name, output_path.stat().st_size // 1024)
    return output_path


# Розмір шматка при кодуванні AAC: тримаємо пам'ять сталою навіть для
# багатогодинної книги (цілий масив одним кадром — це сотні мегабайт копії).
_M4B_CHUNK_SECONDS = 5

# Ключі тегів → те, що розуміє muxer MP4 (ffmpeg сам перекладе їх в ©nam тощо).
_M4B_METADATA_KEYS = {
    "title": "title",
    "album": "album",
    "artist": "artist",
    "album_artist": "album_artist",
    "genre": "genre",
    "date": "date",
    "comment": "comment",
}


def export_m4b(
    audio: np.ndarray,
    sample_rate: int,
    output_path: Path,
    chapters: list[ChapterSlice] | None = None,
    tags: dict[str, str] | None = None,
    bitrate: int = 64000,
) -> Path:
    """Зберегти аудіо як M4B (AAC у MP4) з розділами й метаданими.

    M4B — формат аудіокниг: у ньому живуть **розділи**, за якими плейер
    (і Apple Books, і VLC, і будь-який інший) дає переходити між частинами
    книги. Без розділів M4B — просто AAC-файл, тож розділи тут не «бонус»,
    а сенс формату.

    Кодує PyAV — Python-колесо зі вбудованими бібліотеками FFmpeg (ADR-013),
    тому системний `ffmpeg` не потрібен. Доти M4B був заблокований саме ним:
    у системі є лише snap-версія, яку PLAN називає непридатною.
    """
    try:
        import av
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "PyAV (пакет av) не встановлено — M4B недоступний. "
            "Встановіть: uv sync (av є в базових залежностях)"
        ) from exc

    output_path.parent.mkdir(parents=True, exist_ok=True)
    chapters = [chapter for chapter in (chapters or []) if chapter.duration_ms > 0]

    container = av.open(str(output_path), mode="w", format="ipod")
    try:
        stream = container.add_stream("aac", rate=sample_rate)
        stream.layout = "mono"
        stream.bit_rate = bitrate

        for key, value in (tags or {}).items():
            if value:
                container.metadata[_M4B_METADATA_KEYS.get(key, key)] = value

        if chapters:
            container.set_chapters([
                {
                    "id": index,
                    "start": chapter.start_ms,
                    "end": chapter.end_ms,
                    "time_base": Fraction(1, 1000),
                    "metadata": {"title": chapter.title},
                }
                for index, chapter in enumerate(chapters)
            ])

        pcm = (audio * 32767).clip(-32768, 32767).astype(np.int16)
        chunk_samples = max(1, _M4B_CHUNK_SECONDS * sample_rate)

        for offset in range(0, len(pcm), chunk_samples):
            chunk = pcm[offset : offset + chunk_samples]
            frame = av.AudioFrame.from_ndarray(
                chunk.reshape(1, -1), format="s16", layout="mono"
            )
            frame.sample_rate = sample_rate
            frame.pts = offset
            for packet in stream.encode(frame):
                container.mux(packet)

        for packet in stream.encode(None):
            container.mux(packet)
    finally:
        container.close()

    logger.info(
        "M4B збережено: %s (%d KB, розділів: %d)",
        output_path.name, output_path.stat().st_size // 1024, len(chapters),
    )
    return output_path


def export_chapter_zip(
    audio: np.ndarray,
    sample_rate: int,
    output_path: Path,
    chapters: list[ChapterSlice],
    tags: dict[str, str] | None = None,
    bitrate: int = 192,
) -> Path:
    """Запакувати книгу в zip — окремий MP3 на кожен розділ.

    Навіщо, якщо вже є M4B: частина слухачів і пристроїв (старі плеєри,
    програвачі в машині, аудіо-конвертери) не читають розділи з контейнера й
    хочуть окремі файли. Порізка йде з **уже зведеного** аудіо, тому рівень
    гучності між розділами однаковий, а синтез не повторюється.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    base_tags = dict(tags or {})
    written = 0

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for index, chapter in enumerate(chapters, start=1):
                start = _ms_to_samples(chapter.start_ms, sample_rate)
                end = _ms_to_samples(chapter.end_ms, sample_rate)
                piece = audio[start:end]
                if piece.size == 0:
                    logger.warning("Розділ %r порожній — пропускаємо", chapter.title)
                    continue

                member = chapter.name or _fallback_chapter_name(chapter, index)
                chapter_tags = dict(base_tags)
                chapter_tags["title"] = chapter.title
                chapter_tags["track"] = f"{index}/{len(chapters)}"

                # MP3 кодуємо у тимчасовий файл, бо ID3 пише mutagen уже в
                # готовий файл; в архів кладемо вже з тегами.
                tmp_path = tmp_dir / f"{index:04d}.mp3"
                export_mp3(
                    piece, sample_rate, tmp_path, bitrate=bitrate, tags=chapter_tags
                )
                archive.write(tmp_path, arcname=member)
                written += 1

    logger.info(
        "ZIP зібрано: %s (%d KB, розділів: %d)",
        output_path.name, output_path.stat().st_size // 1024, written,
    )
    return output_path


def _fallback_chapter_name(chapter: ChapterSlice, index: int) -> str:
    """Ім'я файлу в архіві, якщо шар бібліотеки його не дав."""
    safe = "".join(ch if ch.isalnum() or ch in " -_" else "" for ch in chapter.title)
    safe = safe.strip().replace(" ", "-")[:60] or "rozdil"
    return f"{index:02d}-{safe}.mp3"


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
    chapters: list[ChapterSpan] | None = None,
    tags: dict[str, str] | None = None,
    m4b_bitrate: int = 64000,
    mp3_bitrate: int = 192,
    chapter_names: dict[str, str] | None = None,
) -> dict[str, Path]:
    """Повний цикл збірки: сегменти → нормалізований WAV → MP3/M4B/zip.

    `pauses_ms` і `energies_db` — по-сегментні значення з ProsodyPlan; якщо не
    передані, використовується однакова пауза `pause_ms` (поведінка до F4).

    `chapters` — розділи в термінах сегментів (`ChapterSpan`); у M4B вони
    стають мітками розділів, а для формату `zip` — окремими файлами.

    Returns:
        dict: {"mp3": Path, "wav": Path, "m4b": Path, "zip": Path} — лише ті
        формати, що запитані й успішно створені.
    """
    requested = normalize_formats(formats)
    audio, timings = assemble_with_timeline(
        segment_paths,
        pause_ms_between=pause_ms,
        target_sample_rate=target_sample_rate,
        target_lufs=target_lufs,
        pauses_ms=pauses_ms,
        energies_db=energies_db,
    )

    slices = chapter_slices(chapters or [], timings, names=chapter_names)
    if chapters and not slices:
        logger.warning("Розділи задані, але жоден не має сегментів — межі не ставимо")

    results: dict[str, Path] = {}

    if "wav" in requested:
        results["wav"] = export_wav(
            audio, target_sample_rate, output_dir / f"{base_name}.wav"
        )

    if "mp3" in requested:
        results["mp3"] = export_mp3(
            audio,
            target_sample_rate,
            output_dir / f"{base_name}.mp3",
            bitrate=mp3_bitrate,
            tags=tags,
        )

    if "m4b" in requested:
        results["m4b"] = export_m4b(
            audio,
            target_sample_rate,
            output_dir / f"{base_name}.m4b",
            chapters=slices,
            tags=tags,
            bitrate=m4b_bitrate,
        )

    if "zip" in requested:
        results["zip"] = export_chapter_zip(
            audio,
            target_sample_rate,
            output_dir / f"{base_name}.zip",
            chapters=slices,
            tags=tags,
            bitrate=mp3_bitrate,
        )

    return results
