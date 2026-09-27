"""Сервіс бібліотеки: документи та блоки.

Спільна логіка для JSON-API (`/api/v1`) і HTML-інтерфейсу (`/ui`).
Імпортувати звідси, а не з підмодулів:

    from app.services.library import documents, blocks
    from app.services.library.errors import DocumentNotFoundError
"""

from __future__ import annotations

from app.services.library import blocks, documents
from app.services.library.documents import DocumentResult
from app.services.library.errors import (
    BlockNotFoundError,
    DocumentNotFoundError,
    ExtractorUnavailableError,
    LibraryError,
    UnsupportedFormatError,
    UploadTooLargeError,
)

__all__ = [
    "BlockNotFoundError",
    "DocumentNotFoundError",
    "DocumentResult",
    "ExtractorUnavailableError",
    "LibraryError",
    "UnsupportedFormatError",
    "UploadTooLargeError",
    "blocks",
    "documents",
]
