"""Тести витягу тексту з PDF, DOCX, EPUB і HTML (фаза F4, частково).

Доти підтримувались лише `.txt` і `.md`, а решта форматів давала 422. Тут
перевіряємо кожен формат на **справжньому файлі**, який тест сам і створює:
жодних мережевих завантажень і жодних бінарних фікстур у репозиторії.

Окремо перевіряємо евристику «це скан, а не PDF»: найлегше зламати саме її —
короткий цифровий документ не має вважатися сканом.

Запуск: uv run pytest tests/test_extraction_formats.py -q
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from sqlmodel import Session, delete

from app.models import Block, Document, Job, Segment
from app.services.extraction.documents import (
    DocumentExtractionError,
    strip_metadata_header,
)
from app.services.extraction.pdf import (
    PdfError,
    PdfScanDetectedError,
    extract_pdf,
    page_count,
)

# Шрифт із кирилицею для генерації PDF. PyMuPDF має лише base-14 (латиниця),
# тому без цього в PDF записалися б «·» замість українських літер.
_CYRILLIC_FONT = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
_needs_font = pytest.mark.skipif(
    not _CYRILLIC_FONT.exists(), reason="немає системного шрифту з кирилицею"
)


@pytest.fixture(autouse=True)
def clean_db(db_engine):
    with Session(db_engine) as session:
        for model in (Segment, Block, Job, Document):
            session.exec(delete(model))
        session.commit()
    yield


# ── Генерація справжніх файлів ────────────────────────────────────────────────

def make_pdf(path: Path, pages: list[list[tuple[str, int]]]) -> Path:
    """Створити PDF: сторінки → список (текст, кегль)."""
    import pymupdf

    font = pymupdf.Font(fontfile=str(_CYRILLIC_FONT))
    document = pymupdf.open()
    for lines in pages:
        page = document.new_page()
        writer = pymupdf.TextWriter(page.rect)
        y = 90
        for text, size in lines:
            writer.append((72, y), text, font=font, fontsize=size)
            y += 28
        writer.write_text(page)
    document.save(str(path))
    document.close()
    return path


def make_blank_pdf(path: Path, pages: int = 1) -> Path:
    """PDF, у якому лише зображення — модель скана без текстового шару."""
    import pymupdf

    document = pymupdf.open()
    for _ in range(pages):
        page = document.new_page()
        page.draw_rect(pymupdf.Rect(72, 72, 420, 420), fill=(0.85, 0.85, 0.85))
    document.save(str(path))
    document.close()
    return path


def make_docx(path: Path, *, heading: str, paragraph: str) -> Path:
    """Мінімальний валідний DOCX зібраний вручну (без python-docx)."""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
            'package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-'
            'package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.'
            'openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            "</Types>",
        )
        archive.writestr(
            "_rels/.rels",
            '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.'
            'org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
            'officeDocument/2006/relationships/officeDocument" '
            'Target="word/document.xml"/></Relationships>',
        )
        archive.writestr(
            "word/document.xml",
            '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.'
            'org/wordprocessingml/2006/main"><w:body>'
            f'<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
            f"<w:r><w:t>{heading}</w:t></w:r></w:p>"
            f"<w:p><w:r><w:t>{paragraph}</w:t></w:r></w:p>"
            "</w:body></w:document>",
        )
    return path


def make_epub(path: Path, *, title: str, heading: str, paragraph: str) -> Path:
    """Мінімальний валідний EPUB 3 (mimetype першим і без стиснення)."""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            zipfile.ZipInfo("mimetype"), "application/epub+zip",
            compress_type=zipfile.ZIP_STORED,
        )
        archive.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
            '<rootfile full-path="OEBPS/content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>',
        )
        archive.writestr(
            "OEBPS/content.opf",
            '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
            'version="3.0" unique-identifier="id"><metadata '
            'xmlns:dc="http://purl.org/dc/elements/1.1/">'
            '<dc:identifier id="id">urn:uuid:test</dc:identifier>'
            f"<dc:title>{title}</dc:title><dc:language>uk</dc:language></metadata>"
            '<manifest><item id="ch1" href="ch1.xhtml" '
            'media-type="application/xhtml+xml"/></manifest>'
            "<spine><itemref idref=\"ch1\"/></spine></package>",
        )
        archive.writestr(
            "OEBPS/ch1.xhtml",
            '<?xml version="1.0" encoding="utf-8"?>'
            '<html xmlns="http://www.w3.org/1999/xhtml"><head>'
            f"<title>{title}</title></head><body><h1>{heading}</h1>"
            f"<p>{paragraph}</p></body></html>",
        )
    return path


# ── PDF ───────────────────────────────────────────────────────────────────────

@_needs_font
def test_pdf_extracts_text_and_heading(tmp_path: Path) -> None:
    pdf = make_pdf(
        tmp_path / "book.pdf",
        [[
            ("Розділ перший", 22),
            ("Доброго ранку, — сказала вона й усміхнулась.", 11),
            ("У 2007 році їх було 25 %, а ціна — 1500 грн.", 11),
        ]],
    )

    blocks = extract_pdf(pdf)

    assert len(blocks) == 3
    assert blocks[0].kind.value == "heading"
    assert blocks[0].heading_level == 1
    assert blocks[0].text_raw == "Розділ перший"
    assert blocks[1].kind.value == "paragraph"
    assert "усміхнулась" in blocks[1].text_raw
    # Кирилиця не має перетворюватися на сміття
    assert "2007" in blocks[2].text_raw


@_needs_font
def test_pdf_joins_wrapped_lines(tmp_path: Path) -> None:
    """Розрив рядка всередині абзацу — це перенос, а не новий абзац."""
    import pymupdf

    font = pymupdf.Font(fontfile=str(_CYRILLIC_FONT))
    pdf = tmp_path / "wrapped.pdf"
    document = pymupdf.open()
    page = document.new_page()
    writer = pymupdf.TextWriter(page.rect)
    writer.append((72, 90), "Це довге речення, яке", font=font, fontsize=11)
    writer.append((72, 106), "перенесли на другий рядок.", font=font, fontsize=11)
    writer.append((72, 140), "А це вже новий абзац, і він доволі довгий, "
                             "щоб не вважатися заголовком ані за довжиною.", font=font, fontsize=11)
    writer.write_text(page)
    document.save(str(pdf))
    document.close()

    blocks = extract_pdf(pdf)
    assert "яке перенесли" in blocks[0].text_raw, "перенос рядка має стати пробілом"


def test_pdf_reports_page_count(tmp_path: Path) -> None:
    pdf = make_blank_pdf(tmp_path / "three.pdf", pages=3)
    assert page_count(pdf) == 3


def test_pdf_scan_without_text_layer_is_detected(tmp_path: Path) -> None:
    """Скан без тексту — явна помилка, а не порожній документ."""
    pdf = make_blank_pdf(tmp_path / "scan.pdf", pages=2)
    with pytest.raises(PdfScanDetectedError) as error:
        extract_pdf(pdf)
    assert "скан" in str(error.value)


@_needs_font
def test_pdf_scan_with_page_numbers_is_detected(tmp_path: Path) -> None:
    """Багатосторінковий скан із самими номерами сторінок теж має виявлятися."""
    pdf = make_pdf(
        tmp_path / "numbers.pdf",
        [[(f"— {i} —", 10)] for i in range(1, 6)],
    )
    with pytest.raises(PdfScanDetectedError):
        extract_pdf(pdf)


@_needs_font
def test_short_digital_pdf_is_not_mistaken_for_scan(tmp_path: Path) -> None:
    """Регресія: короткий цифровий PDF має читатися, а не вважатися сканом.

    Саме тут ламалася перша версія евристики: «менш ніж 100 символів на
    сторінку» відкидало записку на три рядки.
    """
    pdf = make_pdf(
        tmp_path / "note.pdf",
        [[("Розділ перший", 22), ("Доброго ранку, — сказала вона.", 11)]],
    )
    blocks = extract_pdf(pdf)
    assert len(blocks) == 2
    assert "Доброго ранку" in blocks[1].text_raw


def test_corrupt_pdf_raises_clear_error(tmp_path: Path) -> None:
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4 not really a pdf")
    with pytest.raises(PdfError):
        extract_pdf(broken)


# ── DOCX / EPUB / HTML ────────────────────────────────────────────────────────

def test_docx_extraction(tmp_path: Path) -> None:
    docx = make_docx(tmp_path / "doc.docx", heading="Розділ", paragraph="Текст абзацу з docx.")
    from app.services.extraction.documents import extract_document

    blocks = extract_document(docx)

    assert blocks[0].kind.value == "heading"
    assert blocks[0].text_raw == "Розділ"
    assert any("docx" in block.text_raw for block in blocks)


def test_epub_extraction_drops_metadata(tmp_path: Path) -> None:
    """Метадані EPUB не мають потрапляти в озвучення."""
    epub = make_epub(
        tmp_path / "book.epub", title="Тестова книжка",
        heading="Розділ перший", paragraph="Абзац із epub.",
    )
    from app.services.extraction.documents import extract_document

    blocks = extract_document(epub)
    texts = " ".join(block.text_raw for block in blocks)

    assert "Розділ перший" in texts
    assert "Абзац із epub" in texts
    assert "Title" not in texts
    assert "Тестова книжка" not in texts
    assert "Identifier" not in texts


def test_html_extraction_keeps_structure(tmp_path: Path) -> None:
    html = tmp_path / "page.html"
    html.write_text(
        "<html><body><h1>Заголовок</h1><p>Абзац тексту.</p>"
        "<ul><li>Перший пункт</li><li>Другий пункт</li></ul></body></html>",
        encoding="utf-8",
    )
    from app.services.extraction.documents import extract_document

    blocks = extract_document(html)
    kinds = [block.kind.value for block in blocks]

    assert kinds[0] == "heading"
    assert "paragraph" in kinds
    assert kinds.count("list_item") == 2


def test_empty_document_reports_error(tmp_path: Path) -> None:
    """Порожній файл — це помилка з поясненням, а не документ без блоків."""
    html = tmp_path / "empty.html"
    html.write_text("<html><body></body></html>", encoding="utf-8")
    from app.services.extraction.documents import extract_document

    with pytest.raises(DocumentExtractionError):
        extract_document(html)


def test_metadata_stripper_keeps_regular_content() -> None:
    markdown = "**Title:** Книга\n**Author:** Хтось\n\n# Розділ\n\nТекст."
    assert strip_metadata_header(markdown).lstrip().startswith("# Розділ")

    plain = "# Розділ\n\nЗвичайний текст без метаданих."
    assert strip_metadata_header(plain) == plain


# ── Наскрізний шлях через завантаження ────────────────────────────────────────

@pytest.mark.parametrize("suffix", [".pdf", ".docx", ".epub", ".html"])
@_needs_font
def test_upload_supported_formats_via_api(client, tmp_path: Path, suffix: str) -> None:
    """Раніше всі ці формати давали 422 — тепер мають створювати документ."""
    target = tmp_path / f"sample{suffix}"
    if suffix == ".pdf":
        make_pdf(target, [[("Розділ", 22), ("Текст із pdf-файлу, доволі довгий рядок.", 11)]])
    elif suffix == ".docx":
        make_docx(target, heading="Розділ", paragraph="Текст із docx.")
    elif suffix == ".epub":
        make_epub(target, title="Книга", heading="Розділ", paragraph="Текст із epub.")
    else:
        target.write_text("<html><body><p>Текст із html.</p></body></html>", encoding="utf-8")

    response = client.post(
        "/api/v1/documents",
        files={"file": (target.name, target.read_bytes(), "application/octet-stream")},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["blocks"], f"{suffix}: не витягнуто жодного блоку"
    assert body["status"] == "ready"


@_needs_font
def test_pdf_page_count_is_saved(client, tmp_path: Path) -> None:
    pdf = make_pdf(
        tmp_path / "two.pdf",
        [
            [("Сторінка один, з доволі довгим текстом для читання.", 11)],
            [("Сторінка два, також із довгим текстом для чилитання.", 11)],
        ],
    )
    response = client.post(
        "/api/v1/documents",
        files={"file": ("two.pdf", pdf.read_bytes(), "application/pdf")},
    )
    assert response.status_code == 201
    assert response.json()["page_count"] == 2


def test_scan_pdf_gets_readable_error(client, tmp_path: Path) -> None:
    """Скан: 422 із підказкою про OCR, а не порожній документ."""
    pdf = make_blank_pdf(tmp_path / "scan.pdf", pages=2)
    response = client.post(
        "/api/v1/documents",
        files={"file": ("scan.pdf", pdf.read_bytes(), "application/pdf")},
    )
    assert response.status_code == 422
    assert "скан" in response.json()["detail"]


@_needs_font
def test_uploaded_pdf_shows_blocks_in_ui(client, tmp_path: Path) -> None:
    pdf = make_pdf(
        tmp_path / "ui.pdf",
        [[("Розділ перший", 22), ("Доброго ранку, — сказала вона.", 11)]],
    )
    response = client.post(
        "/ui/documents",
        files={"file": ("ui.pdf", pdf.read_bytes(), "application/pdf")},
        follow_redirects=False,
    )
    assert response.status_code == 303

    page = client.get(response.headers["location"])
    assert page.status_code == 200
    assert "Розділ перший" in page.text
    assert "Доброго ранку" in page.text


def test_allowlisted_but_unimplemented_format_still_422(client, monkeypatch) -> None:
    """Запобіжник лишається: якщо формат у білому списку, а екстрактора немає."""
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(
        settings, "allowed_extensions", ".txt,.md,.rtf", raising=False
    )

    buffer = io.BytesIO(b"{\\rtf1 not supported}")
    response = client.post(
        "/api/v1/documents",
        files={"file": ("doc.rtf", buffer.read(), "application/rtf")},
    )
    assert response.status_code == 422
    assert "не реалізований" in response.json()["detail"]


def test_blocks_from_all_formats_are_speakable(client, tmp_path: Path) -> None:
    """Кожен блок із нового формату має бути готовий до озвучення."""
    html = tmp_path / "ready.html"
    html.write_text("<html><body><p>Текст для озвучення.</p></body></html>", encoding="utf-8")
    response = client.post(
        "/api/v1/documents",
        files={"file": ("ready.html", html.read_bytes(), "text/html")},
    )
    blocks = response.json()["blocks"]
    assert all(block["speak"] for block in blocks)
    assert all(block["text_normalized"] for block in blocks)


def test_document_page_count_column_exists(client) -> None:
    """`page_count` має лишатися в схемі відповіді (його використовує UI)."""
    html_body = b"<html><body><p>\xd0\xa2\xd0\xb5\xd0\xba\xd1\x81\xd1\x82.</p></body></html>"
    response = client.post(
        "/api/v1/documents", files={"file": ("x.html", html_body, "text/html")}
    )
    assert "page_count" in response.json()


def test_segments_not_created_by_extraction(client, tmp_path: Path) -> None:
    """Витяг не має створювати завдання чи сегменти — це окремий крок."""
    html = tmp_path / "quiet.html"
    html.write_text("<html><body><p>Текст.</p></body></html>", encoding="utf-8")
    client.post("/api/v1/documents", files={"file": ("quiet.html", html.read_bytes(), "text/html")})
    assert client.get("/api/v1/jobs").json() == []
