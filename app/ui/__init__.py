"""HTML-інтерфейс (Jinja2 + htmx + Alpine, ADR-008).

Тут живуть сторінки й фрагменти для браузера. JSON-API (`/api/v1`) лишається
окремим: HTML-роутер кличе той самий сервісний шар
(`app/services/library/`), а не власний HTTP.

Роутер підключається напряму: `from app.ui.router import router`. Свідомо
НЕ реекспортуємо його з пакета — інакше імʼя `router` у пакеті затіняє
підмодуль `app.ui.router`, і його константи стають недоступними.
"""

from __future__ import annotations
