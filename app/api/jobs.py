"""API-роутер: завдання синтезу + SSE-прогрес.

POST /api/v1/jobs               — створити завдання
GET  /api/v1/jobs               — список завдань
GET  /api/v1/jobs/{id}/events   — SSE: прогрес у реальному часі
POST /api/v1/jobs/{id}/cancel   — скасувати
GET  /api/v1/jobs/{id}/download — завантажити результат
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from sqlmodel import Session, select

from app.db import get_session
from app.models import Job, JobCreate, JobRead, JobStatus
from app.worker.queue import cancel_job, enqueue_job, subscribe_job, unsubscribe_job

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])


@router.post("", response_model=JobRead, status_code=201)
def create_job(payload: JobCreate, session: Session = Depends(get_session)):
    """Створити завдання синтезу та поставити в чергу воркера."""
    job = Job(
        document_id=payload.document_id,
        engine_id=payload.engine_id,
        voice_id=payload.voice_id,
        options_json=payload.options,
        status=JobStatus.QUEUED,
    )
    session.add(job)
    session.commit()
    session.refresh(job)

    enqueue_job(job.id)
    logger.info("Завдання %d поставлено в чергу (doc=%d)", job.id, job.document_id)
    return JobRead.model_validate(job)


@router.get("", response_model=list[JobRead])
def list_jobs(session: Session = Depends(get_session)):
    """Список усіх завдань (остання черга — вгорі)."""
    jobs = session.exec(select(Job).order_by(Job.created_at.desc())).all()
    return [JobRead.model_validate(j) for j in jobs]


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
                except asyncio.TimeoutError:
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


@router.get("/{job_id}/download")
def download_result(
    job_id: int,
    format: str = "mp3",
    session: Session = Depends(get_session),
):
    """Завантажити готовий результат синтезу."""
    job = session.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Завдання не знайдено")
    if job.status != JobStatus.DONE:
        raise HTTPException(
            status_code=409,
            detail=f"Завдання ще не завершено (статус: {job.status!r})",
        )

    output = Path(job.output_path)
    if not output.exists():
        raise HTTPException(status_code=404, detail="Файл результату не знайдено")

    media_types = {
        "mp3": "audio/mpeg",
        "wav": "audio/wav",
        "m4b": "audio/mp4",
    }
    return FileResponse(
        path=str(output),
        media_type=media_types.get(format, "application/octet-stream"),
        filename=output.name,
    )
