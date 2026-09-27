"""Налаштування Jinja2 для HTML-інтерфейсу.

Один екземпляр `templates` на весь застосунок. Усі підписи й форматування
підключені як фільтри/глобальні функції, щоб шаблони лишалися читабельними
(і щоб жодного рядка UI не було в JS).
"""

from __future__ import annotations

from fastapi.templating import Jinja2Templates

from app.config import get_settings
from app.ui import presentation


def _build_templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(get_settings().templates_dir))

    env = templates.env
    env.filters.update(
        duration=presentation.format_duration_ms,
        bytesize=presentation.format_bytes,
        percent=presentation.format_percent,
        estimate=presentation.format_estimate_minutes,
    )
    env.globals.update(
        emotion_label=presentation.emotion_label,
        emotion_icon=presentation.emotion_icon,
        document_status_label=presentation.document_status_label,
        job_status_chip=presentation.job_status_chip,
        prosody_summary=presentation.prosody_summary,
        kind_label=presentation.kind_label,
    )
    return templates


templates = _build_templates()
