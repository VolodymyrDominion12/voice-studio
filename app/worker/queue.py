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
from datetime import UTC, datetime
from pathlib import Path

from sqlmodel import Session, select

from app.config import get_settings
from app.db import get_engine
from app.models import (
    Block,
    Job,
    JobStatus,
    Segment,
    SegmentStatus,
)
from app.services.audio.assemble import build_audio
from app.services.expression.profiles import get_prosody
from app.services.segment.splitter import split_sentences
from app.services.tts.openai_compat import get_openai_compat_engine

logger = logging.getLogger(__name__)

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
            if t.exception():
                logger.error("Завдання %d завершилось з помилкою: %s", jid, t.exception())

        task.add_done_callback(_done)


async def _process_job(job_id: int, semaphore: asyncio.Semaphore) -> None:
    """Обробити одне завдання синтезу."""
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

        _start_job(session, job)

    # Синтезуємо сегменти
    all_segment_paths: list[Path] = []
    total_segments = sum(
        len(split_sentences(
            (b.text_edited or b.text_normalized or b.text_raw),
            max_chars=engine.capabilities().max_chars,
        ))
        for b in blocks
    )
    done_count = 0

    await _broadcast(job_id, {
        "event": "progress",
        "job_id": job_id,
        "done": 0,
        "total": total_segments,
        "percent": 0.0,
        "status": "running",
    })

    seg_ordinal = 0
    for block in blocks:
        text = block.text_edited or block.text_normalized or block.text_raw
        if not text.strip():
            continue

        prosody = get_prosody(block.emotion, block.intensity)
        sentences = split_sentences(text, max_chars=engine.capabilities().max_chars)

        for sent in sentences:
            if not sent.strip():
                continue

            seg_path = (
                settings.renders_dir
                / "segments"
                / f"job{job_id}_block{block.id}_seg{seg_ordinal}.wav"
            )
            seg_path.parent.mkdir(parents=True, exist_ok=True)

            # Ідемпотентність: якщо файл є — пропускаємо
            if seg_path.exists() and seg_path.stat().st_size > 0:
                logger.debug("Сегмент вже є: %s", seg_path.name)
            else:
                from app.services.tts.base import SynthRequest
                async with semaphore:
                    await asyncio.to_thread(
                        engine.synthesize,
                        SynthRequest(
                            text=sent,
                            voice_id=job.voice_id or settings.default_voice,
                            speed=prosody.speed,
                            output_path=seg_path,
                        ),
                    )

            # Зберегти сегмент у БД
            with Session(get_engine()) as session:
                job_check = session.get(Job, job_id)
                if job_check and job_check.status == JobStatus.CANCELLED:
                    logger.info("Завдання %d скасовано", job_id)
                    return

                seg = Segment(
                    job_id=job_id,
                    block_id=block.id,
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
            seg_ordinal += 1

            await _broadcast(job_id, {
                "event": "segment",
                "job_id": job_id,
                "block_id": block.id,
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

    # Збірка фінального файлу
    if all_segment_paths:
        try:
            results = await asyncio.to_thread(
                build_audio,
                all_segment_paths,
                settings.renders_dir,
                f"job_{job_id}",
                pause_ms=400,
                target_sample_rate=settings.target_sample_rate,
                target_lufs=settings.target_lufs,
            )
            output_path = str(results.get("mp3", results.get("wav", "")))
        except Exception as exc:
            logger.error("Помилка збірки аудіо для завдання %d: %s", job_id, exc)
            with Session(get_engine()) as session:
                job = session.get(Job, job_id)
                _finish_job(session, job, JobStatus.FAILED, str(exc))
            return
    else:
        output_path = ""

    with Session(get_engine()) as session:
        job = session.get(Job, job_id)
        job.output_path = output_path
        _finish_job(session, job, JobStatus.DONE)

    await _broadcast(job_id, {
        "event": "finished",
        "job_id": job_id,
        "output_path": output_path,
        "status": "done",
    })
    logger.info("Завдання %d завершено: %s", job_id, output_path)


def _start_job(session: Session, job: Job) -> None:
    job.status = JobStatus.RUNNING
    job.started_at = datetime.now(UTC).replace(tzinfo=None)
    session.add(job)
    session.commit()


def _finish_job(session: Session, job: Job, status: JobStatus, error: str = "") -> None:
    job.status = status
    job.error = error
    job.progress = 1.0 if status == JobStatus.DONE else job.progress
    job.finished_at = datetime.now(UTC).replace(tzinfo=None)
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
        job.finished_at = datetime.now(UTC).replace(tzinfo=None)
        session.add(job)
        session.commit()

    task = _running_jobs.get(job_id)
    if task and not task.done():
        task.cancel()

    await _broadcast(job_id, {"event": "cancelled", "job_id": job_id})
    return True
