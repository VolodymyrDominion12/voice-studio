"""Операції над блоками документа.

Блок — структурна одиниця тексту (абзац, заголовок, код). Тут живе правило
«який текст насправді піде в синтез» і точкові/масові правки.
"""

from __future__ import annotations

import logging

from sqlmodel import Session

from app.models import Block
from app.services.library.errors import BlockNotFoundError

logger = logging.getLogger(__name__)


def block_effective_text(block: Block) -> str:
    """Текст блоку, який насправді піде в синтез.

    Єдине місце з цим правилом: пріоритет «мій → нормалізований → вихідний».
    Дублювання логіки в UI чи воркері дало б розбіжність у тому, що саме
    озвучено (docs/FRONTEND.md, розд. 6.3).
    """
    return block.text_edited or block.text_normalized or block.text_raw


def count_speakable(blocks: list[Block]) -> int:
    """Скільки блоків піде в синтез.

    Порожній блок із `speak=True` не дасть жодного сегмента, тому рахуємо
    лише ті, у яких є непорожній ефективний текст.
    """
    return sum(1 for block in blocks if block.speak and block_effective_text(block).strip())


def update_block(session: Session, document_id: int, block_id: int, changes: dict) -> Block:
    """Оновити текст, емоцію, інтенсивність або speak-прапорець блоку."""
    block = session.get(Block, block_id)
    if not block or block.document_id != document_id:
        raise BlockNotFoundError(f"Блок {block_id} не знайдено в документі {document_id}")

    for key, value in changes.items():
        setattr(block, key, value)
    session.add(block)
    session.commit()
    session.refresh(block)
    return block


def update_blocks(session: Session, document_id: int, updates: list[dict]) -> list[Block]:
    """Масова правка блоків однією транзакцією.

    Потрібна для масової розмітки емоцій: 142 окремі PATCH-и — це 142
    транзакції й 142 перерендери (docs/FRONTEND.md, розд. 6.6 і знахідка 16.5).
    Кожен елемент — словник із `block_id` і полями для зміни.
    """
    updated: list[Block] = []
    for item in updates:
        changes = dict(item)
        block_id = changes.pop("block_id")
        block = session.get(Block, block_id)
        if not block or block.document_id != document_id:
            raise BlockNotFoundError(f"Блок {block_id} не знайдено в документі {document_id}")
        for key, value in changes.items():
            setattr(block, key, value)
        session.add(block)
        updated.append(block)

    session.commit()
    for block in updated:
        session.refresh(block)
    return updated
