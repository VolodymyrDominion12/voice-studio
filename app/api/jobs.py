"""API-роутер: завдання синтезу + SSE-прогрес.

POST /api/v1/jobs                              — створити завдання
GET  /api/v1/jobs                              — список завдань
GET  /api/v1/jobs/{id}                         — знімок одного завдання
GET  /api/v1/jobs/{id}/events                  — SSE: прогрес у реальному часі
GET  /api/v1/jobs/{id}/segments                — сегменти завдання
GET  /api/v1/jobs/{id}/segments/{ord}/audio    — WAV одного сегмента
POST /api/v1/jobs/{id}/cancel                  — скасувати
POST /api/v1/jobs/{id}/retry                   — повторити
GET  /api/v1/jobs/{id}/download                — завантажити результат
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from sqlmodel import Session, select

from app.config import get_settings
from app.db import get_session
from app.models import Document, Job, JobCreate, JobRead, JobStatus, Segment
from app.services.audio.assemble import normalize_formats, primary_format
from app.services.library import documents as library
from app.services.library.errors import DocumentNotFoundError
from app.worker.queue import cancel_job, enqueue_job, subscribe_job, unsubscribe_job

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])

def _job_read(job: Job, session: Session) -> JobRead:
    """JobRead разом із назвою документа (16.8b)."""
    data = JobRead.model_validate(job)
    document = session.get(Document, job.document_id)
    data.document_name = document.filename if document else ""
    return data


def _find_by_token(session: Session, token: str) -> Job | None:
    """Знайти незавершене завдання за ключем ідемпотентності.

    Ключ лежить у `options_json` (окрема колонка вимагала б міграції, а
    проєкт свідомо живе на `create_all`). Перебираємо лише активні завдання —
    їх мало, і саме вони цікаві для захисту від подвійного кліку.
    """
    if not token:
        return None
    active = session.exec(
        select(Job)
        .where(Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]))
        .order_by(Job.created_at.desc())
    ).all()
    for job in active:
        if (job.options_json or {}).get("client_token") == token:
            return job
    return None


# Формат → MIME. Ключі — розширення файлів, які реально створює build_audio.
_MEDIA_TYPES = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4b": "audio/mp4",
    ".zip": "application/zip",
}


@router.post("", response_model=JobRead, status_code=201)
def create_job(payload: JobCreate, session: Session = Depends(get_session)):
    """Створити завдання синтезу та поставити в чергу воркера.

    Документ і озвучувані блоки перевіряються ДО створення: інакше завдання
    створювалось би й одразу падало з «Немає блоків для синтезу», а користувач
    бачив би збій замість зрозумілої відмови (docs/FRONTEND.md, знахідка 16.8d).
    """
    from app.services.library.blocks import count_speakable

    try:
        result = library.get_document_with_blocks(session, payload.document_id)
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    speakable = count_speakable(result.blocks)
    if speakable == 0:
        raise HTTPException(
            status_code=422,
            detail=(
                "У документі немає жодного озвучуваного блоку з непорожнім текстом. "
                "Позначте хоча б один блок як «озвучувати»."
            ),
        )

    existing = _find_by_token(session, payload.client_token)
    if existing:
        logger.info("Повторний запит із тим самим ключем — повертаємо завдання %s", existing.id)
        return _job_read(existing, session)

    options = dict(payload.options)
    try:
        # Невідомий формат — це 422 одразу, а не «завдання виконалось, а файлу
        # такого немає»: користувач дізнається про помилку до синтезу книги.
        options["formats"] = list(
            normalize_formats(options.get("formats"), default=get_settings().output_format_list)
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if payload.client_token:
        options["client_token"] = payload.client_token

    job = Job(
        document_id=payload.document_id,
        engine_id=payload.engine_id,
        voice_id=payload.voice_id,
        options_json=options,
        status=JobStatus.QUEUED,
    )
    session.add(job)
    session.commit()
    session.refresh(job)

    enqueue_job(job.id)
    logger.info(
        "Завдання %d поставлено в чергу (doc=%d, блоків до синтезу=%d)",
        job.id, job.document_id, speakable,
    )
    return _job_read(job, session)


@router.get("", response_model=list[JobRead])
def list_jobs(session: Session = Depends(get_session)):
    """Список усіх завдань (остання черга — вгорі)."""
    jobs = session.exec(select(Job).order_by(Job.created_at.desc())).all()
    return [_job_read(job, session) for job in jobs]


@router.get("/{job_id}", response_model=JobRead)
def get_job(job_id: int, session: Session = Depends(get_session)):
    """Знімок одного завдання.

    Потрібен сторінці завдання як базова лінія перед SSE: стрім віддає лише
    МАЙБУТНІ події, тож без знімка сторінка після F5 показувала б 0 %
    (docs/FRONTEND.md, розд. 12).
    """
    job = session.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Завдання не знайдено")
    return _job_read(job, session)


@router.get("/{job_id}/segments")
def list_job_segments(job_id: int, session: Session = Depends(get_session)):
    """Сегменти завдання — що вже синтезовано, а що ні.

    Дає змогу слухати готові сегменти, не чекаючи завершення всього завдання
    (docs/FRONTEND.md, розд. 7.2).
    """
    job = session.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Завдання не знайдено")

    segments = session.exec(
        select(Segment).where(Segment.job_id == job_id).order_by(Segment.ordinal)
    ).all()
    return {
        "job_id": job_id,
        "status": job.status,
        "total": len(segments),
        "segments": [
            {
                "ordinal": segment.ordinal,
                "block_id": segment.block_id,
                "text": segment.text,
                "duration_ms": segment.duration_ms,
                "status": segment.status,
                "prosody": segment.prosody_json,
                "has_audio": bool(segment.audio_path),
            }
            for segment in segments
        ],
    }


@router.get("/{job_id}/segments/{ordinal}/audio")
def segment_audio(job_id: int, ordinal: int, session: Session = Depends(get_session)):
    """Віддати WAV одного сегмента (прослухати, не чекаючи кінця завдання)."""
    segment = session.exec(
        select(Segment).where(Segment.job_id == job_id, Segment.ordinal == ordinal)
    ).first()
    if not segment or not segment.audio_path:
        raise HTTPException(status_code=404, detail="Сегмент не знайдено")

    path = Path(segment.audio_path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Файл сегмента зник із диска")
    return FileResponse(path=str(path), media_type="audio/wav")


@router.post("/{job_id}/retry", response_model=JobRead)
def retry_job(job_id: int, session: Session = Depends(get_session)):
    """Повторити невдале або скасоване завдання.

    Готові сегменти на диску не синтезуються вдруге (ADR-007), тож повтор
    фактично доганяє те, що не встигло.
    """
    job = session.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Завдання не знайдено")

    if job.status in (JobStatus.QUEUED, JobStatus.RUNNING):
        raise HTTPException(
            status_code=409,
            detail=f"Завдання вже виконується (статус: {job.status!r})",
        )

    job.status = JobStatus.QUEUED
    job.error = ""
    job.progress = 0.0
    job.started_at = None
    job.finished_at = None
    session.add(job)
    session.commit()
    session.refresh(job)

    enqueue_job(job.id)
    logger.info("Завдання %d поставлено в чергу повторно", job_id)
    return _job_read(job, session)


@router.get("/{job_id}/events")
async def job_events(job_id: int, session: Session = Depends(get_session)):
    """SSE-стрім прогресу синтезу.

    Формат подій:
      event: progress  data: {job_id, done, total, percent, status}
      event: segment   data: {job_id, block_id, ordinal, status}
      event: finished  data: {job_id, output_path, status}
      event: cancelled data: {job_id}
    """
    job = session.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Завдання не знайдено")

    # Якщо вже завершено — одразу повертаємо фінальну подію
    if job.status in (JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED):
        async def _immediate():
            event_name = {
                JobStatus.DONE: "finished",
                JobStatus.FAILED: "error",
                JobStatus.CANCELLED: "cancelled",
            }[job.status]
            data = {"job_id": job_id, "status": job.status}
            if job.status == JobStatus.DONE:
                data["output_path"] = job.output_path
            yield f"event: {event_name}\ndata: {json.dumps(data)}\n\n"
        return StreamingResponse(_immediate(), media_type="text/event-stream")

    q = subscribe_job(job_id)

    async def _stream():
        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=30.0)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue

                event_name = event.pop("event", "message")
                yield f"event: {event_name}\ndata: {json.dumps(event)}\n\n"

                if event_name in ("finished", "cancelled", "error"):
                    break
        finally:
            unsubscribe_job(job_id, q)

    return StreamingResponse(_stream(), media_type="text/event-stream")


@router.post("/{job_id}/cancel")
async def cancel_job_endpoint(job_id: int, session: Session = Depends(get_session)):
    """Скасувати завдання."""
    job = session.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Завдання не знайдено")

    cancelled = await cancel_job(job_id)
    if not cancelled:
        raise HTTPException(
            status_code=409,
            detail=f"Завдання вже завершено зі статусом {job.status!r}",
        )
    return {"job_id": job_id, "status": "cancelled"}


def _job_artifacts(job: Job) -> dict[str, str]:
    """Файли, які реально створила збірка: «формат → шлях».

    Головний файл (`output_path`) додаємо самі: старі завдання, створені до
    появи переліку, не мають `artifacts`, і вони мають лишатися завантажуваними.
    """
    artifacts = {
        str(name): str(path)
        for name, path in (job.options_json or {}).get("artifacts", {}).items()
        if path
    }
    if job.output_path and not artifacts:
        artifacts[Path(job.output_path).suffix.lower().lstrip(".")] = job.output_path
    return artifacts


@router.get("/{job_id}/download")
def download_result(
    job_id: int,
    format: str | None = None,
    session: Session = Depends(get_session),
):
    """Завантажити готовий результат синтезу.

    `format` обирає ОДИН із реально створених файлів завдання. Раніше параметр
    змінював лише `Content-Type`, через що `?format=wav` віддавав MP3 із
    заголовком `audio/wav`; потім — давав 409, бо конвертації не було
    (docs/FRONTEND.md, знахідка 16.3). Тепер формати створюються на етапі
    збірки (`options.formats`), тож запитати можна саме той, який існує, а
    відсутній дає 404 зі списком доступних.
    """
    job = session.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Завдання не знайдено")
    if job.status != JobStatus.DONE:
        raise HTTPException(
            status_code=409,
            detail=f"Завдання ще не завершено (статус: {job.status!r})",
        )

    artifacts = _job_artifacts(job)
    available = sorted(artifacts)

    if format:
        requested = format.lower().lstrip(".")
        if requested not in artifacts:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Завдання не створило файл формату {requested!r}. "
                    f"Доступні: {', '.join(available) or 'жодного'}. "
                    "Формати задаються при створенні завдання (options.formats)."
                ),
            )
        chosen = Path(artifacts[requested])
    else:
        # Без параметра віддаємо головний файл: MP3, якщо він є, інакше M4B/WAV.
        primary = primary_format(tuple(available))
        chosen = Path(artifacts[primary]) if primary in artifacts else Path(job.output_path)

    if not chosen.is_file():
        raise HTTPException(status_code=404, detail="Файл результату не знайдено на диску")

    suffix = chosen.suffix.lower()
    return FileResponse(
        path=str(chosen),
        media_type=_MEDIA_TYPES.get(suffix, "application/octet-stream"),
        filename=chosen.name,
    )
