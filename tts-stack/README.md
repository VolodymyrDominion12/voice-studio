# TTS-стек: Speaches + мапінг українських голосів

Ця тека — конфігурація OpenAI-сумісного TTS-шлюзу.

**Чому Speaches, а не `openedai-speech`.** Проєкт `openedai-speech` офіційно
застарів — у його README прямо написано *«This software is mostly obsolete and
will no longer be updated»*, останній реліз 0.18.2 від 16.08.2024. Speaches —
ідейний наступник: той самий OpenAI-сумісний контракт, але живі Piper + Kokoro,
динамічне завантаження моделей і підтримка CPU/GPU. Деталі — `docs/DECISIONS.md`,
ADR-005.

## Запуск

```bash
cd voice-studio
docker compose -f docker/docker-compose.yml up -d tts

curl -fsS http://127.0.0.1:8001/health && echo "  ← шлюз живий"
```

Перевірка синтезу:

```bash
curl -fsS http://127.0.0.1:8001/v1/audio/speech \
  -H 'Content-Type: application/json' \
  -d '{"model":"tts-1","input":"Привіт, це перевірка українського голосу.","voice":"uk_UA-tetiana-high","response_format":"wav"}' \
  --output /tmp/probe.wav

ffprobe /tmp/probe.wav 2>/dev/null || ls -la /tmp/probe.wav
```

## Другий сервер: ZONOS2 (для виразу)

Piper дає швидкість, але читає рівно. Для **виразу українською** є лише один
кандидат — ZONOS2. Він ставить **окремий** сервер на порту 1919, і це не
конфліктує зі Speaches на 8001.

```bash
# Самодостатня збірка під Linux x64 (CPU), без інсталятора й спільних бібліотек
# 1. Завантажити архів з releases → https://github.com/Zyphra/zonos2.cpp/releases
# 2. Розпакувати й запустити ЯВНО з q4_k:
./start-zonos2.sh --cpu --quant q4_k --no-browser
```

**Чому явно `--quant q4_k`.** Типовий перший запуск тягне **Q6_K + DAC +
speaker encoder ≈ 7.1 GB**. Для диска, де вільно 11 GB, це забагато. `q4_k` —
4.92 GB, і заявлено, що він звучить «within eval noise of F16».

**`ffmpeg` потрібен лише для клонування голосу** (декодування референсного
аудіо). Звичайний синтез без нього працює. Якщо `ffmpeg` уже є в `PATH`,
ZONOS2 його використає; інакше вкажіть `ZONOS2_FFMPEG=/path/to/ffmpeg`.

Перевірка, що OpenAI-сумісний ендпоінт живий:

```bash
curl -fsS http://127.0.0.1:1919/v1/audio/speech \
  -H 'Content-Type: application/json' \
  -d '{"model":"zonos2","input":"Привіт, це перевірка виразу.","voice":"default","response_format":"wav"}' \
  --output /tmp/zonos2.wav
```

Потім виміряйте швидкість — **це головне питання проєкту**:

```bash
uv run python scripts/benchmark.py --engine zonos2
```

> **Не робіть висновків, доки не почуєте.** Українська в ZONOS2 — Tier 3
> (найнижчий рівень), і якість саме української ніде не виміряна. README
> обіцяє реальний час **на GPU**, а не на CPU. Якщо українська звучить погано
> або 8 ядер не тягнуть — ZONOS2 викреслюється, і план не змінюється. Деталі:
> `docs/PLAN.md`, етап 3.1; `docs/DECISIONS.md`, ADR-011.

## Українські голоси Piper

Перевірено з каталогу `rhasspy/piper-voices` (`voices.json`):

| Голос | Якість | Розмір | Стать |
|---|---|---|---|
| `uk_UA-tetiana-high` | high | 114.2 MB | жіночий |
| `uk_UA-mykyta-high` | high | 114.2 MB | чоловічий |
| `uk_UA-oleksa-high` | high | 114.2 MB | чоловічий |
| `uk_UA-ukrainian_tts-medium` | medium | 76.7 MB | 3 спікери |
| `uk_UA-lada-x_low` | x_low | 20.6 MB | жіночий |

Разом ~440 MB. Рекомендація для MVP: почати з `uk_UA-tetiana-high` (жіночий) і
`uk_UA-mykyta-high` (чоловічий), далі порівняти з `lada` на швидкості.

Завантажити всі:

```bash
python -m piper.download_voices \
  uk_UA-tetiana-high uk_UA-mykyta-high uk_UA-oleksa-high uk_UA-lada-x_low \
  --data-dir ./data/models/piper
```

## Мапінг голосів

Ідея запозичена з `voice_to_speaker.yaml` у `openedai-speech`: імена голосів
OpenAI (`alloy`, `nova`, …) відображаються на конкретні моделі. Це дає змогу
застосунку надсилати звичні імена й не знати, який рушій за ними стоїть.

```yaml
# tts-stack/voice_map.yaml
# Ліва частина — ім'я, яке надсилає застосунок.
# Права — реальний голос і параметри просоді за замовчуванням.
voices:
  # OpenAI-сумісні псевдоніми → українські голоси
  alloy:  { voice: uk_UA-tetiana-high,          speed: 1.00, note: "жіночий, нейтральний" }
  nova:   { voice: uk_UA-lada-x_low,            speed: 1.05, note: "жіночий, швидший" }
  onyx:   { voice: uk_UA-mykyta-high,           speed: 0.95, note: "чоловічий, глибший" }
  echo:   { voice: uk_UA-oleksa-high,           speed: 1.00, note: "чоловічий" }
  fable:  { voice: uk_UA-ukrainian_tts-medium,  speed: 1.00, note: "3 спікери" }

  # Прямі імена — для явного вибору в UI
  uk-tetiana: { voice: uk_UA-tetiana-high }
  uk-mykyta:  { voice: uk_UA-mykyta-high }
  uk-oleksa:  { voice: uk_UA-oleksa-high }
  uk-lada:    { voice: uk_UA-lada-x_low }
```

## Виправлення вимови

Другий корисний патерн з `openedai-speech` — `pre_process_map.yaml`: таблиця
«regex → заміна» для слів, які рушій читає неправильно. Той самий механізм
варто мати в застосунку (див. `docs/ARCHITECTURE.md`, розділ 2.2), бо він дає
користувачу точку контролю без правки коду.

```yaml
# tts-stack/pre_process_map.yaml
- pattern: '\bIT\b'          # латиниця читається як літери
  replace: 'ай-ті'
- pattern: '\bAPI\b'
  replace: 'ей-пі-ай'
- pattern: '\bPDF\b'
  replace: 'пі-ді-еф'
- pattern: '(\d+)\s*%'
  replace: '\1 відсотків'
```

## Кеш моделей

Том `../data/models/hf` монтується в кеш HuggingFace контейнера. Це принципово:
диск на цій машині заповнений на 96 %, і перекачувати моделі після кожного
перезапуску контейнера неприпустимо.

Якщо місця критично мало — винести `data/models` на зовнішній носій і вказати
шлях у `.env` (`VOICE_STUDIO_MODEL_DIR`).

## Перевірка стану GPU

Перед спробою GPU-профілю:

```bash
nvidia-smi        # запускати у ВЛАСНОМУ терміналі, не через агента
docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi
```

На цій машині `nvidia-smi`, запущений із-під пісочниці агента, падає з
`Failed to initialize NVML: Unknown Error` — але причина в знятих capabilities
оболонки (`CapEff: 0`), а не обов'язково в залізі. Драйвер 580.178.04
встановлено, пристрої `/dev/nvidia*` присутні.

**Навіть якщо GPU працює:** у GeForce MX330 (GP108M) лише **2 GB VRAM**. Цього
недостатньо для XTTS-v2 (~4 GB за документацією `openedai-speech`) і для
важких виразних моделей. CPU-first — не тимчасове рішення, а основний режим
для цієї машини.
