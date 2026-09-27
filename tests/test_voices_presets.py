"""Тести сторінки голосів і пресетів (фаза F3).

Перевіряють те, чого раніше не існувало зовсім: прослуховування голосу
(знахідка 16.6 — таблиця `voices` не використовувалась) і CRUD пресетів
(таблиця `presets` була в моделі, але без API).

Запуск: uv run pytest tests/test_voices_presets.py -q
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlmodel import Session, delete, select

from app.config import get_settings
from app.models import Block, Document, Job, Preset, Segment

SAMPLE_MD = "Перший абзац із текстом. Ще речення.\n\nДругий абзац.\n"


@pytest.fixture(autouse=True)
def clean_db(db_engine):
    with Session(db_engine) as session:
        for model in (Segment, Block, Job, Document, Preset):
            session.exec(delete(model))
        session.commit()
    yield


def upload(client, name: str = "книга.md", content: str = SAMPLE_MD) -> int:
    response = client.post(
        "/ui/documents",
        files={"file": (name, content.encode("utf-8"), "text/markdown")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return int(response.headers["location"].split("?")[0].rsplit("/", 1)[-1])


class _FakePreview:
    def __init__(self, **overrides) -> None:
        preview_dir = get_settings().renders_dir / "preview"
        preview_dir.mkdir(parents=True, exist_ok=True)
        self.path = preview_dir / "voice_sample.wav"
        self.path.write_bytes(b"RIFF....WAVEfmt ")
        self.text = "щось"
        self.requested_chars = 10
        self.max_chars = 500
        self.voice_id = "uk_UA-tetiana-high"
        self.emotion = "neutral"
        self.intensity = 1.0
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


# ── Сторінка рушіїв і голосів ─────────────────────────────────────────────────

def test_voices_page_lists_engines_with_capabilities(client) -> None:
    page = client.get("/voices")
    assert page.status_code == 200
    assert "макс. 500 симв." in page.text          # capabilities доступного рушія
    assert "емоції нативно: ні" in page.text       # ADR-004: чесно про межі
    assert "керована просодія" in page.text


def test_voices_page_shows_unavailable_engines_with_reason(client) -> None:
    """Недоступні рушії показуємо з поясненням, а не ховаємо."""
    page = client.get("/voices").text
    assert "ZONOS2" in page
    assert "Chatterbox" in page
    assert "української серед них немає" in page
    assert "адаптера немає" in page


def test_voices_page_lists_ukrainian_voices(client) -> None:
    page = client.get("/voices").text
    for voice in ("uk_UA-tetiana-high", "uk_UA-mykyta-high", "uk_UA-lada-x_low"):
        assert voice in page


def test_every_voice_has_audition_button(client) -> None:
    page = client.get("/voices").text
    assert page.count("data-voice-audition") == 5   # п'ять українських голосів


def test_audition_uses_same_reference_phrase_for_all_voices(client, monkeypatch) -> None:
    """Порівнювати голоси на різних фразах не можна — фраза одна для всіх."""
    seen: list[str] = []

    async def fake(**kwargs):
        seen.append(kwargs["text"])
        return _FakePreview(voice_id=kwargs["voice_id"])

    monkeypatch.setattr("app.services.preview.synthesize_preview_async", fake)

    for voice in ("uk_UA-tetiana-high", "uk_UA-mykyta-high", "uk_UA-lada-x_low"):
        response = client.post("/ui/voices/preview", data={"voice_id": voice})
        assert response.status_code == 200
        assert "<audio" in response.text

    assert len(set(seen)) == 1, "еталонна фраза має бути однаковою"
    assert "приклад голосу" in seen[0]


def test_audition_reports_gateway_failure(client, monkeypatch) -> None:
    from app.services.preview import PreviewUnavailableError

    async def fake(**kwargs):
        raise PreviewUnavailableError("connection refused")

    monkeypatch.setattr("app.services.preview.synthesize_preview_async", fake)

    response = client.post("/ui/voices/preview", data={"voice_id": "uk_UA-tetiana-high"})
    assert response.status_code == 200
    assert "Прослухати не вдалося" in response.text
    assert "TTS-шлюз" in response.text


def test_voices_js_served(client) -> None:
    assert "/static/js/voices.js" in client.get("/voices").text
    assert client.get("/static/js/voices.js").status_code == 200


# ── Пресети: API ──────────────────────────────────────────────────────────────

def test_preset_crud_via_api(client) -> None:
    created = client.post(
        "/api/v1/presets",
        json={
            "name": "Аудіокнига",
            "engine_id": "openai_compat",
            "voice_id": "uk_UA-mykyta-high",
            "emotion": "warm",
            "intensity": 0.6,
        },
    )
    assert created.status_code == 201
    preset_id = created.json()["id"]
    assert created.json()["emotion"] == "warm"

    assert len(client.get("/api/v1/presets").json()) == 1

    renamed = client.patch(f"/api/v1/presets/{preset_id}", json={"name": "Книга, теплий"})
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Книга, теплий"

    assert client.delete(f"/api/v1/presets/{preset_id}").status_code == 204
    assert client.get("/api/v1/presets").json() == []


def test_preset_names_must_be_unique(client) -> None:
    client.post("/api/v1/presets", json={"name": "Однакова"})
    duplicate = client.post("/api/v1/presets", json={"name": "Однакова"})
    assert duplicate.status_code == 409
    assert "уже є" in duplicate.json()["detail"]


def test_preset_empty_name_rejected(client) -> None:
    assert client.post("/api/v1/presets", json={"name": "   "}).status_code == 422


def test_preset_not_found(client) -> None:
    assert client.patch("/api/v1/presets/999", json={"name": "x"}).status_code == 404
    assert client.delete("/api/v1/presets/999").status_code == 404


def test_preset_intensity_bounds(client) -> None:
    assert client.post("/api/v1/presets", json={"name": "x", "intensity": 1.5}).status_code == 422


# ── Пресети: застосування ─────────────────────────────────────────────────────

def test_apply_preset_sets_emotion_on_speakable_blocks(client, session) -> None:
    doc_id = upload(client)
    preset = client.post(
        "/api/v1/presets", json={"name": "Сумно", "emotion": "sad", "intensity": 0.8}
    ).json()

    # Один блок позначимо як «не озвучувати» — його чіпати не можна
    blocks = list(session.exec(select(Block).where(Block.document_id == doc_id)).all())
    blocks[0].speak = False
    session.add(blocks[0])
    session.commit()

    response = client.post(
        f"/api/v1/presets/{preset['id']}/apply", json={"document_id": doc_id}
    )
    assert response.status_code == 200
    assert response.json()["blocks_changed"] == len(blocks) - 1

    session.expire_all()
    updated = list(session.exec(select(Block).where(Block.document_id == doc_id)).all())
    silenced = [b for b in updated if not b.speak]
    speakable = [b for b in updated if b.speak]
    assert silenced[0].emotion == "neutral", "блок без озвучення не мав змінитися"
    assert all(b.emotion == "sad" for b in speakable)
    assert all(b.intensity == pytest.approx(0.8) for b in speakable)


def test_apply_preset_from_editor_ui(client, session) -> None:
    doc_id = upload(client)
    preset = client.post(
        "/api/v1/presets", json={"name": "Радісно", "emotion": "joyful", "intensity": 1.0}
    ).json()

    response = client.post(
        f"/ui/documents/{doc_id}/presets/{preset['id']}/apply", follow_redirects=False
    )
    assert response.status_code == 303
    assert "flash=preset_applied" in response.headers["location"]

    session.expire_all()
    blocks = list(session.exec(select(Block).where(Block.document_id == doc_id)).all())
    assert all(b.emotion == "joyful" for b in blocks)

    # Повідомлення має бути видно користувачу
    assert "Емоцію пресета застосовано" in client.get("/documents/1?flash=preset_applied").text


def test_apply_missing_preset_is_not_an_error(client) -> None:
    """Застаріле посилання не має валити сторінку."""
    doc_id = upload(client)
    response = client.post(
        f"/ui/documents/{doc_id}/presets/999/apply", follow_redirects=False
    )
    assert response.status_code == 303


# ── Пресети в редакторі ───────────────────────────────────────────────────────

def test_editor_preset_selects_engine_and_voice(client) -> None:
    doc_id = upload(client)
    preset = client.post(
        "/api/v1/presets",
        json={
            "name": "Микита",
            "engine_id": "openai_compat",
            "voice_id": "uk_UA-mykyta-high",
            "emotion": "serious",
            "intensity": 0.7,
        },
    ).json()

    page = client.get(f"/documents/{doc_id}?preset={preset['id']}").text
    assert 'value="uk_UA-mykyta-high" selected' in page
    assert "Застосувати емоцію до всіх блоків" in page
    assert "серйозно" in page                      # емоція пресета показана


def test_editor_saves_preset_from_toolbar(client, session) -> None:
    doc_id = upload(client)
    response = client.post(
        "/ui/presets",
        data={
            "name": "З редактора",
            "engine": "openai_compat",
            "voice": "uk_UA-oleksa-high",
            "emotion": "neutral",
            "intensity": 0.5,
            "back": f"/documents/{doc_id}",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/documents/{doc_id}"

    saved = list(session.exec(select(Preset)).all())
    assert len(saved) == 1
    assert saved[0].voice_id == "uk_UA-oleksa-high"


def test_editor_preset_list_is_offered(client) -> None:
    doc_id = upload(client)
    client.post("/api/v1/presets", json={"name": "Мій пресет", "voice_id": "uk_UA-lada-x_low"})
    page = client.get(f"/documents/{doc_id}").text
    assert "Мій пресет" in page
    assert "не вибрано" in page


def test_unknown_preset_in_url_does_not_break_editor(client) -> None:
    doc_id = upload(client)
    response = client.get(f"/documents/{doc_id}?preset=999")
    assert response.status_code == 200
    assert "data-block-row" in response.text


def test_presets_visible_on_voices_page(client) -> None:
    client.post("/api/v1/presets", json={"name": "Для сторінки", "emotion": "warm"})
    page = client.get("/voices").text
    assert "Для сторінки" in page
    assert "тепло" in page


def test_preset_delete_from_ui(client) -> None:
    preset = client.post("/api/v1/presets", json={"name": "Тимчасовий"}).json()
    response = client.post(
        f"/ui/presets/{preset['id']}/delete", data={"back": "/voices"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert client.get("/api/v1/presets").json() == []


def test_preset_templates_exist() -> None:
    root = Path(get_settings().templates_dir)
    assert (root / "pages/voices.html").is_file()
    assert (root / "partials/voice_preview.html").is_file()
