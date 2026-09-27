"""Тести чесності щодо голосів: що шлюз справді має, а що лише в каталозі.

Привід — реальна знахідка: запит із голосом `uk_UA-tetiana-high` до моделі
`piper-uk_UA-lada-x_low` повертає **200** і звук голосом `lada`. Тобто шлюз
приймає будь-яке імʼя голосу, а озвучує тим, що встановлений. Без перевірки
сторінка «Голоси» показувала б пʼять різних голосів, які звучать однаково.

Запуск: uv run pytest tests/test_voice_availability.py -q
"""

from __future__ import annotations

import pytest

from app.services.system import (
    installed_voices_label,
    probe_tts_gateway_cached,
    reset_probe_cache,
    voice_is_installed,
)


def probe_with(models: list[dict], configured: str = "speaches-ai/piper-uk_UA-lada-x_low"):
    """Знімок пробника з довільним вмістом шлюзу."""
    entry = next((m for m in models if m.get("id") == configured), None)
    return {
        "url": "http://x/v1/models",
        "configured_model": configured,
        "reachable": True,
        "latency_ms": 5,
        "models": [m.get("id", "") for m in models],
        "model_voices": [
            v.get("id", "") for v in (entry or {}).get("voices", [])
        ],
        "model_sample_rate": (entry or {}).get("sample_rate"),
        "model_installed": entry is not None,
        "hint": "",
    }


# ── Логіка порівняння імен ────────────────────────────────────────────────────

def test_installed_voice_matches_by_normalised_name() -> None:
    """Каталог каже `uk_UA-lada-x_low`, шлюз — `lada`: це той самий голос."""
    probe = probe_with([
        {"id": "speaches-ai/piper-uk_UA-lada-x_low", "voices": [{"id": "lada"}]}
    ])
    assert voice_is_installed(probe, "uk_UA-lada-x_low") is True


def test_voice_from_model_id_is_recognised() -> None:
    """Якщо шлюз не перелічив голосів, орієнтуємось на id моделі."""
    probe = probe_with([{"id": "speaches-ai/piper-uk_UA-lada-x_low"}])
    # model_voices порожній → перевірити неможливо
    assert voice_is_installed(probe, "uk_UA-lada-x_low") is None


def test_missing_voice_is_reported_as_missing() -> None:
    probe = probe_with([
        {"id": "speaches-ai/piper-uk_UA-lada-x_low", "voices": [{"id": "lada"}]}
    ])
    for voice in ("uk_UA-tetiana-high", "uk_UA-mykyta-high", "uk_UA-oleksa-high"):
        assert voice_is_installed(probe, voice) is False


def test_unknown_state_when_gateway_unreachable() -> None:
    """Якщо перевірити не вдалося — жодних тверджень, лише None."""
    probe = probe_with([])
    probe["reachable"] = False
    assert voice_is_installed(probe, "uk_UA-lada-x_low") is None


def test_unknown_state_when_model_not_installed() -> None:
    probe = probe_with([{"id": "інша-модель", "voices": [{"id": "x"}]}])
    assert voice_is_installed(probe, "uk_UA-lada-x_low") is None


def test_installed_voices_label_lists_gateway_voices() -> None:
    probe = probe_with([
        {"id": "speaches-ai/piper-uk_UA-lada-x_low", "voices": [{"id": "lada"}, {"id": "mykyta"}]}
    ])
    assert installed_voices_label(probe) == "lada, mykyta"


def test_installed_voices_label_empty_when_unknown() -> None:
    assert installed_voices_label({"model_voices": []}) == ""


# ── Кеш пробника ──────────────────────────────────────────────────────────────

def test_probe_cache_avoids_repeat_calls(monkeypatch) -> None:
    """Сторінки не мають ходити в мережу на кожен рендер."""
    from app.services import system

    calls: list[int] = []

    def counting_probe(*args, **kwargs):
        calls.append(1)
        return probe_with([{"id": "m", "voices": [{"id": "v"}]}])

    reset_probe_cache()
    monkeypatch.setattr(system, "probe_tts_gateway", counting_probe)

    probe_tts_gateway_cached()
    probe_tts_gateway_cached()
    probe_tts_gateway_cached()

    assert len(calls) == 1, "пробник мав викликатися один раз"

    reset_probe_cache()
    probe_tts_gateway_cached()
    assert len(calls) == 2, "після скидання кешу — новий виклик"


def test_probe_cache_expires(monkeypatch) -> None:
    from app.services import system

    calls: list[int] = []

    def counting_probe(*args, **kwargs):
        calls.append(1)
        return probe_with([{"id": "m", "voices": [{"id": "v"}]}])

    reset_probe_cache()
    monkeypatch.setattr(system, "probe_tts_gateway", counting_probe)

    probe_tts_gateway_cached(max_age=0.0)
    probe_tts_gateway_cached(max_age=0.0)

    assert len(calls) == 2
    reset_probe_cache()


# ── Сторінка голосів ──────────────────────────────────────────────────────────

def test_voices_page_marks_missing_voices(client) -> None:
    """Стандартний стан із conftest: шлюз має лише `lada`."""
    page = client.get("/voices").text

    assert "встановлено" in page
    assert "немає в шлюзі" in page
    assert "у шлюзі" in page
    # Пояснення, чому пʼять голосів звучатимуть однаково
    assert "звучатимуть голосом" in page


def test_voices_page_counts_missing_voices(client) -> None:
    page = client.get("/voices").text
    # Каталог має 5 голосів, встановлено 1 → 4 відсутні
    assert page.count("немає в шлюзі") == 4


def test_voices_page_reports_unverified_state(client, monkeypatch) -> None:
    """Якщо шлюз недоступний, сторінка не вигадує, а каже що не перевірено."""
    from app.services import system

    reset_probe_cache()
    monkeypatch.setattr(
        system,
        "probe_tts_gateway",
        lambda *a, **k: {
            "url": "http://x/v1/models", "configured_model": "m", "reachable": False,
            "latency_ms": None, "models": [], "model_voices": [],
            "model_sample_rate": None, "model_installed": None,
            "hint": "connection refused",
        },
    )

    page = client.get("/voices")
    assert page.status_code == 200
    assert "не перевірено" in page.text
    assert "не вдалося перевірити" in page.text.lower()

    reset_probe_cache()


# ── Прослуховування голосу ────────────────────────────────────────────────────

def test_audition_warns_when_voice_not_installed(client, monkeypatch) -> None:
    """Користувач має знати, що чує не той голос, який вибрав."""
    from app.config import get_settings
    from app.services.preview import PreviewUnavailableError

    preview_dir = get_settings().renders_dir / "preview"
    preview_dir.mkdir(parents=True, exist_ok=True)

    class FakeResult:
        path = preview_dir / "warning.wav"
        text = "щось"
        requested_chars = 10
        max_chars = 500
        voice_id = "uk_UA-tetiana-high"
        emotion = "neutral"
        intensity = 1.0
        speed = 1.0
        pause_after_ms = 400
        cached = False

        @property
        def truncated(self) -> bool:
            return False

        @property
        def effective_chars(self) -> int:
            return len(self.text)

    async def fake(**kwargs):
        return FakeResult()

    monkeypatch.setattr("app.services.preview.synthesize_preview_async", fake)
    assert PreviewUnavailableError  # імпорт використано

    response = client.post("/ui/voices/preview", data={"voice_id": "uk_UA-tetiana-high"})
    assert response.status_code == 200
    assert "немає в шлюзі" in response.text
    assert "ви чуєте голос" in response.text
    assert "lada" in response.text


def test_audition_has_no_warning_for_installed_voice(client, monkeypatch) -> None:
    from app.config import get_settings

    preview_dir = get_settings().renders_dir / "preview"
    preview_dir.mkdir(parents=True, exist_ok=True)

    class FakeResult:
        path = preview_dir / "ok.wav"
        text = "щось"
        requested_chars = 10
        max_chars = 500
        voice_id = "uk_UA-lada-x_low"
        emotion = "neutral"
        intensity = 1.0
        speed = 1.0
        pause_after_ms = 400
        cached = False

        @property
        def truncated(self) -> bool:
            return False

        @property
        def effective_chars(self) -> int:
            return len(self.text)

    async def fake(**kwargs):
        return FakeResult()

    monkeypatch.setattr("app.services.preview.synthesize_preview_async", fake)

    response = client.post("/ui/voices/preview", data={"voice_id": "uk_UA-lada-x_low"})
    assert response.status_code == 200
    assert "ви чуєте голос" not in response.text


# ── Редактор ──────────────────────────────────────────────────────────────────

def test_editor_marks_voices_missing_in_gateway(client) -> None:
    doc_id = client.post(
        "/ui/documents",
        files={"file": ("k.md", "Текст для перевірки.\n".encode(), "text/markdown")},
        follow_redirects=False,
    ).headers["location"]
    page = client.get(doc_id.split("?")[0]).text

    assert "у шлюзі: lada" in page
    assert "(немає в шлюзі)" in page


@pytest.mark.parametrize(
    ("voice", "expected"),
    [
        ("uk_UA-lada-x_low", True),
        ("uk_UA-tetiana-high", False),
    ],
)
def test_editor_marks_selected_voice_status(client, voice, expected) -> None:
    from app.services.system import probe_tts_gateway_cached, voice_is_installed

    probe = probe_tts_gateway_cached()
    assert voice_is_installed(probe, voice) is expected
