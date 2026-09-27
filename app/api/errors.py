"""Переклад помилок сервісів у HTTPException.

Сервісний шар не знає про HTTP: він кидає власні винятки. Цей модуль — єдине
місце, де вони стають статус-кодами, тож `documents`, `jobs` і `presets`
поводяться однаково.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException

from app.services.library.errors import (
    BlockNotFoundError,
    DocumentNotFoundError,
    ExtractorUnavailableError,
    ScanPdfError,
    UnsupportedFormatError,
    UploadTooLargeError,
)

logger = logging.getLogger(__name__)

# Помилка сервісу → статус-код. Типи не перетинаються, порядок не важливий.
STATUS_BY_ERROR: tuple[tuple[type[Exception], int], ...] = (
    (UnsupportedFormatError, 415),
    (UploadTooLargeError, 413),
    (ExtractorUnavailableError, 422),
    (ScanPdfError, 422),
    (DocumentNotFoundError, 404),
    (BlockNotFoundError, 404),
)


def to_http_error(exc: Exception) -> HTTPException:
    """Помилка сервісу → HTTPException.

    Невідомі помилки не ковтаємо: вони мають стати 500 і потрапити в лог,
    а не вдавати помилку користувача.
    """
    for error_type, status in STATUS_BY_ERROR:
        if isinstance(exc, error_type):
            return HTTPException(status_code=status, detail=str(exc))

    logger.exception("Непередбачена помилка сервісу")
    return HTTPException(status_code=500, detail="Внутрішня помилка сервера")
