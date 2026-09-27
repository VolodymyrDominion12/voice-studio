"""Тести пробника TTS-шлюзу та запобіжників перед синтезом.

Пробник — відповідь на найдорожчу помилку цього застосунку: створити завдання
на десятки хвилин і дізнатися, що шлюз відповідає `404 Model ... is not
installed`. Тут перевіряємо і пробник, і те, що UI не дає створити приречене
завдання.

Запуск: uv run pytest tests/test_guards.py -q
"""

from __future__ import annotations

import pytest
from sqlmodel import Session, delete, select

from app.config import get_settings
from app.models import Block, Document, Job, JobStatus, Segment
from app.services.system import probe_tts_gateway, tts_ready_for_synthesis

# Текст навмисно такий, що нормалізатор його змінює (тире й числа) —
# інакше панель «Різниця» не рендериться взагалі й тест нічого не перевіряє.
SAMPLE_MD = "Перший абзац — з тире. Ще речення.\n\nУ 2007 році було 25 %.\n"


@pytest.fixture(autouse=True)
def clean_db(db_engine):
    with Session(db_engine) as session:
        for model in (Segment, Block, Job, Document):
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


# ── Пробник шлюзу ─────────────────────────────────────────────────────────────

def test_probe_reports_unreachable_gateway(monkeypatch) -> None:
    """Недоступний шлюз — це стан із підказкою, а не виняток."""
    settings = get_settings()
    monkeypatch.setattr(settings, "tts_base_url", "http://127.0.0.1:9/v1")

    probe = probe_tts_gateway(timeout=1.0)

    assert probe["reachable"] is False
    assert "недоступний" in probe["hint"]
    assert "docker compose" in probe["hint"]


def test_probe_detects_missing_model(monkeypatch) -> None:
    """Найважливіший випадок: шлюз живий, але потрібної моделі немає."""
    settings = get_settings()
    monkeypatch.setattr(settings, "tts_model", "ось-такої-моделі-немає")

    probe = probe_tts_gateway()

    if not probe["reachable"]:
        pytest.skip("TTS-шлюз не піднятий у цьому середовищі")

    assert probe["model_installed"] is False
    assert "TTS_MODEL" in probe["hint"]
    assert "ось-такої-моделі-немає" in probe["hint"]


def test_ready_for_synthesis_blocks_missing_model(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "tts_model", "ось-такої-моделі-немає")

    ready, reason = tts_ready_for_synthesis()

    if ready:
        pytest.skip("TTS-шлюз не піднятий у цьому середовищі")
    assert "немає" in reason


def test_probe_endpoint_shape(client) -> None:
    """Контракт маршруту має бути стабільним для UI і зовнішніх клієнтів."""
    body = client.get("/api/v1/health/tts").json()
    for key in ("url", "configured_model", "reachable", "latency_ms", "models",
                "model_installed", "hint"):
        assert key in body, f"немає поля {key}"


def test_settings_probe_renders_verdict(client, monkeypatch) -> None:
    """Сторінка «Система» показує вердикт пробника (регресія Jinja-тесту)."""
    monkeypatch.setattr(
        "app.ui.router.probe_tts_gateway",
        lambda: {
            "url": "http://x/v1/models",
            "configured_model": "m",
            "reachable": True,
            "latency_ms": 12,
            "models": ["m"],
            "model_installed": True,
            "hint": "",
        },
    )
    page = client.get("/settings?probe=1")
    assert page.status_code == 200
    assert "Шлюз живий" in page.text


def test_settings_probe_shows_hint_when_broken(client, monkeypatch) -> None:
    monkeypatch.setattr(
        "app.ui.router.probe_tts_gateway",
        lambda: {
            "url": "http://x/v1/models",
            "configured_model": "tts-1",
            "reachable": True,
            "latency_ms": 5,
            "models": ["piper-uk"],
            "model_installed": False,
            "hint": "Шлюз живий, але моделі 'tts-1' у ньому немає.",
        },
    )
    page = client.get("/settings?probe=1")
    assert page.status_code == 200
    assert "Синтез не працюватиме" in page.text
    assert "piper-uk" in page.text


def test_settings_without_probe_offers_button(client) -> None:
    """Без `?probe=1` сторінка не ходить у мережу — лише пропонує перевірку."""
    page = client.get("/settings")
    assert page.status_code == 200
    assert "Перевірити шлюз зараз" in page.text
    assert "Шлюз живий" not in page.text


# ── Запобіжник: не створювати приречене завдання ──────────────────────────────

def test_job_not_created_when_gateway_down(client, monkeypatch) -> None:
    doc_id = upload(client)
    monkeypatch.setattr(
        "app.ui.router.tts_ready_for_synthesis",
        lambda: (False, "Шлюз недоступний за http://127.0.0.1:9/v1"),
    )

    response = client.post(
        f"/ui/documents/{doc_id}/jobs", data={}, follow_redirects=False
    )
    assert response.status_code == 303
    assert "flash=tts_not_ready" in response.headers["location"]
    assert client.get("/api/v1/jobs").json() == [], "завдання не мало створитися"

    assert "не запущено" in client.get(f"/documents/{doc_id}?flash=tts_not_ready").text


def test_ready_gateway_still_creates_job(client, monkeypatch) -> None:
    """Запобіжник не має блокувати нормальну роботу."""
    doc_id = upload(client)
    monkeypatch.setattr("app.ui.router.tts_ready_for_synthesis", lambda: (True, ""))

    response = client.post(
        f"/ui/documents/{doc_id}/jobs", data={}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/jobs/")
    assert len(client.get("/api/v1/jobs").json()) == 1


# ── Дубль завдання ────────────────────────────────────────────────────────────

def test_second_job_for_same_document_reuses_active_one(client, monkeypatch) -> None:
    """Подвійний клік і F5 не мають плодити друге завдання."""
    doc_id = upload(client)
    monkeypatch.setattr("app.ui.router.tts_ready_for_synthesis", lambda: (True, ""))

    first = client.post(f"/ui/documents/{doc_id}/jobs", data={}, follow_redirects=False)
    second = client.post(f"/ui/documents/{doc_id}/jobs", data={}, follow_redirects=False)

    assert first.headers["location"] == "/jobs/1"
    assert second.headers["location"].startswith("/jobs/1")
    assert "flash=already_running" in second.headers["location"]
    assert len(client.get("/api/v1/jobs").json()) == 1

    assert "уже озвучується" in client.get("/jobs/1?flash=already_running").text


def test_new_job_allowed_after_previous_finished(client, monkeypatch, session) -> None:
    """Коли попереднє завдання завершилось, нове створювати можна."""
    doc_id = upload(client)
    monkeypatch.setattr("app.ui.router.tts_ready_for_synthesis", lambda: (True, ""))
    client.post(f"/ui/documents/{doc_id}/jobs", data={}, follow_redirects=False)

    job = session.exec(select(Job)).first()
    job.status = JobStatus.DONE
    session.add(job)
    session.commit()

    response = client.post(f"/ui/documents/{doc_id}/jobs", data={}, follow_redirects=False)
    assert response.headers["location"] == "/jobs/2"
    assert len(client.get("/api/v1/jobs").json()) == 2


def test_api_client_token_is_idempotent(client, monkeypatch) -> None:
    """Ключ ідемпотентності в JSON-API захищає програмних клієнтів."""
    doc_id = upload(client)
    payload = {"document_id": doc_id, "engine_id": "openai_compat", "client_token": "abc-1"}

    first = client.post("/api/v1/jobs", json=payload)
    second = client.post("/api/v1/jobs", json=payload)

    assert first.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert len(client.get("/api/v1/jobs").json()) == 1


def test_api_without_token_creates_two_jobs(client, monkeypatch) -> None:
    """Без ключа поведінка не змінюється — два запити, два завдання."""
    doc_id = upload(client)
    payload = {"document_id": doc_id, "engine_id": "openai_compat"}

    client.post("/api/v1/jobs", json=payload)
    client.post("/api/v1/jobs", json=payload)

    assert len(client.get("/api/v1/jobs").json()) == 2


# ── Назва документа в JobRead (16.8b) ─────────────────────────────────────────

def test_jobs_api_includes_document_name(client, monkeypatch) -> None:
    """Клієнтам не потрібні N+1 запити, щоб підписати завдання."""
    doc_id = upload(client, name="роман.md")
    client.post("/api/v1/jobs", json={"document_id": doc_id})

    items = client.get("/api/v1/jobs").json()
    assert items[0]["document_name"] == "роман.md"
    assert client.get(f"/api/v1/jobs/{items[0]['id']}").json()["document_name"] == "роман.md"


# ── Компактний режим ──────────────────────────────────────────────────────────

def test_compact_mode_hides_diff(client) -> None:
    doc_id = upload(client)
    normal = client.get(f"/documents/{doc_id}").text
    compact = client.get(f"/documents/{doc_id}?compact=true").text

    assert 'class="blocks"' in normal
    assert 'class="blocks compact"' in compact
    assert "Різниця: вихідний / нормалізований" in normal
    assert "Різниця: вихідний / нормалізований" not in compact
    # Блоки на місці в обох режимах
    assert normal.count("data-block-row") == compact.count("data-block-row")


def test_compact_link_switches_both_ways(client) -> None:
    doc_id = upload(client)
    assert "compact=true" in client.get(f"/documents/{doc_id}").text
    assert "Звичайний вигляд" in client.get(f"/documents/{doc_id}?compact=true").text


def test_compact_flag_propagates_to_fragment(client) -> None:
    """Довантажені рядки мають бути в тому ж режимі, що й перші."""
    doc_id = upload(client)
    fragment = client.get(f"/ui/documents/{doc_id}/blocks?offset=0&compact=true")
    assert fragment.status_code == 200
    assert "Різниця: вихідний" not in fragment.text
