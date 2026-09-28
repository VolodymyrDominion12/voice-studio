"""Тести аудіокниги: розділи, M4B, ID3-теги, zip і вибір формату.

Що тут перевіряється і чому саме так:

  * **План розділів** — заголовки документа досі нікуди не вели (`heading_level`
    зберігався й не використовувався). Тепер вони дають наскрізну нумерацію в
    метаданих і порізку на файли, тож правила («найвищий наявний рівень»,
    «вступ без номера», «не дублювати вже наявний номер») — це контракт.
  * **Таймлайн** — межі розділів беруться з тривалостей сегментів і пауз, а не
    з оцінки «скільки символів». Помилка тут дала б розділ, що починається
    посеред речення.
  * **M4B** — головний пункт: розділи й теги читаються назад через той самий
    PyAV, яким файл записано, тобто перевіряються реальні атоми контейнера, а
    не те, що функція не кинула винятку.
  * **ID3** — mutagen був оголошеною залежністю, яку ніде не використовували
    (PLAN, етап 2). Тепер теги перевіряються читанням.
  * **Вибір формату** — замінює знахідку 16.3: раніше `?format=` або підміняв
    заголовок, або давав 409.

Запуск: uv run pytest tests/test_audiobook.py -q
"""

from __future__ import annotations

import math
import re
import struct
import time
import wave
import zipfile
from pathlib import Path

import numpy as np
import pytest
from mutagen.id3 import ID3
from mutagen.mp4 import MP4
from sqlmodel import Session, delete

from app.models import Block, BlockKind, Document, Job, Segment
from app.services.audio.assemble import (
    ChapterSpan,
    assemble_segments,
    assemble_with_timeline,
    build_audio,
    chapter_slices,
    normalize_formats,
    primary_format,
)
from app.services.library.chapters import (
    Chapter,
    build_chapter_plan,
    chapter_file_name,
    chapter_heading_level,
)

SAMPLE_RATE = 24000


# ── Допоміжні ─────────────────────────────────────────────────────────────────

def write_tone(path: Path, seconds: float = 0.2, amplitude: int = 12000) -> Path:
    """Короткий тон відомої тривалості — щоб точно знати межі в часі."""
    frames = int(seconds * SAMPLE_RATE)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(
            b"".join(
                struct.pack(
                    "<h", int(amplitude * math.sin(2 * math.pi * 440 * i / SAMPLE_RATE))
                )
                for i in range(frames)
            )
        )
    return path


class FakeBlock:
    """Мінімальний «блок» для плану розділів — без ORM і без БД.

    Саме для цього в `chapters` оголошено протокол: правило «що є розділом»
    перевіряється без завантаження документа в SQLite.
    """

    def __init__(self, ordinal: int, text: str, kind=BlockKind.PARAGRAPH, level=None):
        self.ordinal = ordinal
        self.text = text
        self.kind = kind
        self.heading_level = level


def heading(ordinal: int, text: str, level: int = 1) -> FakeBlock:
    return FakeBlock(ordinal, text, BlockKind.HEADING, level)


def paragraph(ordinal: int, text: str) -> FakeBlock:
    return FakeBlock(ordinal, text, BlockKind.PARAGRAPH, None)


# ── План розділів ─────────────────────────────────────────────────────────────

def test_no_headings_gives_single_chapter() -> None:
    """Документ без заголовків — одна «книга», названа за іменем файлу."""
    plan = build_chapter_plan(
        [paragraph(0, "Перший."), paragraph(1, "Другий.")], document_title="моя-книга"
    )
    assert len(plan) == 1
    assert plan[0].title == "моя-книга"
    assert plan[0].number is None
    assert (plan[0].first_block, plan[0].last_block) == (0, 1)


def test_intro_then_numbered_chapters() -> None:
    """Текст до першого заголовка — «Вступ» без номера, далі розділи 1..N."""
    plan = build_chapter_plan(
        [
            paragraph(0, "Передмова."),
            heading(1, "Перший розділ"),
            paragraph(2, "Тіло."),
            heading(3, "Другий розділ"),
            paragraph(4, "Ще тіло."),
        ],
        document_title="книга",
    )
    assert [chapter.title for chapter in plan] == [
        "Вступ",
        "1. Перший розділ",
        "2. Другий розділ",
    ]
    assert [chapter.number for chapter in plan] == [None, 1, 2]
    # Межі не перетинаються й покривають усі блоки
    assert (plan[0].first_block, plan[0].last_block) == (0, 0)
    assert (plan[1].first_block, plan[1].last_block) == (1, 2)
    assert (plan[2].first_block, plan[2].last_block) == (3, 4)


def test_document_starting_with_heading_has_no_intro() -> None:
    plan = build_chapter_plan([heading(0, "Розділ"), paragraph(1, "Тіло.")])
    assert [chapter.title for chapter in plan] == ["1. Розділ"]


def test_shallowest_present_level_is_the_chapter() -> None:
    """Якщо в документі є лише H2 — розділи це H2, а не «один розділ на все».

    Це головна пастка правила «розділ = H1»: у книзі, де H1 лише назва
    книги, воно дало б одну теку на всю книгу (або жодної).
    """
    blocks = [
        paragraph(0, "Вступ."),
        heading(1, "Розділ А", level=2),
        paragraph(2, "Текст."),
        heading(3, "Розділ Б", level=2),
        paragraph(4, "Текст."),
    ]
    assert chapter_heading_level(blocks) == 2
    plan = build_chapter_plan(blocks)
    assert [chapter.title for chapter in plan] == ["Вступ", "1. Розділ А", "2. Розділ Б"]
    assert all(chapter.heading_level == 2 for chapter in plan if chapter.number)


def test_deeper_headings_stay_inside_chapter() -> None:
    """H3 усередині H1-розділу не створює нового розділу."""
    plan = build_chapter_plan(
        [
            heading(0, "Частина", level=1),
            heading(1, "Підрозділ", level=3),
            paragraph(2, "Текст."),
        ]
    )
    assert len(plan) == 1
    assert (plan[0].first_block, plan[0].last_block) == (0, 2)


def test_existing_number_is_not_duplicated() -> None:
    """«5. Назва» не стає «1. 5. Назва» — інакше в M4B був би абсурд."""
    plan = build_chapter_plan([heading(0, "5. Розділ п'ятий"), heading(1, "Розділ VI")])
    assert plan[0].title == "5. Розділ п'ятий"
    assert plan[1].title == "Розділ VI"


@pytest.mark.parametrize(
    "text",
    ["Розділ 12", "Глава 3", "Частина II", "5. Назва", "VII. Назва"],
)
def test_heading_with_its_own_number_keeps_it(text: str) -> None:
    """Заголовок уже з номером — свій номер не додаємо."""
    plan = build_chapter_plan([heading(0, text)])
    assert plan[0].title == text


def test_plain_heading_gets_sequence_number() -> None:
    plan = build_chapter_plan([heading(0, "Звичайна назва")])
    assert plan[0].title == "1. Звичайна назва"
    assert plan[0].heading_text == "Звичайна назва"


def test_long_heading_is_truncated() -> None:
    plan = build_chapter_plan([heading(0, "Дуже довга назва " * 20)])
    assert len(plan[0].title) <= 84  # 80 + «1. »
    assert plan[0].title.endswith("…")


def test_empty_block_list_gives_empty_plan() -> None:
    assert build_chapter_plan([]) == []


def test_heading_without_level_is_not_a_chapter_boundary() -> None:
    """Заголовок без рівня не робить розділів — краще одна книга, ніж N пустих.

    Екстрактори завжди ставлять рівень (`txt` — з кількості `#`, PDF — з
    розміру шрифту), тож випадок захисний: якщо рівня немає, план має лишитися
    коректним, а не зламатися на `min()` порожньої послідовності.
    """
    blocks = [FakeBlock(0, "Заголовок", BlockKind.HEADING, None), paragraph(1, "Тіло.")]
    assert chapter_heading_level(blocks) is None
    plan = build_chapter_plan(blocks, document_title="книга")
    assert [chapter.title for chapter in plan] == ["книга"]
    assert (plan[0].first_block, plan[0].last_block) == (0, 1)


def test_chapter_file_name_keeps_cyrillic_and_numbers() -> None:
    """Імена файлів читає людина: кирилиця лишається, небезпечні символи — ні."""
    name = chapter_file_name(Chapter(3, "3. Зустріч: «Львів» / ранок", "", 0, 0))
    assert name.startswith("03-")
    assert name.endswith(".mp3")
    assert "/" not in name and ":" not in name and "«" not in name
    assert "Зустріч" in name
    # Номер не дублюється: «03-Зустріч...», а не «03-3.-Зустріч...»
    stem = name.removesuffix(".mp3")
    assert stem.count("3") == 1, stem
    assert "3." not in stem


def test_chapter_file_name_keeps_word_numbering() -> None:
    """«Розділ 5» — частина назви, а не технічний номер: не ріжемо його."""
    name = chapter_file_name(Chapter(2, "2. Розділ 5. Фінал", "", 0, 0))
    assert name.startswith("02-")
    assert "Розділ-5" in name


def test_chapter_file_name_for_intro() -> None:
    assert chapter_file_name(Chapter(None, "Вступ", "", 0, 0)) == "00-Вступ.mp3"


# ── Таймлайн і межі розділів ──────────────────────────────────────────────────

def test_timeline_offsets_match_pauses(tmp_path: Path) -> None:
    """Кожен сегмент 200 мс; паузи 100 і 300 → старти 0, 300 і 800 мс."""
    paths = [write_tone(tmp_path / f"s{i}.wav", seconds=0.2) for i in range(3)]

    audio, timings = assemble_with_timeline(
        paths, pause_ms_between=0, target_sample_rate=SAMPLE_RATE, pauses_ms=[100, 300, 0]
    )

    assert [timing.start_ms for timing in timings] == [0, 300, 800]
    assert [timing.end_ms for timing in timings] == [200, 500, 1000]
    # Таймлайн описує саме той масив, який повернули
    assert len(audio) == pytest.approx(1.0 * SAMPLE_RATE, abs=2)


def test_assemble_segments_still_returns_array(tmp_path: Path) -> None:
    """Старий API не змінився — інакше поїхали б усі попередні тести."""
    paths = [write_tone(tmp_path / "a.wav"), write_tone(tmp_path / "b.wav")]
    audio = assemble_segments(paths, pause_ms_between=100, target_sample_rate=SAMPLE_RATE)
    assert isinstance(audio, np.ndarray)
    assert audio.ndim == 1


def test_chapter_slices_use_segment_timings(tmp_path: Path) -> None:
    paths = [write_tone(tmp_path / f"s{i}.wav", seconds=0.2) for i in range(4)]
    _, timings = assemble_with_timeline(
        paths, pause_ms_between=0, target_sample_rate=SAMPLE_RATE, pauses_ms=[0, 0, 0, 0]
    )
    slices = chapter_slices(
        [ChapterSpan("A", 0, 1), ChapterSpan("B", 2, 3)],
        timings,
        names={"A": "01-A.mp3", "B": "02-B.mp3"},
    )
    assert [(s.title, s.start_ms, s.end_ms) for s in slices] == [
        ("A", 0, 400),
        ("B", 400, 800),
    ]
    assert slices[0].name == "01-A.mp3"


def test_chapter_slices_survive_missing_segment(tmp_path: Path) -> None:
    """Якщо сегмент зник із диска, межу беремо з найближчого наявного."""
    paths = [write_tone(tmp_path / f"s{i}.wav", seconds=0.2) for i in range(3)]
    paths[1].unlink()

    _, timings = assemble_with_timeline(
        paths, pause_ms_between=0, target_sample_rate=SAMPLE_RATE, pauses_ms=[0, 0, 0]
    )
    assert [timing.index for timing in timings] == [0, 2]

    slices = chapter_slices([ChapterSpan("A", 0, 1), ChapterSpan("B", 2, 2)], timings)
    assert len(slices) == 2
    # Розділ A закінчується там, де почався (сегмента 1 немає), але не зникає
    assert slices[0].start_ms == 0
    assert slices[1].start_ms == slices[1].end_ms or slices[1].end_ms > 0


def test_chapter_slices_empty_without_timings() -> None:
    assert chapter_slices([ChapterSpan("A", 0, 1)], []) == []


# ── Нормалізація вибору форматів ──────────────────────────────────────────────

def test_normalize_formats_accepts_string_list_and_dedupes() -> None:
    assert normalize_formats("mp3,m4b") == ("mp3", "m4b")
    assert normalize_formats(["M4B", ".m4b", "wav"]) == ("m4b", "wav")
    assert normalize_formats(None, default=("wav",)) == ("wav",)
    assert normalize_formats([], default=("wav",)) == ("wav",)


def test_normalize_formats_rejects_unknown() -> None:
    """Невідомий формат — помилка, а не мовчазний MP3 замість M4B."""
    with pytest.raises(ValueError, match="mp4"):
        normalize_formats(["mp3", "mp4"])
    with pytest.raises(ValueError):
        normalize_formats(42)


def test_primary_format_prefers_mp3() -> None:
    assert primary_format(("wav", "m4b", "mp3")) == "mp3"
    assert primary_format(("m4b", "wav")) == "m4b"
    assert primary_format(("zip",)) == "zip"


# ── Експорт: M4B, ID3, zip ────────────────────────────────────────────────────

def test_m4b_has_chapters_and_tags(tmp_path: Path) -> None:
    """M4B — сенс формату в розділах: перевіряємо, що вони справді в контейнері."""
    paths = [write_tone(tmp_path / f"s{i}.wav", seconds=0.3) for i in range(4)]

    results = build_audio(
        paths,
        tmp_path,
        "book",
        pause_ms=0,
        target_sample_rate=SAMPLE_RATE,
        formats=("m4b",),
        pauses_ms=[0, 0, 0, 0],
        chapters=[ChapterSpan("1. Перший", 0, 1), ChapterSpan("2. Другий", 2, 3)],
        chapter_names={"1. Перший": "01-pershyi.m4b", "2. Другий": "02-druhyi.m4b"},
        tags={"title": "Моя книга", "album": "Моя книга", "artist": "Voice Studio"},
    )

    m4b = results["m4b"]
    assert m4b.is_file() and m4b.stat().st_size > 1000

    import av

    with av.open(str(m4b)) as container:
        assert container.format.name.startswith(("mov", "mp4"))
        chapters = container.chapters()
        assert [dict(ch["metadata"])["title"] for ch in chapters] == [
            "1. Перший",
            "2. Другий",
        ]
        # Межі розділів монотонні й у межах файлу
        starts = [ch["start"] for ch in chapters]
        assert starts == sorted(starts)
        assert container.duration > 0

    # Читається і mutagen-ом — тобто це звичайний MP4, а не «щось своє»
    tags = MP4(str(m4b))
    assert tags.tags["©nam"] == ["Моя книга"]
    assert tags.tags["©ART"] == ["Voice Studio"]


def test_m4b_without_chapters_is_still_valid(tmp_path: Path) -> None:
    """Документ без заголовків: M4B без розділів, але з тегами й звуком."""
    paths = [write_tone(tmp_path / "s.wav", seconds=0.25)]
    results = build_audio(
        paths,
        tmp_path,
        "plain",
        pause_ms=0,
        target_sample_rate=SAMPLE_RATE,
        formats=("m4b",),
        tags={"title": "Без розділів"},
    )

    import av

    with av.open(str(results["m4b"])) as container:
        assert container.chapters() == []
        assert dict(container.metadata)["title"] == "Без розділів"
        assert len(container.streams.audio) == 1


def test_mp3_gets_id3_tags(tmp_path: Path) -> None:
    """mutagen був у залежностях, але не використовувався — тепер теги є."""
    paths = [write_tone(tmp_path / "s.wav", seconds=0.25)]
    results = build_audio(
        paths,
        tmp_path,
        "tagged",
        pause_ms=0,
        target_sample_rate=SAMPLE_RATE,
        formats=("mp3",),
        tags={
            "title": "Моя книга",
            "album": "Моя книга",
            "artist": "Voice Studio",
            "genre": "Аудіокнига",
            "date": "2025",
            "track": "1/3",
        },
    )

    tags = ID3(str(results["mp3"]))
    assert tags["TIT2"].text == ["Моя книга"]
    assert tags["TALB"].text == ["Моя книга"]
    assert tags["TPE1"].text == ["Voice Studio"]
    assert tags["TRCK"].text == ["1/3"]


def test_mp3_without_tags_has_no_id3_and_no_crash(tmp_path: Path) -> None:
    """Без тегів файл лишається чистим MP3 (синхронізація кадру на початку)."""
    paths = [write_tone(tmp_path / "s.wav", seconds=0.2)]
    results = build_audio(
        paths, tmp_path, "bare", pause_ms=0, target_sample_rate=SAMPLE_RATE, formats=("mp3",)
    )
    data = results["mp3"].read_bytes()
    assert len(data) > 500
    assert not data.startswith(b"ID3")
    # 11 бітів синхронізації кадру MPEG: 0xFF і три старші біти другого байта
    assert data[0] == 0xFF and data[1] & 0xE0 == 0xE0


def test_zip_has_one_file_per_chapter_with_tags(tmp_path: Path) -> None:
    """zip — окремий MP3 на розділ; ріжемо вже зведене аудіо, без нового синтезу."""
    paths = [write_tone(tmp_path / f"s{i}.wav", seconds=0.3) for i in range(4)]

    results = build_audio(
        paths,
        tmp_path,
        "book",
        pause_ms=0,
        target_sample_rate=SAMPLE_RATE,
        formats=("zip",),
        pauses_ms=[0, 0, 0, 0],
        chapters=[ChapterSpan("1. Перший", 0, 1), ChapterSpan("2. Другий", 2, 3)],
        chapter_names={"1. Перший": "01-pershyi.mp3", "2. Другий": "02-druhyi.mp3"},
        tags={"album": "Моя книга", "artist": "Voice Studio"},
    )

    with zipfile.ZipFile(results["zip"]) as archive:
        names = sorted(archive.namelist())
        assert names == ["01-pershyi.mp3", "02-druhyi.mp3"]
        for name in names:
            payload = archive.read(name)
            assert len(payload) > 500  # не порожній файл


def test_zip_chapters_are_tagged_per_track(tmp_path: Path) -> None:
    """Кожен файл у архіві має свій номер і назву — інакше це «трек 1» двічі."""
    import tempfile

    paths = [write_tone(tmp_path / f"s{i}.wav", seconds=0.25) for i in range(2)]
    results = build_audio(
        paths,
        tmp_path,
        "book",
        pause_ms=0,
        target_sample_rate=SAMPLE_RATE,
        formats=("zip",),
        pauses_ms=[0, 0],
        chapters=[ChapterSpan("1. А", 0, 0), ChapterSpan("2. Б", 1, 1)],
        tags={"album": "Книга"},
    )

    with zipfile.ZipFile(results["zip"]) as archive, tempfile.TemporaryDirectory() as tmp:
        for index, name in enumerate(sorted(archive.namelist()), start=1):
            target = Path(tmp) / f"{index}.mp3"
            target.write_bytes(archive.read(name))
            tags = ID3(str(target))
            assert tags["TRCK"].text == [f"{index}/2"]
            assert tags["TIT2"].text[0].startswith(str(index))


def test_build_audio_multiple_formats(tmp_path: Path) -> None:
    """Кілька форматів за один прохід — і кожен існує на диску."""
    paths = [write_tone(tmp_path / "s.wav", seconds=0.25)]
    results = build_audio(
        paths,
        tmp_path,
        "multi",
        pause_ms=0,
        target_sample_rate=SAMPLE_RATE,
        formats=("mp3", "wav", "m4b"),
        tags={"title": "Книга"},
    )
    assert set(results) == {"mp3", "wav", "m4b"}
    assert all(path.is_file() for path in results.values())


# ── Наскрізно: завдання з форматами ──────────────────────────────────────────

BOOK_MD = (
    "Передмова до книжки.\n\n"
    "# Перший розділ\n\n"
    "Текст першого розділу тут.\n\n"
    "# Другий розділ\n\n"
    "Текст другого розділу тут.\n"
)


@pytest.fixture(autouse=True)
def clean_db(db_engine):
    """Порожня БД і порожні теки рендерів перед кожним тестом."""
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
    monkeypatch.setattr("app.worker.queue.get_engine", lambda: db_engine)
    yield


@pytest.fixture
def fake_engine(monkeypatch):
    """Підмінити синтез: справжній WAV замість звернення до шлюзу."""
    calls: list[Path] = []

    def fake_synthesize(self, request):
        target = Path(request.output_path)
        write_tone(target, seconds=0.15, amplitude=12000)
        calls.append(target)
        return target

    monkeypatch.setattr(
        "app.services.tts.openai_compat.OpenAICompatEngine.synthesize", fake_synthesize
    )
    return calls


def upload(client, content: str = BOOK_MD, name: str = "книга.md") -> int:
    response = client.post(
        "/ui/documents",
        files={"file": (name, content.encode("utf-8"), "text/markdown")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return int(response.headers["location"].split("?")[0].rsplit("/", 1)[-1])


def wait_for_status(client, job_id: int, timeout: float = 20.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = client.get(f"/api/v1/jobs/{job_id}").json()
        if last["status"] in ("done", "failed", "cancelled"):
            return last
        time.sleep(0.15)
    raise AssertionError(f"Завдання не завершилось за {timeout} с: {last}")


def test_ui_job_creates_m4b_and_zip(client, fake_engine) -> None:
    """Повний шлях від чекбоксів у редакторі до M4B із розділами й zip."""
    doc_id = upload(client)

    response = client.post(
        f"/ui/documents/{doc_id}/jobs",
        data={
            "voice": "uk_UA-tetiana-high",
            "engine": "openai_compat",
            "formats": ["mp3", "m4b"],
            "chapter_split": "true",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    job_id = int(response.headers["location"].rsplit("/", 1)[-1])

    job = wait_for_status(client, job_id)
    assert job["status"] == "done", job["error"]
    assert job["output_path"].endswith(".mp3"), "головний файл — MP3"

    artifacts = client.get(f"/api/v1/jobs/{job_id}").json()
    assert artifacts["output_path"].endswith(".mp3")

    # Кожен формат завантажується зі своїм типом вмісту
    for name, content_type in (
        ("mp3", "audio/mpeg"),
        ("m4b", "audio/mp4"),
        ("zip", "application/zip"),
    ):
        download = client.get(f"/api/v1/jobs/{job_id}/download?format={name}")
        assert download.status_code == 200, name
        assert download.headers["content-type"] == content_type
        assert len(download.content) > 500

    # І M4B справді містить розділи з заголовків документа
    import av

    m4b_bytes = client.get(f"/api/v1/jobs/{job_id}/download?format=m4b").content
    with av.open(_to_path(m4b_bytes)) as container:
        titles = [dict(ch["metadata"])["title"] for ch in container.chapters()]
    assert titles == ["Вступ", "1. Перший розділ", "2. Другий розділ"]

    # zip містить три файли-розділи
    zip_bytes = client.get(f"/api/v1/jobs/{job_id}/download?format=zip").content
    archive_path = _to_path(zip_bytes)
    with zipfile.ZipFile(archive_path) as archive:
        assert len(archive.namelist()) == 3


def test_download_unknown_format_lists_available(client, fake_engine) -> None:
    """Знахідка 16.3: раніше був 409 без пояснення, тепер 404 зі списком."""
    doc_id = upload(client)
    job_id = start_job(client, doc_id, formats=["m4b"])
    job = wait_for_status(client, job_id)
    assert job["status"] == "done", job["error"]

    response = client.get(f"/api/v1/jobs/{job_id}/download?format=wav")
    assert response.status_code == 404
    assert "m4b" in response.json()["detail"]


def test_download_without_format_serves_primary(client, fake_engine) -> None:
    doc_id = upload(client)
    job_id = start_job(client, doc_id, formats=["wav", "mp3"])
    assert wait_for_status(client, job_id)["status"] == "done"

    response = client.get(f"/api/v1/jobs/{job_id}/download")
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/mpeg"


def start_job(client, doc_id: int, formats: list[str] | None = None) -> int:
    payload: dict = {"document_id": doc_id, "engine_id": "openai_compat"}
    if formats is not None:
        payload["options"] = {"formats": formats}
    response = client.post("/api/v1/jobs", json=payload)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_api_rejects_unknown_format_before_synthesis(client, fake_engine) -> None:
    """Невідомий формат — 422 до синтезу, а не «завдання готове, файлу немає»."""
    doc_id = upload(client)
    response = client.post(
        "/api/v1/jobs",
        json={"document_id": doc_id, "options": {"formats": ["mp3", "mp4"]}},
    )
    assert response.status_code == 422
    assert "mp4" in response.json()["detail"]
    assert not fake_engine, "синтез не мав початися"


def test_api_default_format_from_settings(client, fake_engine) -> None:
    """Без options.formats завдання бере формати з налаштувань."""
    doc_id = upload(client)
    job_id = start_job(client, doc_id)
    job = wait_for_status(client, job_id)
    assert job["output_path"].endswith(".mp3")


def test_job_page_lists_downloads(client, fake_engine) -> None:
    """Сторінка завдання показує всі створені файли, а не лише головний."""
    doc_id = upload(client)
    job_id = start_job(client, doc_id, formats=["mp3", "m4b"])
    assert wait_for_status(client, job_id)["status"] == "done"

    body = client.get(f"/jobs/{job_id}").text
    assert f"/api/v1/jobs/{job_id}/download?format=m4b" in body
    assert f"/api/v1/jobs/{job_id}/download?format=mp3" in body
    # І сторінка більше не каже, що формат вибрати неможливо
    assert "Формат не вибирається" not in body


def test_editor_offers_format_choices(client, fake_engine) -> None:
    """Чекбокси форматів є в редакторі, і дефолт приходить із налаштувань."""
    doc_id = upload(client)
    body = client.get(f"/documents/{doc_id}").text
    assert 'name="formats"' in body
    assert 'value="m4b"' in body
    assert 'value="wav"' in body
    assert 'name="chapter_split"' in body
    # MP3 — типовий формат, тож його чекбокс позначено
    assert re.search(r'value="mp3"\s+checked', body)


def test_editor_default_formats_follow_settings(client, fake_engine, monkeypatch) -> None:
    """Зміна OUTPUT_FORMATS міняє й типові чекбокси в редакторі."""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "output_formats", "m4b", raising=False)
    doc_id = upload(client)
    body = client.get(f"/documents/{doc_id}").text
    assert re.search(r'value="m4b"\s+checked', body)


def test_bad_format_from_ui_redirects_with_flash(client, fake_engine) -> None:
    """Помилковий формат у формі не створює завдання — людина бачить причину."""
    doc_id = upload(client)
    response = client.post(
        f"/ui/documents/{doc_id}/jobs",
        data={"formats": ["mp3", "flac"]},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash=bad_format" in response.headers["location"]
    assert not fake_engine

    page = client.get(f"/documents/{doc_id}?flash=bad_format").text
    assert "Невідомий формат" in page


def test_formats_are_persisted_in_job_options(client, fake_engine, db_engine) -> None:
    """Формати живуть у `options_json`, а не в пам'яті воркера.

    Інакше після перезапуску процесу (коли `requeue_incomplete_jobs` повертає
    завдання в чергу) повтор зібрав би лише MP3, хоч людина просила M4B.
    """
    doc_id = upload(client)
    job_id = start_job(client, doc_id, formats=["mp3", "m4b"])
    assert wait_for_status(client, job_id)["status"] == "done"

    with Session(db_engine) as session:
        job = session.get(Job, job_id)
        assert job.options_json["formats"] == ["mp3", "m4b"]
        assert set(job.options_json["artifacts"]) == {"mp3", "m4b"}
        assert Path(job.options_json["artifacts"]["m4b"]).is_file()


def _to_path(payload: bytes) -> Path:
    """Записати байти у тимчасовий файл — `av` і mutagen читають з диска."""
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as handle:
        handle.write(payload)
        return Path(handle.name)
