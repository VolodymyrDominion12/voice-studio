"""API-роутер: документи та блоки.

POST /api/v1/documents      — завантажити файл
GET  /api/v1/documents      — список
GET  /api/v1/documents/{id} — документ із блоками
PATCH /api/v1/documents/{id}/blocks/{bid} — оновити текст/емоцію
POST /api/v1/documents/{id}/normalize     — перезапустити нормалізацію
"""

from __future__ import annotations

import hashlib
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlmodel import Session, select

from app.config import get_settings
from app.db import get_session
from app.models import (
    Block, BlockUpdate, BlockRead,
    Document, DocumentRead, DocumentStatus, DocumentWithBlocks,
)
from app.services.extraction.txt import extract_text_file
from app.services.normalize.uk import normalize

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/documents", tags=["documents"])


# ── Допоміжні функції ──────────────────────────────────────────────────────────

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _mime_from_suffix(suffix: str) -> str:
    return {
        ".txt":      "text/plain",
        ".md":       "text/markdown",
        ".markdown": "text/markdown",
        ".pdf":      "application/pdf",
        ".docx":     "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".epub":     "application/epub+zip",
        ".html":     "text/html",
        ".htm":      "text/html",
    }.get(suffix.lower(), "application/octet-stream")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── Ендпоінти ─────────────────────────────────────────────────────────────────

@router.post("", response_model=DocumentWithBlocks, status_code=201)
async def upload_document(
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
):
    """Завантажити файл і витягти блоки тексту."""
    settings = get_settings()
    suffix = Path(file.filename or "").suffix.lower()

    if suffix not in settings.allowed_suffixes:
        raise HTTPException(
            status_code=415,
            detail=f"Формат {suffix!r} не підтримується. Дозволено: {settings.allowed_extensions}",
        )

    # Зберегти файл
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    dest = settings.uploads_dir / (file.filename or "upload")
    with dest.open("wb") as out:
        shutil.copyfileobj(file.file, out)

    sha = _sha256(dest)

    # Перевірка дублікату
    existing = session.exec(select(Document).where(Document.sha256 == sha)).first()
    if existing:
        blocks = session.exec(select(Block).where(Block.document_id == existing.id)).all()
        return DocumentWithBlocks.model_validate(existing, update={"blocks": [BlockRead.model_validate(b) for b in blocks]})

    # Створити запис документа
    doc = Document(
        filename=file.filename or dest.name,
        sha256=sha,
        mime=_mime_from_suffix(suffix),
        source_path=str(dest),
        status=DocumentStatus.EXTRACTING,
    )
    session.add(doc)
    session.commit()
    session.refresh(doc)

    # Витяг блоків
    try:
        raw_blocks = _extract_blocks(dest, suffix)
    except Exception as exc:
        doc.status = DocumentStatus.ERROR
        session.add(doc)
        session.commit()
        raise HTTPException(status_code=422, detail=f"Помилка витягу тексту: {exc}") from exc

    # Нормалізація і збереження
    db_blocks: list[Block] = []
    for b in raw_blocks:
        b.document_id = doc.id
        b.text_normalized = normalize(b.text_raw) if b.speak else ""
        session.add(b)
        db_blocks.append(b)

    doc.status = DocumentStatus.READY
    doc.updated_at = _now_utc()
    session.add(doc)
    session.commit()
    session.refresh(doc)

    logger.info("Документ %d створено: %d блоків із %s", doc.id, len(db_blocks), file.filename)
    block_reads = [BlockRead.model_validate(b) for b in db_blocks]
    return DocumentWithBlocks.model_validate(doc, update={"blocks": block_reads})


def _extract_blocks(path: Path, suffix: str) -> list[Block]:
    """Вибрати екстрактор за розширенням."""
    if suffix in (".txt", ".md", ".markdown"):
        return extract_text_file(path)
    # PDF, EPUB, DOCX — планується в Етапі 2 (markitdown/PyMuPDF)
    raise NotImplementedError(
        f"Екстрактор для {suffix!r} ще не реалізований (заплановано в Етапі 2). "
        "Завантажте .txt або .md файл."
    )


@router.get("", response_model=list[DocumentRead])
def list_documents(session: Session = Depends(get_session)):
    """Список усіх документів (без блоків)."""
    docs = session.exec(select(Document).order_by(Document.created_at.desc())).all()
    return [DocumentRead.model_validate(d) for d in docs]


@router.get("/{doc_id}", response_model=DocumentWithBlocks)
def get_document(doc_id: int, session: Session = Depends(get_session)):
    """Документ із усіма блоками."""
    doc = session.get(Document, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Документ не знайдено")
    blocks = session.exec(
        select(Block).where(Block.document_id == doc_id).order_by(Block.ordinal)
    ).all()
    return DocumentWithBlocks.model_validate(
        doc, update={"blocks": [BlockRead.model_validate(b) for b in blocks]}
    )


@router.patch("/{doc_id}/blocks/{block_id}", response_model=BlockRead)
def update_block(
    doc_id: int,
    block_id: int,
    update: BlockUpdate,
    session: Session = Depends(get_session),
):
    """Оновити текст, емоцію або speak-прапорець блоку."""
    block = session.get(Block, block_id)
    if not block or block.document_id != doc_id:
        raise HTTPException(status_code=404, detail="Блок не знайдено")

    data = update.model_dump(exclude_unset=True)
    for key, value in data.items():
        setattr(block, key, value)

    session.add(block)
    session.commit()
    session.refresh(block)
    return BlockRead.model_validate(block)


@router.post("/{doc_id}/normalize", response_model=DocumentWithBlocks)
def renormalize_document(doc_id: int, session: Session = Depends(get_session)):
    """Перезапустити нормалізацію для всіх блоків документа."""
    doc = session.get(Document, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Документ не знайдено")

    blocks = session.exec(
        select(Block).where(Block.document_id == doc_id)
    ).all()

    for block in blocks:
        if block.speak:
            block.text_normalized = normalize(block.text_raw)
            session.add(block)

    doc.updated_at = _now_utc()
    session.add(doc)
    session.commit()

    return get_document(doc_id, session)
