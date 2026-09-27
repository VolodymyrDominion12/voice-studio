"""Сторінки та фрагменти HTML-інтерфейсу.

Фаза F0 (docs/FRONTEND.md, розд. 17): робоча стола, перегляд документа й
система — тобто «файл завантажується з браузера, документи й блоки видно».
Редактор із правкою блоків, прев'ю та чергою синтезу — фази F1–F2.

Маршрути:
  GET  /                              — робоча стола
  POST /ui/documents                  — завантаження файлу (multipart)
  GET  /documents/{id}                — документ із блоками
  POST /ui/documents/{id}/normalize   — перенормалізувати
  POST /ui/documents/{id}/delete      — видалити
  GET  /settings                      — стан системи й довідка про емоції
  GET  /ui/queue                      — фрагмент: стрічка завдань
  GET  /ui/health                     — фрагмент: чип стану
"""

from __future__ import annotations

import contextlib
import logging

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlmodel import Session, select

from app.db import get_session
from app.models import Job
from app.services.expression.profiles import all_profiles_dict
from app.services.library import documents as library
from app.services.library.errors import (
    DocumentNotFoundError,
    ExtractorUnavailableError,
    UnsupportedFormatError,
    UploadTooLargeError,
)
from app.services.system import health_snapshot
from app.ui import presentation
from app.ui.templating import templates

logger = logging.getLogger(__name__)
router = APIRouter(tags=["ui"])

# Скільки блоків віддавати на сторінку. Книга на 1000+ блоків не вміщується
# в один DOM — пагінація з самого початку (docs/FRONTEND.md, розд. 6.7).
BLOCKS_PER_PAGE = 200

# Повідомлення після переспрямування (без сесій і cookies: усе в URL)
FLASH_MESSAGES: dict[str, tuple[str, str]] = {
    "created":    ("Документ завантажено й розбито на блоки.", "ok"),
    "duplicate":  ("Цей файл уже завантажено — відкрито наявну копію.", "warn"),
    "normalized": ("Нормалізацію перезапущено.", "ok"),
    "deleted":    ("Документ видалено.", "ok"),
}

EMOTION_ORDER = (
    "neutral", "warm", "serious", "joyful",
    "excited", "sad", "tense", "questioning",
)


# ── Допоміжні функції ─────────────────────────────────────────────────────────

def _flash(kind: str | None) -> tuple[str, str] | None:
    return FLASH_MESSAGES.get(kind) if kind else None


def _error_text(exc: Exception) -> tuple[str, int]:
    """Помилка сервісу → (текст для людини, статус-код)."""
    if isinstance(exc, UnsupportedFormatError):
        return str(exc), 415
    if isinstance(exc, UploadTooLargeError):
        return str(exc), 413
    if isinstance(exc, ExtractorUnavailableError):
        return str(exc), 422
    if isinstance(exc, DocumentNotFoundError):
        return "Документ не знайдено.", 404
    logger.exception("Непередбачена помилка в UI-роутері")
    return "Внутрішня помилка сервера. Деталі — у логах.", 500


def _recent_jobs(session: Session, limit: int = 5) -> list[Job]:
    return list(session.exec(select(Job).order_by(Job.created_at.desc()).limit(limit)).all())


def _document_rows(session: Session) -> list[dict]:
    """Список документів разом із кількістю блоків у кожному."""
    return [
        {
            "document": document,
            "blocks": len(library.list_document_blocks(session, document.id or 0)),
        }
        for document in library.list_documents(session)
    ]


def _workspace_context(
    session: Session,
    flash: tuple[str, str] | None = None,
    error: str | None = None,
) -> dict:
    return {
        "active_nav": "workspace",
        "health": health_snapshot(),
        "rows": _document_rows(session),
        "jobs": _recent_jobs(session),
        "flash": flash,
        "error": error,
    }


# ── Робоча стола ──────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
def workspace(
    request: Request,
    session: Session = Depends(get_session),
    flash: str | None = None,
):
    """Головна: стан системи, завантаження файлу, список документів, черга."""
    return templates.TemplateResponse(
        request, "pages/workspace.html", _workspace_context(session, _flash(flash))
    )


@router.post("/ui/documents")
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
):
    """Прийняти файл із форми й переспрямувати на сторінку документа.

    Дублікат (за sha256) не створює новий документ — користувач має побачити
    явне повідомлення, інакше вирішить, що завантаження не спрацювало
    (docs/FRONTEND.md, розд. 5).
    """
    try:
        result = library.create_from_upload(session, file.filename or "", file.file)
    except Exception as exc:
        message, status = _error_text(exc)
        if status == 500:
            raise
        # Повертаємо ту саму сторінку з поясненням, а не голий JSON:
        # користувач працює в браузері.
        return templates.TemplateResponse(
            request,
            "pages/workspace.html",
            _workspace_context(session, error=message),
            status_code=status,
        )

    kind = "duplicate" if result.duplicate else "created"
    return RedirectResponse(url=f"/documents/{result.document.id}?flash={kind}", status_code=303)


# ── Сторінка документа ────────────────────────────────────────────────────────

@router.get("/documents/{doc_id}", response_class=HTMLResponse)
def document_page(
    request: Request,
    doc_id: int,
    session: Session = Depends(get_session),
    mode: str = "effective",
    offset: int = 0,
    flash: str | None = None,
):
    """Документ із блоками: перегляд у трьох режимах тексту.

    Фаза F0 — лише перегляд. Правка блоків, емоції та прев'ю — F1.
    """
    try:
        result = library.get_document_with_blocks(session, doc_id)
    except DocumentNotFoundError:
        return templates.TemplateResponse(
            request,
            "pages/not_found.html",
            {"active_nav": "workspace", "health": health_snapshot(), "doc_id": doc_id},
            status_code=404,
        )

    offset = max(0, offset)
    page_blocks = result.blocks[offset : offset + BLOCKS_PER_PAGE]
    views = presentation.build_block_views(page_blocks, mode=mode)

    return templates.TemplateResponse(
        request,
        "pages/document.html",
        {
            "active_nav": "workspace",
            "health": health_snapshot(),
            "document": result.document,
            "views": views,
            "stats": presentation.document_stats(result.blocks),
            "mode": mode,
            "offset": offset,
            "total": len(result.blocks),
            "has_prev": offset > 0,
            "has_next": offset + BLOCKS_PER_PAGE < len(result.blocks),
            "prev_offset": max(0, offset - BLOCKS_PER_PAGE),
            "next_offset": offset + BLOCKS_PER_PAGE,
            "flash": _flash(flash),
        },
    )


@router.post("/ui/documents/{doc_id}/normalize")
def renormalize(doc_id: int, session: Session = Depends(get_session)):
    """Перезапустити нормалізацію й повернутися на сторінку документа."""
    try:
        library.renormalize(session, doc_id)
    except DocumentNotFoundError:
        return RedirectResponse(url="/?flash=deleted", status_code=303)
    return RedirectResponse(url=f"/documents/{doc_id}?flash=normalized", status_code=303)


@router.post("/ui/documents/{doc_id}/delete")
def delete(doc_id: int, session: Session = Depends(get_session)):
    """Видалити документ і повернутися на робочу столу.

    Повторне видалення не є помилкою: користувач хотів, щоб документа не
    було, — і його немає.
    """
    with contextlib.suppress(DocumentNotFoundError):
        library.delete_document(session, doc_id)
    return RedirectResponse(url="/?flash=deleted", status_code=303)


# ── Система ───────────────────────────────────────────────────────────────────

@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    """Стан системи, довідка про емоції та стан фаз."""
    return templates.TemplateResponse(
        request,
        "pages/settings.html",
        {
            "active_nav": "settings",
            "health": health_snapshot(),
            "profiles": all_profiles_dict(),
            "emotion_order": EMOTION_ORDER,
        },
    )


# ── Фрагменти (заготовки під htmx-оновлення) ──────────────────────────────────

@router.get("/ui/health", response_class=HTMLResponse)
def health_fragment(request: Request):
    """Чип стану системи — фрагмент без каркаса сторінки."""
    return templates.TemplateResponse(
        request, "partials/health_chip.html", {"health": health_snapshot()}
    )


@router.get("/ui/queue", response_class=HTMLResponse)
def queue_fragment(request: Request, session: Session = Depends(get_session)):
    """Стрічка останніх завдань — фрагмент."""
    return templates.TemplateResponse(
        request, "partials/job_strip.html", {"jobs": _recent_jobs(session)}
    )


@router.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    """Порожня відповідь, щоб браузер не логував 404 на кожній сторінці."""
    return Response(status_code=204)
