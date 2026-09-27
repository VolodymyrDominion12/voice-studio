"""Витяг блоків із PDF (PyMuPDF).

Дві речі, які тут принципові (ARCHITECTURE §2.1, ADR-009):

1. **Скан ≠ PDF.** Якщо з файлу витягнуто менш ніж ~100 символів на сторінку,
   це майже напевно скан без текстового шару. Тоді ми повертаємо явну помилку
   з підказкою про OCR, а не сміттєвий текст, який потім озвучиться як абракадабра.
2. **Заголовки треба вгадати.** PyMuPDF віддає абзаци без структури, тому
   розмір шрифту порівнюємо з медіаною по документу: помітно більший і короткий
   рядок — це заголовок. Без цього книжка втратила б розділи, а разом із ними
   й паузи між ними.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from pathlib import Path

from app.models import Block, BlockKind

logger = logging.getLogger(__name__)

# Поріг скана: символів на сторінку (ARCHITECTURE §2.1)
MIN_CHARS_PER_PAGE = 100

# Сигнал 1: на сторінці немає тексту взагалі. Менш ніж 20 символів — це не
# текст, а номер сторінки або колонтитул.
_SCAN_CHARS_PER_PAGE = 20

# Сигнал 2: середнє стає значущим лише на довгому документі. На 1–2 сторінках
# «менш ніж 100 символів на сторінку» — це просто короткий цифровий документ,
# а не скан (перевірено на PDF із трьома рядками: 82 символи на 1 сторінку).
_SCAN_MIN_PAGES = 3

# У скільки разів шрифт має бути більшим за медіану, щоб вважати це заголовком
_HEADING_RATIOS = ((1.55, 1), (1.35, 2), (1.18, 3))

# Перенос слова в кінці рядка: «сло-\nво» → «слово»
_RE_HYPHEN_BREAK = re.compile(r"(\w)-\n(\w)")
# Одиночний перенос усередині абзацу — це перенос рядка, а не новий абзац
_RE_SOFT_BREAK = re.compile(r"(?<![.!?:;»])\n(?=\w)")


class PdfError(RuntimeError):
    """PDF не вдалося прочитати."""


class PdfScanDetectedError(PdfError):
    """У PDF немає текстового шару — потрібен OCR."""


def _import_pymupdf():
    """PyMuPDF: сучасне імʼя `pymupdf`, старе — `fitz`."""
    try:
        import pymupdf  # type: ignore[import-not-found]
        return pymupdf
    except ImportError:  # старіші версії
        import fitz  # type: ignore[import-not-found]
        return fitz


def page_count(path: Path) -> int:
    """Кількість сторінок у PDF (0, якщо прочитати не вдалося)."""
    pymupdf = _import_pymupdf()
    try:
        with pymupdf.open(str(path)) as document:
            return document.page_count
    except Exception as exc:
        logger.warning("Не вдалося прочитати %s: %s", path.name, exc)
        return 0


def _median_font_size(document) -> float:
    """Типовий розмір основного тексту — точка відліку для пошуку заголовків.

    Беремо не медіану, а **моду** (найчастіший розмір): у книзі основний текст
    набирають одним кеглем, і саме він домінує за кількістю фрагментів. Медіана
    на короткому документі з двох рядків дає середину між заголовком і текстом,
    через що рівень заголовка визначається неточно.
    """
    counter: Counter[float] = Counter()
    for page in document:
        for block in page.get_text("dict").get("blocks", []):
            if block.get("type") != 0:  # 0 = текст, 1 = зображення
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    text = span.get("text", "")
                    size = round(float(span.get("size", 0)), 1)
                    if text.strip() and size > 0:
                        # Важимо за кількістю символів: один довгий абзац
                        # важливіший за десяток коротких підписів.
                        counter[size] += len(text)

    if not counter:
        return 0.0
    # Найчастіший за сумарною довжиною; за рівності — більший кегль
    return counter.most_common(1)[0][0]


def _block_max_font_size(block: dict) -> float:
    sizes = [
        float(span.get("size", 0))
        for line in block.get("lines", [])
        for span in line.get("spans", [])
        if span.get("text", "").strip()
    ]
    return max(sizes) if sizes else 0.0


def _heading_level(size: float, median: float) -> int | None:
    """Рівень заголовка за співвідношенням розмірів, або None."""
    if median <= 0 or size <= 0:
        return None
    ratio = size / median
    for threshold, level in _HEADING_RATIOS:
        if ratio >= threshold:
            return level
    return None


def _clean_paragraph(text: str) -> str:
    """Прибрати переноси рядків усередині абзацу, зшити розірвані слова."""
    text = _RE_HYPHEN_BREAK.sub(r"\1\2", text)
    text = _RE_SOFT_BREAK.sub(" ", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def extract_pdf(path: Path, min_chars_per_page: int = MIN_CHARS_PER_PAGE) -> list[Block]:
    """Витягти блоки з PDF.

    Raises:
        PdfScanDetectedError: якщо тексту майже немає (скан без OCR).
        PdfError:             якщо файл не читається взагалі.
    """
    pymupdf = _import_pymupdf()

    try:
        document = pymupdf.open(str(path))
    except Exception as exc:
        raise PdfError(f"Не вдалося відкрити PDF: {exc}") from exc

    with document:
        if document.needs_pass:
            raise PdfError("PDF захищено паролем — озвучити його не можна без пароля")

        median = _median_font_size(document)
        blocks: list[Block] = []
        ordinal = 0
        total_chars = 0

        for page in document:
            for raw in page.get_text("dict").get("blocks", []):
                if raw.get("type") != 0:
                    continue  # зображення всередині PDF не озвучуємо

                text = _clean_paragraph(
                    "\n".join(
                        "".join(span.get("text", "") for span in line.get("spans", []))
                        for line in raw.get("lines", [])
                    )
                )
                if not text:
                    continue

                total_chars += len(text)
                size = _block_max_font_size(raw)
                level = _heading_level(size, median) if len(text) <= 120 else None

                if level is not None:
                    blocks.append(Block(
                        ordinal=ordinal,
                        kind=BlockKind.HEADING,
                        heading_level=level,
                        text_raw=text,
                        text_normalized=text,
                        speak=True,
                    ))
                else:
                    blocks.append(Block(
                        ordinal=ordinal,
                        kind=BlockKind.PARAGRAPH,
                        text_raw=text,
                        text_normalized=text,
                        speak=True,
                    ))
                ordinal += 1

        pages = document.page_count or 1
        per_page = total_chars / pages

        # Два незалежні сигнали скана. Одного «менш ніж 100 символів на
        # сторінку» мало: так відкидався б короткий цифровий PDF
        # (наприклад, записка на три рядки).
        looks_like_scan = (
            per_page < _SCAN_CHARS_PER_PAGE
            or (pages >= _SCAN_MIN_PAGES and per_page < min_chars_per_page)
        )
        if looks_like_scan:
            raise PdfScanDetectedError(
                f"У PDF майже немає тексту ({total_chars} символів на {pages} стор., "
                f"{per_page:.0f} на сторінку) — схоже, це скан без текстового шару. "
                "Потрібен OCR (не в MVP, див. docs/PLAN.md, етап 2)."
            )

        logger.info("PDF %s: %d блоків із %d сторінок", path.name, len(blocks), pages)
        return blocks
