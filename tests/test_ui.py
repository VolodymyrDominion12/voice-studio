"""Тести HTML-інтерфейсу (фаза F0, docs/FRONTEND.md).

Перевіряють те, що обіцяє F0: «файл завантажується з браузера, документи й
блоки видно». Редактор, прев'ю та черга синтезу — фази F1–F2, їх тут немає.

Запуск: uv run pytest tests/test_ui.py -q
"""

from __future__ import annotations

import html as html_module
import re
from html.parser import HTMLParser

import pytest
from sqlmodel import Session, delete

from app.models import Block, Document, Job, Segment

# Витяг текстів блоків із розмітки сторінки документа.
# У редакторі (F1) текст у <textarea class="txt-input">, тому шукаємо його.
_TEXTAREA = re.compile(r'<textarea class="txt-input"[^>]*>(.*?)</textarea>', re.S)

SAMPLE_MD = (
    "Розділ перший\n\n"
    "Доброго ранку, — сказала вона й усміхнулась.\n\n"
    "У 2007 році їх було 25 %, і це тішило. Ціна — 1500 грн.\n"
)


@pytest.fixture(autouse=True)
def clean_db(db_engine):
    """Порожня БД перед кожним тестом (движок у conftest — session-scoped)."""
    with Session(db_engine) as session:
        # Порядок важливий: спершу залежні таблиці
        for model in (Segment, Block, Job, Document):
            session.exec(delete(model))
        session.commit()
    yield


# ── Допоміжні перевірки розмітки ──────────────────────────────────────────────

_VOID_TAGS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input",
     "link", "meta", "source", "track", "wbr"}
)


class _TagBalance(HTMLParser):
    """Перевіряє, що всі теги закриті, — дешевий захист від зламаного шаблону."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag not in _VOID_TAGS:
            self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID_TAGS:
            return
        if not self.stack:
            self.errors.append(f"зайвий </{tag}>")
            return
        opened = self.stack.pop()
        if opened != tag:
            self.errors.append(f"</{tag}> закриває <{opened}>")


def assert_html_well_formed(html: str) -> None:
    parser = _TagBalance()
    parser.feed(html)
    parser.close()
    assert not parser.errors, f"незбалансована розмітка: {parser.errors}"
    assert not parser.stack, f"незакриті теги: {parser.stack}"


def upload(client, filename: str = "оповідання.md", content: str = SAMPLE_MD):
    """Завантажити файл і повернути (шлях документа, відповідь)."""
    response = client.post(
        "/ui/documents",
        files={"file": (filename, content.encode("utf-8"), "text/markdown")},
        follow_redirects=False,
    )
    return response


# ── Сторінки ──────────────────────────────────────────────────────────────────

def test_workspace_renders(client) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "Робоча стола" in response.text
    assert 'action="/ui/documents"' in response.text
    assert "Ще немає жодного документа" in response.text
    assert_html_well_formed(response.text)


def test_settings_renders(client) -> None:
    response = client.get("/settings")
    assert response.status_code == 200
    # Таблиця емоцій має бути на сторінці (8 профілів)
    for emotion in ("нейтрально", "тепло", "сумно", "питально"):
        assert emotion in response.text
    assert "LUFS" in response.text
    assert_html_well_formed(response.text)


def test_static_css_served(client) -> None:
    response = client.get("/static/css/app.css")
    assert response.status_code == 200
    assert "text/css" in response.headers["content-type"]


def test_health_and_queue_fragments(client) -> None:
    assert client.get("/ui/health").status_code == 200
    assert client.get("/ui/queue").status_code == 200


def test_health_api_exposes_extended_fields(client) -> None:
    """UI і API читають один знімок стану (app/services/system.py)."""
    body = client.get("/api/v1/health").json()
    assert body["status"] == "ok"
    assert body["synth_concurrency"] >= 1
    assert body["max_upload_mb"] > 0
    assert body["default_voice"]


# ── Завантаження ──────────────────────────────────────────────────────────────

def test_upload_redirects_to_document(client) -> None:
    response = upload(client)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/documents/")
    assert "flash=created" in response.headers["location"]


def test_uploaded_document_shows_blocks(client) -> None:
    location = upload(client).headers["location"]
    page = client.get(location)
    assert page.status_code == 200
    assert "оповідання.md" in page.text
    assert "Доброго ранку" in page.text
    # Нормалізатор прибрав тире й розгорнув числа
    assert "дві тисячі сім році" in page.text
    assert "п'ять гривень" in page.text or "гривень" in page.text
    assert_html_well_formed(page.text)


def test_duplicate_upload_returns_same_document(client) -> None:
    first = upload(client).headers["location"].split("?")[0]
    second = upload(client)
    assert second.status_code == 303
    assert second.headers["location"].split("?")[0] == first
    assert "flash=duplicate" in second.headers["location"]

    # Пояснення має бути видно користувачу, а не лише в URL
    page = client.get(second.headers["location"])
    assert "уже завантажено" in page.text


def test_unsupported_extension_returns_415(client) -> None:
    response = client.post(
        "/ui/documents",
        files={"file": ("virus.exe", b"MZ", "application/octet-stream")},
    )
    assert response.status_code == 415
    assert "не підтримується" in response.text
    assert_html_well_formed(response.text)


def test_unsupported_but_allowlisted_extension_returns_422(client, monkeypatch) -> None:
    """Формат у білому списку, але екстрактора немає — це 422 із підказкою.

    `.pdf` тут більше не годиться: його витяг реалізовано (етап 2 зроблено).
    """
    from app.config import get_settings

    monkeypatch.setattr(
        get_settings(), "allowed_extensions", ".txt,.md,.doc", raising=False
    )
    response = client.post(
        "/ui/documents",
        files={"file": ("old.doc", "привіт".encode(), "application/msword")},
    )
    assert response.status_code == 422
    assert "не реалізований" in response.text
    assert ".doc" in response.text


def test_oversized_upload_returns_413(client, monkeypatch) -> None:
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "max_upload_mb", 1, raising=False)
    response = client.post(
        "/ui/documents",
        files={"file": ("big.txt", b"a" * (2 * 1024 * 1024), "text/plain")},
    )
    assert response.status_code == 413


# ── Сторінка документа (редактор, F1) ─────────────────────────────────────────

def blocks_section(html: str) -> str:
    """Витягти лише секцію блоків — без панелі інструментів і гарячих клавіш.

    Фрагменти (`/ui/documents/{id}/blocks`) повертають самі рядки без
    обгортки, тож для них беремо весь HTML.
    """
    try:
        start = html.index('<div class="blocks"')
    except ValueError:
        return html
    end = html.find('class="card hotkeys-card"', start)
    if end == -1:
        end = html.find("<footer", start)
    return html[start:end]


def block_texts(html: str) -> list[str]:
    """Тексти блоків у порядку показу.

    У редакторі (F1) текст живе в `<textarea class="txt-input">`, а не в
    `<div class="txt">` як у режимі перегляду F0.
    """
    fragments = _TEXTAREA.findall(blocks_section(html))
    return [" ".join(html_module.unescape(fragment).split()) for fragment in fragments]


def test_text_modes_differ(client) -> None:
    path = upload(client).headers["location"].split("?")[0]

    effective = client.get(f"{path}?mode=effective").text
    normalized = client.get(f"{path}?mode=normalized").text
    raw = client.get(f"{path}?mode=raw").text

    raw_texts = block_texts(raw)
    normalized_texts = block_texts(normalized)
    effective_texts = block_texts(effective)

    assert len(raw_texts) == len(normalized_texts) == 3
    # Вихідний текст містить тире і цифри, нормалізований — уже ні
    assert any("— сказала" in text for text in raw_texts)
    assert any("1500 грн" in text for text in raw_texts)
    assert not any("—" in text for text in normalized_texts)
    assert not any("1500" in text for text in normalized_texts)
    # «Мій» без правок = нормалізований
    assert effective_texts == normalized_texts
    assert effective_texts != raw_texts


def test_normalizer_diff_is_available_for_reference(client) -> None:
    """Різницю «вихідний / нормалізований» видно завжди.

    У F0 її показували лише в режимі «Мій». У редакторі (F1) вона потрібна
    постійно: це довідка, з якою користувач звіряє власну правку.
    """
    path = upload(client).headers["location"].split("?")[0]
    for mode in ("effective", "normalized", "raw"):
        body = client.get(f"{path}?mode={mode}").text
        assert "Різниця: вихідний / нормалізований" in body, f"немає різниці в режимі {mode}"


def test_document_404_page(client) -> None:
    response = client.get("/documents/99999")
    assert response.status_code == 404
    assert "не знайдено" in response.text
    assert_html_well_formed(response.text)


def test_pagination(client, monkeypatch) -> None:
    from app.ui import router as ui_router

    monkeypatch.setattr(ui_router, "BLOCKS_PER_PAGE", 2)
    content = "\n\n".join(f"Абзац номер {i}." for i in range(5))
    path = upload(client, "багато.md", content).headers["location"].split("?")[0]
    doc_id = path.rsplit("/", 1)[-1]

    first = client.get(path)
    assert first.status_code == 200
    assert len(block_texts(first.text)) == 2
    assert "more-sentinel" in first.text           # є що довантажувати

    # Сентинел веде на фрагмент наступної сторінки
    fragment = client.get(f"/ui/documents/{doc_id}/blocks?offset=2")
    assert fragment.status_code == 200
    assert len(block_texts(fragment.text)) == 2

    last = client.get(f"/ui/documents/{doc_id}/blocks?offset=4")
    assert len(block_texts(last.text)) == 1
    assert "more-sentinel" not in last.text
    assert "за поточним фільтром" in last.text


def test_normalize_redirect(client) -> None:
    path = upload(client).headers["location"].split("?")[0]
    doc_id = path.rsplit("/", 1)[-1]
    response = client.post(f"/ui/documents/{doc_id}/normalize", follow_redirects=False)
    assert response.status_code == 303
    assert "flash=normalized" in response.headers["location"]


def test_delete_document(client) -> None:
    path = upload(client).headers["location"].split("?")[0]
    doc_id = path.rsplit("/", 1)[-1]

    response = client.post(f"/ui/documents/{doc_id}/delete", follow_redirects=False)
    assert response.status_code == 303
    assert client.get(path).status_code == 404
    assert client.get("/api/v1/documents").json() == []


def test_delete_missing_document_is_not_an_error(client) -> None:
    response = client.post("/ui/documents/4242/delete", follow_redirects=False)
    assert response.status_code == 303
