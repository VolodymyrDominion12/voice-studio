"""Регресійні тести нормалізатора українського тексту.

Ці тести фіксують три дефекти, знайдені під час реалізації фронтенду (F0):
  1) тире «—» перетворювалось на кому, лишаючи «, ,» і руйнуючи діалог;
  2) жадібний `[\\d\\s]*` зʼїдав пробіл після числа: «2007 році» → «дві тисячі
     сімроці», «25 %» → «двадцять п'ятьвідсотків»;
  3) узгодження числівника з іменником було відсутнє: «2 млн» → «два мільйонів».

Запуск: uv run pytest tests/test_normalize.py -q
"""

from __future__ import annotations

import pytest

from app.services.normalize.uk import normalize

# ── Дефект 1: тире ────────────────────────────────────────────────────────────

def test_dash_becomes_pause_without_double_punctuation() -> None:
    """Тире всередині речення не має лишати «, ,»."""
    result = normalize("Доброго ранку, — сказала вона й усміхнулась.")
    assert result == "Доброго ранку, сказала вона й усміхнулась."
    assert ", ," not in result
    assert "—" not in result


def test_dash_after_word_becomes_comma() -> None:
    """Тире між словами стає комою (паузою), без пробілу перед нею."""
    assert normalize("Ціна — 100.") == "Ціна, сто."


def test_dialogue_lines_stay_separate() -> None:
    """Тире-маркер діалогу прибирається, але рядки не зліпаються."""
    result = normalize("— Сідай, — сказала вона.\n— Дякую, я постою.")
    assert result == "Сідай, сказала вона.\nДякую, я постою."
    assert "\n" in result


# ── Дефект 2: пробіл після числа ──────────────────────────────────────────────

@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("У 2007 році.", "У дві тисячі сім році."),
        ("Було 25 %.", "Було двадцять п'ять відсотків."),
        ("Це 1 500 осіб.", "Це одна тисяча п'ятсот осіб."),
    ],
)
def test_space_after_number_is_kept(source: str, expected: str) -> None:
    """Число не має злипатися з наступним словом."""
    result = normalize(source)
    assert result == expected
    assert "сімроці" not in result
    assert "п'ятьвідсотків" not in result


# ── Дефект 3: узгодження числівника з одиницею ────────────────────────────────

@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("1 грн", "одна гривня"),
        ("2 грн", "дві гривні"),
        ("5 грн", "п'ять гривень"),
        ("21 грн", "двадцять одна гривня"),
        ("22 грн", "двадцять дві гривні"),
        ("11 грн", "одинадцять гривень"),
        ("2 %", "два відсотки"),
        ("5 %", "п'ять відсотків"),
        ("1 кг", "один кілограм"),
        ("3 кг", "три кілограми"),
        ("2 млн", "два мільйони"),
        ("5 млн", "п'ять мільйонів"),
        ("3 год", "три години"),
    ],
)
def test_unit_agreement(source: str, expected: str) -> None:
    """Числівник узгоджується з одиницею: 1 / 2–4 / 5+ і рід."""
    assert normalize(source) == expected


def test_unit_without_number_uses_fallback() -> None:
    """Одиниця без числа розгортається (родовий множини як найчастіша форма)."""
    assert normalize("кілька млн осіб") == "кілька мільйонів осіб"


# ── Крайові випадки, які легко зламати ────────────────────────────────────────

def test_sentence_final_dot_is_preserved() -> None:
    """Крапка в кінці речення не має зникати разом зі скороченням."""
    assert normalize("Відстань 12 кг.") == "Відстань дванадцять кілограмів."
    assert normalize("Ціна 5 грн. і все.") == "Ціна п'ять гривень і все."


def test_markdown_markup_stripped() -> None:
    """Посилання й зображення лишають текст, а не розмітку."""
    assert normalize("Маркдаун [текст](http://x) і ![alt](i.png).") == "Маркдаун текст і alt."


def test_markup_strip_leaves_no_double_spaces() -> None:
    result = normalize("Слово <b>жирне</b> і   пробіли.")
    assert "  " not in result


def test_numbers_skipped_when_disabled() -> None:
    """З apply_numbers=False цифри лишаються, але розмітка прибирається."""
    result = normalize("Було 25 %.", apply_numbers=False)
    assert "25" in result
    assert "відсотків" in result


def test_decimal_number() -> None:
    """Десятковий дріб читається як «три кома чотирнадцять»."""
    assert normalize("Число 3,14.") == "Число три кома чотирнадцять."


def test_empty_and_whitespace_input() -> None:
    assert normalize("") == ""
    assert normalize("   \n\n  ") == ""
