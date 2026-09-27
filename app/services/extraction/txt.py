"""Витяг блоків з текстових файлів (.txt, .md, .markdown).

Вихід: list[Block] з відповідними kind і speak.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.models import Block, BlockKind

# ── TXT ────────────────────────────────────────────────────────────────────────

_ENCODING_CHAIN = ("utf-8", "cp1251", "latin-1")


def _read_text_file(path: Path) -> str:
    """Спроба прочитати файл через ланцюг кодувань (ADR-009)."""
    for enc in _ENCODING_CHAIN:
        try:
            return path.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"Не вдалося прочитати {path.name}: невідоме кодування")


def extract_txt(path: Path) -> list[Block]:
    """Витяг блоків із .txt файлу.

    Параграфи розділяються порожніми рядками. Всі блоки — PARAGRAPH.
    """
    raw = _read_text_file(path)
    return _paragraphs_to_blocks(raw)


def _paragraphs_to_blocks(text: str) -> list[Block]:
    paragraphs = re.split(r"\n{2,}", text.strip())
    blocks: list[Block] = []
    for ordinal, para in enumerate(paragraphs):
        para = para.strip()
        if not para:
            continue
        blocks.append(Block(
            ordinal=ordinal,
            kind=BlockKind.PARAGRAPH,
            text_raw=para,
            text_normalized=para,  # нормалізація — окремий крок
            speak=True,
        ))
    return blocks


# ── Markdown ────────────────────────────────────────────────────────────────────

def extract_markdown(path: Path) -> list[Block]:
    """Витяг блоків із .md файлу через markdown-it-py."""
    try:
        from markdown_it import MarkdownIt
    except ImportError as exc:
        raise ImportError("markdown-it-py не встановлено: uv sync") from exc

    raw = _read_text_file(path)
    md = MarkdownIt()
    tokens = md.parse(raw)
    return _tokens_to_blocks(tokens)


def _tokens_to_blocks(tokens) -> list[Block]:
    """Конвертувати токени markdown-it у Block."""
    blocks: list[Block] = []
    ordinal = 0

    i = 0
    while i < len(tokens):
        tok = tokens[i]

        if tok.type == "heading_open":
            level = int(tok.tag[1])  # h1→1, h2→2…
            # Наступний токен — inline з текстом
            inline = tokens[i + 1] if i + 1 < len(tokens) else None
            text = inline.content if inline and inline.type == "inline" else ""
            if text:
                blocks.append(Block(
                    ordinal=ordinal,
                    kind=BlockKind.HEADING,
                    heading_level=level,
                    text_raw=text,
                    text_normalized=text,
                    speak=True,
                ))
                ordinal += 1
            i += 3  # heading_open + inline + heading_close
            continue

        if tok.type == "paragraph_open":
            inline = tokens[i + 1] if i + 1 < len(tokens) else None
            text = inline.content if inline and inline.type == "inline" else ""
            if text:
                blocks.append(Block(
                    ordinal=ordinal,
                    kind=BlockKind.PARAGRAPH,
                    text_raw=text,
                    text_normalized=text,
                    speak=True,
                ))
                ordinal += 1
            i += 3
            continue

        if tok.type == "bullet_list_open" or tok.type == "ordered_list_open":
            # Збираємо всі list_item всередині
            i += 1
            while i < len(tokens) and tok.type not in (
                "bullet_list_close", "ordered_list_close"
            ):
                tok = tokens[i]
                if tok.type == "inline" and tok.content:
                    blocks.append(Block(
                        ordinal=ordinal,
                        kind=BlockKind.LIST_ITEM,
                        text_raw=tok.content,
                        text_normalized=tok.content,
                        speak=True,
                    ))
                    ordinal += 1
                i += 1
            continue

        if tok.type == "fence" or tok.type == "code_block":
            # Код не озвучуємо
            blocks.append(Block(
                ordinal=ordinal,
                kind=BlockKind.CODE,
                text_raw=tok.content,
                text_normalized="",
                speak=False,
            ))
            ordinal += 1

        if tok.type == "blockquote_open":
            # Збираємо inline всередині quote
            i += 1
            while i < len(tokens) and tokens[i].type != "blockquote_close":
                if tokens[i].type == "inline" and tokens[i].content:
                    blocks.append(Block(
                        ordinal=ordinal,
                        kind=BlockKind.QUOTE,
                        text_raw=tokens[i].content,
                        text_normalized=tokens[i].content,
                        speak=True,
                    ))
                    ordinal += 1
                i += 1

        i += 1

    return blocks


# ── Роутер за розширенням ──────────────────────────────────────────────────────

def extract_text_file(path: Path) -> list[Block]:
    """Обрати екстрактор за розширенням файлу."""
    suffix = path.suffix.lower()
    if suffix in (".md", ".markdown"):
        return extract_markdown(path)
    if suffix == ".txt":
        return extract_txt(path)
    raise ValueError(f"Непідтримуваний формат для text-extractor: {suffix!r}")
