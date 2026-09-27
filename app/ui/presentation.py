"""Презентаційний шар інтерфейсу: підписи, бейджі, форматування.

Уся текстова «косметика» UI живе тут, а не в шаблонах: одна точка правди для
українських підписів (docs/FRONTEND.md, розд. 14 — усі рядки в одному місці,
жодного рядка в JS).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models import Block, BlockKind, DocumentStatus, Job, JobStatus
from app.services.expression.profiles import ProsodyPlan, get_prosody

# ── Підписи ───────────────────────────────────────────────────────────────────

KIND_BADGES: dict[str, str] = {
    BlockKind.HEADING:    "H",
    BlockKind.PARAGRAPH:  "¶",
    BlockKind.QUOTE:      "❝",
    BlockKind.LIST_ITEM:  "•",
    BlockKind.TABLE:      "▦",
    BlockKind.CODE:       "</>",
    BlockKind.FOOTNOTE:   "*",
}

KIND_LABELS: dict[str, str] = {
    BlockKind.HEADING:    "заголовок",
    BlockKind.PARAGRAPH:  "абзац",
    BlockKind.QUOTE:      "цитата",
    BlockKind.LIST_ITEM:  "пункт списку",
    BlockKind.TABLE:      "таблиця",
    BlockKind.CODE:       "код",
    BlockKind.FOOTNOTE:   "зноска",
}

EMOTION_LABELS: dict[str, str] = {
    "neutral":     "нейтрально",
    "warm":        "тепло",
    "serious":     "серйозно",
    "joyful":      "радісно",
    "excited":     "захоплено",
    "sad":         "сумно",
    "tense":       "напружено",
    "questioning": "питально",
}

EMOTION_ICONS: dict[str, str] = {
    "neutral":     "😐",
    "warm":        "🙂",
    "serious":     "🧐",
    "joyful":      "😄",
    "excited":     "🤩",
    "sad":         "😔",
    "tense":       "😬",
    "questioning": "🤔",
}

DOCUMENT_STATUS_LABELS: dict[str, str] = {
    DocumentStatus.PENDING:    "очікує обробки",
    DocumentStatus.EXTRACTING: "витяг тексту",
    DocumentStatus.READY:      "готовий",
    DocumentStatus.ERROR:      "помилка",
}

# Чип статусу завдання: (підпис, css-клас)
JOB_STATUS_CHIPS: dict[str, tuple[str, str]] = {
    JobStatus.QUEUED:    ("у черзі", "queued"),
    JobStatus.RUNNING:   ("виконується", "run"),
    JobStatus.DONE:      ("готово", "ok"),
    JobStatus.FAILED:    ("збій", "err"),
    JobStatus.CANCELLED: ("скасовано", "warn"),
}


def kind_badge(block: Block) -> str:
    """Бейдж типу блоку; для заголовків — з рівнем (H1…H6)."""
    if block.kind == BlockKind.HEADING and block.heading_level:
        return f"H{block.heading_level}"
    return KIND_BADGES.get(block.kind, "¶")


def kind_label(block: Block) -> str:
    return KIND_LABELS.get(block.kind, str(block.kind))


def emotion_label(emotion: str) -> str:
    return EMOTION_LABELS.get(emotion, emotion)


def emotion_icon(emotion: str) -> str:
    return EMOTION_ICONS.get(emotion, "")


def document_status_label(status: str) -> str:
    return DOCUMENT_STATUS_LABELS.get(status, str(status))


def job_status_chip(status: str) -> tuple[str, str]:
    return JOB_STATUS_CHIPS.get(status, (str(status), "queued"))


# ── Форматування ──────────────────────────────────────────────────────────────

def format_duration_ms(milliseconds: int) -> str:
    """Тривалість у людському вигляді: «3,4 с» або «4:12»."""
    if milliseconds <= 0:
        return "—"
    seconds = milliseconds / 1000
    if seconds < 60:
        return f"{seconds:.1f} с".replace(".", ",")
    minutes, rest = divmod(round(seconds), 60)
    return f"{minutes}:{rest:02d}"


def format_bytes(size: int) -> str:
    if size <= 0:
        return "—"
    megabytes = size / (1024 * 1024)
    if megabytes < 1:
        return f"{size / 1024:.0f} КБ"
    return f"{megabytes:.1f} МБ".replace(".", ",")


def format_percent(value: float) -> str:
    return f"{value * 100:.1f} %".replace(".", ",")


def estimate_audio_minutes(characters: int) -> float:
    """Дуже груба оцінка тривалості аудіо за символами.

    Порядок величини — ~14 символів за секунду. Це **оцінка, а не вимір**:
    справжній real-time factor має дати scripts/benchmark.py (PLAN, етап 1).
    UI зобов'язаний показувати її з «≈».
    """
    if characters <= 0:
        return 0.0
    return characters / 14 / 60


def format_estimate_minutes(minutes: float) -> str:
    if minutes <= 0:
        return "—"
    if minutes < 1:
        return "менше хвилини"
    if minutes < 60:
        return f"≈{round(minutes)} хв"
    hours = minutes / 60
    return f"≈{hours:.1f} год".replace(".", ",")


def prosody_summary(plan: ProsodyPlan) -> str:
    """Людський опис просодії: «темп 0,98×, пауза 460 мс»."""
    speed = f"{plan.speed:.2f}".replace(".", ",")
    parts = [f"темп {speed}×"]
    if plan.energy_db:
        parts.append(f"гучність {plan.energy_db:+.0f} дБ")
    parts.append(f"пауза {plan.pause_after_ms} мс")
    return ", ".join(parts)


# ── Моделі подання ────────────────────────────────────────────────────────────

@dataclass
class BlockView:
    """Блок, готовий до рендерингу: текст у вибраному режимі + просодія."""
    block: Block
    text: str
    prosody: ProsodyPlan
    badge: str
    kind_label: str


@dataclass
class DocumentStats:
    """Зведення по документу для шапки сторінки."""
    blocks: int = 0
    speakable: int = 0
    edited: int = 0
    characters: int = 0

    @property
    def estimate(self) -> str:
        return format_estimate_minutes(estimate_audio_minutes(self.characters))


def build_block_views(blocks: list[Block], mode: str = "effective") -> list[BlockView]:
    """Побудувати моделі подання для блоків.

    mode: «raw» — вихідний текст, «normalized» — нормалізований,
    «effective» — те, що реально піде в синтез (мій → нормалізований → вихідний).
    """
    from app.services.library.blocks import block_effective_text

    views: list[BlockView] = []
    for block in blocks:
        if mode == "raw":
            text = block.text_raw
        elif mode == "normalized":
            text = block.text_normalized
        else:
            text = block_effective_text(block)

        views.append(
            BlockView(
                block=block,
                text=text,
                prosody=get_prosody(block.emotion, block.intensity),
                badge=kind_badge(block),
                kind_label=kind_label(block),
            )
        )
    return views


def document_stats(blocks: list[Block]) -> DocumentStats:
    """Порахувати зведення для шапки сторінки документа."""
    from app.services.library.blocks import block_effective_text, count_speakable

    characters = sum(len(block_effective_text(block)) for block in blocks if block.speak)
    return DocumentStats(
        blocks=len(blocks),
        speakable=count_speakable(blocks),
        edited=sum(1 for block in blocks if block.text_edited),
        characters=characters,
    )


def job_progress_percent(job: Job) -> str:
    return format_percent(job.progress or 0.0)
