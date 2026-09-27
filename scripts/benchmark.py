#!/usr/bin/env python3
"""Бенчмарк українських TTS-рушіїв на цій машині.

Мета — замінити оцінки в docs/RESEARCH.md реальними числами (real-time factor,
пікова RAM, розмір виходу). Див. PLAN.md, «Що виміряти, а не вгадати».

Запуск:
    uv run python scripts/benchmark.py                 # усі доступні рушії
    uv run python scripts/benchmark.py --engine openai-compat --voice uk_UA-lada-x_low
    uv run python scripts/benchmark.py --pipeline      # весь конвеєр, а не лише синтез
    uv run python scripts/benchmark.py --repeat 3

Скрипт нічого не встановлює. Якщо рушій недоступний — пише про це й іде далі.

ВАЖЛИВО: налаштування беруться з `app.config.get_settings()`, тобто з `.env`,
а не з оточення процесу. Раніше тут читався `os.environ`, і `.env` із
`TTS_MODEL=…` не діяв — скрипт ішов у шлюз із `tts-1` і діставав 404 від
Speaches (той самий випадок, що описаний у README §12).
"""

from __future__ import annotations

import argparse
import json
import resource
import statistics
import sys
import time
import wave
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "benchmark"

# Щоб `import app…` працював при запуску скриптом із будь-якого каталогу
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Тестовий текст: навмисно містить те, на чому TTS зазвичай спотикається —
# числа, скорочення, латиницю, довге речення, питальну інтонацію,
# слова з наголосами, які часто плутають.
SAMPLE_TEXT = (
    "У 2024 році компанія IT-сектору збільшила дохід на 15 відсотків. "
    "Це становить близько 2,5 мільйона гривень, тобто №1 показник за останні 5 років. "
    "Але чи справді це так? Замок на горі — не те саме, що замок на дверях. "
    "Дослідження показало: швидкість обробки зросла втричі, а витрати впали вдвічі. "
    "Ми мусимо визнати, що подальше зростання вимагає нових інвестицій."
)


@dataclass
class Result:
    engine: str
    voice: str
    ok: bool
    audio_seconds: float = 0.0
    wall_seconds: float = 0.0
    rtf: float = 0.0  # real-time factor: <1 = швидше за реальний час
    peak_rss_mb: float = 0.0
    bytes_out: int = 0
    error: str = ""
    notes: list[str] = field(default_factory=list)
    # Додано для виміру всього конвеєра (--pipeline)
    stage: str = "synthesis"
    segments: int = 0
    synth_seconds: float = 0.0
    assemble_seconds: float = 0.0


def peak_rss_mb() -> float:
    """Пікове споживання RAM поточним процесом, у МБ."""
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux віддає кілобайти, macOS — байти.
    return usage / 1024 if sys.platform != "darwin" else usage / (1024 * 1024)


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


# ── Рушії ────────────────────────────────────────────────────────────────
# Кожен повертає (шлях_до_wav, нотатки) або кидає виняток.


def bench_piper(voice: str, out: Path) -> tuple[Path, list[str]]:
    """Piper напряму через piper-tts. Голос — напр. uk_UA-tetiana-high."""
    try:
        from piper import PiperVoice  # piper-tts >= 1.2
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("piper-tts не встановлено (uv sync --extra piper)") from exc

    model_dir = ROOT / "data" / "models" / "piper"
    onnx = model_dir / f"{voice}.onnx"
    if not onnx.exists():
        raise RuntimeError(
            f"немає моделі {onnx}. Завантаж: "
            f"python -m piper.download_voices {voice} --data-dir {model_dir}"
        )

    import wave as _wave

    t0 = time.perf_counter()
    v = PiperVoice.load(onnx)
    load_s = time.perf_counter() - t0

    t1 = time.perf_counter()
    with _wave.open(str(out), "wb") as wf:
        v.synthesize_wav(SAMPLE_TEXT, wf)
    synth_s = time.perf_counter() - t1

    return out, [f"завантаження моделі: {load_s:.2f} с", f"синтез: {synth_s:.2f} с"]


def bench_ukrainian_tts(voice: str, out: Path) -> tuple[Path, list[str]]:
    """robinhad/ukrainian-tts — MIT, з автоматичним наголосом."""
    try:
        from ukrainian_tts.tts import TTS, Stress, Voices
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("ukrainian-tts не встановлено (uv sync --extra uk)") from exc

    # Голоси: tetiana, mykyta, lada, dmytro, oleksa
    try:
        v = Voices(voice)
    except ValueError as exc:
        raise RuntimeError(
            f"невідомий голос {voice!r}; доступні: "
            f"{[x.value for x in Voices]}"
        ) from exc

    t0 = time.perf_counter()
    tts = TTS(device="cpu")
    load_s = time.perf_counter() - t0

    t1 = time.perf_counter()
    with open(out, "wb") as f:
        _, accented = tts.tts(SAMPLE_TEXT, v.value, Stress.Dictionary.value, f)
    synth_s = time.perf_counter() - t1

    return out, [
        f"завантаження: {load_s:.2f} с",
        f"синтез: {synth_s:.2f} с",
        f"наголошений текст: {accented[:120]}…",
    ]


def bench_openai_compat(voice: str, out: Path) -> tuple[Path, list[str]]:
    """Будь-який OpenAI-сумісний /v1/audio/speech — Speaches, Kokoro-FastAPI.

    Налаштування — з `.env` через `get_settings()`, а не з оточення: інакше
    `TTS_MODEL` з `.env` не діяв і шлюз отримував `tts-1` (див. README §12).
    """
    import urllib.error
    import urllib.request

    from app.config import get_settings

    settings = get_settings()
    base = settings.tts_base_url.rstrip("/")
    payload = json.dumps(
        {
            "model": settings.tts_model,
            "input": SAMPLE_TEXT,
            "voice": voice,
            "response_format": "wav",
        }
    ).encode()

    req = urllib.request.Request(
        f"{base}/audio/speech",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {settings.tts_api_key}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            out.write_bytes(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        raise RuntimeError(
            f"{base} повернув {exc.code}: {detail}. "
            f"Модель у запиті: {settings.tts_model!r} — перевірте TTS_MODEL у .env"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"сервер недоступний за {base}: {exc}") from exc

    return out, [f"сервер: {base}", f"модель: {settings.tts_model}", f"голос: {voice}"]


def bench_zonos2(voice: str, out: Path) -> tuple[Path, list[str]]:
    """ZONOS2 через zonos2-server (OpenAI-сумісний /v1/audio/speech).

    Це вимірювання, від якого залежить ризик R2 плану: README zonos2.cpp
    обіцяє реальний час ЛИШЕ на GPU. На CPU (наш випадок) швидкість невідома.

    Голос може бути іменем збереженого speaker-embedding або порожнім рядком,
    якщо сервер має голос за замовчуванням.
    """
    import os
    import urllib.error
    import urllib.request

    base = os.environ.get("ZONOS2_BASE_URL", "http://127.0.0.1:1919/v1").rstrip("/")
    payload = json.dumps(
        {
            "model": "zonos2",
            "input": SAMPLE_TEXT,
            "voice": voice or "default",
            "response_format": "wav",
        }
    ).encode()

    req = urllib.request.Request(
        f"{base}/audio/speech",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=3600) as resp:
            out.write_bytes(resp.read())
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"zonos2-server недоступний за {base}: {exc}. "
            f"Запусти ./start-zonos2.sh --cpu --quant q4_k"
        ) from exc

    return out, [f"сервер: {base}", "режим CPU/GPU невідомий із відповіді"]


ENGINES = {
    "piper": (bench_piper, ["uk_UA-tetiana-high", "uk_UA-mykyta-high"]),
    "ukrainian-tts": (bench_ukrainian_tts, ["tetiana", "mykyta", "lada"]),
    "openai-compat": (bench_openai_compat, ["uk_UA-lada-x_low", "uk_UA-tetiana-high"]),
    # Критичне вимірювання: ризик R2 плану (швидкість ZONOS2 на CPU).
    "zonos2": (bench_zonos2, ["default"]),
}


# ── Вимір усього конвеєра ────────────────────────────────────────────────
# Синтез — це лише частина роботи: далі сегменти склеюються, нормалізуються
# за EBU R128 і кодуються в MP3. Саме цей шлях проходить користувач, тож
# важливо знати не лише RTF моделі, а й ціну постобробки.

def bench_pipeline(voice: str, out_dir: Path) -> tuple[Path, list[str], dict]:
    """Повний конвеєр: нормалізація → сегментація → синтез → склейка → MP3.

    Використовує ТІ САМІ сервіси застосунку, що й воркер, тому числа
    стосуються реального шляху, а не окремого виклику моделі.
    """
    from app.models import Block, BlockKind
    from app.services.audio.assemble import build_audio
    from app.services.expression.profiles import get_prosody
    from app.services.library.blocks import block_effective_text
    from app.services.normalize.uk import normalize
    from app.services.segment.splitter import split_sentences
    from app.services.tts.base import SynthRequest
    from app.services.tts.openai_compat import get_openai_compat_engine

    engine = get_openai_compat_engine()
    max_chars = engine.capabilities().max_chars

    # 1. Нормалізація й сегментація — так, як це робить завантаження документа
    block = Block(
        ordinal=0, kind=BlockKind.PARAGRAPH, text_raw=SAMPLE_TEXT,
        text_normalized=normalize(SAMPLE_TEXT), speak=True,
    )
    normalized_seconds = 0.0
    sentences = split_sentences(block_effective_text(block), max_chars=max_chars)

    segment_dir = out_dir / "pipeline_segments"
    segment_dir.mkdir(parents=True, exist_ok=True)
    for stale in segment_dir.glob("*.wav"):
        stale.unlink()

    # 2. Синтез кожного сегмента — з просодією, як у воркері
    prosody = get_prosody("neutral", 1.0)
    paths: list[Path] = []
    pauses: list[int] = []
    energies: list[float] = []

    started = time.perf_counter()
    for index, sentence in enumerate(sentences):
        target = segment_dir / f"seg{index}.wav"
        engine.synthesize(
            SynthRequest(
                text=sentence, voice_id=voice, speed=prosody.speed, output_path=target
            )
        )
        paths.append(target)
        pauses.append(prosody.pause_after_ms)
        energies.append(prosody.energy_db)
    synth_seconds = time.perf_counter() - started

    # 3. Склейка + LUFS + MP3
    started = time.perf_counter()
    results = build_audio(
        paths, out_dir, "pipeline",
        pause_ms=400,
        target_sample_rate=24000,
        target_lufs=-18.0,
        formats=("wav", "mp3"),
        pauses_ms=pauses,
        energies_db=energies,
    )
    assemble_seconds = time.perf_counter() - started

    mp3 = results["mp3"]
    notes = [
        f"сегментів: {len(sentences)} (ліміт рушія {max_chars} симв.)",
        f"нормалізований текст: {len(block_effective_text(block))} символів",
        f"синтез: {synth_seconds:.2f} с · склейка+LUFS+MP3: {assemble_seconds:.2f} с",
        f"WAV для перевірки: {results['wav'].name}",
    ]
    extra = {
        "segments": len(sentences),
        "synth_seconds": synth_seconds,
        "assemble_seconds": assemble_seconds,
        "normalized_seconds": normalized_seconds,
    }
    return mp3, notes, extra


def duration_of(path: Path) -> float:
    """Тривалість аудіо: WAV читаємо напряму, MP3 — через soundfile."""
    if path.suffix.lower() == ".wav":
        return wav_duration(path)
    import soundfile as sf

    info = sf.info(str(path))
    return info.frames / float(info.samplerate)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--engine", choices=[*ENGINES, "all"], default="all")
    ap.add_argument("--voice", help="один конкретний голос замість типового набору")
    ap.add_argument("--repeat", type=int, default=1, help="повторів на голос (для медіани)")
    ap.add_argument("--text-file", type=Path, help="власний текст замість вбудованого")
    ap.add_argument(
        "--pipeline", action="store_true",
        help="міряти весь конвеєр (сегментація → синтез → склейка → LUFS → MP3)",
    )
    args = ap.parse_args()

    global SAMPLE_TEXT
    if args.text_file:
        SAMPLE_TEXT = args.text_file.read_text(encoding="utf-8")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Текст: {len(SAMPLE_TEXT)} символів")
    print(f"Пікова RAM до старту: {peak_rss_mb():.0f} MB\n")

    if args.pipeline:
        return run_pipeline(args)

    selected = list(ENGINES) if args.engine == "all" else [args.engine]
    results: list[Result] = []

    for name in selected:
        fn, default_voices = ENGINES[name]
        voices = [args.voice] if args.voice else default_voices
        for voice in voices:
            times: list[float] = []
            last: Result | None = None

            for run in range(args.repeat):
                out = OUT_DIR / f"{name}__{voice}__{run}.wav"
                res = Result(engine=name, voice=voice, ok=False)
                try:
                    t0 = time.perf_counter()
                    path, notes = fn(voice, out)
                    res.wall_seconds = time.perf_counter() - t0
                    res.audio_seconds = wav_duration(path)
                    res.bytes_out = path.stat().st_size
                    res.rtf = res.wall_seconds / res.audio_seconds if res.audio_seconds else 0.0
                    res.ok = True
                    res.notes = notes
                    times.append(res.rtf)
                except Exception as exc:  # Рушій недоступний — це нормальний результат
                    res.error = f"{type(exc).__name__}: {exc}"
                res.peak_rss_mb = peak_rss_mb()
                last = res

            if last is None:
                continue
            if len(times) > 1:
                last.rtf = statistics.median(times)
                last.notes.append(
                    f"RTF медіана з {len(times)}: {last.rtf:.3f} "
                    f"(мін {min(times):.3f}, макс {max(times):.3f})"
                )

            results.append(last)
            if last.ok:
                print(
                    f"[OK]   {name:15s} {voice:26s} "
                    f"аудіо {last.audio_seconds:6.1f} с | стіна {last.wall_seconds:6.2f} с | "
                    f"RTF {last.rtf:5.2f} | RAM {last.peak_rss_mb:6.0f} MB"
                )
                for n in last.notes:
                    print(f"         · {n}")
            else:
                print(f"[SKIP] {name:15s} {voice:26s} {last.error}")

    return finish(results)


def run_pipeline(args) -> int:
    """Виміряти весь конвеєр для наявних голосів."""
    from app.config import get_settings

    settings = get_settings()
    voices = [args.voice] if args.voice else ENGINES["openai-compat"][1]
    results: list[Result] = []

    print(f"Рушій: openai_compat · модель {settings.tts_model}")
    print(f"Постобробка: склейка + EBU R128 ({settings.target_lufs} LUFS) + MP3\n")

    for voice in voices:
        res = Result(engine="pipeline", voice=voice, ok=False, stage="pipeline")
        try:
            path, notes, extra = bench_pipeline(voice, OUT_DIR)
            res.audio_seconds = duration_of(path)
            res.wall_seconds = extra["synth_seconds"] + extra["assemble_seconds"]
            res.rtf = res.wall_seconds / res.audio_seconds if res.audio_seconds else 0.0
            res.bytes_out = path.stat().st_size
            res.segments = extra["segments"]
            res.synth_seconds = extra["synth_seconds"]
            res.assemble_seconds = extra["assemble_seconds"]
            res.notes = notes
            res.ok = True
        except Exception as exc:
            res.error = f"{type(exc).__name__}: {exc}"

        res.peak_rss_mb = peak_rss_mb()
        results.append(res)

        if res.ok:
            share = res.assemble_seconds / res.wall_seconds * 100 if res.wall_seconds else 0
            print(
                f"[OK]   {voice:26s} аудіо {res.audio_seconds:6.1f} с | "
                f"усього {res.wall_seconds:6.2f} с | RTF {res.rtf:5.2f} | "
                f"постобробка {share:4.1f} % часу"
            )
            for note in res.notes:
                print(f"         · {note}")
        else:
            print(f"[SKIP] {voice:26s} {res.error}")

    return finish(results)


def finish(results: list[Result]) -> int:
    """Записати звіт і підсумувати."""
    report = OUT_DIR / "benchmark.json"
    existing: list[dict] = []
    if report.exists():
        try:
            existing = json.loads(report.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = []

    report.write_text(
        json.dumps(existing + [asdict(r) for r in results], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\nЗвіт: {report}")

    ok = [r for r in results if r.ok]
    if ok:
        best = min(ok, key=lambda r: r.rtf)
        print(
            f"Найшвидший: {best.engine} / {best.voice} — RTF {best.rtf:.2f} "
            f"({best.audio_seconds:.1f} с аудіо за {best.wall_seconds:.1f} с)"
        )
        print("\nВнеси ці числа в docs/RESEARCH.md і ARCHITECTURE.md, розділ 8.")
    else:
        print("\nЖоден рушій не відпрацював. Почни з: uv sync --extra piper")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
