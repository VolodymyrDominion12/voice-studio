"""Тести редактора (фаза F1, docs/FRONTEND.md).

Перевіряють серверну частину редактора: розмітку контролів, фрагменти для
htmx, фільтри, прев'ю (із замоканним TTS) і семантику «повернути до
нормалізованого».

Клієнтська логіка (автозбереження, просодія в браузері) синтаксично
перевіряється `node --check`, а її формула просодії звірена з Python
у `test_normalize.py`-стилі нижче — тут для неї є окремий тест.

Запуск: uv run pytest tests/test_editor.py -q
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlmodel import Session, delete, select

from app.config import get_settings
from app.models import Block, Document, Job, Segment
from app.services.expression.profiles import get_prosody

SAMPLE_MD = (
    "Розділ перший\n\n"
    "Доброго ранку, — сказала вона й усміхнулась.\n\n"
    "У 2007 році їх було 25 %, а ціна — 1500 грн.\n"
)


@pytest.fixture(autouse=True)
def clean_db(db_engine):
    with Session(db_engine) as session:
        for model in (Segment, Block, Job, Document):
            session.exec(delete(model))
        session.commit()
    yield


def upload_sample(client, content: str = SAMPLE_MD, name: str = "оповідання.md") -> str:
    response = client.post(
        "/ui/documents",
        files={"file": (name, content.encode("utf-8"), "text/markdown")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return response.headers["location"].split("?")[0]


def block_ids(html: str) -> list[int]:
    import re

    return [int(m) for m in re.findall(r'data-block-id="(\d+)"', html)]


# ── Розмітка редактора ────────────────────────────────────────────────────────

def test_editor_has_all_controls(client) -> None:
    path = upload_sample(client)
    page = client.get(path)
    assert page.status_code == 200
    for control in (
        "data-text",             # редагований текст
        "data-emotion-select",   # емоція
        "data-intensity-range",  # інтенсивність
        "data-speak",            # озвучувати/ні
        "data-pick",             # вибір для масових дій
        "data-preview-btn",      # прослухати блок
        "data-revert-btn",       # повернути до нормалізованого
        "data-normalized-source",
    ):
        assert control in page.text, f"немає контролу {control}"


def test_editor_ships_vendored_js(client) -> None:
    """htmx і Alpine віддаються локально — сторінка не залежить від мережі."""
    page = client.get(upload_sample(client)).text
    for asset in ("/static/vendor/htmx.min.js", "/static/vendor/alpine.min.js",
                  "/static/js/editor.js"):
        assert asset in page
        assert client.get(asset).status_code == 200


def test_profiles_for_browser_match_python(client) -> None:
    """Формула просодії в браузері має ту саму таблицю, що й бекенд."""
    page = client.get(upload_sample(client)).text
    start = page.index('id="vs-profiles">') + len('id="vs-profiles">')
    payload = json.loads(page[start : page.index("</script>", start)])

    for name in ("neutral", "warm", "sad", "excited"):
        python_profile = get_prosody(name, 1.0)
        browser_profile = payload[name]
        assert browser_profile["speed"] == pytest.approx(python_profile.speed)
        assert browser_profile["pause_after_ms"] == python_profile.pause_after_ms


# ── Правка блоків (JSON-API, який викликає автозбереження) ────────────────────

def test_block_edit_then_revert(client, session) -> None:
    path = upload_sample(client)
    doc_id = int(path.rsplit("/", 1)[-1])
    first = session.exec(select(Block).where(Block.document_id == doc_id)).first()
    normalized = first.text_normalized

    # Автозбереження надсилає text_edited
    response = client.patch(
        f"/api/v1/documents/{doc_id}/blocks/{first.id}",
        json={"text_edited": "Мій власний текст."},
    )
    assert response.status_code == 200
    assert response.json()["text_edited"] == "Мій власний текст."

    # «Повернути до нормалізованого» = порожній text_edited
    response = client.patch(
        f"/api/v1/documents/{doc_id}/blocks/{first.id}", json={"text_edited": ""}
    )
    assert response.status_code == 200
    assert response.json()["text_edited"] == ""
    assert normalized  # нормалізований текст лишається джерелом


def test_emotion_and_intensity_update(client, session) -> None:
    path = upload_sample(client)
    doc_id = int(path.rsplit("/", 1)[-1])
    block = session.exec(select(Block).where(Block.document_id == doc_id)).first()

    response = client.patch(
        f"/api/v1/documents/{doc_id}/blocks/{block.id}",
        json={"emotion": "warm", "intensity": 0.7},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["emotion"] == "warm"
    assert body["intensity"] == pytest.approx(0.7)


def test_bulk_emotion_applies_to_all(client, session) -> None:
    path = upload_sample(client)
    doc_id = int(path.rsplit("/", 1)[-1])
    ids = [b.id for b in list(session.exec(select(Block).where(Block.document_id == doc_id)).all())]

    response = client.patch(
        f"/api/v1/documents/{doc_id}/blocks",
        json={"updates": [{"block_id": i, "emotion": "serious"} for i in ids]},
    )
    assert response.status_code == 200
    assert all(item["emotion"] == "serious" for item in response.json())


def test_bulk_update_rejects_foreign_block(client, session) -> None:
    """Блок з іншого документа не можна правити через цей маршрут."""
    first = upload_sample(client, name="a.md")
    second = upload_sample(client, name="b.md", content="Інший текст. Ще речення.\n")
    second_id = int(second.rsplit("/", 1)[-1])
    other_block = session.exec(select(Block).where(Block.document_id == second_id)).first()

    response = client.patch(
        f"/api/v1/documents/{int(first.rsplit('/', 1)[-1])}/blocks",
        json={"updates": [{"block_id": other_block.id, "emotion": "warm"}]},
    )
    assert response.status_code == 404


# ── Фільтри ───────────────────────────────────────────────────────────────────

def test_search_filter(client) -> None:
    path = upload_sample(client)
    assert len(block_ids(client.get(path).text)) == 3
    assert len(block_ids(client.get(f"{path}?q=ранку").text)) == 1
    assert len(block_ids(client.get(f"{path}?q=немаєтакого").text)) == 0


def test_only_edited_filter(client) -> None:
    path = upload_sample(client)
    doc_id = int(path.rsplit("/", 1)[-1])
    assert len(block_ids(client.get(f"{path}?only_edited=true").text)) == 0

    first_id = block_ids(client.get(path).text)[0]
    client.patch(f"/api/v1/documents/{doc_id}/blocks/{first_id}",
                 json={"text_edited": "Змінено."})
    assert len(block_ids(client.get(f"{path}?only_edited=true").text)) == 1


def test_emotion_filter(client) -> None:
    path = upload_sample(client)
    doc_id = int(path.rsplit("/", 1)[-1])
    assert len(block_ids(client.get(f"{path}?emotion=warm").text)) == 0

    first_id = block_ids(client.get(path).text)[0]
    client.patch(f"/api/v1/documents/{doc_id}/blocks/{first_id}", json={"emotion": "warm"})
    assert len(block_ids(client.get(f"{path}?emotion=warm").text)) == 1


def test_blocks_fragment_pages(client, monkeypatch) -> None:
    from app.ui import router as ui_router

    monkeypatch.setattr(ui_router, "BLOCKS_PER_PAGE", 2)
    content = "\n\n".join(f"Абзац {i}." for i in range(5))
    path = upload_sample(client, content, name="багато.md")
    doc_id = int(path.rsplit("/", 1)[-1])

    first_page = client.get(path)
    assert len(block_ids(first_page.text)) == 2
    assert "more-sentinel" in first_page.text

    second_page = client.get(f"/ui/documents/{doc_id}/blocks?offset=2")
    assert second_page.status_code == 200
    assert len(block_ids(second_page.text)) == 2

    last_page = client.get(f"/ui/documents/{doc_id}/blocks?offset=4")
    assert len(block_ids(last_page.text)) == 1
    assert "more-sentinel" not in last_page.text


# ── Прев'ю ────────────────────────────────────────────────────────────────────

class _FakePreview:
    """Підміна результату синтезу — у тестах мережу не чіпаємо."""

    def __init__(self, **overrides) -> None:
        settings = get_settings()
        preview_dir = settings.renders_dir / "preview"
        preview_dir.mkdir(parents=True, exist_ok=True)
        self.path = preview_dir / "fake_preview.wav"
        self.path.write_bytes(b"RIFF....WAVEfmt ")

        self.text = "щось"
        self.requested_chars = 10
        self.max_chars = 500
        self.voice_id = "uk_UA-tetiana-high"
        self.emotion = "neutral"
        self.intensity = 0.5
        self.speed = 1.0
        self.pause_after_ms = 400
        self.cached = False
        for key, value in overrides.items():
            setattr(self, key, value)

    @property
    def truncated(self) -> bool:
        return self.requested_chars > self.max_chars

    @property
    def effective_chars(self) -> int:
        return len(self.text)


def test_preview_success_fragment(client, monkeypatch) -> None:
    async def fake(**kwargs):
        return _FakePreview(text="Доброго ранку", requested_chars=14)

    monkeypatch.setattr("app.services.preview.synthesize_preview_async", fake)

    path = upload_sample(client)
    doc_id = int(path.rsplit("/", 1)[-1])
    block_id = block_ids(client.get(path).text)[0]

    response = client.post(
        f"/ui/blocks/{block_id}/preview", data={"voice_id": "uk_UA-tetiana-high"}
    )
    assert response.status_code == 200
    assert "<audio" in response.text
    assert "/ui/preview/fake_preview.wav" in response.text
    # Пауза в прев'ю не звучить — це має бути сказано прямо
    assert "під час склейки" in response.text
    assert doc_id  # документ використано


def test_preview_reports_truncation(client, monkeypatch) -> None:
    async def fake(**kwargs):
        return _FakePreview(text="х" * 500, requested_chars=780, max_chars=500)

    monkeypatch.setattr("app.services.preview.synthesize_preview_async", fake)

    path = upload_sample(client)
    block_id = block_ids(client.get(path).text)[0]
    body = client.post(f"/ui/blocks/{block_id}/preview").text

    assert "Долетіло 500 із 780" in body


def test_preview_gateway_down_shows_hint(client, monkeypatch) -> None:
    from app.services.preview import PreviewUnavailableError

    async def fake(**kwargs):
        raise PreviewUnavailableError("connection refused")

    monkeypatch.setattr("app.services.preview.synthesize_preview_async", fake)

    path = upload_sample(client)
    block_id = block_ids(client.get(path).text)[0]
    response = client.post(f"/ui/blocks/{block_id}/preview")

    # Не 500: користувач має побачити зрозумілу підказку
    assert response.status_code == 200
    assert "TTS-шлюз не відповідає" in response.text
    assert "Docker" in response.text


def test_preview_rejects_unimplemented_engine(client) -> None:
    """Недоступний рушій не підмінюється мовчки — і до мережі не доходить.

    Мок тут навмисно відсутній: перевірка рушія відбувається всередині
    synthesize_preview() ДО звернення до шлюзу, тож підміна функції цілком
    обійшла б саме те, що перевіряється.
    """
    path = upload_sample(client)
    block_id = block_ids(client.get(path).text)[0]

    body = client.post(f"/ui/blocks/{block_id}/preview", data={"engine_id": "zonos2"}).text

    assert "ще не реалізовано" in body
    assert "<audio" not in body


def test_preview_rejects_unknown_engine(client) -> None:
    path = upload_sample(client)
    block_id = block_ids(client.get(path).text)[0]
    body = client.post(f"/ui/blocks/{block_id}/preview", data={"engine_id": "неіснуючий"}).text
    assert "не знайдено в каталозі" in body


def test_preview_audio_route_serves_only_preview_dir(client) -> None:
    settings = get_settings()
    preview_dir = settings.renders_dir / "preview"
    preview_dir.mkdir(parents=True, exist_ok=True)
    (preview_dir / "ok.wav").write_bytes(b"RIFF....WAVE")

    assert client.get("/ui/preview/ok.wav").status_code == 200
    assert client.get("/ui/preview/missing.wav").status_code == 404
    # Імʼя файлу зі шляхом не має виводити за межі теки прев'ю
    assert client.get("/ui/preview/..%2F..%2Fsecret.txt").status_code in (404, 400)


# ── Синхронізація з воркером ──────────────────────────────────────────────────

def test_effective_text_rule_is_shared(client, session) -> None:
    """Те, що показує редактор, і те, що озвучує воркер — одне правило."""
    from app.services.library.blocks import block_effective_text

    path = upload_sample(client)
    doc_id = int(path.rsplit("/", 1)[-1])
    block = session.exec(select(Block).where(Block.document_id == doc_id)).first()

    assert block_effective_text(block) == block.text_normalized

    client.patch(f"/api/v1/documents/{doc_id}/blocks/{block.id}",
                 json={"text_edited": "Мій текст."})
    session.refresh(block)
    assert block_effective_text(block) == "Мій текст."


def test_render_helpers_are_registered() -> None:
    """Спільні фільтри Jinja не мають зникати при рефакторингу шаблонів."""
    from app.ui.templating import templates

    for name in ("duration", "bytesize", "percent", "estimate"):
        assert name in templates.env.filters
    for name in ("emotion_label", "emotion_icon", "prosody_summary", "job_status_chip"):
        assert name in templates.env.globals


def test_templates_dir_contains_expected_files() -> None:
    root = Path(get_settings().templates_dir)
    for relative in (
        "layout/base.html",
        "pages/workspace.html",
        "pages/document.html",
        "pages/settings.html",
        "pages/not_found.html",
        "partials/block_row.html",
        "partials/block_list.html",
        "partials/preview_player.html",
    ):
        assert (root / relative).is_file(), f"немає шаблону {relative}"
