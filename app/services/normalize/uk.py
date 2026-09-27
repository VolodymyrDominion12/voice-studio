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
# Подвоєна пунктуація, що виникає після заміни тире: "ранку, , сказала" → "ранку, сказала"
_RE_DOUBLE_PUNCT = re.compile(r"([,;:])\s*[,;:]+")
# Пробіл перед знаком пунктуації: "ранку ," → "ранку,"
_RE_SPACE_BEFORE_PUNCT = re.compile(r"[ \t]+([,;:.!?…])")

# Символи → слова
_SYMBOL_MAP: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(\d+)\s*%"),     r"\1 відсотків"),
    (re.compile(r"№\s*(\d+)"),     r"номер \1"),
    (re.compile(r"§\s*(\d+)"),     r"параграф \1"),
    (re.compile(r"→|->"),          "далі"),
    (re.compile(r"&"),             "і"),
    (re.compile(r"\.{3}"),         "…"),
    # Тире — це пауза, а не звук. Замінюємо на кому з пробілом; подвоєну
    # пунктуацію («ранку, — сказала») прибирає _RE_DOUBLE_PUNCT нижче.
    # Пробіли лише горизонтальні, щоб не зліпити рядки в один.
    (re.compile(r"[ \t]*[—–][ \t]*"), ", "),
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
    # Одиниці БЕЗ числа («кілька млн», «грн») — родовий множини як найчастіша
    # форма. Одиниці З числом обробляє _expand_units, який узгоджує відмінок.
    (re.compile(r"\bгрн\b\.?"),  "гривень"),
    (re.compile(r"\bмлн\b\.?"),  "мільйонів"),
    (re.compile(r"\bмлрд\b\.?"), "мільярдів"),
    (re.compile(r"\bтис\b\.?"),  "тисяч"),
    (re.compile(r"\bкм\b"),      "кілометрів"),
    (re.compile(r"\bкг\b"),      "кілограмів"),
    (re.compile(r"\bсм\b"),      "сантиметрів"),
    (re.compile(r"\bмм\b"),      "міліметрів"),
    (re.compile(r"\bхв\b\.?"),   "хвилин"),
    (re.compile(r"\bрр\."),      "років"),
    (re.compile(r"\bт\.\s*д\."), "тощо"),
    (re.compile(r"\bт\.\s*п\."), "тощо"),
    (re.compile(r"\bобл\."),     "область"),
    (re.compile(r"\bвул\."),     "вулиця"),
]

# ── Одиниці виміру: числівник + іменник разом ─────────────────────────────────
# Українське узгодження: 1 → однина, 2–4 → множина, 0 і 5+ → родовий множини.
# Без цього «2 млн» читалося б як «два мільйонів», а «2 %» — як
# «два відсотків» замість правильної форми для 2–4.
# Формат: (однина, 2–4, 5+, рід числівника) — рід у формі num2words.
_UNIT_FORMS: dict[str, tuple[str, str, str, str]] = {
    "%":    ("відсоток",  "відсотки",   "відсотків",  "masculine"),
    "грн":  ("гривня",    "гривні",     "гривень",    "feminine"),
    "коп":  ("копійка",   "копійки",    "копійок",    "feminine"),
    "млн":  ("мільйон",   "мільйони",   "мільйонів",  "masculine"),
    "млрд": ("мільярд",   "мільярди",   "мільярдів",  "masculine"),
    "тис":  ("тисяча",    "тисячі",     "тисяч",      "feminine"),
    "км":   ("кілометр",  "кілометри",  "кілометрів", "masculine"),
    "кг":   ("кілограм",  "кілограми",  "кілограмів", "masculine"),
    "см":   ("сантиметр", "сантиметри", "сантиметрів", "masculine"),
    "мм":   ("міліметр",  "міліметри",  "міліметрів", "masculine"),
    "хв":   ("хвилина",   "хвилини",    "хвилин",     "feminine"),
    "год":  ("година",    "години",     "годин",      "feminine"),
}

# Довші позначення — першими, щоб «млн» не зматчилося як «м» + «лн».
# «%» окремою гілкою: у нього немає межі слова.
#
# Крапка після одиниці зʼїдається ЛИШЕ якщо це крапка скорочення (далі йде
# мала літера чи кома). Крапка в кінці речення лишається на місці, інакше
# «Відстань 12 кг.» втрачало б кінець речення.
_UNIT_RE = re.compile(
    r"(\d+)\s*(млрд|млн|тис|грн|коп|год|км|кг|см|мм|хв)\b(?:\.(?=\s*[а-яіїєґa-z]|,))?"
    r"|(\d+)\s*%"
)


def _plural_form(value: int, forms: tuple[str, str, str, str]) -> str:
    """Вибрати форму іменника за числівником (українське правило)."""
    one, few, many, _gender = forms
    n = abs(value) % 100
    if 11 <= n <= 14:
        return many
    last = n % 10
    if last == 1:
        return one
    if 2 <= last <= 4:
        return few
    return many


def _expand_units(text: str) -> str:
    """«2 млн» → «два мільйони», «25 %» → «двадцять п'ять відсотків».

    Числівник відразу перетворюється на слово, тому подальший прохід
    _numbers_to_words ці числа вже не бачить.
    """
    try:
        from num2words import num2words
    except ImportError:
        return text  # якщо бібліотека не встановлена — пропускаємо

    def _replace(match: re.Match) -> str:
        raw_number = match.group(1) or match.group(3)
        unit = match.group(2) or "%"
        forms = _UNIT_FORMS.get(unit, ("", "", "", "m"))
        value = int(raw_number)
        word = num2words(value, lang="uk", gender=forms[3])
        return f"{word} {_plural_form(value, forms)}"

    return _UNIT_RE.sub(_replace, text)


def _numbers_to_words(text: str) -> str:
    """Конвертувати числа у слова через num2words.

    Відоме обмеження: роки читаються кількісним числівником
    («у дві тисячі сім році» замість «у дві тисячі сьомому році»).
    num2words уміє лише називний відмінок (`to="ordinal"` дає «сьомий»),
    а для місцевого відмінка потрібна таблиця відмінювання — свідомо не
    робимо напіввиправлення.
    """
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

    # Числа: цілі, тисячі з пробілом («1 500») і десяткові («3,14»).
    # УВАГА: у класі навмисно НЕМАЄ \s — інакше пробіл після числа зʼїдається
    # разом із числом, і «У 2007 році» стає «У дві тисячі сімроці»,
    # а «25 %» — «двадцять п'ятьвідсотків».
    return re.sub(r"\b\d+(?:[ ]\d+)*(?:[.,]\d+)?\b", _replace, text)


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

    # 3. Одиниці виміру разом із числом — до символів, бо «%» теж одиниця
    if apply_numbers:
        text = _expand_units(text)

    # 4. Символи → слова
    for pattern, replacement in _SYMBOL_MAP:
        text = pattern.sub(replacement, text)

    # 4.5 Пунктуація після замін: тире → кома могло дати «, ,» або « ,»
    text = _RE_DOUBLE_PUNCT.sub(r"\1", text)
    text = _RE_SPACE_BEFORE_PUNCT.sub(r"\1", text)

    # 5. Абревіатури
    for pattern, replacement in _ABBREV_MAP:
        text = pattern.sub(replacement, text)

    # 6. Числа → слова
    if apply_numbers:
        text = _numbers_to_words(text)

    # 7. YAML-правила (тонкі точкові виправлення)
    for pattern, replacement in _load_yaml_rules():
        text = pattern.sub(replacement, text)

    # 7. Прибирання зайвих пробілів
    text = _RE_MULTISPACE.sub(" ", text)
    text = _RE_MULTILINE.sub("\n\n", text)
    text = text.strip()

    return text
