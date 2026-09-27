"""Керування документами як сервіс.

Спільна логіка для JSON-API (`/api/v1`) і HTML-інтерфейсу (`/ui`).
Раніше жила прямо в `app/api/documents.py` — винесено сюди, щоб дві поверхні
не розʼїхалися двома реалізаціями однієї дії (docs/FRONTEND.md, розд. 2).

Шар не знає нічого про HTTP: кидає власні винятки, а поверхня перекладає їх
у статус-коди.
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from sqlmodel import Session, select

from app.config import Settings, get_settings
from app.models import Block, Document, DocumentStatus, utc_now
from app.services.extraction.txt import extract_text_file
from app.services.library.errors import (
    DocumentNotFoundError,
    ExtractorUnavailableError,
    UnsupportedFormatError,
    UploadTooLargeError,
)
from app.services.normalize.uk import normalize

logger = logging.getLogger(__name__)


# ── Результати ────────────────────────────────────────────────────────────────

@dataclass
class DocumentResult:
    """Документ разом із блоками + чи це був дублікат за sha256."""
    document: Document
    blocks: list[Block] = field(default_factory=list)
    duplicate: bool = False


# ── Допоміжні функції ─────────────────────────────────────────────────────────

_MIME_BY_SUFFIX: dict[str, str] = {
    ".txt":      "text/plain",
    ".md":       "text/markdown",
    ".markdown": "text/markdown",
    ".pdf":      "application/pdf",
    ".docx":     "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".epub":     "application/epub+zip",
    ".html":     "text/html",
    ".htm":      "text/html",
}

# Екстрактори, які вже реалізовано
_IMPLEMENTED_SUFFIXES = frozenset({".txt", ".md", ".markdown"})


def mime_from_suffix(suffix: str) -> str:
    """MIME-тип за розширенням (типово application/octet-stream)."""
    return _MIME_BY_SUFFIX.get(suffix.lower(), "application/octet-stream")


def sha256_of(path: Path) -> str:
    """SHA-256 файлу — ключ дедуплікації документів."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_upload_name(filename: str) -> str:
    """Безпечне імʼя файлу для зберігання на диску.

    Прибирає шляхи (`../`), керуючі символи й обмежує довжину. Оригінальна
    назва зберігається в БД окремо — на диску вона не потрібна
    (docs/FRONTEND.md, знахідка 16.8a).
    """
    base = Path(filename or "").name or "upload"
    base = re.sub(r"[^\w.\- ]+", "_", base, flags=re.UNICODE).strip(" .") or "upload"
    return base[:120]


def check_suffix(filename: str, settings: Settings | None = None) -> str:
    """Перевірити розширення; повернути його ж у нижньому регістрі."""
    settings = settings or get_settings()
    suffix = Path(filename or "").suffix.lower()
    if suffix not in settings.allowed_suffixes:
        raise UnsupportedFormatError(
            f"Формат {suffix!r} не підтримується. Дозволено: {settings.allowed_extensions}"
        )
    return suffix


def save_upload(filename: str, stream: BinaryIO, settings: Settings | None = None) -> Path:
    """Зберегти завантажений файл у data/uploads/ із перевіркою розміру.

    Розмір перевіряється під час запису (MAX_UPLOAD_MB), інакше 78-мегабайтний
    файл спершу ліг би на диск, а вже потім був би відкинутий.
    """
    settings = settings or get_settings()
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)

    destination = settings.uploads_dir / f"{uuid.uuid4().hex[:8]}_{safe_upload_name(filename)}"
    limit_bytes = settings.max_upload_mb * 1024 * 1024
    written = 0

    try:
        with destination.open("wb") as out:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    break
                written += len(chunk)
                if written > limit_bytes:
                    raise UploadTooLargeError(
                        f"Файл більший за ліміт {settings.max_upload_mb} МБ"
                    )
                out.write(chunk)
    except UploadTooLargeError:
        destination.unlink(missing_ok=True)
        raise
    except Exception:
        destination.unlink(missing_ok=True)
        raise

    return destination


def extract_blocks(path: Path, suffix: str) -> list[Block]:
    """Витягти блоки з файлу. Каскад екстракторів за розширенням (ADR-009)."""
    if suffix in _IMPLEMENTED_SUFFIXES:
        return extract_text_file(path)

    raise ExtractorUnavailableError(
        f"Екстрактор для {suffix!r} ще не реалізований (заплановано в Етапі 2). "
        "Завантажте .txt або .md файл."
    )


# ── Операції з документами ────────────────────────────────────────────────────

def create_from_upload(
    session: Session,
    filename: str,
    stream: BinaryIO,
    settings: Settings | None = None,
) -> DocumentResult:
    """Завантажити файл → документ із блоками.

    Той самий файл (за sha256) удруге не створює новий документ: повертається
    наявний із `duplicate=True`. UI зобовʼязаний це озвучити користувачу
    (docs/FRONTEND.md, розд. 5).
    """
    settings = settings or get_settings()
    suffix = check_suffix(filename, settings)
    path = save_upload(filename, stream, settings)

    return create_from_path(session, filename, path, suffix, settings)


def create_from_path(
    session: Session,
    filename: str,
    path: Path,
    suffix: str | None = None,
    settings: Settings | None = None,
) -> DocumentResult:
    """Створити документ із файлу, який уже лежить на диску."""
    suffix = suffix or Path(filename).suffix.lower()
    sha = sha256_of(path)

    existing = session.exec(select(Document).where(Document.sha256 == sha)).first()
    if existing:
        logger.info("Документ уже є (sha256=%s…), повертаємо id=%s", sha[:8], existing.id)
        return DocumentResult(
            document=existing,
            blocks=list_document_blocks(session, existing.id or 0),
            duplicate=True,
        )

    document = Document(
        filename=filename,
        sha256=sha,
        mime=mime_from_suffix(suffix),
        source_path=str(path),
        status=DocumentStatus.EXTRACTING,
    )
    session.add(document)
    session.commit()
    session.refresh(document)

    try:
        raw_blocks = extract_blocks(path, suffix)
    except ExtractorUnavailableError:
        document.status = DocumentStatus.ERROR
        session.add(document)
        session.commit()
        raise
    except Exception as exc:
        document.status = DocumentStatus.ERROR
        session.add(document)
        session.commit()
        raise ExtractorUnavailableError(f"Помилка витягу тексту: {exc}") from exc

    blocks: list[Block] = []
    for block in raw_blocks:
        block.document_id = document.id
        # Нормалізуємо лише те, що буде озвучено: код і таблиці не читаються.
        block.text_normalized = normalize(block.text_raw) if block.speak else ""
        session.add(block)
        blocks.append(block)

    document.status = DocumentStatus.READY
    document.updated_at = utc_now()
    session.add(document)
    session.commit()

    logger.info("Документ %s створено: %d блоків із %s", document.id, len(blocks), filename)
    return DocumentResult(document=document, blocks=blocks, duplicate=False)


def list_documents(session: Session) -> list[Document]:
    """Усі документи, найновіші — вгорі."""
    return list(session.exec(select(Document).order_by(Document.created_at.desc())).all())


def get_document(session: Session, document_id: int) -> Document:
    """Документ за id або DocumentNotFoundError."""
    document = session.get(Document, document_id)
    if not document:
        raise DocumentNotFoundError(f"Документ {document_id} не знайдено")
    return document


def list_document_blocks(session: Session, document_id: int) -> list[Block]:
    """Блоки документа у порядку читання."""
    return list(
        session.exec(
            select(Block).where(Block.document_id == document_id).order_by(Block.ordinal)
        ).all()
    )


def get_document_with_blocks(session: Session, document_id: int) -> DocumentResult:
    """Документ разом із блоками."""
    document = get_document(session, document_id)
    return DocumentResult(document=document, blocks=list_document_blocks(session, document_id))


def renormalize(session: Session, document_id: int) -> DocumentResult:
    """Перезапустити нормалізацію всіх озвучуваних блоків.

    Не чіпає `text_edited`: повторна нормалізація стосується лише
    `text_normalized` (docs/FRONTEND.md, розд. 6.3).
    """
    document = get_document(session, document_id)
    blocks = list_document_blocks(session, document_id)

    changed = 0
    for block in blocks:
        if not block.speak:
            continue
        normalized = normalize(block.text_raw)
        if normalized != block.text_normalized:
            block.text_normalized = normalized
            session.add(block)
            changed += 1

    document.updated_at = utc_now()
    session.add(document)
    session.commit()

    logger.info("Документ %s: перенормалізовано %d блоків", document_id, changed)
    return get_document_with_blocks(session, document_id)


def delete_document(session: Session, document_id: int, remove_files: bool = True) -> None:
    """Видалити документ, його блоки та (опційно) вихідний файл із диска."""
    document = get_document(session, document_id)
    source = Path(document.source_path) if document.source_path else None

    for block in list_document_blocks(session, document_id):
        session.delete(block)
    session.delete(document)
    session.commit()

    if remove_files and source:
        try:
            source.unlink(missing_ok=True)
        except OSError as exc:  # файл міг зникнути поза застосунком
            logger.warning("Не вдалося видалити %s: %s", source, exc)

    logger.info("Документ %s видалено", document_id)


def copy_to_uploads(path: Path, settings: Settings | None = None) -> Path:
    """Скопіювати зовнішній файл у теку завантажень (для скриптів і тестів)."""
    settings = settings or get_settings()
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    destination = settings.uploads_dir / f"{uuid.uuid4().hex[:8]}_{safe_upload_name(path.name)}"
    shutil.copyfile(path, destination)
    return destination
