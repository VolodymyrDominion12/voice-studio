# Архітектура

## 1. Огляд

Один процес FastAPI + один допоміжний Docker-контейнер із TTS-сервером.
Стан — SQLite і файли на диску. Фронтенд — серверний рендеринг.

```
┌───────────────────────────────────────────────────────────────┐
│ Браузер                                                       │
│   Завантаження │ Редактор блоків │ Голоси/емоції │ Черга      │
│   (Jinja2 + htmx + Alpine.js, без кроку збірки)               │
└──────────────────────────┬────────────────────────────────────┘
                           │  HTTP  +  SSE (прогрес)
┌──────────────────────────▼────────────────────────────────────┐
│ FastAPI (один процес)                                         │
│                                                               │
│  api/          documents · blocks · engines · preview · jobs  │
│  services/                                                    │
│    extraction/  файл ──────────────► [Block]                  │
│    normalize/   [Block] ───────────► текст, готовий до TTS    │
│    segment/     текст ─────────────► [Segment]                │
│    expression/  [Segment] ─────────► + емоція → параметри     │
│    tts/         [Segment] ─────────► WAV  (адаптер рушія)     │
│    audio/       [WAV] ─────────────► склейка · LUFS · MP3/M4B │
│                                                               │
│  worker/        asyncio-воркер + черга в SQLite + SSE         │
│  store/         SQLite (метадані) + data/ (файли)             │
└───────┬───────────────────────────────────┬───────────────────┘
        │ in-process                        │ HTTP /v1/audio/speech
┌───────▼─────────────────┐   ┌─────────────▼───────────────────┐
│ Прямі адаптери          │   │ OpenAI-сумісний шлюз (Docker)   │
│  • ukrainian-tts (MIT)  │   │  • Speaches: piper + kokoro     │
│  • piper-tts            │   │  • (резерв) openedai-speech     │
│  • chatterbox (ет. 3)   │   │  • хмарний API (опційно)        │
└─────────────────────────┘   └─────────────────────────────────┘
```

**Ключова властивість:** усе, що специфічне для конкретної моделі, живе в
`services/tts/`. Решта застосунку знає лише про інтерфейс `TTSEngine` і про
`EngineCapabilities`.

## 2. Конвеєр обробки

Кожен етап можна запускати окремо — це принципово, бо користувач має бачити
й правити текст **до** синтезу.

### 2.1 Витяг (`services/extraction/`)

Вхід: файл. Вихід: `list[Block]`.

```python
class BlockKind(StrEnum):
    HEADING = "heading"; PARAGRAPH = "paragraph"; QUOTE = "quote"
    LIST_ITEM = "list_item"; TABLE = "table"; CODE = "code"; FOOTNOTE = "footnote"

@dataclass
class Block:
    ordinal: int
    kind: BlockKind
    text: str
    heading_level: int | None = None
    speak: bool = True        # код/таблиці типово не озвучуємо
```

Маршрутизація за розширенням:

| Розширення | Екстрактор | Причина |
|---|---|---|
| `.txt` | прямий читач (детект кодування: UTF-8 → CP1251 → latin-1) | тривіально |
| `.md` | `markdown-it-py` → блоки | зберігає структуру заголовків |
| `.pdf` | `PyMuPDF` | швидко й точно на цифрових PDF |
| `.docx`, `.html`, `.epub` | `markitdown` | одна залежність на все інше |
| скан-PDF | OCR (етап 4) | у MVP лише повідомлення користувачу |

Правило: якщо з PDF витягнуто менше ніж ~100 символів на сторінку, це скан —
повертаємо явну помилку з підказкою, а не сміттєвий текст.

### 2.2 Нормалізація (`services/normalize/`)

Вхід: `Block.text`. Вихід: текст, який рушій вимовить правильно.
Це **найдешевший спосіб підняти якість** — дешевший за будь-яку зміну моделі.

Правила (українська як основна мова):
- цифри → слова: роки, кількості, десяткові дроби, діапазони (`num2words` з `lang="uk"`);
- скорочення й абревіатури → розгортання (`тобто`, `наприклад`, `і таке інше`);
- символи → слова: `%` → «відсотків», `№` → «номер», `§`, `⇢`, `→`;
- латиниця/acronymи → українська вимова (`IT` → «ай-ті») — словник винятків;
- прибирання розмітки: посилання → текст, зображення, зноски, HTML-теги;
- типографіка: `«»`, тире `—` як маркер діалогу → пауза, а не вимовляння;
- згортання повторних пробілів і переносів.

Нормалізатор **не мутує** вихідний текст: результат зберігається в
`blocks.text_normalized`, а `blocks.text_edited` — те, що правив користувач.
У редакторі видно обидві версії й різницю.

### 2.3 Сегментація (`services/segment/`)

Вхід: нормалізований текст блоку. Вихід: `list[Segment]` — одиниці синтезу.

- ділення за реченнями (`. ! ? …`), далі за потреби — за `;` і `,`,
  якщо сегмент довший за ліміт;
- ліміт довжини залежить від рушія (`TTSEngine.max_chars()`): Piper — 500+,
  XTTS-подібні — ~200–250, бо довгі речення втрачають просодію;
- збереження приналежності до блоку: пауза між абзацами довша, ніж між
  реченнями;
- жодних розривів усередині чисел, ініціалів, скорочень (`тобто`, `2007 р.`).

Українські речення пунктуаційно регулярні, тому власний rule-based сплітер
надійніший за загальні бібліотеки (`pysbd` не оновлювався з 2021 р.).

### 2.4 Шар виразу (`services/expression/`)

Це серце проєкту (див. ADR-004).

```python
class Emotion(StrEnum):
    NEUTRAL="neutral"; WARM="warm"; SERIOUS="serious"; JOYFUL="joyful"
    SAD="sad"; TENSE="tense"; EXCITED="excited"; QUESTIONING="questioning"

@dataclass
class ProsodyPlan:
    speed: float              # множник темпу
    pitch_semitones: float    # зсув тону (DSP, опційно)
    energy_db: float          # корекція гучності сегмента
    pause_before_ms: int
    pause_after_ms: int
    engine_hints: dict        # length_scale / noise_scale / noise_w / exaggeration…
```

Порядок застосування — **від найменш руйнівного**:

1. **Параметри рушія** (без артефактів). Piper: `length_scale` задає темп
   нативно, `noise_scale`/`noise_w` — варіативність/виразність. Chatterbox:
   `exaggeration`, `cfg_weight`. XTTS-подібні: `temperature`.
2. **Паузи** — вставка тиші між сегментами при склейці. Безкоштовно, дуже
   помітно на слух.
3. **Гучність сегмента** — корекція в дБ за профілем емоції.
4. **Тон і темп DSP** — лише якщо рушій не вміє темпу нативно, і лише в
   консервативних межах (±2 півтони). Типово **вимкнено**.

Профілі — одна декларативна таблиця, яку легко тюнінгувати:

| Емоція | speed | pitch | energy | пауза після |
|---|---|---|---|---|
| neutral | 1.00 | 0 | 0 dB | 400 ms |
| warm | 0.96 | −0.5 | 0 dB | 500 ms |
| serious | 0.94 | −1.0 | −1 dB | 650 ms |
| joyful | 1.08 | +1.5 | +1 dB | 350 ms |
| excited | 1.15 | +2.0 | +2 dB | 250 ms |
| sad | 0.88 | −1.5 | −2 dB | 900 ms |
| tense | 1.05 | +1.0 | 0 dB | 180 ms |
| questioning | 1.02 | +1.0 | 0 dB | 450 ms |

`intensity ∈ [0,1]` масштабує відхилення від `neutral`.

Розмітка емоцій — двоє джерела:
- **вручну** — дропдаун на кожному блоці в редакторі (основний шлях у MVP);
- **автоматично** — опційно, LLM розмічає блоки за змістом (етап 4).
  Застосунок **не залежить** від LLM: без нього все працює.

### 2.5 Синтез (`services/tts/`)

```python
class EngineCapabilities(BaseModel):
    emotions: bool = False        # чи вміє емоції нативно
    emotion_tags: bool = False    # чи приймає теги в тексті
    voice_cloning: bool = False
    speed: bool = True
    streaming: bool = False
    max_chars: int = 400
    sample_rate: int = 22050

class TTSEngine(Protocol):
    id: str
    def capabilities(self) -> EngineCapabilities: ...
    def voices(self) -> list[Voice]: ...
    def synthesize(self, req: SynthRequest) -> Path: ...   # → WAV на диску
```

Адаптери:
- `ukrainian_tts.py` — прямий in-process, ESPNET/ONNX, MIT, автоматичний наголос.
- `piper.py` — прямий in-process або через ONNX Runtime.
- `openai_compat.py` — будь-який `/v1/audio/speech`: Speaches, Kokoro-FastAPI,
  `zonos2-server`, хмарний API. Один адаптер закриває всіх.
- `zonos2.py` (ет. 3) — типово через `openai_compat`, бо `zonos2-server` дає
  OpenAI-сумісний ендпоінт; окремий адаптер лише якщо контракт відрізняється.
  Єдина модель із `emotions=True` **і** `voice_cloning=True` **і** українською
  (ADR-011) — саме тому `capabilities()` тут не формальність.
- `chatterbox.py` (ет. 3) — емоції нативно, англійська.

### 2.6 Пост-обробка та збірка (`services/audio/`)

Базовий цикл **без зовнішніх бінарників** (ADR-006):
`soundfile` (WAV/FLAC/OGG) + `lameenc` (MP3) + `pyloudnorm` (EBU R128).

Ланцюг:
1. кожен сегмент → WAV 24 kHz mono (цільова частота), обрізання тиші з країв;
2. корекція гучності сегмента за профілем емоції;
3. склейка з паузами за профілем і межами абзаців;
4. **глобальна** нормалізація гучності до −18 LUFS (рівень аудіокниги;
   −16 LUFS для подкастів) — саме глобальна, щоб не «сплющити» динаміку;
5. експорт: WAV / MP3 (через `lameenc`, з тегами `mutagen`);
6. M4B з розділами — етап 2, потребує ffmpeg (нативний, не snap).

## 3. Модель даних (SQLite)

```sql
documents(id, filename, sha256, mime, source_path, page_count, status,
          created_at, updated_at)
blocks(id, document_id→documents, ordinal, kind, heading_level,
       text_raw, text_normalized, text_edited, speak, emotion, intensity,
       UNIQUE(document_id, ordinal))
segments(id, job_id→jobs, block_id→blocks, ordinal, text,
         prosody_json, audio_path, duration_ms, status,
         UNIQUE(job_id, block_id, ordinal))
jobs(id, document_id→documents, engine_id, voice_id, status, progress,
     options_json, output_path, error, created_at, started_at, finished_at)
voices(id, engine_id, name, lang, gender, sample_path, meta_json)
presets(id, name, engine_id, voice_id, emotion, intensity, options_json)
```

`jobs.status`: `queued → running → done | failed | cancelled`.
`segments.status`: `pending → done | failed` — завдяки цьому синтез
**відновлюється з місця зупинки** після перезапуску процесу (ADR-007).

## 4. HTTP API

| Метод | Шлях | Призначення |
|---|---|---|
| `POST` | `/api/v1/documents` | завантажити файл → документ + блоки |
| `GET` | `/api/v1/documents` | список документів |
| `GET` | `/api/v1/documents/{id}` | документ із блоками |
| `PATCH` | `/api/v1/documents/{id}/blocks/{bid}` | правити текст/емоцію/`speak` |
| `POST` | `/api/v1/documents/{id}/normalize` | перезапустити нормалізацію |
| `GET` | `/api/v1/engines` | рушії та їхні `capabilities` |
| `GET` | `/api/v1/engines/{id}/voices` | голоси рушія |
| `POST` | `/api/v1/preview` | синтез одного блоку → `audio/wav` (швидкий) |
| `POST` | `/api/v1/jobs` | створити завдання синтезу |
| `GET` | `/api/v1/jobs` | черга |
| `GET` | `/api/v1/jobs/{id}/events` | **SSE**: прогрес, статус сегментів |
| `POST` | `/api/v1/jobs/{id}/cancel` | скасувати |
| `GET` | `/api/v1/jobs/{id}/segments/{ord}/audio` | прослухати сегмент |
| `GET` | `/api/v1/jobs/{id}/download?format=mp3\|wav\|m4b\|zip` | готовий результат |

Окремо `POST /api/v1/preview` — критично для UX: дає змогу підібрати голос і
емоцію за секунди, не запускаючи синтез цілої книги.

### Формат SSE

```
event: progress
data: {"job_id":7,"status":"running","done":42,"total":180,"percent":23.3,
       "current_block":11,"eta_seconds":320}

event: segment
data: {"job_id":7,"block_id":11,"ordinal":0,"duration_ms":4200,"status":"done"}

event: finished
data: {"job_id":7,"output_path":"...","duration_ms":998000}
```

## 5. Черга та воркер

- Один `asyncio`-воркер у процесі FastAPI, пул на `N` паралельних синтезів
  (`N` = 2 для CPU, бо моделі й так займають ядра).
- Стан — SQLite (WAL). Прогрес — у пам'яті + запис у БД на кожному сегменті.
- Скасування — `asyncio.CancelledError` + прапорець у БД.
- Ідемпотентність: перед синтезом перевіряється, чи є вже готовий WAV сегмента.
  Тому перезапуск процесу = продовження роботи, а не початок заново.
- Сегменти пишуться на диск одразу — RAM не тримає цілу книгу.

## 6. Структура репозиторію

```
voice-studio/
├── README.md
├── pyproject.toml                 # uv, Python 3.11
├── .env.example
├── docs/                          # RESEARCH / PLAN / ARCHITECTURE / ENVIRONMENT / DECISIONS
├── app/
│   ├── main.py                    # FastAPI, lifespan, монтування
│   ├── config.py                  # pydantic-settings
│   ├── db.py                      # SQLite, міграції
│   ├── models.py                  # SQLModel/pydantic-схеми
│   ├── api/                       # documents · blocks · engines · preview · jobs
│   ├── services/
│   │   ├── extraction/            # txt · markdown · pdf · markitdown
│   │   ├── normalize/             # правила для української
│   │   ├── segment/               # сплітер речень
│   │   ├── expression/            # Emotion, ProsodyPlan, profiles.py
│   │   ├── tts/                   # base.py + адаптери рушіїв
│   │   └── audio/                 # склейка · LUFS · mp3 · m4b
│   ├── worker/                    # черга, планувальник, SSE-брокер
│   ├── templates/                 # Jinja2
│   └── static/                    # css · js · htmx · alpine
├── tts-stack/                     # compose + config для Speaches
├── docker/docker-compose.yml
├── scripts/                       # завантаження моделей, бенчмарки
├── tests/
└── data/                          # gitignored: uploads · renders · voices · models
```

## 7. Розгортання

`docker/docker-compose.yml`:

```yaml
services:
  tts:
    image: ghcr.io/speaches-ai/speaches:latest-cpu
    ports: ["8001:8000"]
    volumes: ["../data/models:/home/ubuntu/.cache/huggingface/hub"]
    environment:
      - ENABLE_UI=false
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://localhost:8000/health"]
      interval: 15s
      retries: 5

  app:
    build: { context: .., dockerfile: docker/Dockerfile.app }
    ports: ["8000:8000"]
    volumes:
      - ../data:/app/data
      - ../app:/app/app          # dev: гаряче перезавантаження
    environment:
      - TTS_BASE_URL=http://tts:8000/v1
      - VOICE_STUDIO_MODEL_DIR=/app/data/models
    depends_on:
      tts: { condition: service_healthy }
```

CPU-профіль — типовий для цієї машини. GPU-профіль — той самий файл плюс
`deploy.resources.reservations.devices` і тег `latest-cuda`; вмикається, коли
з'явиться відповідне залізо.

## 8. Продуктивність: що очікувати на цій машині

| Рушій | Пристрій | Орієнтовно | Джерело оцінки |
|---|---|---|---|
| Piper (`medium`) | CPU, 8 ядер | швидше за реальний час у рази | Piper позиціюється як «very fast, runs on cpu» |
| `ukrainian-tts` (ONNX) | CPU | близько до реального часу | заявлено роботу навіть на мобільних |
| **ZONOS2** (`q4_k`, 7.6B MoE) | **CPU** | **невідомо — головне питання етапу 3** | README обіцяє реальний час лише **на GPU**; про CPU обіцянки немає |
| ZONOS2 (`q4_k`) | GPU | реальний час | README `zonos2.cpp` |
| XTTS-v2 | GPU | потребує ~4 GB VRAM | README openedai-speech |
| Chatterbox 0.5B | GPU | 2–3 GB VRAM мінімум | оцінка, **не перевірено** |

Числа для Piper, `ukrainian-tts` і ZONOS2 **треба виміряти** на цій машині —
скрипт `scripts/benchmark.py` створюється саме для цього. Наведені оцінки не є
вимірюваннями.

> **ZONOS2 — 7.6B MoE проти 82M у Kokoro.** Це на два порядки більше
> параметрів. Те, що він має CPU-збірку, **не означає**, що він на CPU швидкий:
> MoE активує лише частину експертів на токен, що допомагає, але 8 ядер — це не
> GPU. Real-time factor ZONOS2 на CPU лишається невиміряним і є ризиком R2
> плану. Саме тому він не в MVP.

Реалістичний висновок: **озвучення 10-хвилинного розділу на CPU займе від
одиниць хвилин до ~20 хвилин** залежно від рушія. Саме тому архітектура
сегментна, з прогресом, частковим прослуховуванням і відновленням — а не
«натиснути кнопку і чекати мовчки».

## 9. Межі MVP

**Входить:** txt/md/pdf, редактор блоків, нормалізація, 5 українських голосів,
8 емоцій, прев'ю блоку, черга з SSE, WAV/MP3, відновлення після перезапуску.

**Не входить:** клонування голосу (ADR-010), OCR, M4B, багатокористувацький
режим, хмарні API, авторизація.
