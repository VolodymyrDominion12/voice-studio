# ════════════════════════════════════════════════════════════════════════════
# Voice Studio — корисні команди проєкту.
#
#   make                 → список усіх команд (те саме, що `make help`)
#   make <команда>       → виконати
#   make <команда> VAR=… → перекрити будь-яку змінну нижче, напр.
#                          make dev PORT=9000
#                          make bench ARGS="--engine piper --repeat 3"
#
# Два навмисні рішення:
#
#   * Python-команди йдуть через інтерпретатор у `.venv`, а не через `uv run`:
#     `uv run` перед кожним запуском синхронізує середовище (а отже, може
#     піти в мережу), тоді як README та перевірки описують саме
#     `.venv/bin/python`. Хочете через uv — `make test PY="uv run python"`.
#
#   * порт розробки — 8010, бо 8000 тримає Docker-версія застосунку
#     (README §1, «Варіант B»).
# ════════════════════════════════════════════════════════════════════════════

SHELL := /bin/bash
.DEFAULT_GOAL := help

# ── Змінні (усі перекриваються з командного рядка) ───────────────────────────
VENV      ?= .venv
PY        ?= $(VENV)/bin/python
UV        ?= uv
HOST      ?= 127.0.0.1
# УВАГА: після значення не має бути пробілів перед `#` — GNU Make лишає їх
# усередині змінної, і `http://host:8001   /health` тихо перетворюється на 404.
PORT      ?= 8010
TTS_PORT  ?= 8001
DATA_DIR  ?= data
DB        ?= $(DATA_DIR)/voice_studio.sqlite3
APP_URL   ?= http://$(HOST):$(PORT)
API       ?= $(APP_URL)/api/v1
TTS       ?= http://$(HOST):$(TTS_PORT)
COMPOSE   ?= docker compose -f docker/docker-compose.yml
MODEL     ?= speaches-ai/piper-uk_UA-lada-x_low
VOICE     ?= uk_UA-lada-x_low
EMOTION   ?= warm
INTENSITY ?= 0.6
TEXT      ?= Привіт, це перевірка українського голосу.
ARGS      ?=
CONFIRM   ?=

.PHONY: help check-venv doctor \
        venv sync sync-piper sync-uk sync-expressive sync-dual \
        venv-expressive lock env python-version \
        run dev serve wait open shell config routes \
        test test-v test-file test-k lint lint-fix fmt fmt-check \
        typecheck typecheck-report check check-all \
        up up-gpu down ps logs logs-app logs-tts build restart-tts docker-shell \
        tts-health tts-models tts-registry tts-voices tts-ps \
        tts-model-add tts-model-rm tts-probe preview health \
        piper-voices bench \
        db-tables db-stats reset clean-cache clean \
        ports disk yaml-check docs open-docs open-tts

##@ Довідка та перевірка

help: ## Показати цей список команд
	@printf '\n\033[1mVoice Studio — команди\033[0m  (повний довідник: README.md §2)\n'
	@awk 'BEGIN {FS = ":.*##"} \
		/^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 5); next } \
		/^[a-zA-Z0-9_.-]+:.*##/ { printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2 }' \
		$(MAKEFILE_LIST)
	@printf '\n'

check-venv:
	@test -x $(PY) || { \
		echo "✗ Не знайдено $(PY)"; \
		echo "  Створіть середовище:  make venv && make sync"; \
		exit 1; }

doctor: ## Перевірити, що середовище взагалі готове до роботи
	@echo "── інструменти ──────────────────────────────"
	@command -v $(UV)   >/dev/null && echo "✓ uv      $$($(UV) --version)" || echo "✗ uv не встановлено"
	@command -v docker  >/dev/null && echo "✓ docker  $$(docker --version)"  || echo "✗ docker не встановлено"
	@command -v make    >/dev/null && echo "✓ make    $$(make --version | head -1)" || echo "✗ make не встановлено"
	@echo "── python ───────────────────────────────────"
	@test -x $(PY) \
		&& echo "✓ $(PY) → $$($(PY) --version)" \
		|| echo "✗ $(PY) немає (потрібен Python 3.11, ADR-002: make venv && make sync)"
	@echo "── проєкт ───────────────────────────────────"
	@test -f .env && echo "✓ .env на місці" || echo "✗ .env немає (make env)"
	@test -f data/voice_studio.sqlite3 && echo "✓ БД $(DB)" || echo "· БД ще не створена (з'явиться після першого запуску)"
	@echo "── TTS-шлюз $(TTS) ──"
	@curl -fsS --max-time 3 $(TTS)/health >/dev/null 2>&1 \
		&& echo "✓ шлюз живий (docker compose up -d tts)" \
		|| echo "· шлюз не відповідає — синтез дасть 503"

##@ Середовище й залежності

venv: ## Створити .venv на Python 3.11 (ADR-002)
	$(UV) venv --python 3.11

sync: ## Поставити базу + dev-інструменти (pytest/ruff/mypy) — щоденний режим
	$(UV) sync --extra dev

sync-piper: ## + piper-tts (MVP-рушій; працює і як окремий голос у шлюзі)
	$(UV) sync --extra dev --extra piper

sync-uk: ## + ukrainian-tts (in-process укр. синтез)
	$(UV) sync --extra dev --extra uk

sync-expressive: ## + chatterbox-tts (англійська, виразна)
	$(UV) sync --extra dev --extra expressive

sync-dual: ## Показати, ЧОМУ uk і expressive не сумісні (ADR-012) — команда навмисно падає
	@echo "Очікувана помилка uv — це і є документована межа (ADR-012):"
	@$(UV) sync --extra uk --extra expressive

venv-expressive: ## Друге середовище .venv-expressive для конфліктного рушія
	UV_PROJECT_ENVIRONMENT=.venv-expressive $(UV) sync --extra expressive
	@echo "✓ Готово. Запуск у ньому: UV_PROJECT_ENVIRONMENT=.venv-expressive make test"

lock: ## Перерахувати uv.lock
	$(UV) lock

env: ## Створити .env із .env.example (наявний не чіпає)
	@test -f .env && echo "· .env уже є — не змінюю" || { cp .env.example .env && echo "✓ .env створено. Не забудьте TTS_MODEL (README §1)"; }

python-version: check-venv ## Показати версію інтерпретатора проєкту
	@$(PY) --version

##@ Запуск

run: check-venv ## Запустити застосунок (те саме, що dev)
	@$(PY) -m uvicorn app.main:app --reload --host $(HOST) --port $(PORT)

dev: run ## Запустити з автоперезапуском (аліас run)

serve: check-venv ## Запустити без автоперезапуску
	@$(PY) -m uvicorn app.main:app --host $(HOST) --port $(PORT)

wait: ## Чекати, доки застосунок відповість на /health (до 30 с)
	@for i in $$(seq 1 30); do \
		curl -fsS $(API)/health >/dev/null 2>&1 && { echo "✓ застосунок живий на $(API)"; exit 0; }; \
		sleep 1; \
	done; \
	echo "✗ за $(API) ніхто не відповів за 30 с"; exit 1

open: ## Відкрити інтерфейс у браузері
	@xdg-open $(APP_URL)/ 2>/dev/null || echo "Відкрийте $(APP_URL)/"

shell: check-venv ## IPython у контексті проєкту
	@$(PY) -m IPython

config: check-venv ## Показати чинні налаштування (з .env, після підстановок)
	@$(PY) -c "from app.config import get_settings as g; s = g(); \
print('data_dir   ', s.data_dir); \
print('model_dir  ', s.model_dir); \
print('db         ', s.db_path); \
print('tts        ', s.tts_base_url); \
print('tts_model  ', s.tts_model); \
print('engine     ', s.default_engine); \
print('voice      ', s.default_voice); \
print('formats    ', s.output_format_list)"

routes: check-venv ## Список HTTP-маршрутів (з OpenAPI-схеми — джерело правди)
	@$(PY) -c 'from app.main import app; paths = app.openapi()["paths"]; [print(",".join(sorted(k.upper() for k in v)), p) for p, v in sorted(paths.items())]'

##@ Якість коду

test: check-venv ## Усі тести
	@$(PY) -m pytest tests/ -q

test-v: check-venv ## Усі тести, докладно
	@$(PY) -m pytest tests/ -v

test-file: check-venv ## Один файл: make test-file F=tests/test_smoke.py
	@test -n "$(F)" || { echo "✗ Вкажіть файл: make test-file F=tests/test_smoke.py"; exit 1; }
	@$(PY) -m pytest $(F) -q

test-k: check-venv ## Тести за виразом: make test-k K=audiobook
	@test -n "$(K)" || { echo "✗ Вкажіть вираз: make test-k K=audiobook"; exit 1; }
	@$(PY) -m pytest tests/ -q -k "$(K)"

lint: check-venv ## ruff check (0 зауважень — очікуваний стан)
	@$(PY) -m ruff check .

lint-fix: check-venv ## ruff check --fix
	@$(PY) -m ruff check . --fix

fmt: check-venv ## Форматування ruff
	@$(PY) -m ruff format .

fmt-check: check-venv ## Скільки файлів розходиться з `ruff format` (зараз 39 — код писався вручну)
	@$(PY) -m ruff format . --check 2>&1 | tail -1 || true
	@echo "· Це поточний стан проєкту, а не помилка. Вирівняти: make fmt"

typecheck: check-venv ## mypy (прапорець обов'язковий: в app/ немає __init__.py); впаде на відомих 29
	@$(PY) -m mypy --explicit-package-bases app

typecheck-report: check-venv ## Скільком зауваженням mypy відповідає код зараз (базлайн, не падає)
	@$(PY) -m mypy --explicit-package-bases app 2>&1 | tail -1 || true

# Два гейти навмисно розділені, бо два інструменти мають відомий, не нульовий
# базлайн на цьому коді:
#   * mypy — 29 зауважень у 6 файлах: слід SQLModel, який типізує
#     `Job.status.in_(…)` і `Job.created_at.desc()` як звичайні атрибути;
#   * `ruff format --check` — 39 файлів, бо код відформатований вручну, а
#     `ruff check` (єдине, що описано як «0 зауважень» у README §2) — зелений.
# Гейт, який червоний завжди, не гейт: тому `check` = лінт + тести (зелені),
# а типи й форматування живуть окремими командами й у звіті.
check: lint test ## Гейт: лінт + тести (обидва на цій машині зелені)
	@echo "✓ lint + test пройдено"

check-all: test typecheck-report fmt-check ## Тести + ЗВІТ типів і форматування (не гейт)
	@echo "✓ тести пройдено; типи й форматування — рядками вище (README §2)"

##@ Docker

up: ## Підняти CPU-стек (app + tts)
	$(COMPOSE) up -d

up-gpu: ## Підняти GPU-профіль шлюзу (перевірте nvidia-smi!)
	$(COMPOSE) --profile gpu up -d

down: ## Зупинити стек
	$(COMPOSE) down

ps: ## Стан контейнерів (усі мають бути healthy)
	$(COMPOSE) ps

logs: logs-app ## Логи застосунку (аліас logs-app)

logs-app: ## Логи контейнера app
	$(COMPOSE) logs -f app

logs-tts: ## Логи TTS-шлюзу
	$(COMPOSE) logs -f tts

build: ## Перезібрати образ застосунку
	$(COMPOSE) build app

restart-tts: ## Перезапустити лише шлюз
	$(COMPOSE) restart tts

docker-shell: ## Shell усередині контейнера app
	$(COMPOSE) exec app bash

##@ TTS-шлюз (Speaches)

health: ## Статус застосунку й шлюзу
	@echo "── застосунок $(API) ──"
	@curl -fsS --max-time 5 $(API)/health | $(PY) -m json.tool 2>/dev/null || echo "✗ застосунок не відповідає (make dev)"
	@echo "── шлюз $(TTS) ──"
	@curl -fsS --max-time 5 $(TTS)/health 2>/dev/null || echo "✗ шлюз не відповідає (docker compose up -d tts)"

tts-health: ## Шлюз: /health
	@curl -fsS $(TTS)/health && echo

tts-models: ## Шлюз: які моделі вже завантажено
	@curl -fsS $(TTS)/v1/models | $(PY) -m json.tool

tts-registry: ## Шлюз: доступні в реєстрі моделі (ukr. Piper-голоси)
	@curl -fsS $(TTS)/v1/registry | $(PY) -c "import sys, json; \
[print(m['id'], '|', m.get('sample_rate'), '|', [v['id'] for v in m.get('voices', [])]) \
 for m in json.load(sys.stdin)['data'] if 'piper' in m.get('id', '') and 'uk' in m.get('id', '').lower()]"

tts-voices: ## Шлюз: голоси завантажених моделей
	@curl -fsS $(TTS)/v1/audio/voices | $(PY) -m json.tool

tts-ps: ## Шлюз: що зараз тримається в памʼяті
	@curl -fsS $(TTS)/api/ps | $(PY) -m json.tool

tts-model-add: ## Завантажити модель у шлюз (MODEL=…, ~20 MB, ~4 с)
	@curl -fsS -X POST $(TTS)/v1/models/$(MODEL) && echo "✓ $(MODEL)"
	@echo "· Не забудьте TTS_MODEL=$(MODEL) у .env, інакше синтез дасть 503"

tts-model-rm: ## Видалити модель зі шлюзу й звільнити диск (MODEL=…)
	@curl -fsS -X DELETE $(TTS)/v1/models/$(MODEL) && echo "✓ $(MODEL) видалено"

tts-probe: ## Синтез напряму в шлюз, повз застосунок → /tmp/probe.wav
	@curl -fsS $(TTS)/v1/audio/speech \
		-H 'Content-Type: application/json' \
		-d '{"model":"$(MODEL)","input":"$(TEXT)","voice":"lada","response_format":"wav"}' \
		--output /tmp/probe.wav
	@file /tmp/probe.wav

preview: ## Синтез фрагмента через застосунок → /tmp/preview.wav
	@curl -fsS -X POST $(API)/preview \
		-H 'Content-Type: application/json' \
		-d '{"text":"$(TEXT)","voice_id":"$(VOICE)","emotion":"$(EMOTION)","intensity":$(INTENSITY)}' \
		--output /tmp/preview.wav
	@file /tmp/preview.wav

piper-voices: ## Завантажити українські Piper-голоси для --extra piper
	@$(PY) -m piper.download_voices \
		uk_UA-tetiana-high uk_UA-mykyta-high uk_UA-oleksa-high uk_UA-lada-x_low \
		--data-dir ./data/models/piper

##@ Бенчмарк

bench: check-venv ## Бенчмарк рушіїв (RTF, пікова RAM). ARGS="--engine piper --repeat 3"
	@$(PY) scripts/benchmark.py $(ARGS)
	@echo "· Результат: data/benchmark/benchmark.json"

##@ Дані, стан, обслуговування

db-tables: ## Таблиці в SQLite
	@$(PY) -c "import sqlite3; c = sqlite3.connect('$(DB)'); \
print([r[0] for r in c.execute(\"select name from sqlite_master where type='table'\")])"

db-stats: ## Кількість рядків у ключових таблицях
	@$(PY) -c "import sqlite3; c = sqlite3.connect('$(DB)'); \
[print(f'{t:12}', c.execute(f'select count(*) from {t}').fetchone()[0]) \
 for t in ('documents', 'blocks', 'segments', 'jobs')]"

reset: ## СКИНУТИ стан: БД + uploads + renders. Потрібно CONFIRM=yes
	@test "$(CONFIRM)" = "yes" || { \
		echo "Це видалить $(DB) (з -wal/-shm) і вміст $(DATA_DIR)/uploads, $(DATA_DIR)/renders."; \
		echo "Застосунок має бути зупинений. Повторіть:  make reset CONFIRM=yes"; \
		exit 1; }
	rm -f $(DB) $(DB)-wal $(DB)-shm
	rm -rf $(DATA_DIR)/renders/* $(DATA_DIR)/uploads/*
	@echo "✓ Стан скинуто — таблиці створяться при наступному старті"

clean-cache: ## Прибрати лише кеші (безпечно, БД не чіпає)
	rm -rf .pytest_cache .ruff_cache .mypy_cache
	find . -path ./.venv -prune -o -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@echo "✓ Кеші прибрано"

clean: clean-cache ## Те саме, що clean-cache (аліас)

ports: ## Хто займає порти 8000/8001/8002/8010
	@ss -ltnp 2>/dev/null | grep -E ':(8000|8001|8002|8010)\b' || echo "· ці порти вільні"

disk: ## Скільки займають дані, моделі, кеші
	@du -sh $(DATA_DIR) 2>/dev/null; du -sh $(DATA_DIR)/* 2>/dev/null | sort -h
	@du -sh $(DATA_DIR)/models/hf 2>/dev/null || true
	@df -h / | tail -1

yaml-check: ## Перевірити YAML-мапи голосів і нормалізації (tts-stack/)
	@$(PY) -c "import pathlib, yaml; \
[print('✓', p) for p in sorted(pathlib.Path('tts-stack').glob('*.yaml')) if yaml.safe_load(p.read_text()) is not None]"

##@ Документація

docs: ## Відкрити docs/PLAN.md у браузері/редакторі
	@xdg-open docs/PLAN.md 2>/dev/null || echo "docs/PLAN.md, docs/ARCHITECTURE.md, docs/DECISIONS.md"

open-docs: ## Swagger UI застосунку
	@xdg-open $(APP_URL)/docs 2>/dev/null || echo "Swagger: $(APP_URL)/docs"

open-tts: ## Swagger TTS-шлюзу
	@xdg-open $(TTS)/docs 2>/dev/null || echo "Swagger шлюзу: $(TTS)/docs"
