"""Розділи аудіокниги: заголовки документа → межі розділів.

Заголовки вже визначаються при витягу (`heading_level`: 1–6 для PDF — за
розміром шрифту, для `.md` — за кількістю `#`), але досі нікуди не вели: блок
зберігав рівень, і на цьому все закінчувалось. Тут цей рівень перетворюється
на **план розділів**, який дає дві речі, яких бракувало (PLAN, етап 2
«Збірка книги»):

  * наскрізну нумерацію розділів у метаданих аудіо (M4B) і в назвах файлів
    при експорті zip по розділах;
  * можливість розділити готове аудіо на файли-розділи, не синтезуючи нічого
    вдруге — межі беруться з таймлайну сегментів.

**Який рівень вважати розділом.** Не «завжди H1»: у книзі, де розділи — це
H2 (а H1 — лише назва книги), правило «H1» дало б один розділ на всю книгу.
Тому розділом вважається **найвищий рівень, який у документі реально є**
(`min` наявних рівнів). Це узгоджується з тим, як людина бачить структуру:
найважливіший наявний заголовок і є межею розділу.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from app.models import BlockKind

# Скільки символів заголовка лишаємо в назві розділу (метадані, імена файлів)
MAX_TITLE_CHARS = 80

# Заголовок уже має номер? «5. Назва», «V. Назва», «Розділ 5», «Глава 12»,
# «Частина II» — тоді свій номер не додаємо, щоб не було «1. Розділ 5».
#
# Слово-префікс («Розділ», «Глава») саме по собі номером НЕ є: «Розділ А» —
# це назва без номера, і нумерувати її треба. Тому після слова обов'язково
# має йти число.
_NUMBERED_RE = re.compile(
    r"^\s*(?:\d+|[IVXLCDM]{1,7})\s*[.)]?\s+\S"
    r"|^\s*(?:розділ|глава|частина|книга|том|chapter|part|book)"
    r"\s+(?:\d+|[IVXLCDM]{1,7})\b",
    re.IGNORECASE,
)

_UNSAFE_FILENAME_RE = re.compile(r"[^\w\s.-]", re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")
# Провідний номер у назві («3. Зустріч») — у імені файлу він зайвий, бо номер
# уже стоїть у префіксі: «03-Зустріч.mp3», а не «03-3.-Зустріч.mp3».
_LEADING_NUMBER_RE = re.compile(r"^\s*(?:\d+|[IVXLCDM]{1,7})\s*[.)]\s+", re.IGNORECASE)


class ChapterSourceLike(Protocol):
    """Мінімум, потрібний від блоку, щоб побудувати план розділів.

    Протокол, а не конкретний клас: план будується і з ORM-блоку (API), і зі
    знімка `BlockTask` (воркер), і з простої структури в тестах — жоден із них
    не має тягнути за собою решту.
    """

    ordinal: int
    kind: BlockKind
    heading_level: int | None
    text: str


@dataclass(frozen=True)
class Chapter:
    """Один розділ книги — діапазон блоків, а не готові секунди аудіо.

    Секунди з'являються пізніше, коли відомо тривалості сегментів: той самий
    план використовується і для метаданих M4B, і для порізки на файли.
    """

    number: int | None      # наскрізний номер; None — вступ без номера
    title: str              # «3. Зустріч у Львові» — для метаданих і файлів
    heading_text: str       # текст заголовка без номера (він і озвучується)
    first_block: int        # ordinal першого блоку розділу (включно)
    last_block: int         # ordinal останнього блоку розділу (включно)
    heading_level: int | None = None


def chapter_heading_level(blocks: Sequence[ChapterSourceLike]) -> int | None:
    """Найвищий наявний рівень заголовка — він і є межею розділу.

    Повертає `None`, якщо заголовків немає зовсім.
    """
    levels = [
        block.heading_level
        for block in blocks
        if block.kind == BlockKind.HEADING and block.heading_level
    ]
    return min(levels) if levels else None


def clean_title(text: str, limit: int = MAX_TITLE_CHARS) -> str:
    """Заголовок → придатна назва: один рядок, без зайвих пробілів, недовга."""
    flat = _WHITESPACE_RE.sub(" ", (text or "").strip())
    if len(flat) > limit:
        flat = flat[: limit - 1].rstrip() + "…"
    return flat


def chapter_file_name(chapter: Chapter, suffix: str = "mp3") -> str:
    """Ім'я файлу розділу: «03-zustrich-u-lvovi.mp3»-подібне, але з Unicode.

    Кирилицю НЕ транслітеруємо: імена читає людина, а не URL. Небезпечні для
    файлової системи символи (`/`, `:`, `*`, лапки) замінюються на `-`.
    """
    prefix = f"{chapter.number:02d}" if chapter.number else "00"
    safe = _UNSAFE_FILENAME_RE.sub("", chapter.title).strip()
    # Номер уже в префіксі — у назві він був би вдруге («03-3.-Зустріч»)
    safe = _LEADING_NUMBER_RE.sub("", safe)
    safe = _WHITESPACE_RE.sub("-", safe).strip("-.")
    # Обрізаємо з запасом під «NN-» і розширення
    safe = safe[:60].strip("-.") or "rozdil"
    return f"{prefix}-{safe}.{suffix}"


def _numbered_title(number: int, heading_text: str) -> str:
    """Додати наскрізний номер, якщо його немає в самому заголовку."""
    if _NUMBERED_RE.match(heading_text):
        return heading_text
    return f"{number}. {heading_text}"


def build_chapter_plan(
    blocks: Sequence[ChapterSourceLike],
    document_title: str = "",
    intro_title: str = "Вступ",
) -> list[Chapter]:
    """Побудувати план розділів з озвучуваних блоків у порядку читання.

    `blocks` має бути тим самим списком, що піде в синтез (ті самі фільтри
    `speak` і непорожній текст), інакше межі розділів не збігатимуться з
    межами аудіо.

    Правила:
      * текст ДО першого заголовка — окремий розділ «Вступ» без номера
        (його немає, якщо документ починається з заголовка);
      * кожен заголовок найвищого наявного рівня починає новий розділ;
      * заголовки глибшого рівня лишаються тілом розділу;
      * якщо заголовків немає — один розділ із назвою документа.

    Розділ без жодного блоку неможливий за побудовою: заголовок сам є блоком,
    тож кожен розділ містить щонайменше свій заголовок.
    """
    ordered = list(blocks)
    if not ordered:
        return []

    chapter_level = chapter_heading_level(ordered)
    chapters: list[Chapter] = []

    if chapter_level is None:
        # Заголовків немає — вся книга один розділ. Назва з імені файлу:
        # без неї метадані M4B були б порожні.
        first, last = ordered[0].ordinal, ordered[-1].ordinal
        return [
            Chapter(
                number=None,
                title=clean_title(document_title) or "Аудіокнига",
                heading_text=clean_title(document_title),
                first_block=first,
                last_block=last,
            )
        ]

    # 1) Вступ — усе до першого заголовка рівня розділу
    chapter_starts = [
        index
        for index, block in enumerate(ordered)
        if block.kind == BlockKind.HEADING and block.heading_level == chapter_level
    ]

    if chapter_starts[0] > 0:
        head = ordered[: chapter_starts[0]]
        chapters.append(
            Chapter(
                number=None,
                title=clean_title(intro_title) or "Вступ",
                heading_text=clean_title(intro_title),
                first_block=head[0].ordinal,
                last_block=head[-1].ordinal,
            )
        )

    # 2) Розділи за заголовками
    for position, start in enumerate(chapter_starts):
        end = (
            chapter_starts[position + 1] - 1
            if position + 1 < len(chapter_starts)
            else len(ordered) - 1
        )
        section = ordered[start : end + 1]
        heading_text = clean_title(ordered[start].text) or f"Розділ {position + 1}"
        chapters.append(
            Chapter(
                number=position + 1,
                title=_numbered_title(position + 1, heading_text),
                heading_text=heading_text,
                first_block=section[0].ordinal,
                last_block=section[-1].ordinal,
                heading_level=chapter_level,
            )
        )

    return chapters


def chapter_titles(chapters: Sequence[Chapter]) -> list[str]:
    """Заголовки розділів — для метаданих аудіо."""
    return [chapter.title for chapter in chapters]
