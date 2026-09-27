"""Тести повного конвеєра синтезу (фаза F2).

Живий TTS-шлюз тут не потрібен: рушій підмінюється функцією, яка пише
справжній WAV-файл. Це дає змогу перевірити весь шлях —
сегментація → синтез → склейка → LUFS → MP3 → завантаження — і водночас
не залежати від Docker і завантажених моделей.

Запуск: uv run pytest tests/test_jobs_flow.py -q
"""

from __future__ import annotations

import math
import struct
import time
import wave
from pathlib import Path

import pytest
from sqlmodel import Session, delete, select

from app.models import Block, Document, Job, JobStatus, Segment
from app.worker.queue import requeue_incomplete_jobs

SAMPLE_MD = (
    "Перший абзац. Друге речення тут.\n\n"
    "Другий абзац із текстом. І ще одне речення.\n"
)


@pytest.fixture(autouse=True)
def clean_db(db_engine):
    """Порожня БД і порожні теки рендерів перед кожним тестом.

    Файли чистимо обовʼязково: ідемпотентність тримається саме на них
    (готовий WAV = не синтезувати вдруге), тож залишок від попереднього
    тесту слушно скасовував би синтез у наступному — і тест бачив би
    «нуль викликів синтезу» там, де очікував роботу.
    """
    from app.config import get_settings

    with Session(db_engine) as session:
        for model in (Segment, Block, Job, Document):
            session.exec(delete(model))
        session.commit()

    renders = get_settings().renders_dir
    for sub in ("segments", "preview"):
        folder = renders / sub
        if folder.is_dir():
            for path in folder.iterdir():
                if path.is_file():
                    path.unlink()
    yield


@pytest.fixture(autouse=True)
def worker_uses_test_db(db_engine, monkeypatch):
    """Воркер має писати в ТУ САМУ БД, що й API.

    `app.worker.queue` бере сесії через власний `get_engine()`, тому
    підміна залежності FastAPI (`get_session`) його не стосується: без цієї
    фікстури воркер писав би у файлову БД, а тести чекали б статусу, який
    ніколи не зʼявиться в тестовій.
    """
    monkeypatch.setattr("app.worker.queue.get_engine", lambda: db_engine)
    yield


@pytest.fixture
def fake_engine(monkeypatch):
    """Підмінити синтез: записати короткий валідний WAV замість звернення до шлюзу."""
    calls: list[Path] = []

    def fake_synthesize(self, request):
        target = Path(request.output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_tone(target, seconds=0.15, sample_rate=22050)
        calls.append(target)
        return target

    monkeypatch.setattr(
        "app.services.tts.openai_compat.OpenAICompatEngine.synthesize", fake_synthesize
    )
    return calls


def _write_tone(path: Path, seconds: float, sample_rate: int) -> None:
    """Записати справжній моно-WAV — його читає soundfile під час склейки."""
    frames = int(seconds * sample_rate)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        data = b"".join(
            struct.pack("<h", int(12000 * math.sin(2 * math.pi * 440 * i / sample_rate)))
            for i in range(frames)
        )
        handle.writeframes(data)


def upload(client, name: str = "книга.md", content: str = SAMPLE_MD) -> int:
    response = client.post(
        "/ui/documents",
        files={"file": (name, content.encode("utf-8"), "text/markdown")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return int(response.headers["location"].split("?")[0].rsplit("/", 1)[-1])


def wait_for_status(client, job_id: int, statuses=("done", "failed", "cancelled"), timeout=12.0):
    """Дочекатися кінцевого стану завдання (воркер працює у своєму потоці)."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = client.get(f"/api/v1/jobs/{job_id}").json()
        if last["status"] in statuses:
            return last
        time.sleep(0.15)
    raise AssertionError(f"Завдання не завершилось за {timeout} с: {last}")


# ── Повний шлях ───────────────────────────────────────────────────────────────

def test_full_synthesis_pipeline(client, fake_engine) -> None:
    """Від «Озвучити все» до готового MP3 — без жодного curl."""
    doc_id = upload(client)

    response = client.post(
        f"/ui/documents/{doc_id}/jobs",
        data={"voice": "uk_UA-tetiana-high", "engine": "openai_compat"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    job_id = int(response.headers["location"].rsplit("/", 1)[-1])

    job = wait_for_status(client, job_id)
    assert job["status"] == "done", job["error"]
    assert job["progress"] == pytest.approx(1.0)
    assert job["output_path"].endswith(".mp3")
    assert Path(job["output_path"]).is_file()
    assert fake_engine, "синтез мав бути викликаний"

    # Сегменти збережені й доступні для прослуховування
    segments = client.get(f"/api/v1/jobs/{job_id}/segments").json()
    assert segments["total"] >= 2
    assert all(item["status"] == "done" for item in segments["segments"])
    assert all(item["prosody"] for item in segments["segments"])

    audio = client.get(f"/api/v1/jobs/{job_id}/segments/0/audio")
    assert audio.status_code == 200
    assert audio.headers["content-type"] == "audio/wav"

    # Завантаження віддає реальний формат
    download = client.get(f"/api/v1/jobs/{job_id}/download")
    assert download.status_code == 200
    assert download.headers["content-type"] == "audio/mpeg"


def test_download_rejects_mismatched_format(client, fake_engine) -> None:
    """`?format=wav` на MP3-результаті — честна помилка, а не підміна заголовка."""
    doc_id = upload(client)
    client.post(f"/ui/documents/{doc_id}/jobs", data={}, follow_redirects=False)
    job = wait_for_status(client, 1)
    assert job["status"] == "done"

    response = client.get("/api/v1/jobs/1/download?format=wav")
    assert response.status_code == 409
    assert "wav" in response.json()["detail"]


def test_job_page_reaches_done_and_offers_download(client, fake_engine) -> None:
    doc_id = upload(client)
    client.post(f"/ui/documents/{doc_id}/jobs", data={}, follow_redirects=False)
    wait_for_status(client, 1)

    page = client.get("/jobs/1").text
    assert "готово" in page
    assert "/api/v1/jobs/1/download" in page
    assert "MP3" in page


# ── Ідемпотентність і повтори ─────────────────────────────────────────────────

def test_retry_does_not_duplicate_segments(client, fake_engine) -> None:
    """Повторний запуск перебудовує рядки сегментів, а не додає дублі (16.8c)."""
    doc_id = upload(client)
    client.post(f"/ui/documents/{doc_id}/jobs", data={}, follow_redirects=False)
    wait_for_status(client, 1)
    first = client.get("/api/v1/jobs/1/segments").json()["total"]

    # Повтор: файли на диску лишаються, тож синтез не виконується вдруге
    assert client.post("/ui/jobs/1/retry", follow_redirects=False).status_code == 303
    wait_for_status(client, 1)
    second = client.get("/api/v1/jobs/1/segments").json()["total"]

    assert first == second, "сегменти задублювались після повтору"


def test_retry_skips_already_synthesized_files(client, fake_engine) -> None:
    """Ідемпотентність: готові WAV не синтезуються ще раз (ADR-007)."""
    doc_id = upload(client)
    client.post(f"/ui/documents/{doc_id}/jobs", data={}, follow_redirects=False)
    wait_for_status(client, 1)
    calls_after_first = len(fake_engine)
    assert calls_after_first > 0

    client.post("/ui/jobs/1/retry", follow_redirects=False)
    wait_for_status(client, 1)

    assert len(fake_engine) == calls_after_first, "файли пересинтезовано попри наявність"


def test_creating_job_without_speakable_blocks_is_rejected(client) -> None:
    """Порожній документ не створює завдання, яке одразу впаде (16.8d)."""
    doc_id = upload(client, name="порожній.md", content="   \n\n   \n")
    response = client.post(
        "/api/v1/jobs", json={"document_id": doc_id, "engine_id": "openai_compat"}
    )
    assert response.status_code == 422
    assert "озвучуваного блоку" in response.json()["detail"]


def test_job_for_missing_document_is_404(client) -> None:
    response = client.post(
        "/api/v1/jobs", json={"document_id": 4242, "engine_id": "openai_compat"}
    )
    assert response.status_code == 404


# ── Відновлення після перезапуску процесу (знахідка 16.2) ─────────────────────

def test_requeue_incomplete_jobs(client, session) -> None:
    """Після рестарту незавершені завдання повертаються в чергу.

    Саме цього кроку бракувало: черга живе в памʼяті процесу, тож завдання
    в статусі `running` лишалось таким назавжди.
    """
    doc_id = upload(client)
    job = Job(document_id=doc_id, engine_id="openai_compat", voice_id="v", status=JobStatus.RUNNING)
    done = Job(document_id=doc_id, engine_id="openai_compat", voice_id="v", status=JobStatus.DONE)
    session.add(job)
    session.add(done)
    session.commit()
    session.refresh(job)
    session.refresh(done)

    requeued = requeue_incomplete_jobs()
    assert job.id in requeued
    assert done.id not in requeued

    session.refresh(job)
    assert job.status == JobStatus.QUEUED


def test_requeue_leaves_finished_jobs_alone(client, session) -> None:
    doc_id = upload(client)
    for status in (JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED):
        session.add(Job(document_id=doc_id, engine_id="openai_compat", voice_id="v", status=status))
    session.commit()

    assert requeue_incomplete_jobs() == []


# ── Черга ─────────────────────────────────────────────────────────────────────

def test_queue_page_lists_jobs(client, fake_engine) -> None:
    doc_id = upload(client)
    client.post(f"/ui/documents/{doc_id}/jobs", data={}, follow_redirects=False)
    wait_for_status(client, 1)

    page = client.get("/jobs")
    assert page.status_code == 200
    assert "Черга завдань" in page.text
    assert "книга.md" in page.text, "у черзі має бути назва документа (16.8b)"
    assert "Завантажити" in page.text


def test_cancel_running_job_marks_cancelled(client) -> None:
    """Скасування доступне і працює (без синтезу — завдання просто в черзі)."""
    doc_id = upload(client)
    client.post(f"/ui/documents/{doc_id}/jobs", data={}, follow_redirects=False)

    response = client.post("/ui/jobs/1/cancel", follow_redirects=False)
    assert response.status_code == 303
    assert client.get("/api/v1/jobs/1").json()["status"] == "cancelled"


def test_cancel_finished_job_is_conflict(client, fake_engine) -> None:
    doc_id = upload(client)
    client.post(f"/ui/documents/{doc_id}/jobs", data={}, follow_redirects=False)
    wait_for_status(client, 1)

    assert client.post("/api/v1/jobs/1/cancel").status_code == 409


def test_segments_fragment_renders_players(client, fake_engine) -> None:
    doc_id = upload(client)
    client.post(f"/ui/documents/{doc_id}/jobs", data={}, follow_redirects=False)
    wait_for_status(client, 1)

    fragment = client.get("/ui/jobs/1/segments")
    assert fragment.status_code == 200
    assert "<audio" in fragment.text
    assert "segments/0/audio" in fragment.text


def test_job_card_fragment(client, fake_engine) -> None:
    doc_id = upload(client)
    client.post(f"/ui/documents/{doc_id}/jobs", data={}, follow_redirects=False)
    wait_for_status(client, 1)

    card = client.get("/ui/jobs/1/card")
    assert card.status_code == 200
    assert "data-job-card" in card.text
    assert client.get("/ui/jobs/999/card").status_code == 404


# ── Синхронізація шаблонів і JS ───────────────────────────────────────────────

def test_jobs_page_ships_sse_client(client, fake_engine) -> None:
    doc_id = upload(client)
    client.post(f"/ui/documents/{doc_id}/jobs", data={}, follow_redirects=False)
    wait_for_status(client, 1)

    for path in ("/jobs", "/jobs/1"):
        body = client.get(path).text
        assert "/static/js/jobs.js" in body
        assert client.get("/static/js/jobs.js").status_code == 200


def test_workspace_shows_queue_strip(client, fake_engine) -> None:
    doc_id = upload(client)
    client.post(f"/ui/documents/{doc_id}/jobs", data={}, follow_redirects=False)
    wait_for_status(client, 1)

    home = client.get("/")
    assert home.status_code == 200
    assert "/jobs/1" in home.text or "Уся черга" in home.text


def test_segment_rows_have_unique_ordinals(client, fake_engine, session) -> None:
    """Ординали сегментів унікальні в межах завдання — UI на них покладається."""
    doc_id = upload(client)
    client.post(f"/ui/documents/{doc_id}/jobs", data={}, follow_redirects=False)
    wait_for_status(client, 1)

    rows = list(session.exec(select(Segment).where(Segment.job_id == 1)).all())
    ordinals = [row.ordinal for row in rows]
    assert len(ordinals) == len(set(ordinals))


def test_delete_document_with_jobs_cascades(client, fake_engine, session) -> None:
    """Видалення документа разом із завданнями не має падати.

    Регресія: раніше `delete_document` видаляв лише блоки, і SQLAlchemy
    намагалась обнулити `jobs.document_id` (NOT NULL) → IntegrityError.
    Тобто кнопка «Видалити» ламалася для будь-якого озвученого документа.
    """
    doc_id = upload(client)
    client.post(f"/ui/documents/{doc_id}/jobs", data={}, follow_redirects=False)
    wait_for_status(client, 1)

    response = client.post(f"/ui/documents/{doc_id}/delete", follow_redirects=False)
    assert response.status_code == 303

    assert client.get(f"/api/v1/documents/{doc_id}").status_code == 404
    assert client.get("/api/v1/jobs").json() == []
    assert session.exec(select(Segment)).all() == []
    assert session.exec(select(Job)).all() == []


def test_delete_document_api_with_jobs(client, fake_engine) -> None:
    """Те саме через JSON-API (ним користуються зовнішні клієнти)."""
    doc_id = upload(client)
    client.post(f"/ui/documents/{doc_id}/jobs", data={}, follow_redirects=False)
    wait_for_status(client, 1)

    assert client.delete(f"/api/v1/documents/{doc_id}").status_code == 204


def _output_duration_ms(client, job_id: int) -> float:
    """Тривалість готового файлу завдання (у мілісекундах)."""
    import soundfile as sf

    job = client.get(f"/api/v1/jobs/{job_id}").json()
    audio, sample_rate = sf.read(job["output_path"])
    return len(audio) / sample_rate * 1000


def test_emotion_changes_pause_structure_in_output(client, fake_engine, session) -> None:
    """Емоція має змінювати не лише темп, а й паузи в готовому файлі.

    Це наскрізний доказ, що воркер справді передає по-сегментну просодію в
    збірку: підставний рушій пише однакові тони, тож різниця в довжині файлу
    може походити тільки від пауз між сегментами.
    """
    from app.models import Block as BlockModel

    three_blocks = "Перше речення.\n\nДруге речення.\n\nТретє речення.\n"

    def synthesize_with(emotion: str, name: str) -> float:
        doc_id = upload(client, name=name, content=three_blocks)
        blocks = list(
            session.exec(select(BlockModel).where(BlockModel.document_id == doc_id)).all()
        )
        client.patch(
            f"/api/v1/documents/{doc_id}/blocks",
            json={
                "updates": [
                    {"block_id": b.id, "emotion": emotion, "intensity": 1.0} for b in blocks
                ]
            },
        )
        job_id = len(client.get("/api/v1/jobs").json()) + 1
        client.post(f"/ui/documents/{doc_id}/jobs", data={}, follow_redirects=False)
        result = wait_for_status(client, job_id)
        assert result["status"] == "done", result["error"]
        return _output_duration_ms(client, job_id)

    sad_ms = synthesize_with("sad", "сумно.md")
    excited_ms = synthesize_with("excited", "захоплено.md")

    # sad: 900 мс пауза (×1.5 на межі абзацу); excited: 250 мс (×1.5)
    assert sad_ms > excited_ms + 1000, (
        f"сумний варіант мав бути помітно довшим: {sad_ms:.0f} мс проти {excited_ms:.0f} мс"
    )
