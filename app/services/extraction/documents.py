"""Витяг блоків із «офісних» форматів через markitdown (ADR-009).

Один конвертер закриває `.docx`, `.epub`, `.html`: markitdown перетворює їх
у Markdown, а структуру блоків (заголовки, цитати, списки, код) уже розбирає
той самий парсер, що й для `.md`. Так немає двох реалізацій однієї роботи.

Скан-PDF сюди не потрапляє: це окремий випадок у `extraction/pdf.py`.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from app.models import Block
from app.services.extraction.txt import markdown_to_blocks

logger = logging.getLogger(__name__)

# Розширення, які віддаємо markitdown
MARKITDOWN_SUFFIXES = frozenset({".docx", ".epub", ".html", ".htm"})

# markitdown додає на початок службовий блок метаданих у вигляді
# `**Title:** …`. Для аудіокниги це сміття: диктор читав би «Title, Тест,
# Language, uk». Прибираємо ЛИШЕ провідні рядки метаданих — решту тексту
# не чіпаємо.
_RE_METADATA_LINE = re.compile(
    r"^\*\*(?:Title|Author|Authors|Language|Identifier|Publisher|Date|"
    r"Subject|Description|Keywords|Creator|Contributor):\*\*",
    re.IGNORECASE,
)


class DocumentExtractionError(RuntimeError):
    """markitdown не зміг прочитати файл."""


def strip_metadata_header(markdown: str) -> str:
    """Прибрати провідний блок метаданих, який додає markitdown."""
    lines = markdown.split("\n")
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if not stripped or _RE_METADATA_LINE.match(stripped):
            index += 1
            continue
        break
    return "\n".join(lines[index:])


def extract_document(path: Path) -> list[Block]:
    """Витягти блоки з .docx / .epub / .html через markitdown.

    Raises:
        DocumentExtractionError: якщо конвертер не встановлено, файл зіпсований
            або тексту в ньому немає. Порожній результат без винятку означав би,
            що користувач отримає документ без блоків і не зрозуміє, чому.
    """
    try:
        from markitdown import MarkItDown
    except ImportError as exc:
        raise DocumentExtractionError("markitdown не встановлено: uv sync") from exc

    try:
        result = MarkItDown(enable_plugins=False).convert(str(path))
    except Exception as exc:
        raise DocumentExtractionError(
            f"Не вдалося прочитати {path.suffix} файл: {exc}"
        ) from exc

    markdown = strip_metadata_header(getattr(result, "text_content", "") or "")
    if not markdown.strip():
        raise DocumentExtractionError(
            f"У файлі {path.name} не знайдено тексту — можливо, він містить лише "
            "зображення або таблиці."
        )

    blocks = markdown_to_blocks(markdown)
    logger.info("%s: %d блоків через markitdown", path.name, len(blocks))
    return blocks
