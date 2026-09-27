"""Моделі даних SQLModel для Voice Studio.

Таблиці: Document, Block, Segment, Job, Voice, Preset.
Усі часові мітки — UTC, зберігаються без часового поясу (SQLite не підтримує
timestamptz, але ми завжди передаємо UTC).

УВАГА: у цьому файлі не можна додавати `from __future__ import annotations`
і не можна повертати анотацію `datetime` замість `NaiveDatetime`.
Обидві речі ламають роботу з БД (README §11, блокери 1 і 2):

  1) із відкладеними анотаціями SQLModel 0.0.47 передає в `relationship()`
     рядок `list['Block']` замість класу → `InvalidRequestError` на будь-якому
     запиті до БД;
  2) наївний час без анотації `NaiveDatetime` не проходить валідацію SQLModel
     → `ValueError: Datetime values must have timezone information`.
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import NaiveDatetime
from sqlmodel import JSON, Column, Field, Relationship, SQLModel

# ── Перелічення ────────────────────────────────────────────────────────────────

class BlockKind(StrEnum):
    HEADING    = "heading"
    PARAGRAPH  = "paragraph"
    QUOTE      = "quote"
    LIST_ITEM  = "list_item"
    TABLE      = "table"
    CODE       = "code"
    FOOTNOTE   = "footnote"


class DocumentStatus(StrEnum):
    PENDING    = "pending"    # завантажено, ще не оброблено
    EXTRACTING = "extracting"
    READY      = "ready"      # блоки витягнуто
    ERROR      = "error"


class JobStatus(StrEnum):
    QUEUED    = "queued"
    RUNNING   = "running"
    DONE      = "done"
    FAILED    = "failed"
    CANCELLED = "cancelled"


class SegmentStatus(StrEnum):
    PENDING = "pending"
    DONE    = "done"
    FAILED  = "failed"


# ── Хелпери ────────────────────────────────────────────────────────────────────

def utc_now() -> datetime:
    """Поточний UTC без tzinfo — узгоджено з полями `NaiveDatetime`.

    Єдине місце, де створюється «зараз» для БД: якщо колись знадобиться
    tz-aware час, змінювати треба тут.
    """
    return datetime.now(UTC).replace(tzinfo=None)


# ── Document ───────────────────────────────────────────────────────────────────

class Document(SQLModel, table=True):
    """Завантажений файл. Один документ → багато блоків."""
    __tablename__ = "documents"

    id: int | None = Field(default=None, primary_key=True)
    filename: str
    sha256: str = Field(index=True)
    mime: str = ""
    source_path: str = ""          # шлях до файлу в data/uploads/
    page_count: int | None = None  # для PDF
    status: DocumentStatus = DocumentStatus.PENDING
    created_at: NaiveDatetime = Field(default_factory=utc_now)
    updated_at: NaiveDatetime = Field(default_factory=utc_now)

    blocks: list["Block"] = Relationship(back_populates="document")
    jobs: list["Job"] = Relationship(back_populates="document")


class DocumentRead(SQLModel):
    id: int
    filename: str
    sha256: str
    mime: str
    page_count: int | None
    status: DocumentStatus
    created_at: NaiveDatetime
    updated_at: NaiveDatetime


class DocumentWithBlocks(DocumentRead):
    blocks: list["BlockRead"] = []


# ── Block ──────────────────────────────────────────────────────────────────────

class Block(SQLModel, table=True):
    """Структурна одиниця документа (абзац, заголовок, тощо)."""
    __tablename__ = "blocks"

    id: int | None = Field(default=None, primary_key=True)
    document_id: int = Field(foreign_key="documents.id", index=True)
    ordinal: int                      # порядковий номер у документі
    kind: BlockKind = BlockKind.PARAGRAPH
    heading_level: int | None = None  # 1–6 для HEADING
    text_raw: str = ""                # вихідний текст
    text_normalized: str = ""         # після нормалізатора
    text_edited: str = ""             # що редагував користувач (порожньо = не чіпали)
    speak: bool = True                # False для CODE/TABLE
    emotion: str = "neutral"
    intensity: float = 0.5            # 0.0–1.0

    document: "Document" = Relationship(back_populates="blocks")
    segments: list["Segment"] = Relationship(back_populates="block")


class BlockRead(SQLModel):
    id: int
    document_id: int
    ordinal: int
    kind: BlockKind
    heading_level: int | None
    text_raw: str
    text_normalized: str
    text_edited: str
    speak: bool
    emotion: str
    intensity: float


class BlockUpdate(SQLModel):
    """PATCH /blocks/{bid}: часткове оновлення."""
    text_edited: str | None = None
    speak: bool | None = None
    emotion: str | None = None
    intensity: float | None = None


# ── Job ────────────────────────────────────────────────────────────────────────

class Job(SQLModel, table=True):
    """Завдання синтезу: один документ → один файл на виході."""
    __tablename__ = "jobs"

    id: int | None = Field(default=None, primary_key=True)
    document_id: int = Field(foreign_key="documents.id", index=True)
    engine_id: str = "openai_compat"
    voice_id: str = ""
    status: JobStatus = JobStatus.QUEUED
    progress: float = 0.0            # 0.0–1.0
    options_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    output_path: str = ""            # шлях до готового MP3/WAV
    error: str = ""
    created_at: NaiveDatetime = Field(default_factory=utc_now)
    started_at: NaiveDatetime | None = None
    finished_at: NaiveDatetime | None = None

    document: "Document" = Relationship(back_populates="jobs")
    segments: list["Segment"] = Relationship(back_populates="job")


class JobCreate(SQLModel):
    document_id: int
    engine_id: str = "openai_compat"
    voice_id: str = ""
    options: dict[str, Any] = Field(default_factory=dict)
    # Необовʼязковий ключ ідемпотентності: повторний запит із тим самим
    # ключем повертає вже створене завдання, а не плодить друге.
    client_token: str = ""


class JobRead(SQLModel):
    id: int
    document_id: int
    # Назва документа потрібна UI і клієнтам, щоб не робити N+1 запитів
    # (docs/FRONTEND.md, знахідка 16.8b).
    document_name: str = ""
    engine_id: str
    voice_id: str
    status: JobStatus
    progress: float
    output_path: str
    error: str
    created_at: NaiveDatetime
    started_at: NaiveDatetime | None
    finished_at: NaiveDatetime | None


# ── Segment ────────────────────────────────────────────────────────────────────

class Segment(SQLModel, table=True):
    """Одиниця синтезу — речення або частина речення."""
    __tablename__ = "segments"

    id: int | None = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="jobs.id", index=True)
    block_id: int = Field(foreign_key="blocks.id", index=True)
    ordinal: int                     # порядок у блоці
    text: str = ""
    prosody_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    audio_path: str = ""             # WAV-файл сегмента на диску
    duration_ms: int = 0
    status: SegmentStatus = SegmentStatus.PENDING

    job: "Job" = Relationship(back_populates="segments")
    block: "Block" = Relationship(back_populates="segments")


# ── Voice ──────────────────────────────────────────────────────────────────────

class Voice(SQLModel, table=True):
    """Голос, доступний у рушії."""
    __tablename__ = "voices"

    id: str = Field(primary_key=True)  # напр. "uk_UA-tetiana-high"
    engine_id: str = Field(index=True)
    name: str = ""
    lang: str = ""
    gender: str = ""                  # "f" | "m" | "multi" | ""
    sample_path: str = ""
    meta_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


# ── Preset ─────────────────────────────────────────────────────────────────────

class Preset(SQLModel, table=True):
    """Збережений пресет параметрів синтезу."""
    __tablename__ = "presets"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    engine_id: str = ""
    voice_id: str = ""
    emotion: str = "neutral"
    intensity: float = 0.5
    options_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
