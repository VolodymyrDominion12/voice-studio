"""База даних SQLite з WAL-режимом.

Використовується SQLModel (надбудова над SQLAlchemy 2.x).
Міграції — через create_all при старті (MVP-підхід; для production краще Alembic).
"""

from __future__ import annotations

from collections.abc import Generator

from sqlmodel import Session, SQLModel, create_engine

from app.config import get_settings

_engine = None


def get_engine():
    """Lazy-ініціалізація движка (щоб тести могли підмінити settings)."""
    global _engine
    if _engine is None:
        settings = get_settings()
        db_url = f"sqlite:///{settings.db_path}"
        _engine = create_engine(
            db_url,
            echo=False,
            connect_args={
                "check_same_thread": False,
                # WAL — для одночасних читань воркером і API
                "timeout": 10,
            },
        )
        # Вмикаємо WAL після підключення
        with _engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA journal_mode=WAL")
            conn.exec_driver_sql("PRAGMA synchronous=NORMAL")
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
    return _engine


def create_db_and_tables() -> None:
    """Створити всі таблиці (якщо не існують). Викликається при старті."""
    # Імпорт моделей тут, щоб SQLModel.metadata їх побачив
    import app.models  # noqa: F401
    SQLModel.metadata.create_all(get_engine())


def get_session() -> Generator[Session, None, None]:
    """FastAPI-залежність: видає сесію на запит."""
    with Session(get_engine()) as session:
        yield session
