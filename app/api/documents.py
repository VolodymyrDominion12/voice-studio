"""API-роутер: документи та блоки.

POST   /api/v1/documents                   — завантажити файл
GET    /api/v1/documents                   — список
GET    /api/v1/documents/{id}              — документ із блоками
PATCH  /api/v1/documents/{id}/blocks/{bid} — оновити текст/емоцію блоку
PATCH  /api/v1/documents/{id}/blocks       — масова правка блоків
POST   /api/v1/documents/{id}/normalize    — перезапустити нормалізацію
DELETE /api/v1/documents/{id}              — видалити документ

Логіка живе в app/services/library/ — цей файл лише розбирає запит,
перекладає помилки сервісу в HTTP-коди й серіалізує відповідь.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlmodel import Session

from app.db import get_session
from app.models import BlockRead, BlockUpdate, DocumentRead, DocumentWithBlocks
from app.services.library import blocks as blocks_service
from app.services.library import documents as library
from app.services.library.documents import DocumentResult
from app.services.library.errors import (
    BlockNotFoundError,
    DocumentNotFoundError,
    ExtractorUnavailableError,
    UnsupportedFormatError,
    UploadTooLargeError,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/documents", tags=["documents"])

# Помилка сервісу → статус-код. Порядок не важливий: типи не перетинаються.
_STATUS_BY_ERROR: tuple[tuple[type[Exception], int], ...] = (
    (UnsupportedFormatError, 415),
    (UploadTooLargeError, 413),
    (ExtractorUnavailableError, 422),
    (DocumentNotFoundError, 404),
    (BlockNotFoundError, 404),
)


# ── Допоміжні функції ─────────────────────────────────────────────────────────

def _to_response(result: DocumentResult) -> DocumentWithBlocks:
    """DocumentResult → схема відповіді."""
    return DocumentWithBlocks.model_validate(
        result.document,
        update={"blocks": [BlockRead.model_validate(b) for b in result.blocks]},
    )


def _http_error(exc: Exception) -> HTTPException:
    """Перекласти помилку сервісу в HTTPException.

    Невідомі помилки не ковтаємо: вони мають стати 500 і потрапити в лог,
    а не вдавати помилку користувача.
    """
    for error_type, status in _STATUS_BY_ERROR:
        if isinstance(exc, error_type):
            return HTTPException(status_code=status, detail=str(exc))
    logger.exception("Непередбачена помилка сервісу бібліотеки")
    raise exc


# ── Схеми масової правки ──────────────────────────────────────────────────────

class BlockBatchItem(BlockUpdate):
    """Елемент масової правки: який блок і що в ньому змінити."""
    block_id: int


class BlockBatchUpdate(BaseModel):
    """Тіло масової правки блоків."""
    updates: list[BlockBatchItem]


# ── Ендпоінти ─────────────────────────────────────────────────────────────────

@router.post("", response_model=DocumentWithBlocks, status_code=201)
async def upload_document(
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
):
    """Завантажити файл і витягти блоки тексту.

    Повторне завантаження того самого файлу (за sha256) повертає наявний
    документ, а не створює дубль.
    """
    try:
        result = library.create_from_upload(session, file.filename or "", file.file)
    except Exception as exc:
        raise _http_error(exc) from exc
    return _to_response(result)


@router.get("", response_model=list[DocumentRead])
def list_documents(session: Session = Depends(get_session)):
    """Список усіх документів (без блоків)."""
    return [DocumentRead.model_validate(d) for d in library.list_documents(session)]


@router.get("/{doc_id}", response_model=DocumentWithBlocks)
def get_document(doc_id: int, session: Session = Depends(get_session)):
    """Документ із усіма блоками."""
    try:
        result = library.get_document_with_blocks(session, doc_id)
    except Exception as exc:
        raise _http_error(exc) from exc
    return _to_response(result)


@router.patch("/{doc_id}/blocks/{block_id}", response_model=BlockRead)
def update_block(
    doc_id: int,
    block_id: int,
    update: BlockUpdate,
    session: Session = Depends(get_session),
):
    """Оновити текст, емоцію або speak-прапорець блоку."""
    try:
        block = blocks_service.update_block(
            session, doc_id, block_id, update.model_dump(exclude_unset=True)
        )
    except Exception as exc:
        raise _http_error(exc) from exc
    return BlockRead.model_validate(block)


@router.patch("/{doc_id}/blocks", response_model=list[BlockRead])
def update_blocks(
    doc_id: int,
    payload: BlockBatchUpdate,
    session: Session = Depends(get_session),
):
    """Масова правка блоків однією транзакцією.

    Потрібна редактору для розмітки емоцій на багатьох блоках одразу
    (docs/FRONTEND.md, знахідка 16.5).
    """
    if not payload.updates:
        raise HTTPException(status_code=422, detail="Порожній список updates")

    changes = [
        {"block_id": item.block_id, **item.model_dump(exclude_unset=True, exclude={"block_id"})}
        for item in payload.updates
    ]
    try:
        updated = blocks_service.update_blocks(session, doc_id, changes)
    except Exception as exc:
        raise _http_error(exc) from exc
    return [BlockRead.model_validate(b) for b in updated]


@router.post("/{doc_id}/normalize", response_model=DocumentWithBlocks)
def renormalize_document(doc_id: int, session: Session = Depends(get_session)):
    """Перезапустити нормалізацію для всіх блоків документа."""
    try:
        result = library.renormalize(session, doc_id)
    except Exception as exc:
        raise _http_error(exc) from exc
    return _to_response(result)


@router.delete("/{doc_id}", status_code=204)
def delete_document(doc_id: int, session: Session = Depends(get_session)):
    """Видалити документ разом із блоками та вихідним файлом."""
    try:
        library.delete_document(session, doc_id)
    except Exception as exc:
        raise _http_error(exc) from exc
