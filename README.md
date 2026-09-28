# Voice Studio — озвучення текстів з виразом

Локальний застосунок: завантажуєш `.md` / `.pdf` / `.txt` / `.epub` → отримуєш
чистий текст → редагуєш його → обираєш голос і емоцію → отримуєш аудіо з
виразом (не пласке читання), плюс зібраний MP3 з розділами.

Усе працює локально на цій машині: FastAPI + SQLite + asyncio-воркер, TTS-рушій
за OpenAI-сумісним контрактом (`/v1/audio/speech`). Ніяких зовнішніх API, доки
сам не увімкнеш `REMOTE_TTS_ENABLED` (етап 3 у [`docs/PLAN.md`](docs/PLAN.md)).

**Зміст:** [статус](#0-статус-на-сьогодні) ·
[швидкий старт](#1-швидкий-старт) ·
[довідник команд](#2-довідник-команд) ·
[конфігурація](#3-конфігурація-env) ·
[HTTP API](#4-http-api) ·
[конвеєр](#5-конвеєр-обробки-тексту) ·
[емоції](#6-емоції-шар-виразу) ·
[рушії та голоси](#7-рушії-та-голоси) ·
[дані на диску](#8-дані-на-диску) ·
[розробка](#9-розробка) ·
[ліцензії](#10-ліцензійна-гігієна) ·
[відомі проблеми](#11-відомі-проблеми-й-обмеження) ·
[troubleshooting](#12-типові-проблеми) ·
[що перевірено](#13-що-перевірено-а-що-ні) ·
[куди далі](#14-куди-далі) ·
[карта документації](#15-карта-документації)

---

## 0. Статус на сьогодні

Проєкт — **робочий каркас із працюючим TTS-плечем, робочим конвеєром
«файл → блоки → нормалізація» і першою фазою веб-інтерфейсу**. Обидва блокери
запису в БД закрито. Нижче — чесна карта, бо від неї залежить, що взагалі
можна запустити сьогодні (перевірено на цій машині, див.
[«Що перевірено, а що ні»](#13-що-перевірено-а-що-ні)).

| Шматок | Стан |
|---|---|
| `GET /api/v1/health`, `/engines`, `/engines/{id}/voices`, `/emotions` | ✅ працює |
| `POST /api/v1/preview` (синтез фрагмента в WAV) | ✅ працює, якщо в `.env` задано `TTS_MODEL` (див. [§7](#7-рушії-та-голоси)) |
| TTS-шлюз Speaches (Docker, порт 8001) | ✅ працює, українські Piper-голоси ставляться однією командою |
| Нормалізація української, сплітер речень, профілі емоцій | ✅ працює, покрито тестами (`tests/test_normalize.py`) |
| `pytest tests/ -q` | ✅ **219 passed**, `ruff check app/ tests/` — 0 зауважень |
| Завантаження документів, блоки, редагування, нормалізація, видалення | ✅ працює (блокери 1 і 2 закрито, див. [§11](#11-відомі-проблеми-й-обмеження)) |
| Завдання синтезу (`/jobs`, SSE, скасування, повтор, завантаження) | ✅ працює; повний цикл «файл → правка → MP3» пройдено через інтерфейс |
| Веб-інтерфейс | ✅ фази F0–F3: робоча стола, редактор блоків із автозбереженням і прев'ю, черга зі SSE-прогресом, сегментний плеєр, сторінка голосів, пресети, гарячі клавіші. Концепція — [`docs/FRONTEND.md`](docs/FRONTEND.md), макет — [`docs/frontend-mockup.html`](docs/frontend-mockup.html) |
| PDF / DOCX / EPUB / HTML | ✅ працює: `.pdf` через PyMuPDF (з евристикою скана), решта — через markitdown. Повний цикл «PDF → MP3» пройдено |
| Пресети | ✅ CRUD-API + створення з редактора + застосування емоції до всіх блоків |
| **Аудіокнига:** M4B із розділами, ID3-теги, zip по розділах | ✅ працює: заголовки документа стають розділами (з наскрізною нумерацією), M4B/AAC кодує PyAV — колесо зі вбудованими бібліотеками FFmpeg, тож системний `ffmpeg` **не потрібен** (ADR-013); теги пише `mutagen`. Перевірено наскрізно на живому шлюзі, див. [§13](#13-що-перевірено-а-що-ні) |
| Вибір формату результату | ✅ чекбокси MP3 / M4B / WAV і «zip по розділах» у редакторі (`options.formats`); `?format=` віддає один із **створених** файлів |
| Клонування голосу, OCR, LLM-розмітка емоцій | ❌ етапи 3–4 |

Головна ідея проєкту не змінилась: **не прив'язуватись до однієї TTS-моделі**.

```
текст → нормалізація → сегментація → розмітка емоцій → адаптер рушія TTS → пост-обробка → збірка
```

«Вираз» народжується у двох місцях: (1) у самій моделі, якщо вона вміє емоції,
і (2) у шарі пост-обробки, який керує темпом, паузами та гучністю для кожного
сегмента. Другий шар працює з **будь-яким** рушієм — саме він дає змогу почати
на швидкій CPU-моделі й перейти на важчу, не переписуючи застосунок.

### Головна знахідка дослідження

Більшість «виразних» відкритих моделей українську **не** підтримують: XTTS-v2
(17 мов) і Chatterbox Multilingual V3 (23 мови) — обидва без неї, а робочих
українських файнтюнів XTTS не існує. Виняток — **ZONOS2** (Zyphra, ~7.6B MoE):
українська (Tier 3), вираз, клонування голосу, Apache-2.0 і офіційна CPU-збірка
(`zonos2.cpp`). Він **не** в MVP: ~5 GB на диску і, головне, не перевірено ні
якість української, ні швидкість на CPU — це етап 3 із явною розвилкою.
Деталі — [`docs/RESEARCH.md`](docs/RESEARCH.md), розділ 1.

---

## 1. Швидкий старт

### Варіант A — Docker (рекомендовано, стек уже перевірено)

```bash
cd voice-studio
cp .env.example .env                    # один раз; ключі сюди, не в git
docker compose -f docker/docker-compose.yml up -d
docker compose -f docker/docker-compose.yml ps      # обидва сервіси мають бути healthy

curl -fsS http://127.0.0.1:8000/api/v1/health       # застосунок
curl -fsS http://127.0.0.1:8001/health              # TTS-шлюз
```

Піднімаються два контейнери:

| Сервіс | Порт (лише localhost) | Що це |
|---|---|---|
| `app` | `127.0.0.1:8000` | FastAPI-застосунок, Swagger на `/docs` |
| `tts` | `127.0.0.1:8001` | Speaches — OpenAI-сумісний TTS-шлюз (Piper) |
| `tts-gpu` | `127.0.0.1:8002` | той самий шлюз на CUDA, профіль `--profile gpu`, свідомо окремий сервіс |

### Варіант B — локально через `uv` (Python 3.11)

```bash
cd voice-studio
uv sync --extra dev                     # база + pytest/ruff/mypy у .venv
cp .env.example .env
.venv/bin/python -m pytest tests/ -q                       # → 5 passed
.venv/bin/python -m uvicorn app.main:app --reload --port 8010
```

Порт 8010, а не 8000, бо 8000 займає Docker-версія застосунку. Перевірити, що
зайнято:

```bash
ss -ltnp | grep -E ':(8000|8001|8010)'
```

### Одразу після старту: поставити голос і ввімкнути синтез

Свіжий Speaches **не містить жодної моделі**, тому `/api/v1/preview` віддає 503.
Одна команда це виправляє (перевірено: ~20 MB, ~4 с):

```bash
# Поставити український Piper-голос у шлюз
curl -fsS -X POST http://127.0.0.1:8001/v1/models/speaches-ai/piper-uk_UA-lada-x_low
curl -fsS http://127.0.0.1:8001/v1/models          # перевірити, що з'явився

# Сказати застосунку, яку модель просити в шлюзу
# .env:  TTS_MODEL=speaches-ai/piper-uk_UA-lada-x_low

# Перевірити синтез
curl -fsS -X POST http://127.0.0.1:8000/api/v1/preview \
  -H 'Content-Type: application/json' \
  -d '{"text":"Привіт, це перевірка українського голосу.","voice_id":"uk_UA-lada-x_low","emotion":"joyful","intensity":0.7}' \
  --output /tmp/preview.wav
file /tmp/preview.wav                              # WAVE audio, PCM 16 bit, mono
```

**Чому `TTS_MODEL` обов'язковий.** Типове значення — `tts-1`, і Speaches розуміє
його як Kokoro (`speaches-ai/Kokoro-82M-v1.0-ONNX`), якого локально немає, тож
синтез падає з 503. Явна Piper-модель знімає питання.

Для Docker-версії застосунку `TTS_MODEL` треба додати ще й у
`docker/docker-compose.yml` у `environment:` сервісу `app` (контейнер не читає
`.env` з хоста) і перезапустити:

```bash
docker compose -f docker/docker-compose.yml up -d app
```

---

## 2. Довідник команд

Ті самі команди зібрані в [`Makefile`](Makefile): `make` без аргументів друкує
їхній список із поясненнями, `make doctor` перевіряє, чи середовище взагалі
готове (інтерпретатор, `.env`, БД, Docker, TTS-шлюз). Змінні перекриваються з
командного рядка: `make dev PORT=9000`,
`make bench ARGS="--engine piper --repeat 3"`, `make preview PORT=8000`.
Нижче — той самий довідник у явному вигляді, з поясненнями.

### Середовище й залежності

```bash
uv sync --extra dev            # база + pytest/ruff/mypy
uv sync --extra piper          # MVP-рушій Piper
uv sync --extra uk             # українська, in-process (ukrainian-tts)
uv sync --extra expressive     # англійська, in-process (chatterbox-tts)
# extra `m4b` більше немає: M4B робить `av` (PyAV) із базових залежностей,
# системний ffmpeg для нього не потрібен (ADR-013)
uv lock                        # перерахувати lock
uv python install 3.11         # якщо 3.11 немає в системі
```

`uk` і `expressive` **не сумісні** (різні покоління `torch`/`numpy`, ADR-012) —
`uv` зупинить спробу поставити їх разом. Рушій, чий стек конфліктує з поточним
середовищем, підключається через `openai_compat`, а не імпортом.

```bash
uv sync --extra uk --extra expressive
# → error: Extras `expressive` and `uk` are incompatible with the declared conflicts
```

Друге середовище для другого рушія:

```bash
UV_PROJECT_ENVIRONMENT=.venv-expressive uv sync --extra expressive
```

### Запуск

```bash
.venv/bin/python -m uvicorn app.main:app --reload --port 8010     # dev, автоперезапуск
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8010
uv run python -m uvicorn app.main:app --port 8010                 # те саме через uv
```

Точки входу для читання контракту:

| URL | Що там |
|---|---|
| `http://127.0.0.1:8000/docs` | Swagger UI — єдина « UI » на сьогодні |
| `http://127.0.0.1:8000/redoc` | ReDoc |
| `http://127.0.0.1:8000/openapi.json` | OpenAPI-схема |
| `http://127.0.0.1:8001/docs` | Swagger самого TTS-шлюзу |

### Тести, лінт, типи

```bash
.venv/bin/python -m pytest tests/ -q                 # → 5 passed
.venv/bin/python -m pytest tests/ -q -k engines      # один тест
.venv/bin/python -m pytest tests/test_smoke.py -v

.venv/bin/ruff check .                               # лінт (див. примітку нижче)
.venv/bin/ruff check . --fix                         # автофікси
.venv/bin/ruff format .                              # форматування
.venv/bin/mypy --explicit-package-bases app          # типи
```

Примітки (перевірено 28.09):

* `ruff check .` → **0 зауважень**. Раніше тут було 249, з них 192 —
  `RUF001/RUF002/RUF003` («ambiguous unicode»: кирилиця в рядках і коментарях,
  тобто нормальний стан для українського проєкту). Тепер вони в
  `[tool.ruff.lint].ignore` разом із `B008` (канонічний `Depends()` у FastAPI),
  і лінт показує лише справжні проблеми.
* `mypy app` без прапорця падає на «Source file found twice under different
  module names» — бо в `app/` немає `app/__init__.py`. Робоча команда —
  `mypy --explicit-package-bases app` → **29 зауважень у 6 файлах**. Це
  переважно слід SQLModel: він типізує `Job.status.in_(...)` і
  `Job.created_at.desc()` як звичайні атрибути, тож mypy бачить `int` і
  `bool` там, де SQLAlchemy повертає вирази (`app/api/jobs.py` — 6,
  `app/ui/router.py` — 8, `app/worker/queue.py` — 4). Нові модулі аудіокниги
  (`services/audio/assemble.py`, `services/library/chapters.py`) дають **0**.`

### Бенчмарк рушіїв

```bash
uv run python scripts/benchmark.py                        # усі рушії
uv run python scripts/benchmark.py --engine piper
uv run python scripts/benchmark.py --engine openai-compat --voice uk_UA-lada-x_low
uv run python scripts/benchmark.py --repeat 3             # медіана з 3 прогонів
uv run python scripts/benchmark.py --text-file my.txt     # власний текст
```

Пише реальні числа (RTF, пікова RAM, розмір) у `data/benchmark/benchmark.json`.
Рушій, якого немає, не ламає прогін — рядок `[SKIP]` і далі. Це інструмент, щоб
замінити оцінки в [`docs/RESEARCH.md`](docs/RESEARCH.md) на вимірювання; на цій
машині він **ще не запускався**.

### Docker

```bash
docker compose -f docker/docker-compose.yml up -d            # CPU-стек
docker compose -f docker/docker-compose.yml up -d tts        # лише шлюз
docker compose -f docker/docker-compose.yml ps
docker compose -f docker/docker-compose.yml logs -f app
docker compose -f docker/docker-compose.yml logs -f tts
docker compose -f docker/docker-compose.yml restart tts
docker compose -f docker/docker-compose.yml down             # зупинити
docker compose -f docker/docker-compose.yml --profile gpu up -d   # GPU-профіль (2 GB VRAM — перевірте nvidia-smi)
```

### TTS-шлюз (Speaches) зсередини

```bash
curl -fsS http://127.0.0.1:8001/health
curl -fsS http://127.0.0.1:8001/v1/models                      # що вже завантажено
curl -fsS http://127.0.0.1:8001/v1/registry                    # 750 записів, з них 156 TTS
curl -fsS http://127.0.0.1:8001/v1/audio/voices                # голоси завантажених моделей
curl -fsS -X POST http://127.0.0.1:8001/v1/models/<model_id>   # завантажити модель
curl -fsS -X DELETE http://127.0.0.1:8001/v1/models/<model_id> # видалити (звільнити диск)
curl -fsS http://127.0.0.1:8001/api/ps                         # що тримається в памʼяті
```

Прямий синтез повз застосунок:

```bash
curl -fsS http://127.0.0.1:8001/v1/audio/speech \
  -H 'Content-Type: application/json' \
  -d '{"model":"speaches-ai/piper-uk_UA-lada-x_low","input":"Привіт, це перевірка.","voice":"lada","response_format":"wav"}' \
  --output /tmp/probe.wav && file /tmp/probe.wav
```

Знайти українські моделі в реєстрі:

```bash
curl -fsS http://127.0.0.1:8001/v1/registry | .venv/bin/python -c "
import sys, json
for m in json.load(sys.stdin)['data']:
    if 'piper' in m.get('id','') and 'uk' in m.get('id','').lower():
        print(m['id'], '|', m.get('sample_rate'), '|', [v['id'] for v in m.get('voices',[])])
"
```

### Голоси Piper напряму (для `--extra piper` і `scripts/benchmark.py`)

```bash
python -m piper.download_voices \
  uk_UA-tetiana-high uk_UA-mykyta-high uk_UA-oleksa-high uk_UA-lada-x_low \
  --data-dir ./data/models/piper
```

### Стан на диску й SQLite

```bash
ls -la data data/uploads data/renders data/models/hf
du -sh data/* data/models/hf

.venv/bin/python -c "
import sqlite3; c = sqlite3.connect('data/voice_studio.sqlite3')
print([r[0] for r in c.execute(\"select name from sqlite_master where type='table'\")])
for t in ('documents','blocks','segments','jobs'):
    print(t, c.execute(f'select count(*) from {t}').fetchone()[0])
"
```

Повний скид стану (застосунок має бути зупинений):

```bash
docker compose -f docker/docker-compose.yml stop app
rm -f data/voice_studio.sqlite3 data/voice_studio.sqlite3-wal data/voice_studio.sqlite3-shm
rm -rf data/renders/* data/uploads/*
docker compose -f docker/docker-compose.yml start app      # таблиці створяться знову
```

---

## 3. Конфігурація `.env`

Усі змінні читаються `app/config.py` (pydantic-settings) з `.env` у корені
проєкту; змінні середовища мають пріоритет над файлом. Повний шаблон —
[`.env.example`](.env.example).

| Змінна | Типово | Що робить |
|---|---|---|
| `VOICE_STUDIO_DATA_DIR` (або `DATA_DIR`) | `./data` | корінь даних: uploads, renders, voices, БД. Працюють **обидва** імені: раніше читалося лише `DATA_DIR`, тож перенесення даних на інший диск мовчки ігнорувалося |
| `VOICE_STUDIO_MODEL_DIR` (або `MODEL_DIR`) | `./data/models` | моделі; виносьте на зовнішній носій, якщо тісно з диском |
| `TTS_BASE_URL` | `http://127.0.0.1:8001/v1` | OpenAI-сумісний шлюз (`http://tts:8000/v1` всередині compose) |
| `TTS_API_KEY` | `not-needed` | ключ шлюзу; для локального Speaches не потрібен |
| `TTS_MODEL` | `tts-1` | **модель у запиті до шлюзу.** Для Piper — `speaches-ai/piper-uk_UA-lada-x_low` тощо. У `.env.example` його немає — додайте вручну (див. [§1](#одразу-після-старту-поставити-голос-і-ввімкнути-синтез)) |
| `DEFAULT_ENGINE` | `openai_compat` | рушій за замовчуванням |
| `DEFAULT_VOICE` | `uk_UA-tetiana-high` | голос за замовчуванням. ⚠️ у реєстрі Speaches такого голосу немає — див. [§7](#7-рушії-та-голоси) |
| `DEFAULT_EMOTION` | `neutral` | емоція за замовчуванням |
| `SYNTH_CONCURRENCY` | `2` | паралельних сегментів; для CPU 2 — моделі й так займають ядра (1–16) |
| `TARGET_LUFS` | `-18.0` | цільова гучність: −18 — аудіокнига, −16 — подкаст |
| `TARGET_SAMPLE_RATE` | `24000` | частота готового файлу |
| `SEGMENT_MAX_CHARS` | `300` | максимум символів у сегменті (перекривається `max_chars()` рушія) |
| `ENABLE_PITCH_SHIFT` | `false` | зсув тону — найризикованіша для артефактів операція (ADR-004) |
| `MAX_UPLOAD_MB` | `50` | ліміт завантаження; перевіряється під час запису (413) |
| `OUTPUT_FORMATS` | `mp3` | які файли створювати типово: `mp3`, `m4b`, `wav` (кілька через кому). У редакторі вибір є на кожне завдання окремо |
| `M4B_BITRATE_KBPS` | `64` | бітрейт AAC для M4B (моно 64 kbps — достатньо для аудіокниги) |
| `MP3_BITRATE_KBPS` | `192` | бітрейт MP3 |
| `AUDIOBOOK_ARTIST` | `Voice Studio` | виконавець у тегах ID3/MP4 |
| `ALLOWED_EXTENSIONS` | `.txt,.md,.markdown,.pdf,.docx,.epub,.html,.htm` | білий список розширень (перевіряється — 415) |
| `REMOTE_TTS_ENABLED` | `false` | зовнішній API (ет. 3). Ключі — лише в `.env`, не в git |
| `REMOTE_TTS_BASE_URL`, `REMOTE_TTS_API_KEY` | порожні | параметри того ж зовнішнього API |

---

## 4. HTTP API

Базовий шлях — `/api/v1`. Актуальний перелік завжди в `/openapi.json`.

| Метод | Шлях | Призначення | Стан |
|---|---|---|---|
| `GET` | `/health` | статус, версія, доступність `data_dir`, шлюз | ✅ |
| `GET` | `/engines` | каталог рушіїв із `capabilities` | ✅ |
| `GET` | `/engines/{id}/voices` | голоси рушія (404 для невідомого) | ✅ |
| `GET` | `/emotions` | профілі емоцій (8 штук) | ✅ |
| `POST` | `/preview` | синтез фрагмента ≤ 500 символів → WAV | ✅ за умови `TTS_MODEL` |
| `POST` | `/documents` | завантажити файл → документ + блоки (201) | ❌ 500 |
| `GET` | `/documents` | список документів | ❌ 500 |
| `GET` | `/documents/{id}` | документ із блоками | ❌ 500 |
| `PATCH` | `/documents/{id}/blocks/{bid}` | правити текст / емоцію / `speak` | ❌ 500 |
| `POST` | `/documents/{id}/normalize` | перезапустити нормалізацію | ❌ 500 |
| `POST` | `/jobs` | створити завдання синтезу (201) | ❌ 500 |
| `GET` | `/jobs` | черга завдань | ❌ 500 |
| `GET` | `/jobs/{id}/events` | **SSE**: прогрес у реальному часі | ❌ 500 |
| `POST` | `/jobs/{id}/cancel` | скасувати завдання | ❌ 500 |
| `GET` | `/jobs/{id}/download?format=mp3\|wav\|m4b\|zip` | один із реально створених файлів завдання | ✅ |

`❌ 500` у рядках вище — **історичний знімок** часів спільного блокера:
будь-який запит, що торкався моделей `Document` / `Block` / `Job`, падав з
торкається моделей `Document` / `Block` / `Job`, падає з
`InvalidRequestError` при ініціалізації mapper. Причини й перевірене
виправлення — у [§11](#11-відомі-проблеми-й-обмеження).

### Приклади `curl`

```bash
# Статус
curl -fsS http://127.0.0.1:8000/api/v1/health | .venv/bin/python -m json.tool

# Рушії та голоси
curl -fsS http://127.0.0.1:8000/api/v1/engines        | .venv/bin/python -m json.tool
curl -fsS http://127.0.0.1:8000/api/v1/engines/openai_compat/voices

# Емоції
curl -fsS http://127.0.0.1:8000/api/v1/emotions       | .venv/bin/python -m json.tool

# Прев'ю (WAV у відповідь)
curl -fsS -X POST http://127.0.0.1:8000/api/v1/preview \
  -H 'Content-Type: application/json' \
  -d '{"text":"Короткий фрагмент для проби голосу.","voice_id":"uk_UA-lada-x_low","emotion":"warm","intensity":0.6}' \
  --output /tmp/preview.wav
```

Повний цикл (працюватиме після виправлення блокерів):

```bash
# 1. Завантажити .md → документ із блоками
curl -fsS -X POST http://127.0.0.1:8000/api/v1/documents \
  -F "file=@book.md;type=text/markdown" | .venv/bin/python -m json.tool

# 2. Подивитись блоки (text_raw / text_normalized / emotion / intensity / speak)
curl -fsS http://127.0.0.1:8000/api/v1/documents/1 | .venv/bin/python -m json.tool

# 3. Відредагувати блок: свій текст, емоція, інтенсивність, озвучувати чи ні
curl -fsS -X PATCH http://127.0.0.1:8000/api/v1/documents/1/blocks/7 \
  -H 'Content-Type: application/json' \
  -d '{"text_edited":"Правлений текст.","emotion":"sad","intensity":0.8,"speak":true}'

# 4. Перезапустити нормалізацію
curl -fsS -X POST http://127.0.0.1:8000/api/v1/documents/1/normalize

# 5. Поставити синтез у чергу
curl -fsS -X POST http://127.0.0.1:8000/api/v1/jobs \
  -H 'Content-Type: application/json' \
  -d '{"document_id":1,"engine_id":"openai_compat","voice_id":"uk_UA-lada-x_low"}'

# 6. Слухати прогрес (SSE, -N вимикає буферизацію)
curl -N http://127.0.0.1:8000/api/v1/jobs/1/events

# 7. Скасувати / забрати результат
curl -fsS -X POST http://127.0.0.1:8000/api/v1/jobs/1/cancel
curl -fsS -o book.mp3 'http://127.0.0.1:8000/api/v1/jobs/1/download?format=mp3'
```

### Формат SSE

```
event: progress   data: {"job_id":7,"status":"running","done":42,"total":180,"percent":23.3}
event: segment    data: {"job_id":7,"block_id":11,"ordinal":0,"status":"done"}
event: finished   data: {"job_id":7,"output_path":"data/renders/job_7.mp3","status":"done"}
event: cancelled  data: {"job_id":7}
event: error      data: {"job_id":7,"status":"failed"}
```

Якщо завдання вже завершене, стрім віддає одну фінальну подію й закривається.
Тиша довше 30 с розбавляється коментарем `: keepalive`.

### Коди відповідей

| Код | Коли |
|---|---|
| `415` | розширення не з білого списку (`ALLOWED_EXTENSIONS`) — перевірено |
| `404` | документ / блок / завдання / рушій не знайдено |
| `409` | завдання вже завершене (скасування) або результат ще не готовий |
| `422` | помилка витягу тексту (напр. `.pdf` — екстрактор ще не реалізовано) |
| `503` | TTS-шлюз недоступний або модель не завантажена |

---

## 5. Конвеєр обробки тексту

Кожен етап можна запускати окремо — це принципово: користувач має бачити й
правити текст **до** синтезу.

| Етап | Код | Що робить сьогодні |
|---|---|---|
| Витяг | [`app/services/extraction/txt.py`](app/services/extraction/txt.py) | `.txt` — абзаци за порожніми рядками, ланцюг кодувань UTF-8 → CP1251 → latin-1; `.md` — `markdown-it-py` → блоки `heading` / `paragraph` / `list_item` / `quote` / `code` (код не озвучується, `speak=False`). PDF — PyMuPDF (заголовки за розміром шрифту, евристика скана), DOCX/EPUB/HTML — markitdown |
| Нормалізація | [`app/services/normalize/uk.py`](app/services/normalize/uk.py) | прибирає розмітку й HTML, символи → слова (`%`, `№`, `§`, `→`, `&`), числа → слова (`num2words`, `lang="uk"`), абревіатури (`IT` → «ай-ті» …), типографіка («» і діалогове тире), плюс regex-правила з `tts-stack/pre_process_map.yaml`. Вихідний текст не мутує: результат іде в `blocks.text_normalized` |
| Сегментація | [`app/services/segment/splitter.py`](app/services/segment/splitter.py) | rule-based ділення за `. ! ? …` з захистом абревіатур; завеликі сегменти — за `;` і `,`, далі жорстко за `max_chars` |
| Шар виразу | [`app/services/expression/profiles.py`](app/services/expression/profiles.py) | `get_prosody(emotion, intensity)` → `ProsodyPlan`: темп, тон, гучність, паузи |
| Синтез | [`app/services/tts/`](app/services/tts) | `TTSEngine` + `openai_compat` (єдиний реалізований адаптер) |
| Збірка | [`app/services/audio/assemble.py`](app/services/audio/assemble.py) | склейка WAV, ресемплінг, паузи, нормалізація до `TARGET_LUFS` (`pyloudnorm`), MP3 через `lameenc` + ID3 (`mutagen`), M4B/AAC із розділами через PyAV, zip по розділах — **усе без системного ffmpeg** (ADR-006, ADR-013) |
| Розділи | [`app/services/library/chapters.py`](app/services/library/chapters.py) | заголовки → план розділів: межею стає найвищий наявний рівень, текст до першого заголовка — «Вступ» без номера, наскрізна нумерація для метаданих і імен файлів |
| Черга | [`app/worker/queue.py`](app/worker/queue.py) | asyncio-воркер у процесі FastAPI, стан у SQLite (WAL), ідемпотентність на рівні сегмента, скасування |

Нюанси, які варто знати, перш ніж читати код:

* Воркер пише WAV-сегменти в `data/renders/segments/job{id}_block{bid}_seg{n}.wav`
  і **пропускає вже наявні** — перезапуск процесу продовжує роботу, а не
  починає заново (ADR-007).
* Пауза після кожного сегмента береться з його `ProsodyPlan`
  (`pause_after_ms`), а на межі абзацу множиться на 1.5 — саме це чути як
  структуру тексту. `pause_ms=400` лишається запасним значенням.
* Максимальна довжина сегмента береться з `engine.capabilities().max_chars`
  (у `openai_compat` це 500), а не з `SEGMENT_MAX_CHARS`.
* `POST /preview` завжди синтезує через `openai_compat` (поле `engine_id` у
  запиті приймається, але не використовується) і передає рушію лише `speed` —
  без зсуву тону.
* Формати результату задаються при створенні завдання (`options.formats`:
  `mp3` / `wav` / `m4b`, плюс `chapter_split` для zip) і створюються однією
  збіркою. `GET /jobs/{id}/download?format=` віддає один із **реально
  створених** файлів, а незамовлений формат дає `404` зі списком наявних
  (знахідка 16.3 закрита).

---

## 6. Емоції (шар виразу)

`GET /api/v1/emotions` — джерело правди — таблиця в
`app/services/expression/profiles.py` (ADR-004). Значення нижче — відхилення від
`neutral` при `intensity=1.0`; `intensity ∈ [0,1]` масштабує їх лінійно.

| Емоція | `speed` | тон, півтони | гучність, dB | пауза після, ms |
|---|---|---|---|---|
| `neutral` | 1.00 | 0.0 | 0 | 400 |
| `warm` | 0.96 | −0.5 | 0 | 500 |
| `serious` | 0.94 | −1.0 | −1 | 650 |
| `joyful` | 1.08 | +1.5 | +1 | 350 |
| `excited` | 1.15 | +2.0 | +2 | 250 |
| `sad` | 0.88 | −1.5 | −2 | 900 |
| `tense` | 1.05 | +1.0 | 0 | 180 |
| `questioning` | 1.02 | +1.0 | 0 | 450 |

Це **керована просодія, а не акторська гра моделі**: у MVP рушій (Piper) емоцій
не вміє, тому вираз дає темп (нативний `length_scale`), паузи та вирівнювання
гучності. Зсув тону типово вимкнено — він найлегше звучить штучно.

Приклад: `joyful` з `intensity=0.7` → `speed = 1.0 + (1.08 − 1.0)·0.7 = 1.056`,
пауза після — 365 ms.

Якщо рушій колись оголосить `emotions: true` (ZONOS2, Chatterbox), нативний
шлях має перекривати відповідні параметри шару виразу; решта (паузи, LUFS,
структура) працює завжди.

---

## 7. Рушії та голоси

### Каталог рушіїв

`GET /api/v1/engines` повертає чотири записи; `available` показує, чи є адаптер:

| `id` | `available` | Capabilities | Коментар |
|---|---|---|---|
| `openai_compat` | **true** | `speed`, `streaming`, `max_chars=500`, 22.05 kHz | єдиний реалізований: будь-який `/v1/audio/speech` (Speaches, Kokoro-FastAPI, `zonos2-server`, хмарний API) |
| `ukrainian_tts` | false | `speed`, 400 символів | in-process ESPNet/ONNX, MIT, автоматичний наголос — етап 3 |
| `zonos2` | false | `emotions`, `voice_cloning`, `streaming`, 44.1 kHz | єдина відома відкрита модель «вираз + українська + клонування» (ADR-011) |
| `chatterbox` | false | `emotions`, `voice_cloning` | 23 мови, **української немає** — рушій для англійських текстів |

Оголосити рушій і реалізувати його — різні речі: `available: false` означає
«в каталозі є, адаптера немає».

### Що реально є в Speaches

Із пʼяти голосів, перелічених у каталозі `openai_compat`, у реєстрі Speaches є
лише два українські Piper-пакети (перевірено `GET /v1/registry`):

| Модель у шлюзі | `voice` | Частота |
|---|---|---|
| `speaches-ai/piper-uk_UA-lada-x_low` | `lada` | 16 kHz |
| `speaches-ai/piper-uk_UA-ukrainian_tts-medium` | `ukrainian_tts` | 22.05 kHz |

`uk_UA-tetiana-high`, `uk_UA-mykyta-high`, `uk_UA-oleksa-high` у реєстрі
**відсутні** (`GET /v1/models/speaches-ai/piper-uk_UA-tetiana-high` → 404), хоча
саме `uk_UA-tetiana-high` стоїть у `DEFAULT_VOICE`. Або беріть голос із таблиці
вище, або качайте ці моделі напряму через `piper.download_voices` (для
`--extra piper` і бенчмарку) і піднімайте власний шлюз.

`voice` у запиті можна передавати і коротким (`lada`), і повним
(`uk_UA-lada-x_low`) — обидві форми шлюз приймає (перевірено).

> **Пастка, перевірена 27.09.** `POST /v1/audio/speech` із **чужим** імʼям
> голосу **не повертає помилку**: запит із `voice: uk_UA-tetiana-high` до
> моделі `piper-uk_UA-lada-x_low` дає `200` і звук голосом `lada`. Тобто 404
> вище — це про ендпоінт *моделі*, а не про синтез. Наслідок для користувача:
> можна вибрати «інший голос» і не почути різниці, вирішивши, що застосунок
> зламався.
>
> Тому застосунок показує це прямо:
> * `GET /api/v1/health/tts` віддає `model_voices` — голоси, які **справді** є
>   в моделі (`["lada"]` на цій машині);
> * сторінка «Голоси» позначає кожен голос чипом «встановлено» / «немає в
>   шлюзі» і пише, яким голосом він прозвучить;
> * прев'ю блоку й прослуховування голосу попереджають, якщо звучить не той
>   голос, який вибрано.
>
> Щоб отримати інші українські голоси — встановіть відповідні моделі в
> Speaches: `POST /v1/models/speaches-ai/piper-uk_UA-tetiana-high`.

Реєстр пропонує 750 записів, із них 156 — TTS, але **українських серед них лише
два** (ті самі). Kokoro (24 kHz, багатомовний) української не має — його голоси
це en-us, en-gb, ja, zh, es, fr, hi, it, pt-br. Список:

```bash
curl -fsS http://127.0.0.1:8001/v1/registry | .venv/bin/python -c "
import sys, json
data = json.load(sys.stdin)['data']
tts = [m for m in data if m.get('task') == 'text-to-speech']
print(len(tts), 'TTS-моделей; українські:')
for m in tts:
    if 'uk' in str(m.get('language')).lower():
        print(' ', m['id'], [v['id'] for v in m.get('voices', [])])
"
```

### Два YAML-файли як точка контролю без правки коду

* [`tts-stack/voice_map.yaml`](tts-stack/voice_map.yaml) — псевдоніми голосів
  (`alloy` → `uk_UA-tetiana-high`, …) і типові параметри Piper
  (`length_scale`, `noise_scale`, `noise_w`). Мапиться у контейнер Speaches
  (`/app/voice_map.yaml`).
* [`tts-stack/pre_process_map.yaml`](tts-stack/pre_process_map.yaml) — таблиця
  `regex → заміна` для вимови. **Читає сам застосунок**
  (`app/services/normalize/uk.py`), правила застосовуються після вбудованих,
  зверху вниз. Сюди ж вписують наголоси для слів, які Piper читає неправильно.

---

## 8. Дані на диску

```
data/
├── voice_studio.sqlite3      # метадані: documents · blocks · segments · jobs · voices · presets
├── uploads/                  # завантажені файли як є
├── renders/
│   ├── segments/             # WAV кожного сегмента (ідемпотентність)
│   ├── preview/              # WAV прев'ю з POST /api/v1/preview
│   └── job_<id>.mp3          # готові збірки
├── voices/                   # семпли голосів (для клонування — етап 3)
├── models/
│   ├── piper/                # моделі Piper для локального рушія/бенчмарку
│   └── hf/                   # кеш HuggingFace, змонтований у контейнер Speaches
└── benchmark/                # звіти scripts/benchmark.py
```

`data/` у `.gitignore` — метадані й аудіо не потрапляють у git.

Моделі тримайте в `data/models/hf` **на своєму імені користувача** (uid 1000 —
той самий, що в контейнера). Якщо каталог створив Docker від `root`, шлюз не
зможе завантажити модель — див. [§12](#12-типові-проблеми).

---

## 9. Розробка

### Структура

```
voice-studio/
├── app/
│   ├── main.py            # FastAPI, lifespan, воркер, health/emotions
│   ├── config.py          # pydantic-settings
│   ├── db.py              # SQLite + WAL, create_all при старті
│   ├── models.py          # SQLModel: Document · Block · Segment · Job · Voice · Preset
│   ├── api/               # documents · engines · jobs · preview
│   ├── services/          # extraction · normalize · segment · expression · tts · audio
│   ├── worker/queue.py    # черга, воркер, SSE-брокер
│   ├── templates/         # порожньо: UI ще не написано
│   └── static/            # порожньо
├── scripts/benchmark.py   # RTF/RAM для рушіїв
├── tts-stack/             # voice_map.yaml · pre_process_map.yaml · README про Speaches і ZONOS2
├── docker/                # Dockerfile.app · docker-compose.yml
├── tests/test_smoke.py    # 5 димових тестів: health, каталог рушіїв, емоції
└── docs/                  # RESEARCH · PLAN · ARCHITECTURE · ENVIRONMENT · DECISIONS · FRONTEND
                           #   FRONTEND.md — концепція UI; frontend-mockup.html — статичний макет
```

### Як додати новий рушій

1. Реалізуйте `TTSEngine` (`app/services/tts/base.py`): `id`,
   `capabilities()`, `voices()`, `synthesize(SynthRequest) -> Path`.
   Єдине, що специфічне для моделі, живе в `services/tts/` — решта застосунку
   знає лише про інтерфейс (ADR-001).
2. Внесіть рушій у `_ENGINE_CATALOGUE` (`app/api/engines.py`) з `available: true`
   і чесними `capabilities`. Якщо модель уміє емоції — `emotions: true`, і шар
   виразу має поступитися їй.
3. Якщо стек рушія конфліктує з поточним середовищем (`uk` vs `expressive`),
   піднімайте його окремим сервером і підключайте через `openai_compat`, а не
   імпортом (ADR-012).

### Тести

5 димових тестів перевіряють контракт, на який спираються Docker і майбутній
фронтенд: health, форму каталогу рушіїв (кожен зобов'язаний оголосити
`capabilities`), наявність українських голосів, факт «Chatterbox уміє емоції,
але не має української» і «ZONOS2 — кандидат на вираз + українську».
Поведінка синтезу в тестах не перевіряється: для цього потрібен живий шлюз.

---

## 10. Ліцензійна гігієна

Моделі в цьому проєкті мають **різні** ліцензії, частина — некомерційні
(Coqui Public Model License для XTTS-v2, CC-BY-NC для окремих чекпойнтів
F5-TTS). ZONOS2 — Apache-2.0. Перед комерційним використанням звіряйтеся з
[`docs/RESEARCH.md`](docs/RESEARCH.md), розділ «Ліцензії».

---

## 11. Відомі проблеми й обмеження

### ✅ Блокер 1 (закрито) — `InvalidRequestError` на будь-якому запиті до БД

```
sqlalchemy.exc.InvalidRequestError: When initializing mapper Mapper[Document(documents)],
expression "relationship("list['Block']")" seems to be using a generic class as the
argument to relationship(); please state the generic argument using an annotation,
e.g. "blocks: Mapped[list['Block']] = relationship()"
```

**Причина.** `app/models.py` починався з `from __future__ import annotations`,
тож усі анотації ставали рядками, і SQLModel 0.0.47 (закріплений у `uv.lock`)
передавав у `relationship()` буквальний рядок `list['Block']` замість класу.

**Виправлення (застосовано).** `from __future__ import annotations` прибрано з
`app/models.py`; синтаксис `int | None` у Python 3.11 працює й без нього, а
`list["Block"]` резолвиться через реєстр SQLAlchemy. Варіант з
`Mapped[list["Block"]]` **не** допомагає, доки лишається `from __future__ import
annotations`. У докстрінгу `models.py` стоїть попередження, щоб імпорт не
повернули випадково.

### ✅ Блокер 2 (закрито) — наївні `datetime` не записувались у SQLite

```
ValueError: Datetime values must have timezone information. Use datetime.now(timezone.utc),
or annotate the field with NaiveDatetime for naive storage.
```

**Причина.** `_now_utc()` повертав наївний час, а SQLModel 0.0.47 вимагає або
tz-aware значення, або явну анотацію `NaiveDatetime`.

**Виправлення (застосовано).** Поля часу в `app/models.py` анотовані
`NaiveDatetime` (тип із **pydantic**, не зі sqlmodel — у SQLModel 0.0.47 його
немає на верхньому рівні), а «зараз» створює єдина функція `utc_now()`.

### Інші обмеження

* **Веб-інтерфейс працює (фази F0–F3).** Доступне з браузера: завантаження
  файлу, редактор блоків із автозбереженням, емоції з прочитаною просодією,
  прев'ю блоку й голосу, запуск синтезу, черга зі SSE-прогресом, скасування й
  повтор, сегментний плеєр, завантаження результату, пресети, гарячі клавіші.
  Концепція — [`docs/FRONTEND.md`](docs/FRONTEND.md).
* **Зсув тону (DSP) вимкнено** — керована просодія консервативна за задумом
  (ADR-004). Емоція змінює темп, паузи й гучність, а не «гру» моделі.
  Паузи та гучність тепер справді застосовуються при склейці: пауза після
  кожного сегмента береться з його профілю (у `sad` — 900 мс, у `excited` —
  250 мс), а на межі абзацу множиться на 1.5. Доти емоція впливала лише на
  темп, бо `build_audio` брав одну паузу на все завдання.
* **Скан-PDF без текстового шару не читається.** Такий файл повертає `422` з
  підказкою про OCR. Евристика має два сигнали: «на сторінці менш ніж 20
  символів» і «на 3+ сторінках менш ніж 100 символів на сторінку» — короткий
  цифровий PDF при цьому читається нормально (це була помилка першої версії).
* ~~**M4B і розділи в аудіокнизі не реалізовано**~~ — **закрито**: M4B/AAC із
  мітками розділів кодує PyAV (колесо зі вбудованими бібліотеками FFmpeg,
  ADR-013), тож нативний `ffmpeg` більше не потрібен. Формат вибирається
  чекбоксами біля «Озвучити все», а `chapter_split` додає zip із файлом на
  кожен розділ.
* **Одна попередня вада воркера закрита:** синтез читав поля ORM-обʼєктів після
  закриття сесії й падав з `DetachedInstanceError`, лишаючи завдання вічно в
  `running`. Тепер робота йде зі знімками (`BlockTask`), а будь-яка помилка
  стає видимим `failed` із текстом причини — раніше завдання просто «зависало».
* **Повторний запуск завдання перебудовує рядки сегментів**, а не додає дублі;
  готові WAV на диску не синтезуються вдруге (ADR-007).
* **Завдання переживають перезапуск процесу:** при старті `lifespan` повертає
  незавершені завдання в чергу (`requeue_incomplete_jobs`). Раніше черга жила
  лише в памʼяті, і такі завдання не виконались би ніколи (знахідка 16.2).
* **Авторизації немає** — сервіси слухають лише `127.0.0.1`, і це свідома межа
  MVP, а не недогляд.
* **`GET /jobs/{id}/segments/{ord}/audio`** реалізовано: сегмент можна
  слухати, не чекаючи кінця завдання.
* **Пресети** мають повний CRUD-API (`/api/v1/presets`), створення з
  редактора й застосування емоції до всіх блоків.
* **GPU-профіль** не перевірено: у GeForce MX330 лише 2 GB VRAM, а `nvidia-smi`
  з-під пісочниці агента падає (`CapEff: 0`). Запускайте `nvidia-smi` у
  власному терміналі.

---

## 12. Типові проблеми

| Симптом | Причина | Що робити |
|---|---|---|
| `503 TTS-шлюз повернув 404: Model 'speaches-ai/Kokoro-82M-v1.0-ONNX' is not installed locally` | у запиті `model: tts-1`, а Speaches розуміє його як Kokoro, якого немає | задайте `TTS_MODEL=speaches-ai/piper-uk_UA-lada-x_low` (і в `.env`, і в `environment` сервісу `app`), перезапустіть `app` |
| `POST /v1/models/<id>` → 500, у логах `PermissionError: '/home/ubuntu/.cache/huggingface/hub/...'` | `data/models/hf` створив Docker від `root`, а контейнер пише як uid 1000 | перестворіть каталог від себе й перезапустіть контейнер **обовʼязково** (bind-mount тримає inode):<br>`docker compose -f docker/docker-compose.yml stop tts`<br>`rmdir data/models/hf && mkdir -p data/models/hf`<br>`docker compose -f docker/docker-compose.yml up -d tts` |
| Після перестворення каталогу шлюз пише `FileNotFoundError` на шлях кешу | контейнер лишився привʼязаний до видаленого inode | `docker compose -f docker/docker-compose.yml restart tts` |
| ~~`500 Internal Server Error` на `/documents` і `/jobs`~~ | блокери 1 і 2 закрито ([§11](#11-відомі-проблеми-й-обмеження)) | якщо 500 усе ще є — перевірте, що з `app/models.py` не повернули `from __future__ import annotations` |
| `error: Extras 'expressive' and 'uk' are incompatible with the declared conflicts` | так і задумано (ADR-012) | два середовища: `UV_PROJECT_ENVIRONMENT=.venv-expressive uv sync --extra expressive` |
| `415 Формат '.xyz' не підтримується` | розширення не в `ALLOWED_EXTENSIONS` | додайте його в `.env` (і переконайтесь, що екстрактор існує) |
| Порт 8000/8001 зайнятий | стек уже запущено, або інший сервіс | `ss -ltnp \| grep -E ':(8000\|8001)'`, далі інший порт для uvicorn |
| Синтез іде надто довго | CPU-only, 8 ядер — але виміряний RTF **0,068**, тобто 10-хв розділ ≈ 40 с | перевірте `scripts/benchmark.py --repeat 3` на своєму залізі; якщо справді повільно — зменшіть `SYNTH_CONCURRENCY`, слухайте блоки прев'ю, воркер відновлюється з місця зупинки |
| Вибрав голос, а звучить інакше / усі голоси однакові | шлюз **приймає будь-яке імʼя голосу** й озвучує тим, що встановлений (запит із `uk_UA-tetiana-high` до моделі `piper-uk_UA-lada-x_low` → 200 і голос `lada`) | сторінка «Голоси» показує, які голоси справді є (`GET /api/v1/health/tts` → `model_voices`); щоб отримати інші — встановіть відповідні моделі: `POST /v1/models/speaches-ai/piper-uk_UA-tetiana-high` |
| `PermissionError: ... data/renders/preview/preview_*.wav` | теку створив **root** (наприклад, контейнер або запуск із `sudo`), і користувач не може в неї писати | видаліть і створіть від себе — потрібен лише запис у батьківській теці:<br>`rmdir data/renders/preview && mkdir -p data/renders/preview`<br>Те саме лікує `data/models/hf`, якщо він від root. Перевірка: `find data -type d -user root` |
| Місце на диску | моделі важать сотні MB – GB | `curl -X DELETE http://127.0.0.1:8001/v1/models/<id>`, `du -sh data/models/hf`, винесіть `data/models` на інший носій через `VOICE_STUDIO_MODEL_DIR` |

---

## 13. Що перевірено, а що ні

**Перевірено на цій машині** (Docker-стек піднято, контейнери `healthy`):

* `pytest tests/ -q` → **219 passed**; `ruff check app/ tests/` → **0 зауважень**
  (правила `B008` і `RUF001–003` вимкнено в `pyproject.toml` з поясненням:
  перше — канонічний для FastAPI `Depends()`, друге — кирилиця в коментарях).
* `GET /health`, `/engines`, `/engines/{id}/voices`, `/emotions` → 200;
  невідомий рушій → 404; `.exe` на завантаженні → 415; `.pdf` → 422 з підказкою.
* `POST /v1/models/speaches-ai/piper-uk_UA-lada-x_low` → 200, «downloaded»
  (≈4 с), після чого `POST /v1/audio/speech` віддає WAV 24 kHz.
* Синтез **через адаптер застосунку** (`openai_compat`) з `TTS_MODEL` — реальний
  WAV 1.95 с на 24 kHz, темп узято з профілю `joyful`/`0.7` (`speed = 1.056`).
* `uk_UA-tetiana-high`, `uk_UA-mykyta-high`, `uk_UA-oleksa-high` у реєстрі
  Speaches **відсутні** (404); доступні українські — `lada` і `ukrainian_tts`.
* **Повний цикл «файл → правка → MP3» пройдено через інтерфейс** (27.09):
  завантаження `.md` → 3 блоки → завдання з редактора → `done` за ~3 с →
  `job_1.mp3` 132 КБ (кадри MPEG, не порожній файл) → аудіо сегментів 200
  `audio/wav` → завантаження 200 `audio/mpeg`.
* Перевірено в браузері: `/`, `/documents/{id}`, `/jobs`, `/jobs/{id}`,
  `/voices`, `/settings`, усі фрагменти `/ui/*` і вся статика → 200.
* **Виміряно продуктивність** (27.09, `scripts/benchmark.py --repeat 3`):
  синтез 362 символів → 16,8 с аудіо за 1,12 с (RTF 0,068); увесь конвеєр із
  LUFS і MP3 → RTF 0,13, з них постобробка 12–14 % часу. Пікова RAM процесу
  застосунку — 36 MB (модель живе в контейнері).
* **Повний цикл «PDF → MP3» пройдено** (27.09): PDF із заголовком і двома
  абзацами → 3 блоки (заголовок визначено за розміром шрифту) → завдання
  `done` → `job_1.mp3` 212 КБ → 5 сегментів із нормалізованим текстом.
* **Повний цикл «книга → M4B із розділами» пройдено** (28.09, живий
  TTS-шлюз, не заглушка): `.md` із заголовком і трьома `#`-розділами →
  4 блоки → завдання з `formats=["mp3","m4b"]` і `chapter_split=true` →
  `done` за 2,4 с → три файли: `job_1.mp3` 372 КБ, `job_1.m4b` 145 КБ,
  `job_1.zip` 256 КБ. У M4B — **чотири мітки розділів** («Вступ»,
  «1. Перший розділ», «2. Другий розділ», «3. Третій розділ») з межами
  0 → 3693 → 9336 → 14723 → 18901 мс, тобто рівно там, де вони звучать;
  метадані `©nam` / `©ART` / `©alb` / `©gen` читаються `mutagen`, потік —
  AAC mono 24 кГц. У zip — чотири файли `00-Вступ.mp3` …
  `03-Третій-розділ.mp3`, кожен зі своїми ID3-тегами (`TRCK=2/4`, назва
  розділу, альбом). Незамовлений формат дає `404` зі списком наявних, а не
  мовчазну підміну; контейнер `voice-studio-app` перезібрано з `av` 18.1.0.
* Витяг перевірено на **справжніх** файлах усіх форматів: PDF (текст,
  заголовки, переноси, скан, пошкоджений файл), DOCX, EPUB, HTML — 23 тести.

**Не перевірено:**

* ~~`scripts/benchmark.py` жодного разу не запускався~~ — **запущено 27.09**,
  числа внесено в [§8 ARCHITECTURE](docs/ARCHITECTURE.md#8-продуктивність-що-очікувати-на-цій-машині)
  і [RESEARCH](docs/RESEARCH.md): RTF **0,068** для синтезу й **0,13** для
  всього конвеєра. Найважливіше: **10-хвилинний розділ на цьому CPU — це
  ~40 секунд**, а не «від одиниць хвилин до 20», як оцінювалось.
* `--extra piper`, `--extra uk`, `--extra expressive` у цьому `.venv` не
  встановлені: є лише базові залежності + `dev`.
* Реальна робота з книгою на 1000+ блоків: механізми є (пагінація по 200,
  серверні фільтри), але на такому обсязі не прогнано.
* GPU-профіль і ZONOS2 (якість української та швидкість на CPU).
* Сумісність M4B із конкретними плейерами (Apple Books, Smart AudioBook
  Player): контейнер валідний і читається ffmpeg/mutagen, але на пристроях
  не перевірявся.

---

## 14. Куди далі

| Етап | Суть | Критерій готовності |
|---|---|---|
| **0** | звільнити диск, підняти шлюз, завантажити голоси | `curl` до шлюзу повертає український WAV, і його чути як українську |
| **1** | вертикальний зріз: `.txt`/`.md` → правка → MP3, мінімальний UI | завантажив `.md` на 10 абзаців, послухав абзац, отримав MP3 |
| **2** | PDF/DOCX/EPUB ✅, витяг + нормалізація ✅, шар виразу ✅, M4B із розділами ✅ | лишається перевірити на слух різницю «радісного» й «сумного» абзацу та прогнати книгу на 1000+ блоків |
| **3** | ZONOS2: чи добра українська і чи швидкий на CPU; решта адаптерів | обґрунтоване «так/ні» щодо ZONOS2, зафіксоване в `RESEARCH.md` |
| **4** | OCR, LLM-розмітка емоцій, клонування голосу, пакетний режим | за бажанням |

Найближчий практичний крок — **етап 3**: перевірити ZONOS2 (якість української
та швидкість на CPU) і тим закрити питання справжньої виразності. Збірка книги
— розділи, M4B, теги, zip — уже працює (див. [§13](#13-що-перевірено-а-що-ні)).

---

## 15. Карта документації

| Документ | Що всередині |
|---|---|
| [`docs/RESEARCH.md`](docs/RESEARCH.md) | дослідження: TTS-моделі, витяг тексту, аудіо, стек. Порівняльні таблиці, ліцензії, реальні обмеження |
| [`docs/PLAN.md`](docs/PLAN.md) | етапи 0–4, обсяг MVP, критерії готовності, ризики R1–R9, «що виміряти, а не вгадати» |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | архітектура, модель даних, HTTP/SSE API, структура репозиторію |
| [`docs/ENVIRONMENT.md`](docs/ENVIRONMENT.md) | що реально є на цій машині (GPU, диск, Python) і що з цього випливає |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | журнал рішень ADR-001…ADR-013: що обрано, що відкинуто і чому |
| [`tts-stack/README.md`](tts-stack/README.md) | Speaches + мапінг українських голосів, ZONOS2 на порту 1919, кеш моделей, перевірка GPU |
