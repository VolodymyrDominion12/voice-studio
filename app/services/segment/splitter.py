"""Сплітер речень для українського тексту.

Rule-based підхід — надійніший за загальні бібліотеки для української
(pysbd не оновлювався з 2021 р.). Підтримує ліміти рушія (max_chars).
"""

from __future__ import annotations

import re


# Абревіатури, які не є кінцем речення
_ABBREV = re.compile(
    r"\b(т\.д|т\.ч|тобто|напр|і\.т|ін|проф|акад|д-р|р|с|вул|пр|пл|кв)\."
)

# Крапки, що завершують речення
_SENTENCE_END = re.compile(r"([.!?…]+)\s+(?=[А-ЯA-ZЄІЇЎа-яa-z\d«\"(—–\u2014])")

# Знаки, за якими можна ділити якщо речення завелике
_SECONDARY_SPLIT = re.compile(r"([;,])\s+")


def split_sentences(text: str, max_chars: int = 300) -> list[str]:
    """Розбити текст на речення.

    Args:
        text:       Вхідний текст (один блок).
        max_chars:  Максимум символів у сегменті. Якщо речення більше —
                    ділимо додатково за крапкою з комою або комою.

    Returns:
        Список сегментів, кожен ≤ max_chars (за можливості).
    """
    if not text.strip():
        return []

    # Захищаємо абревіатури тимчасовим замінником
    placeholders: dict[str, str] = {}

    def protect(match: re.Match) -> str:
        key = f"\x00ABBR{len(placeholders)}\x00"
        placeholders[key] = match.group(0)
        return key

    protected = _ABBREV.sub(protect, text)

    # Ділимо за кінцями речень
    parts = _SENTENCE_END.split(protected)

    # Відновлюємо абревіатури і збираємо речення
    sentences: list[str] = []
    i = 0
    current = ""
    while i < len(parts):
        chunk = parts[i]
        # Відновлюємо захищені абревіатури
        for k, v in placeholders.items():
            chunk = chunk.replace(k, v)

        if i + 1 < len(parts) and re.match(r"[.!?…]+", parts[i + 1]):
            # Наступний елемент — пунктуація
            punct = parts[i + 1]
            for k, v in placeholders.items():
                punct = punct.replace(k, v)
            current = (current + chunk + punct).strip()
            i += 2
        else:
            current = (current + chunk).strip()
            i += 1

        if current:
            sentences.append(current)
            current = ""

    if current:
        sentences.append(current)

    # Ділимо речення, що перевищують ліміт
    result: list[str] = []
    for sent in sentences:
        if len(sent) <= max_chars:
            result.append(sent)
        else:
            result.extend(_split_long(sent, max_chars))

    return [s for s in result if s.strip()]


def _split_long(text: str, max_chars: int) -> list[str]:
    """Додаткове ділення довгого сегмента за крапкою з комою / комою."""
    parts = _SECONDARY_SPLIT.split(text)
    result: list[str] = []
    current = ""

    i = 0
    while i < len(parts):
        chunk = parts[i]
        punct = parts[i + 1] if i + 1 < len(parts) and len(parts[i + 1]) <= 1 else ""

        candidate = (current + " " + chunk + punct).strip() if current else (chunk + punct).strip()
        if len(candidate) <= max_chars:
            current = candidate
            i += 2 if punct else 1
        else:
            if current:
                result.append(current)
            # Якщо навіть один шматок більший — додаємо як є
            current = (chunk + punct).strip()
            i += 2 if punct else 1

    if current:
        result.append(current)

    # Фінальний fallback: розбити по max_chars
    final: list[str] = []
    for part in result:
        if len(part) <= max_chars:
            final.append(part)
        else:
            for j in range(0, len(part), max_chars):
                final.append(part[j:j + max_chars])

    return final
