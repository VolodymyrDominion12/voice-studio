"""Помилки сервісу бібліотеки.

Сервіс не знає про HTTP: він кидає ці винятки, а поверхня (JSON-API або
HTML-роутер) перекладає їх у статус-коди.
"""

from __future__ import annotations


class LibraryError(Exception):
    """Базова помилка сервісу документів."""


class UnsupportedFormatError(LibraryError):
    """Розширення не в білому списку (API: 415)."""


class ExtractorUnavailableError(LibraryError):
    """Формат дозволено, але екстрактор ще не реалізовано (API: 422)."""


class UploadTooLargeError(LibraryError):
    """Файл перевищує MAX_UPLOAD_MB (API: 413)."""


class DocumentNotFoundError(LibraryError):
    """Документа з таким id немає (API: 404)."""


class BlockNotFoundError(LibraryError):
    """Блоку з таким id немає в цьому документі (API: 404)."""
