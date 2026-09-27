"""Версія застосунку.

Окремий модуль, щоб і `app.main`, і сервіси могли її читати без циклічних
імпортів. Значення має збігатися з `version` у pyproject.toml.
"""

from __future__ import annotations

__version__ = "0.1.0"
