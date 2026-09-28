"""Сторінки та фрагменти HTML-інтерфейсу.

Фаза F0 (docs/FRONTEND.md, розд. 17): робоча стола, перегляд документа й
система — тобто «файл завантажується з браузера, документи й блоки видно».
Редактор із правкою блоків, прев'ю та чергою синтезу — фази F1–F2.

Маршрути:
  GET  /                              — робоча стола
  POST /ui/documents                  — завантаження файлу (multipart)
  GET  /documents/{id}                — документ із блоками
  POST /ui/documents/{id}/normalize   — перенормалізувати
  POST /ui/documents/{id}/delete      — видалити
  GET  /settings                      — стан системи й довідка про емоції
  GET  /ui/queue                      — фрагмент: стрічка завдань
  GET  /ui/health                     — фрагмент: чип стану
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from sqlmodel import Session, select

from app.config import get_settings
from app.db import get_session
from app.models import Block, Document, Job, JobStatus, Segment, utc_now
from app.services import engines as engines_service
from app.services import presets as presets_service
from app.services.engines import EngineNotFoundError
from app.services.expression.profiles import all_profiles_dict
from app.services.library import documents as library
from app.services.library.errors import (
    DocumentNotFoundError,
    ExtractorUnavailableError,
    UnsupportedFormatError,
    UploadTooLargeError,
)
from app.services.system import (
    health_snapshot,
    installed_voices_label,
    probe_tts_gateway,
    probe_tts_gateway_cached,
    tts_ready_for_synthesis,
    voice_is_installed,
)
from app.ui import presentation
from app.ui.templating import templates

logger = logging.getLogger(__name__)
router = APIRouter(tags=["ui"])

# Скільки блоків віддавати на сторінку. Книга на 1000+ блоків не вміщується
# в один DOM — пагінація з самого початку (docs/FRONTEND.md, розд. 6.7).
BLOCKS_PER_PAGE = 200

# Повідомлення після переспрямування (без сесій і cookies: усе в URL)
FLASH_MESSAGES: dict[str, tuple[str, str]] = {
    "created":    ("Документ завантажено й розбито на блоки.", "ok"),
    "duplicate":  ("Цей файл уже завантажено — відкрито наявну копію.", "warn"),
    "normalized": ("Нормалізацію перезапущено.", "ok"),
    "deleted":    ("Документ видалено.", "ok"),
    "already_running": (
        "Цей документ уже озвучується — відкрито наявне завдання.",
        "warn",
    ),
    "tts_not_ready": (
        "Синтез не запущено: TTS-шлюз недоступний або потрібної моделі в ньому немає. "
        "Деталі — на сторінці «Система», кнопка «Перевірити шлюз».",
        "err",
    ),
    "preset_applied": (
        "Емоцію пресета застосовано до всіх озвучуваних блоків.",
        "ok",
    ),
    "nothing_to_speak": (
        "Немає жодного озвучуваного блоку з непорожнім текстом — синтезувати нічого.",
        "warn",
    ),
    "bad_format": (
        "Невідомий формат результату — завдання не створено. "
        "Доступні: MP3, M4B, WAV (і zip по розділах).",
        "err",
    ),
}

EMOTION_ORDER = (
    "neutral", "warm", "serious", "joyful",
    "excited", "sad", "tense", "questioning",
)

# Вибір результату в редакторі. Порядок — від найпотрібнішого: MP3 слухають
# усюди, M4B — формат аудіокниги з розділами, WAV — для подальшої обробки.
FORMAT_CHOICES: tuple[tuple[str, str, str], ...] = (
    ("mp3", "MP3", "Слухається будь-де. Найменший файл."),
    ("m4b", "M4B", "Формат аудіокниги: розділи з заголовків, метадані, переходи в плейері."),
    ("wav", "WAV", "Без втрат — для монтажу. Файл великий."),
)


# ── Допоміжні функції ─────────────────────────────────────────────────────────

def _flash(kind: str | None) -> tuple[str, str] | None:
    return FLASH_MESSAGES.get(kind) if kind else None


def _error_text(exc: Exception) -> tuple[str, int]:
    """Помилка сервісу → (текст для людини, статус-код)."""
    if isinstance(exc, UnsupportedFormatError):
        return str(exc), 415
    if isinstance(exc, UploadTooLargeError):
        return str(exc), 413
    if isinstance(exc, ExtractorUnavailableError):
        return str(exc), 422
    if isinstance(exc, DocumentNotFoundError):
        return "Документ не знайдено.", 404
    logger.exception("Непередбачена помилка в UI-роутері")
    return "Внутрішня помилка сервера. Деталі — у логах.", 500


def _recent_jobs(session: Session, limit: int = 5) -> list[Job]:
    return list(session.exec(select(Job).order_by(Job.created_at.desc()).limit(limit)).all())


def _document_rows(session: Session) -> list[dict]:
    """Список документів разом із кількістю блоків у кожному."""
    return [
        {
            "document": document,
            "blocks": len(library.list_document_blocks(session, document.id or 0)),
        }
        for document in library.list_documents(session)
    ]


def _workspace_context(
    session: Session,
    flash: tuple[str, str] | None = None,
    error: str | None = None,
) -> dict:
    return {
        "active_nav": "workspace",
        "health": health_snapshot(),
        "rows": _document_rows(session),
        "jobs": _recent_jobs(session),
        "flash": flash,
        "error": error,
    }


# ── Робоча стола ──────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
def workspace(
    request: Request,
    session: Session = Depends(get_session),
    flash: str | None = None,
):
    """Головна: стан системи, завантаження файлу, список документів, черга."""
    return templates.TemplateResponse(
        request, "pages/workspace.html", _workspace_context(session, _flash(flash))
    )


@router.post("/ui/documents")
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
):
    """Прийняти файл із форми й переспрямувати на сторінку документа.

    Дублікат (за sha256) не створює новий документ — користувач має побачити
    явне повідомлення, інакше вирішить, що завантаження не спрацювало
    (docs/FRONTEND.md, розд. 5).
    """
    try:
        result = library.create_from_upload(session, file.filename or "", file.file)
    except Exception as exc:
        message, status = _error_text(exc)
        if status == 500:
            raise
        # Повертаємо ту саму сторінку з поясненням, а не голий JSON:
        # користувач працює в браузері.
        return templates.TemplateResponse(
            request,
            "pages/workspace.html",
            _workspace_context(session, error=message),
            status_code=status,
        )

    kind = "duplicate" if result.duplicate else "created"
    return RedirectResponse(url=f"/documents/{result.document.id}?flash={kind}", status_code=303)


# ── Сторінка документа (редактор, фаза F1) ────────────────────────────────────

def _filter_blocks(
    blocks: list[Block],
    query: str = "",
    only_edited: bool = False,
    emotion: str = "",
) -> list[Block]:
    """Серверні фільтри списку блоків.

    Фільтруємо на сервері, а не в DOM: у книзі тисячі блоків, і тримати їх
    усі в браузері лише щоб ховати — марнотратно (docs/FRONTEND.md, розд. 6.7).
    """
    from app.services.library.blocks import block_effective_text

    filtered = blocks
    if only_edited:
        filtered = [b for b in filtered if b.text_edited]
    if emotion:
        filtered = [b for b in filtered if b.emotion == emotion]
    if query:
        needle = query.strip().casefold()
        filtered = [
            b for b in filtered if needle in block_effective_text(b).casefold()
        ]
    return filtered


def _editor_context(
    request: Request,
    session: Session,
    doc_id: int,
    mode: str,
    offset: int,
    flash: tuple[str, str] | None,
    query: str = "",
    only_edited: bool = False,
    emotion: str = "",
    engine_id: str | None = None,
    voice_id: str | None = None,
    preset_id: int | None = None,
    compact: bool = False,
) -> dict:
    """Контекст сторінки редактора (спільний для сторінки й фрагмента)."""
    result = library.get_document_with_blocks(session, doc_id)
    all_blocks = result.blocks

    # Пресет може підставити рушій і голос — але емоцію до блоків
    # застосовуємо лише на явну дію користувача (кнопка в панелі).
    selected_preset = None
    if preset_id is not None:
        with contextlib.suppress(presets_service.PresetNotFoundError):
            selected_preset = presets_service.get_preset(session, preset_id)
    if selected_preset:
        engine_id = selected_preset.engine_id or engine_id
        voice_id = selected_preset.voice_id or voice_id

    filtered = _filter_blocks(all_blocks, query, only_edited, emotion)
    offset = max(0, offset)
    page_blocks = filtered[offset : offset + BLOCKS_PER_PAGE]

    engine = engines_service.pick_engine(engine_id or request.query_params.get("engine"))
    engine_caps = engine["capabilities"]
    voices = engines_service.voices_of(engine["id"])
    selected_voice = (
        voice_id
        or request.query_params.get("voice")
        or engines_service.default_voice_id()
    )
    if voices and selected_voice not in {v["id"] for v in voices}:
        selected_voice = voices[0]["id"]

    return {
        "active_nav": "workspace",
        "health": health_snapshot(),
        "document": result.document,
        "views": presentation.build_block_views(page_blocks, mode=mode),
        "stats": presentation.document_stats(all_blocks),
        "filtered_total": len(filtered),
        "mode": mode,
        "offset": offset,
        "total": len(all_blocks),
        "has_more": offset + BLOCKS_PER_PAGE < len(filtered),
        "next_offset": offset + BLOCKS_PER_PAGE,
        "page_size": BLOCKS_PER_PAGE,
        "engine": engine,
        "engine_caps": engine_caps,
        "voices": voices,
        "selected_voice": selected_voice,
        "voice_status": {
            voice["id"]: voice_is_installed(probe_tts_gateway_cached(), voice["id"])
            for voice in voices
        },
        "installed_label": installed_voices_label(probe_tts_gateway_cached()),
        "profiles": all_profiles_dict(),
        "emotion_order": EMOTION_ORDER,
        "emotion_labels": presentation.EMOTION_LABELS,
        "emotion_icons": presentation.EMOTION_ICONS,
        "query": query,
        "only_edited": only_edited,
        "emotion_filter": emotion,
        "presets": presets_service.list_presets(session),
        "selected_preset": selected_preset,
        "compact": compact,
        "format_choices": FORMAT_CHOICES,
        "default_formats": get_settings().output_format_list,
        "flash": flash,
    }


@router.get("/documents/{doc_id}", response_class=HTMLResponse)
def document_page(
    request: Request,
    doc_id: int,
    session: Session = Depends(get_session),
    mode: str = "effective",
    offset: int = 0,
    flash: str | None = None,
    q: str = "",
    only_edited: bool = False,
    emotion: str = "",
    preset: int | None = None,
    compact: bool = False,
):
    """Редактор документа: правка блоків, емоції, прев'ю (фаза F1)."""
    try:
        context = _editor_context(
            request, session, doc_id,
            mode=mode, offset=offset, flash=_flash(flash),
            query=q, only_edited=only_edited, emotion=emotion,
            preset_id=preset, compact=compact,
        )
    except DocumentNotFoundError:
        return templates.TemplateResponse(
            request,
            "pages/not_found.html",
            {"active_nav": "workspace", "health": health_snapshot(), "doc_id": doc_id},
            status_code=404,
        )

    return templates.TemplateResponse(request, "pages/document.html", context)


@router.get("/ui/documents/{doc_id}/blocks", response_class=HTMLResponse)
def blocks_fragment(
    request: Request,
    doc_id: int,
    session: Session = Depends(get_session),
    mode: str = "effective",
    offset: int = 0,
    q: str = "",
    only_edited: bool = False,
    emotion: str = "",
    compact: bool = False,
):
    """Фрагмент: наступна сторінка блоків (htmx довантажує в кінець списку)."""
    try:
        context = _editor_context(
            request, session, doc_id,
            mode=mode, offset=offset, flash=None,
            query=q, only_edited=only_edited, emotion=emotion,
            compact=compact,
        )
    except DocumentNotFoundError:
        return HTMLResponse("", status_code=404)

    return templates.TemplateResponse(request, "partials/block_list.html", context)


@router.post("/ui/blocks/{block_id}/preview", response_class=HTMLResponse)
async def preview_block(
    request: Request,
    block_id: int,
    session: Session = Depends(get_session),
    voice_id: str = Form(""),
    engine_id: str = Form(""),
):
    """Фрагмент: плеєр із прев'ю одного блоку.

    Читаємо блок із БД (а не з форми): так прев'ю гарантовано відповідає тому,
    що збережено. Кнопка в редакторі спершу зберігає незбережене, і лише потім
    викликає цей маршрут (див. static/js/editor.js).
    """
    from app.services.library.blocks import block_effective_text
    from app.services.preview import (
        PreviewEngineUnsupportedError,
        PreviewUnavailableError,
        synthesize_preview_async,
    )

    block = session.get(Block, block_id)
    if block is None:
        return templates.TemplateResponse(
            request, "partials/preview_player.html", {"error": "Блок не знайдено"}
        )

    settings = get_settings()

    # Явно вказаний рушій не підміняємо: якщо він недоступний, користувач має
    # побачити це прямо, а не мовчазний перехід на інший голос.
    if engine_id:
        try:
            engine = engines_service.get_engine(engine_id)
        except EngineNotFoundError:
            return templates.TemplateResponse(
                request,
                "partials/preview_player.html",
                {"error": f"Рушій {engine_id!r} не знайдено в каталозі."},
            )
    else:
        engine = engines_service.pick_engine(None)

    caps = engine["capabilities"]
    text = block_effective_text(block)
    voice = voice_id or engines_service.default_voice_id()

    if not text.strip():
        return templates.TemplateResponse(
            request,
            "partials/preview_player.html",
            {"error": "Порожній блок — озвучувати нічого."},
        )

    try:
        result = await synthesize_preview_async(
            text=text,
            voice_id=voice,
            emotion=block.emotion,
            intensity=block.intensity,
            engine_id=engine["id"],
        )
    except PreviewEngineUnsupportedError as exc:
        return templates.TemplateResponse(
            request, "partials/preview_player.html", {"error": str(exc)}
        )
    except PreviewUnavailableError as exc:
        return templates.TemplateResponse(
            request,
            "partials/preview_player.html",
            {
                "error": (
                    f"TTS-шлюз не відповідає ({settings.tts_base_url}): {exc}. "
                    "Перевірте, чи піднято Docker-контейнер."
                )
            },
        )

    probe = probe_tts_gateway_cached()
    return templates.TemplateResponse(
        request,
        "partials/preview_player.html",
        {
            "result": result,
            "block_id": block_id,
            "voice_id": voice,
            "max_chars": caps["max_chars"],
            "voice_installed": voice_is_installed(probe, voice),
            "installed_label": installed_voices_label(probe),
        },
    )


@router.post("/ui/documents/{doc_id}/normalize")
def renormalize(doc_id: int, session: Session = Depends(get_session)):
    """Перезапустити нормалізацію й повернутися на сторінку документа."""
    try:
        library.renormalize(session, doc_id)
    except DocumentNotFoundError:
        return RedirectResponse(url="/?flash=deleted", status_code=303)
    return RedirectResponse(url=f"/documents/{doc_id}?flash=normalized", status_code=303)


@router.post("/ui/documents/{doc_id}/delete")
def delete(doc_id: int, session: Session = Depends(get_session)):
    """Видалити документ і повернутися на робочу столу.

    Повторне видалення не є помилкою: користувач хотів, щоб документа не
    було, — і його немає.
    """
    with contextlib.suppress(DocumentNotFoundError):
        library.delete_document(session, doc_id)
    return RedirectResponse(url="/?flash=deleted", status_code=303)


# ── Система ───────────────────────────────────────────────────────────────────

@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, probe: bool = False):
    """Стан системи, довідка про емоції та стан фаз.

    `?probe=1` запускає перевірку TTS-шлюзу — окремо, бо це мережевий запит
    із таймаутом, який не має сповільнювати кожне відкриття сторінки.
    """
    return templates.TemplateResponse(
        request,
        "pages/settings.html",
        {
            "active_nav": "settings",
            "health": health_snapshot(),
            "profiles": all_profiles_dict(),
            "emotion_order": EMOTION_ORDER,
            "tts_probe": probe_tts_gateway() if probe else None,
        },
    )


# ── Аудіо ─────────────────────────────────────────────────────────────────────

@router.get("/ui/preview/{filename}")
def preview_audio(filename: str) -> FileResponse:
    """Віддати WAV прев'ю з data/renders/preview.

    Окремий маршрут, а не статичне монтування всієї теки `renders`: у ній
    лежать і сегменти, і готові книги, а вони не призначені для перегляду
    каталогу. Імʼя файлу беремо лише як базове — жодних шляхів.
    """
    safe_name = Path(filename).name
    path = get_settings().renders_dir / "preview" / safe_name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Прев'ю не знайдено")
    return FileResponse(path=str(path), media_type="audio/wav")


# ── Рушії та голоси (фаза F3) ─────────────────────────────────────────────────

# Однакова фраза для всіх голосів: порівнювати голоси на різних текстах
# неможливо. Місце правди — тут, а не в JS.
REFERENCE_PHRASE = (
    "Доброго дня. Це приклад голосу для порівняння. "
    "Українська мова має мелодійну інтонацію, і це добре чути."
)


@router.get("/voices", response_class=HTMLResponse)
def voices_page(request: Request, session: Session = Depends(get_session)):
    """Рушії з capabilities, голоси з прослуховуванням і пресети.

    Сторінка перевіряє шлюз (з коротким кешем), бо каталог застосунку
    обіцяє пʼять українських голосів, а шлюз може мати один: запит із чужим
    імʼям голосу він приймає й озвучує тим, що встановлений. Без цієї
    перевірки сторінка показувала б пʼять однакових голосів і вводила б
    користувача в оману.
    """
    probe = probe_tts_gateway_cached()

    available = []
    for engine in engines_service.available_engines():
        enriched = dict(engine)
        enriched["voice_status"] = {
            voice["id"]: voice_is_installed(probe, voice["id"]) for voice in engine["voices"]
        }
        available.append(enriched)

    return templates.TemplateResponse(
        request,
        "pages/voices.html",
        {
            "active_nav": "voices",
            "health": health_snapshot(),
            "available": available,
            "unavailable": engines_service.unavailable_engines(),
            "presets": presets_service.list_presets(session),
            "probe": probe,
            "installed_label": installed_voices_label(probe),
        },
    )


@router.post("/ui/voices/preview", response_class=HTMLResponse)
async def audition_voice(
    request: Request,
    voice_id: str = Form(...),
    engine_id: str = Form(""),
):
    """Синтезувати еталонну фразу вибраним голосом — фрагмент із плеєром."""
    from app.services.preview import (
        PreviewEngineUnsupportedError,
        PreviewUnavailableError,
        synthesize_preview_async,
    )

    engine = engines_service.pick_engine(engine_id or None)
    try:
        result = await synthesize_preview_async(
            text=REFERENCE_PHRASE,
            voice_id=voice_id,
            emotion="neutral",
            intensity=1.0,
            engine_id=engine["id"],
        )
    except (PreviewEngineUnsupportedError, PreviewUnavailableError) as exc:
        return templates.TemplateResponse(
            request,
            "partials/voice_preview.html",
            {
                "error": (
                    f"{exc}. Перевірте, чи піднято TTS-шлюз "
                    f"({get_settings().tts_base_url})."
                )
            },
        )

    probe = probe_tts_gateway_cached()
    return templates.TemplateResponse(
        request,
        "partials/voice_preview.html",
        {
            "result": result,
            # Шлюз озвучить тим голосом, який має, навіть якщо попросили інший —
            # тому чесно кажемо, що саме почує користувач.
            "voice_installed": voice_is_installed(probe, voice_id),
            "installed_label": installed_voices_label(probe),
        },
    )


# ── Пресети (фаза F3) ─────────────────────────────────────────────────────────

@router.post("/ui/presets")
def create_preset_ui(
    request: Request,
    session: Session = Depends(get_session),
    name: str = Form(...),
    engine: str = Form(""),
    voice: str = Form(""),
    emotion: str = Form("neutral"),
    intensity: float = Form(0.5),
    back: str = Form("/voices"),
):
    """Зберегти поточні рушій/голос/емоцію як пресет."""
    try:
        presets_service.create_preset(
            session,
            name=name,
            engine_id=engine,
            voice_id=voice,
            emotion=emotion,
            intensity=intensity,
        )
    except presets_service.PresetNameTakenError as exc:
        logger.info("Пресет не створено: %s", exc)
    return RedirectResponse(url=back, status_code=303)


@router.post("/ui/presets/{preset_id}/delete")
def delete_preset_ui(
    preset_id: int,
    session: Session = Depends(get_session),
    back: str = Form("/voices"),
):
    """Видалити пресет."""
    with contextlib.suppress(presets_service.PresetNotFoundError):
        presets_service.delete_preset(session, preset_id)
    return RedirectResponse(url=back, status_code=303)


@router.post("/ui/documents/{doc_id}/presets/{preset_id}/apply")
def apply_preset_ui(
    doc_id: int,
    preset_id: int,
    session: Session = Depends(get_session),
):
    """Застосувати емоцію пресета до всіх озвучуваних блоків документа."""
    with contextlib.suppress(presets_service.PresetNotFoundError):
        presets_service.apply_to_blocks(session, doc_id, preset_id)
    return RedirectResponse(
        url=f"/documents/{doc_id}?flash=preset_applied", status_code=303
    )


# ── Черга та завдання (фаза F2) ───────────────────────────────────────────────

def _job_view(job: Job, session: Session) -> dict:
    """Дані завдання разом із назвою документа.

    `JobRead` не містить назви документа (знахідка 16.8b), а в HTML-роутері
    вона береться простим JOIN-ом — без N+1 запитів від клієнта.
    """
    document = session.get(Document, job.document_id)
    chip = presentation.job_status_chip(job.status)

    elapsed_ms = 0
    if job.started_at:
        finished = job.finished_at or utc_now()
        elapsed_ms = int((finished - job.started_at).total_seconds() * 1000)

    output = Path(job.output_path) if job.output_path else None
    artifacts = _artifact_rows(job, output)
    return {
        "job": job,
        "document": document,
        "document_name": document.filename if document else f"документ {job.document_id}",
        "chip": chip,
        "percent": presentation.format_percent(job.progress or 0.0),
        "elapsed_ms": elapsed_ms,
        "output_name": output.name if output else "",
        "output_format": output.suffix.lstrip(".").upper() if output else "",
        "output_size": output.stat().st_size if output and output.is_file() else 0,
        "artifacts": artifacts,
        "has_zip": any(row["format"] == "zip" for row in artifacts),
        "is_active": job.status in (JobStatus.QUEUED, JobStatus.RUNNING),
        "is_stalled": job.status == JobStatus.RUNNING and not job.started_at,
    }


def _artifact_rows(job: Job, output: Path | None) -> list[dict]:
    """Створені файли завдання — для списку завантажень на сторінці завдання.

    Головний файл додаємо, якщо переліку немає (старі завдання): сторінка
    завдання має показувати те, що реально лежить на диску, а не те, що
    обіцяє `options_json`.
    """
    artifacts = {
        str(name): str(path)
        for name, path in (job.options_json or {}).get("artifacts", {}).items()
        if path
    }
    if not artifacts and output:
        artifacts[output.suffix.lower().lstrip(".")] = str(output)

    rows = []
    for name in sorted(artifacts, key=lambda item: (item == "zip", item)):
        path = Path(artifacts[name])
        rows.append(
            {
                "format": name,
                "label": "ZIP · розділи" if name == "zip" else name.upper(),
                "file_name": path.name,
                "size": path.stat().st_size if path.is_file() else 0,
                "exists": path.is_file(),
                "url": f"/api/v1/jobs/{job.id}/download?format={name}",
            }
        )
    return rows


def _speakable_count(session: Session, document_id: int) -> int:
    from app.services.library.blocks import count_speakable

    return count_speakable(library.list_document_blocks(session, document_id))


@router.get("/jobs", response_class=HTMLResponse)
def queue_page(
    request: Request,
    session: Session = Depends(get_session),
    flash: str | None = None,
):
    """Черга завдань: що виконується, що в черзі, що готово."""
    jobs = list(session.exec(select(Job).order_by(Job.created_at.desc())).all())
    views = [_job_view(job, session) for job in jobs]
    health = health_snapshot()

    return templates.TemplateResponse(
        request,
        "pages/queue.html",
        {
            "active_nav": "queue",
            "health": health,
            "views": views,
            "running": sum(1 for v in views if v["job"].status == JobStatus.RUNNING),
            "queued": sum(1 for v in views if v["job"].status == JobStatus.QUEUED),
            "concurrency": health["synth_concurrency"],
            "flash": _flash(flash),
        },
    )


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_page(
    request: Request,
    job_id: int,
    session: Session = Depends(get_session),
    flash: str | None = None,
):
    """Одне завдання: прогрес, сегменти, скасування, завантаження."""
    job = session.get(Job, job_id)
    if not job:
        return templates.TemplateResponse(
            request,
            "pages/not_found.html",
            {
                "active_nav": "queue",
                "health": health_snapshot(),
                "doc_id": job_id,
                "what": "Завдання",
            },
            status_code=404,
        )

    segments = list(
        session.exec(
            select(Segment).where(Segment.job_id == job_id).order_by(Segment.ordinal)
        ).all()
    )

    return templates.TemplateResponse(
        request,
        "pages/job.html",
        {
            "active_nav": "queue",
            "health": health_snapshot(),
            "view": _job_view(job, session),
            "segments": segments,
            "block_total": _speakable_count(session, job.document_id),
            "flash": _flash(flash),
        },
    )


@router.post("/ui/documents/{doc_id}/jobs")
def create_job_from_editor(
    doc_id: int,
    session: Session = Depends(get_session),
    voice: str = Form(""),
    engine: str = Form(""),
    formats: list[str] = Form([]),
    chapter_split: bool = Form(False),
):
    """«Озвучити все»: створити завдання і перейти на його сторінку.

    Поля форми звуться `voice` / `engine` — так само, як параметри сторінки
    редактора, бо кнопка надсилає ту саму форму через `formaction`.
    Перевірки (документ існує, є озвучувані блоки) — ті самі, що в JSON-API.

    `formats` і `chapter_split` — вибір результату: MP3/WAV/M4B і zip із
    файлами-розділами. Порожній вибір означає «як у налаштуваннях»
    (`OUTPUT_FORMATS`), а не «жодного файлу».
    """
    from app.services.audio.assemble import normalize_formats
    from app.services.library.blocks import count_speakable
    from app.worker.queue import enqueue_job

    try:
        result = library.get_document_with_blocks(session, doc_id)
    except DocumentNotFoundError:
        return RedirectResponse(url="/?flash=deleted", status_code=303)

    if count_speakable(result.blocks) == 0:
        return RedirectResponse(
            url=f"/documents/{doc_id}?flash=nothing_to_speak", status_code=303
        )

    try:
        selected_formats = normalize_formats(
            [item for item in formats if item], default=get_settings().output_format_list
        )
    except ValueError:
        logger.warning("Завдання не створено: невідомий формат %s", formats)
        return RedirectResponse(
            url=f"/documents/{doc_id}?flash=bad_format", status_code=303
        )

    # Друге завдання на той самий документ нічого не додає, лише дублює
    # роботу: відкриваємо наявне (захист від подвійного кліку й F5).
    active = session.exec(
        select(Job)
        .where(
            Job.document_id == doc_id,
            Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
        )
        .order_by(Job.created_at.desc())
    ).first()
    if active:
        logger.info("Документ %s уже озвучується (завдання %s)", doc_id, active.id)
        return RedirectResponse(
            url=f"/jobs/{active.id}?flash=already_running", status_code=303
        )

    # Не створюємо завдання, яке гарантовано впаде: краще сказати причину
    # зараз, ніж через три сегменти показати «failed» із 404 від шлюзу.
    ready, reason = tts_ready_for_synthesis()
    if not ready:
        logger.warning("Синтез не запущено: %s", reason)
        return RedirectResponse(
            url=f"/documents/{doc_id}?flash=tts_not_ready", status_code=303
        )

    selected_engine = engines_service.pick_engine(engine or None)
    known_voices = {item["id"] for item in engines_service.voices_of(selected_engine["id"])}
    selected_voice = voice if voice in known_voices else engines_service.default_voice_id()
    if known_voices and selected_voice not in known_voices:
        # Голос міг зникнути з конфігурації шлюзу — беремо перший наявний
        selected_voice = sorted(known_voices)[0]

    job = Job(
        document_id=doc_id,
        engine_id=selected_engine["id"],
        voice_id=selected_voice,
        status=JobStatus.QUEUED,
        options_json={
            "created_from": "ui",
            "pause_ms": 400,
            "formats": list(selected_formats),
            "chapter_split": bool(chapter_split),
        },
    )
    session.add(job)
    session.commit()
    session.refresh(job)

    enqueue_job(job.id)
    logger.info(
        "Завдання %s створено з інтерфейсу (doc=%s, формати=%s, zip=%s)",
        job.id, doc_id, ",".join(selected_formats), bool(chapter_split),
    )
    return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)


@router.post("/ui/jobs/{job_id}/cancel")
async def cancel_job_ui(job_id: int, session: Session = Depends(get_session)):
    """Скасувати завдання зі сторінки черги."""
    from app.worker.queue import cancel_job

    if not session.get(Job, job_id):
        return RedirectResponse(url="/jobs", status_code=303)

    await cancel_job(job_id)
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@router.post("/ui/jobs/{job_id}/retry")
def retry_job_ui(job_id: int, session: Session = Depends(get_session)):
    """Повторити невдале або скасоване завдання."""
    from app.worker.queue import enqueue_job

    job = session.get(Job, job_id)
    if not job:
        return RedirectResponse(url="/jobs", status_code=303)
    if job.status in (JobStatus.QUEUED, JobStatus.RUNNING):
        return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)

    job.status = JobStatus.QUEUED
    job.error = ""
    job.progress = 0.0
    job.started_at = None
    job.finished_at = None
    session.add(job)
    session.commit()
    enqueue_job(job.id)
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@router.get("/ui/jobs/{job_id}/card", response_class=HTMLResponse)
def job_card_fragment(request: Request, job_id: int, session: Session = Depends(get_session)):
    """Картка завдання — фрагмент для htmx і для опитування, коли SSE мовчить."""
    job = session.get(Job, job_id)
    if not job:
        return HTMLResponse("", status_code=404)
    return templates.TemplateResponse(
        request, "partials/job_card.html", {"view": _job_view(job, session)}
    )


@router.get("/ui/jobs/{job_id}/segments", response_class=HTMLResponse)
def segments_fragment(request: Request, job_id: int, session: Session = Depends(get_session)):
    """Таблиця сегментів — оновлюється під час синтезу."""
    job = session.get(Job, job_id)
    if not job:
        return HTMLResponse("", status_code=404)

    segments = list(
        session.exec(
            select(Segment).where(Segment.job_id == job_id).order_by(Segment.ordinal)
        ).all()
    )
    return templates.TemplateResponse(
        request,
        "partials/segments_table.html",
        {"job": job, "segments": segments, "view": _job_view(job, session)},
    )


# ── Фрагменти (заготовки під htmx-оновлення) ──────────────────────────────────

@router.get("/ui/health", response_class=HTMLResponse)
def health_fragment(request: Request):
    """Чип стану системи — фрагмент без каркаса сторінки."""
    return templates.TemplateResponse(
        request, "partials/health_chip.html", {"health": health_snapshot()}
    )


@router.get("/ui/queue", response_class=HTMLResponse)
def queue_fragment(request: Request, session: Session = Depends(get_session)):
    """Стрічка останніх завдань — фрагмент."""
    return templates.TemplateResponse(
        request, "partials/job_strip.html", {"jobs": _recent_jobs(session)}
    )


@router.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    """Порожня відповідь, щоб браузер не логував 404 на кожній сторінці."""
    return Response(status_code=204)
