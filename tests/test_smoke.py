"""Димові тести каркаса.

Перевіряють, що застосунок піднімається й віддає контракт, на який уже
спираються Dockerfile і майбутній фронтенд. Поведінка синтезу тут не
тестується — рушіїв ще немає.

Запуск: uv run pytest
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import __version__, app


def test_health_ok() -> None:
    with TestClient(app) as client:
        resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__
    assert body["data_dir_writable"] is True


def test_engines_shape() -> None:
    """Фронтенд будує UI з цього контракту — він має бути стабільним."""
    with TestClient(app) as client:
        resp = client.get("/api/v1/engines")
    assert resp.status_code == 200
    engines = resp.json()["engines"]
    assert engines, "каталог рушіїв не має бути порожнім"

    ids = {e["id"] for e in engines}
    assert {"openai_compat", "ukrainian_tts"} <= ids

    for engine in engines:
        # Ключова вимога ADR-001: кожен рушій зобов'язаний оголосити
        # можливості, щоб шар виразу знав, що брати на себе.
        assert "capabilities" in engine
        caps = engine["capabilities"]
        for key in ("emotions", "emotion_tags", "voice_cloning", "speed",
                    "streaming", "max_chars", "sample_rate"):
            assert key in caps, f"{engine['id']}: немає capabilities.{key}"
        assert caps["max_chars"] > 0
        assert caps["sample_rate"] > 0

    # Українські голоси мають бути присутні в каталозі.
    all_voices = {v["id"] for e in engines for v in e["voices"]}
    assert "uk_UA-tetiana-high" in all_voices


def test_chatterbox_declares_emotions_but_no_ukrainian() -> None:
    """Chatterbox уміє емоції, але української серед його 23 мов немає."""
    with TestClient(app) as client:
        engines = client.get("/api/v1/engines").json()["engines"]
    cb = next(e for e in engines if e["id"] == "chatterbox")
    assert cb["capabilities"]["emotions"] is True
    assert all(v["lang"] != "uk_UA" for v in cb["voices"])


def test_zonos2_is_the_expressive_ukrainian_candidate() -> None:
    """Фіксує головну знахідку дослідження: ZONOS2 — єдина відома відкрита
    модель, яка поєднує вираз, клонування голосу й підтримку української.

    Тест навмисно перевіряє саме ЦІ властивості, бо на них спирається
    етап 3.1 плану (docs/PLAN.md): якщо хтось випадково прибере клонування
    або емоції з можливостей рушія, розвилка етапу 3 втратить сенс.
    """
    with TestClient(app) as client:
        engines = client.get("/api/v1/engines").json()["engines"]
    z = next(e for e in engines if e["id"] == "zonos2")
    caps = z["capabilities"]
    assert caps["emotions"] is True
    assert caps["voice_cloning"] is True
    assert caps["streaming"] is True
    # CPU-збірка ZONOS2 — причина, чому він узагалі розглядається на цій машині.
    assert "CPU" in z["label"]
    # Поки що адаптера немає — і це має бути видно явно, а не мовчки.
    assert z["available"] is False


def test_emotion_profiles_present() -> None:
    with TestClient(app) as client:
        resp = client.get("/api/v1/emotions")
    assert resp.status_code == 200
    profiles = resp.json()["profiles"]
    expected = {"neutral", "warm", "serious", "joyful",
                "excited", "sad", "tense", "questioning"}
    assert expected <= set(profiles)
    # neutral — точка відліку, від якої масштабується intensity.
    assert profiles["neutral"]["speed"] == 1.0
    assert profiles["neutral"]["pitch"] == 0.0
