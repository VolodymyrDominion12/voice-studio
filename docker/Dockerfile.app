# FastAPI-застосунок Voice Studio.
# Python 3.11 — див. ADR-002: частина TTS-рушіїв не має коліс для 3.12+.
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# libsndfile1 — потрібен soundfile. ffmpeg свідомо НЕ ставимо:
# базовий цикл працює без нього (ADR-006). Для M4B — окремий образ.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libsndfile1 \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml README.md ./
# Ставимо лише базові залежності: важкі рушії (ukrainian-tts, chatterbox)
# підключаються на хості, де є потрібне залізо.
RUN uv pip install --system --no-cache . || uv pip install --system --no-cache -e .

COPY app ./app

RUN mkdir -p /app/data/{uploads,renders,voices,models}

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/api/v1/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
