"""Фікстури для тестів Voice Studio."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.config import get_settings
from app.db import get_session
from app.main import app


@pytest.fixture(name="db_engine", scope="session")
def db_engine_fixture():
    """In-memory SQLite для тестів — ізольований від реального data/."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    import app.models  # noqa: F401 — реєструємо моделі
    SQLModel.metadata.create_all(engine)
    return engine


@pytest.fixture(name="session")
def session_fixture(db_engine):
    """Сесія для прямих операцій з БД у тестах."""
    with Session(db_engine) as session:
        yield session


@pytest.fixture(name="client")
def client_fixture(db_engine):
    """TestClient з підміненою БД і налаштуваннями."""
    def _get_session_override():
        with Session(db_engine) as session:
            yield session

    app.dependency_overrides[get_session] = _get_session_override
    get_settings.cache_clear()

    with TestClient(app) as client:
        yield client

    app.dependency_overrides.clear()
    get_settings.cache_clear()
