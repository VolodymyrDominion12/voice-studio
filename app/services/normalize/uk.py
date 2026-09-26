"""Нормалізація тексту для українського TTS.

Найдешевший спосіб підняти якість синтезу (docs/ARCHITECTURE.md, розділ 2.2).
Нормалізатор не мутує вихідний текст: результат → Block.text_normalized.

Правила:
  1. Прибирання розмітки: HTML-теги, markdown-посилання, зображення
  2. Символи → слова: %, №, §, →, &, …
  3. Числа → слова (num2words uk)
  4. Абревіатури та латиниця → вимова
  5. Типографіка: «», тире в діалозі, нерозривні пробіли
  6. Завантаження regex-правил з tts-stack/pre_process_map.yaml (якщо є)
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# Шлях до YAML-файлу з regex-правилами (може не існувати)
_PRE_PROCESS_MAP = Path(__file__).resolve().parents[4] / "tts-stack" / "pre_process_map.yaml"

# Lazy-ініціалізований список правил із YAML
_yaml_rules: list[tuple[re.Pattern, str]] | None = None


def _load_yaml_rules() -> list[tuple[re.Pattern, str]]:
    """Завантажити regex-правила з pre_process_map.yaml."""
    global _yaml_rules
    if _yaml_rules is not None:
        return _yaml_rules

    _yaml_rules = []
    if not _PRE_PROCESS_MAP.exists():
        logger.debug("pre_process_map.yaml не знайдено, пропускаємо")
        return _yaml_rules

    try:
        import yaml  # опційна залежність
        with _PRE_PROCESS_MAP.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
        for rule in data.get("rules", []):
            pattern = re.compile(rule["pattern"], re.MULTILINE)
            _yaml_rules.append((pattern, rule["replace"]))
        logger.debug("Завантажено %d regex-правил з pre_process_map.yaml", len(_yaml_rules))
    except ImportError:
        logger.debug("PyYAML не встановлено, пропускаємо pre_process_map.yaml")
    except Exception as exc:
        logger.warning("Помилка завантаження pre_process_map.yaml: %s", exc)

    return _yaml_rules


# ── Вбудовані правила нормалізації ────────────────────────────────────────────

# Markdown-посилання: [текст](url) → текст
_RE_MD_LINK      = re.compile(r"\[([^\]]+)\]\([^)]+\)")
# Markdown-зображення: ![alt](url) → alt
_RE_MD_IMAGE     = re.compile(r"!\[([^\]]*)\]\([^)]+\)")
# HTML-теги
_RE_HTML_TAG     = re.compile(r"<[^>]+>")
# Нерозривні пробіли та тонкі пробіли
_RE_NBSP         = re.compile(r"[\u00a0\u202f\u2009]")
# Тире на початку рядка (діалог) — пауза, а не слово
_RE_DIALOG_DASH  = re.compile(r"^\s*[—–]\s*", re.MULTILINE)
# Лапки «»
_RE_GUILLEMET    = re.compile(r"[«»]")
# Повторні пробіли/переноси
_RE_MULTISPACE   = re.compile(r"[ \t]{2,}")
_RE_MULTILINE    = re.compile(r"\n{3,}")

# Символи → слова
_SYMBOL_MAP: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(\d+)\s*%"),     r"\1 відсотків"),
    (re.compile(r"№\s*(\d+)"),     r"номер \1"),
    (re.compile(r"§\s*(\d+)"),     r"параграф \1"),
    (re.compile(r"→|->"),          "далі"),
    (re.compile(r"&"),             "і"),
    (re.compile(r"\.{3}"),         "…"),
    (re.compile(r"—"),             ","),   # тире всередині речення → пауза через кому
]

# Абревіатури
_ABBREV_MAP: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bIT\b"),    "ай-ті"),
    (re.compile(r"\bAPI\b"),   "ей-пі-ай"),
    (re.compile(r"\bPDF\b"),   "пі-ді-еф"),
    (re.compile(r"\bHTML\b"),  "ейч-ті-ем-ел"),
    (re.compile(r"\bCSS\b"),   "сі-ес-ес"),
    (re.compile(r"\bURL\b"),   "ю-ар-ел"),
    (re.compile(r"\bCEO\b"),   "сі-і-оу"),
    (re.compile(r"\bAI\b"),    "ей-ай"),
    (re.compile(r"\bML\b"),    "ем-ел"),
    (re.compile(r"\bUX\b"),    "ю-екс"),
    (re.compile(r"\bUI\b"),    "ю-ай"),
    (re.compile(r"\bSQL\b"),   "ес-ку-ел"),
    (re.compile(r"\bCPU\b"),   "сі-пі-ю"),
    (re.compile(r"\bGPU\b"),   "джі-пі-ю"),
    (re.compile(r"\bRAM\b"),   "рем"),
    (re.compile(r"\bSSD\b"),   "ес-ес-ді"),
    (re.compile(r"\bVPN\b"),   "ві-пі-ен"),
    (re.compile(r"\bтобто\b"), "тобто"),     # не абревіатура, але зберігаємо
]


def _numbers_to_words(text: str) -> str:
    """Конвертувати числа у слова через num2words."""
    try:
        from num2words import num2words
    except ImportError:
        return text  # якщо бібліотека не встановлена — пропускаємо

    def _replace(match: re.Match) -> str:
        raw = match.group(0).replace(",", ".").replace(" ", "")
        try:
            value = float(raw) if "." in raw else int(raw)
            return num2words(value, lang="uk")
        except (ValueError, OverflowError):
            return match.group(0)

    # Числа: цілі та десяткові з крапкою/комою
    return re.sub(r"\b\d[\d\s]*(?:[.,]\d+)?\b", _replace, text)


def normalize(text: str, apply_numbers: bool = True) -> str:
    """Нормалізувати текст для TTS.

    Args:
        text:           Вхідний текст.
        apply_numbers:  Чи конвертувати числа у слова.

    Returns:
        Нормалізований текст.
    """
    # 1. Прибирання розмітки
    text = _RE_MD_IMAGE.sub(r"\1", text)
    text = _RE_MD_LINK.sub(r"\1", text)
    text = _RE_HTML_TAG.sub(" ", text)

    # 2. Типографіка
    text = _RE_NBSP.sub(" ", text)
    text = _RE_DIALOG_DASH.sub("", text)
    text = _RE_GUILLEMET.sub("", text)

    # 3. Символи → слова
    for pattern, replacement in _SYMBOL_MAP:
        text = pattern.sub(replacement, text)

    # 4. Абревіатури
    for pattern, replacement in _ABBREV_MAP:
        text = pattern.sub(replacement, text)

    # 5. Числа → слова
    if apply_numbers:
        text = _numbers_to_words(text)

    # 6. YAML-правила (тонкі точкові виправлення)
    for pattern, replacement in _load_yaml_rules():
        text = pattern.sub(replacement, text)

    # 7. Прибирання зайвих пробілів
    text = _RE_MULTISPACE.sub(" ", text)
    text = _RE_MULTILINE.sub("\n\n", text)
    text = text.strip()

    return text
