"""Asyncio-воркер: черга синтезу і SSE-брокер.

Один воркер на процес FastAPI. Черга — asyncio.Queue + стан у SQLite.
Ідемпотентність: перед синтезом перевіряємо, чи вже є готовий WAV (ADR-007).
Паралельність: до N сегментів одночасно (SYNTH_CONCURRENCY з config).

ВАЖЛИВО: asyncio.Queue() НЕ ініціалізується на рівні модуля —
це спричиняє «Queue is bound to a different event loop» у тестах,
де TestClient кожен раз створює новий loop. Натомість черга
ініціалізується lazy в _get_queue() при першому виклику з конкретного loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from pathlib import Path

from sqlmodel import Session, select

from app.config import get_settings
from app.db import get_engine
from app.models import (
    Block,
    BlockKind,
    Document,
    Job,
    JobStatus,
    Segment,
    SegmentStatus,
    utc_now,
)
from app.services.audio.assemble import (
    ChapterSpan,
    build_audio,
    normalize_formats,
    primary_format,
)
from app.services.expression.profiles import get_prosody
from app.services.library.blocks import block_effective_text
from app.services.library.chapters import (
    Chapter,
    build_chapter_plan,
    chapter_file_name,
)
from app.services.segment.splitter import split_sentences
from app.services.tts.openai_compat import get_openai_compat_engine

logger = logging.getLogger(__name__)


# Пауза на межі абзацу/розділу довша за паузу між реченнями — це те, що чути
# на слух як «структура тексту» (ARCHITECTURE §2.3).
_PARAGRAPH_PAUSE_FACTOR = 1.5


@dataclass
class BlockTask:
    """Знімок блоку для синтезу — простий, без ORM-привʼязки.

    Потрібен тому, що синтез іде ПОЗА сесією: сесія закривається, а `commit()`
    у ній робить ORM-обʼєкти застарілими. Звернення до полів відʼєднаного
    обʼєкта падає з DetachedInstanceError, тож усе потрібне знімаємо заздалегідь.

    `kind` і `heading_level` потрібні, щоб після синтезу знати межі розділів:
    план розділів будується з тих самих блоків, що пішли в роботу.
    """

    id: int
    ordinal: int
    text: str
    emotion: str
    intensity: float
    kind: BlockKind = BlockKind.PARAGRAPH
    heading_level: int | None = None

# ── SSE-брокер ─────────────────────────────────────────────────────────────────
# job_id → список черг передплатників
_subscribers: dict[int, list[asyncio.Queue]] = {}
_running_jobs: dict[int, asyncio.Task] = {}

# ── Черга завдань (lazy) ───────────────────────────────────────────────────────
# Не ініціалізуємо на рівні модуля, щоб уникнути binding до іншого event loop.
_job_queue: asyncio.Queue | None = None


def _get_queue() -> asyncio.Queue:
    """Повернути (або створити) чергу для поточного event loop."""
    global _job_queue
    if _job_queue is None:
        _job_queue = asyncio.Queue()
    return _job_queue


def reset_queue() -> None:
    """Скинути стан воркера між тестами."""
    global _job_queue
    _job_queue = None
    _subscribers.clear()
    _running_jobs.clear()


# ── SSE-функції ────────────────────────────────────────────────────────────────

def subscribe_job(job_id: int) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    _subscribers.setdefault(job_id, []).append(q)
    return q


def unsubscribe_job(job_id: int, q: asyncio.Queue) -> None:
    subs = _subscribers.get(job_id, [])
    if q in subs:
        subs.remove(q)


async def _broadcast(job_id: int, event: dict) -> None:
    for q in list(_subscribers.get(job_id, [])):
        # Повільний клієнт: черга переповнена — пропускаємо подію,
        # а не блокуємо синтез.
        with contextlib.suppress(asyncio.QueueFull):
            q.put_nowait(event)


# ── Воркер ─────────────────────────────────────────────────────────────────────

def enqueue_job(job_id: int) -> None:
    """Поставити завдання в чергу воркера."""
    _get_queue().put_nowait(job_id)


def requeue_incomplete_jobs() -> list[int]:
    """Повернути в чергу завдання, обірвані перезапуском процесу.

    Черга живе лише в памʼяті процесу, тому без цього кроку завдання, що
    лишилось у `queued`/`running` у SQLite, не виконалось би ніколи —
    обіцянка ADR-007 («синтез продовжується з місця зупинки») на рівні
    завдання не працювала (docs/FRONTEND.md, знахідка 16.2).

    Ідемпотентність забезпечують файли сегментів на диску: готові WAV не
    синтезуються вдруге, тож «продовжити» тут означає «догнати решту».

    Викликається з lifespan при старті. Повертає id повернутих завдань.
    """
    requeued: list[int] = []
    with Session(get_engine()) as session:
        stale = session.exec(
            select(Job).where(Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]))
        ).all()

        for job in stale:
            if job.status == JobStatus.RUNNING:
                # Процес, який його виконував, більше не існує
                job.status = JobStatus.QUEUED
                job.error = ""
                session.add(job)
            requeued.append(job.id or 0)

        session.commit()

    for job_id in requeued:
        enqueue_job(job_id)

    if requeued:
        logger.info("Повернуто в чергу незавершених завдань: %s", requeued)
    return requeued


async def worker_loop() -> None:
    """Головний цикл воркера. Запускається через lifespan FastAPI."""
    settings = get_settings()
    semaphore = asyncio.Semaphore(settings.synth_concurrency)
    queue = _get_queue()  # прив'язуємо до поточного loop

    logger.info(
        "Воркер запущено (паралельність=%d)", settings.synth_concurrency
    )

    while True:
        job_id = await queue.get()
        task = asyncio.create_task(_process_job(job_id, semaphore))
        _running_jobs[job_id] = task

        def _done(t: asyncio.Task, jid: int = job_id) -> None:
            _running_jobs.pop(jid, None)
            # Порядок важливий: у скасованої задачі t.exception() кидає
            # CancelledError, і колбек ламався під час зупинки застосунку.
            if t.cancelled():
                logger.info("Завдання %d скасовано", jid)
                return
            error = t.exception()
            if error:
                logger.error("Завдання %d завершилось з помилкою: %s", jid, error)

        task.add_done_callback(_done)


async def _process_job(job_id: int, semaphore: asyncio.Semaphore) -> None:
    """Обробити завдання, гарантовано доводячи його до кінцевого стану.

    Без цієї обгортки несподівана помилка (мережа, моделі, диск) лишала
    завдання в статусі `running` назавжди: користувач бачив би 0 %, а
    «скасувати» і «повторити» були б недоступні. Тепер будь-яка помилка
    стає видимим `failed` із текстом причини.
    """
    try:
        await _run_job(job_id, semaphore)
    except asyncio.CancelledError:
        raise  # скасування користувачем — не помилка
    except Exception as exc:
        logger.exception("Завдання %d впало несподівано", job_id)
        await _fail_job(job_id, f"Несподівана помилка: {exc}")


async def _run_job(job_id: int, semaphore: asyncio.Semaphore) -> None:
    """Основна робота: сегменти, синтез, збірка."""
    settings = get_settings()
    engine = get_openai_compat_engine()

    with Session(get_engine()) as session:
        job = session.get(Job, job_id)
        if not job:
            logger.error("Завдання %d не знайдено", job_id)
            return

        if job.status == JobStatus.CANCELLED:
            return

        # Завантажуємо блоки документа
        blocks = session.exec(
            select(Block)
            .where(Block.document_id == job.document_id, Block.speak.is_(True))
            .order_by(Block.ordinal)
        ).all()

        if not blocks:
            _finish_job(session, job, JobStatus.FAILED, "Немає блоків для синтезу")
            return

        # Знімаємо з job і блоків усе потрібне, доки сесія відкрита.
        # `commit()` (у _start_job нижче) робить ORM-обʼєкти застарілими, і
        # звернення до їхніх полів ПОЗА сесією падає з DetachedInstanceError —
        # саме так цей шлях і ламався, доки завдання не можна було створити.
        tasks = [
            BlockTask(
                id=block.id or 0,
                ordinal=block.ordinal,
                text=block_effective_text(block),
                emotion=block.emotion,
                intensity=block.intensity,
                kind=block.kind,
                heading_level=block.heading_level,
            )
            for block in blocks
        ]

        document = session.get(Document, job.document_id)
        document_title = ""
        if document:
            document_title = Path(document.filename).stem

        # Налаштування збірки знімаємо з options_json, доки сесія відкрита
        options = dict(job.options_json or {})

        # Прибираємо рядки сегментів попереднього прогону цього ж завдання.
        # Файли на диску НЕ чіпаємо — саме на них тримається ідемпотентність
        # (ADR-007): готовий WAV не синтезується вдруге. Без цього кроку
        # повторний запуск додавав би сегменти дублем (знахідка 16.8c).
        # Голос теж потрібен поза сесією: після commit() обʼєкт job стає
        # застарілим, і читання job.voice_id у циклі синтезу падало б.
        voice_id = job.voice_id or settings.default_voice

        stale = session.exec(select(Segment).where(Segment.job_id == job_id)).all()
        for segment in stale:
            session.delete(segment)
        session.commit()

        _start_job(session, job)

    # Синтезуємо сегменти (лише прості дані, без ORM-обʼєктів)
    max_chars = engine.capabilities().max_chars
    all_segment_paths: list[Path] = []
    # По-сегментна просодія: пауза ПІСЛЯ кожного сегмента і корекція гучності.
    # Без цього емоція впливала б лише на темп, а «сумно» не мало б довгих пауз.
    pauses_ms: list[int] = []
    energies_db: list[float] = []
    total_segments = sum(
        len(split_sentences(task.text, max_chars=max_chars)) for task in tasks
    )
    done_count = 0

    # ── Розділи ──────────────────────────────────────────────────────────────
    # План будується з ТИХ САМИХ блоків, що пішли в синтез, тож його межі
    # збігаються з межами аудіо. Далі лишається запам'ятати, які саме
    # сегменти належать кожному розділу.
    chapters = build_chapter_plan(tasks, document_title=document_title)
    chapter_of_block = _map_blocks_to_chapters(tasks, chapters)
    segments_by_chapter: dict[int, list[int]] = {}

    await _broadcast(job_id, {
        "event": "progress",
        "job_id": job_id,
        "done": 0,
        "total": total_segments,
        "percent": 0.0,
        "status": "running",
    })

    seg_ordinal = 0
    for task in tasks:
        text = task.text
        if not text.strip():
            continue

        prosody = get_prosody(task.emotion, task.intensity)
        sentences = split_sentences(text, max_chars=max_chars)
        segments_in_block = 0

        for sent in sentences:
            if not sent.strip():
                continue

            seg_path = (
                settings.renders_dir
                / "segments"
                / f"job{job_id}_block{task.id}_seg{seg_ordinal}.wav"
            )
            seg_path.parent.mkdir(parents=True, exist_ok=True)

            # Ідемпотентність: якщо файл є — пропускаємо
            if seg_path.exists() and seg_path.stat().st_size > 0:
                logger.debug("Сегмент вже є: %s", seg_path.name)
            else:
                from app.services.tts.base import SynthRequest

                try:
                    async with semaphore:
                        await asyncio.to_thread(
                            engine.synthesize,
                            SynthRequest(
                                text=sent,
                                voice_id=voice_id,
                                speed=prosody.speed,
                                output_path=seg_path,
                            ),
                        )
                except Exception as exc:
                    # Не продовжуємо: якщо шлюз лежить, решта сегментів теж
                    # впаде — краще зупинитись і сказати причину.
                    await _fail_job(
                        job_id,
                        f"Синтез сегмента {seg_ordinal} не вдався: {exc}",
                    )
                    return

            # Зберегти сегмент у БД
            with Session(get_engine()) as session:
                job_check = session.get(Job, job_id)
                if job_check and job_check.status == JobStatus.CANCELLED:
                    logger.info("Завдання %d скасовано", job_id)
                    return

                seg = Segment(
                    job_id=job_id,
                    block_id=task.id,
                    ordinal=seg_ordinal,
                    text=sent,
                    prosody_json={
                        "speed": prosody.speed,
                        "pause_after_ms": prosody.pause_after_ms,
                    },
                    audio_path=str(seg_path),
                    status=SegmentStatus.DONE,
                )
                session.add(seg)

                done_count += 1
                progress = done_count / total_segments if total_segments else 1.0
                job_check.progress = progress
                session.add(job_check)
                session.commit()

            all_segment_paths.append(seg_path)
            pauses_ms.append(prosody.pause_after_ms)
            energies_db.append(prosody.energy_db)
            segments_in_block += 1

            # Запам'ятовуємо, до якого розділу належить сегмент
            chapter_index = chapter_of_block.get(task.id)
            if chapter_index is not None:
                segments_by_chapter.setdefault(chapter_index, []).append(seg_ordinal)

            seg_ordinal += 1

            await _broadcast(job_id, {
                "event": "segment",
                "job_id": job_id,
                "block_id": task.id,
                "ordinal": seg_ordinal,
                "status": "done",
            })
            await _broadcast(job_id, {
                "event": "progress",
                "job_id": job_id,
                "done": done_count,
                "total": total_segments,
                "percent": round(progress * 100, 1),
                "status": "running",
            })

        # Кінець абзацу: подовжуємо паузу після його останнього сегмента
        if segments_in_block and pauses_ms:
            pauses_ms[-1] = round(pauses_ms[-1] * _PARAGRAPH_PAUSE_FACTOR)

    # Збірка фінального файлу
    artifacts: dict[str, str] = {}
    if all_segment_paths:
        requested_formats = _requested_formats(options)
        chapter_spans = _chapter_spans(chapters, segments_by_chapter)
        chapter_names = {
            chapter.title: chapter_file_name(chapter)
            for index, chapter in enumerate(chapters)
            if index in segments_by_chapter
        }

        try:
            results = await asyncio.to_thread(
                build_audio,
                all_segment_paths,
                settings.renders_dir,
                f"job_{job_id}",
                pause_ms=400,
                target_sample_rate=settings.target_sample_rate,
                target_lufs=settings.target_lufs,
                pauses_ms=pauses_ms,
                energies_db=energies_db,
                formats=requested_formats,
                chapters=chapter_spans,
                chapter_names=chapter_names,
                tags=_audiobook_tags(document_title, job_id),
                m4b_bitrate=settings.m4b_bitrate_kbps * 1000,
                mp3_bitrate=settings.mp3_bitrate_kbps,
            )
            artifacts = {name: str(path) for name, path in results.items()}
            primary = primary_format(tuple(results))
            output_path = artifacts.get(primary, "")
        except Exception as exc:
            logger.error("Помилка збірки аудіо для завдання %d: %s", job_id, exc)
            with Session(get_engine()) as session:
                failed_job = session.get(Job, job_id)
                if failed_job is not None:
                    _finish_job(session, failed_job, JobStatus.FAILED, str(exc))
            return
    else:
        output_path = ""

    with Session(get_engine()) as session:
        job = session.get(Job, job_id)
        if job is None:
            # Завдання видалили, доки воно синтезувалось, — писати нікуди.
            logger.warning("Завдання %d зникло з БД до завершення", job_id)
            return

        job.output_path = output_path
        # Перелік створених файлів живе в options_json: окрема колонка вимагала
        # б ці міграції, а проєкт свідомо живе на create_all (те саме рішення,
        # що й для client_token). Пишемо його ЗАВЖДИ, навіть порожнім: інакше
        # після повтору, який нічого не зібрав, лишився б перелік від
        # попереднього прогону, і завантаження віддавало б старі файли як нові.
        options = dict(job.options_json or {})
        options["artifacts"] = artifacts
        job.options_json = options
        session.add(job)
        _finish_job(session, job, JobStatus.DONE)

    await _broadcast(job_id, {
        "event": "finished",
        "job_id": job_id,
        "output_path": output_path,
        "artifacts": artifacts,
        "status": "done",
    })
    logger.info("Завдання %d завершено: %s (файлів: %d)", job_id, output_path, len(artifacts))


def _start_job(session: Session, job: Job) -> None:
    job.status = JobStatus.RUNNING
    job.started_at = utc_now()
    session.add(job)
    session.commit()


def _map_blocks_to_chapters(
    tasks: list[BlockTask], chapters: list[Chapter]
) -> dict[int, int]:
    """block_id → індекс розділу в плані.

    Обхід один, а не «для кожного блоку шукаємо розділ»: блоки йдуть у порядку
    читання, а розділи — суміжні діапазони `ordinal`, тож курсор достатньо
    рухати вперед. На книзі з 5000 блоків це різниця між O(n) і O(n·m).
    """
    mapping: dict[int, int] = {}
    cursor = 0
    for task in tasks:
        while cursor < len(chapters) and task.ordinal > chapters[cursor].last_block:
            cursor += 1
        if cursor >= len(chapters):
            break
        chapter = chapters[cursor]
        if chapter.first_block <= task.ordinal <= chapter.last_block:
            mapping[task.id] = cursor
    return mapping


def _chapter_spans(
    chapters: list[Chapter], segments_by_chapter: dict[int, list[int]]
) -> list[ChapterSpan]:
    """Розділи в термінах індексів сегментів — те, що потрібно `build_audio`."""
    spans: list[ChapterSpan] = []
    for index in sorted(segments_by_chapter):
        ordinals = segments_by_chapter[index]
        if not ordinals:
            continue
        spans.append(
            ChapterSpan(
                title=chapters[index].title,
                first_segment=min(ordinals),
                last_segment=max(ordinals),
            )
        )
    return spans


def _requested_formats(options: dict[str, object]) -> tuple[str, ...]:
    """Які файли створити для цього завдання.

    `formats` — зі `options_json` (його ставить і API, і інтерфейс), із
    запасним значенням із налаштувань. `chapter_split` додає zip із
    файлами-розділами: це не формат звуку, а упаковка, тому окремим
    прапорцем, а не значенням у `formats` (інакше вибір формату в UI вів би
    до появи архіву, якого ніхто не просив).
    """
    settings = get_settings()
    formats = list(
        normalize_formats(options.get("formats"), default=settings.output_format_list)
    )
    if options.get("chapter_split") and "zip" not in formats:
        formats.append("zip")
    return tuple(formats)


def _audiobook_tags(document_title: str, job_id: int) -> dict[str, str]:
    """Метадані аудіокниги: без них плейер показує «Невідомий альбом».

    PLAN, етап 2 ставить це окремим пунктом («ID3-теги через mutagen»).
    """
    settings = get_settings()
    title = document_title.strip() or f"Завдання {job_id}"
    return {
        "title": title,
        "album": title,
        "artist": settings.audiobook_artist,
        "album_artist": settings.audiobook_artist,
        "genre": "Аудіокнига",
        "date": str(utc_now().year),
        "comment": f"Створено Voice Studio (завдання {job_id})",
    }


def _finish_job(session: Session, job: Job, status: JobStatus, error: str = "") -> None:
    job.status = status
    job.error = error
    job.progress = 1.0 if status == JobStatus.DONE else job.progress
    job.finished_at = utc_now()
    session.add(job)
    session.commit()


async def cancel_job(job_id: int) -> bool:
    """Скасувати завдання (якщо воно ще виконується)."""
    with Session(get_engine()) as session:
        job = session.get(Job, job_id)
        if not job:
            return False
        if job.status in (JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED):
            return False
        job.status = JobStatus.CANCELLED
        job.finished_at = utc_now()
        session.add(job)
        session.commit()

    task = _running_jobs.get(job_id)
    if task and not task.done():
        task.cancel()

    await _broadcast(job_id, {"event": "cancelled", "job_id": job_id})
    return True


async def _fail_job(job_id: int, error: str) -> None:
    """Позначити завдання невдалим і повідомити підписників.

    Єдина точка, де завдання стає `failed`: і для помилки синтезу, і для
    несподіваних винятків. Так стан у БД і подія в SSE не розходяться.
    """
    with Session(get_engine()) as session:
        job = session.get(Job, job_id)
        if not job:
            return
        if job.status in (JobStatus.DONE, JobStatus.CANCELLED):
            return  # уже завершено — не переписуємо кінцевий стан
        _finish_job(session, job, JobStatus.FAILED, error)

    await _broadcast(job_id, {
        "event": "error",
        "job_id": job_id,
        "status": JobStatus.FAILED,
        "error": error,
    })
