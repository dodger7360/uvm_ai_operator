"""Persistence operations for requests and their structured items."""

import json
import sqlite3
from typing import Optional

from database import Database
from product_identity import IDENTITY_FIELDS
from quantity import to_scaled_quantity
from schemas import PurchaseRequest, RequestItem


PRODUCT_SNAPSHOT_FIELDS = ("product", "product_group") + IDENTITY_FIELDS


def normalized_product_snapshot(item: RequestItem) -> Optional[str]:
    values = {field: getattr(item, field) for field in PRODUCT_SNAPSHOT_FIELDS}
    if not any(value is not None for value in values.values()):
        return None
    return json.dumps(
        values,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


class RequestRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create(self, source_text: str, request: PurchaseRequest) -> int:
        """Save the original request and normalized product-only item snapshots."""
        prepared = []
        for position, item in enumerate(request.items, start=1):
            if not item.source_text:
                raise ValueError("Для сохранения позиции нужен её исходный source_text.")
            if item.quantity is None:
                raise ValueError("Для сохранения позиции нужно количество.")
            scaled, unit = to_scaled_quantity(item.quantity, item.unit)
            prepared.append((
                position,
                item.source_text,
                normalized_product_snapshot(item),
                scaled,
                unit,
                item.source_unit or item.unit,
            ))

        with self.database.connection() as connection:
            cursor = connection.execute(
                "INSERT INTO request (source_text, city) VALUES (?, ?)",
                (source_text, request.city),
            )
            request_id = int(cursor.lastrowid)
            connection.executemany(
                """INSERT INTO request_item
                (request_id, position, source_text, product_id,
                 normalized_product_json, required_quantity_scaled,
                 required_unit, source_unit)
                VALUES (?, ?, ?, NULL, ?, ?, ?, ?)""",
                [
                    (request_id, position, source, snapshot, quantity, unit, source_unit)
                    for position, source, snapshot, quantity, unit, source_unit in prepared
                ],
            )
            return request_id

    def get_items(self, request_id: int):
        with self.database.connection() as connection:
            return connection.execute(
                "SELECT * FROM request_item WHERE request_id = ? ORDER BY position",
                (request_id,),
            ).fetchall()

    def link_product(self, item_id: int, product_id: int) -> None:
        with self.database.connection() as connection:
            cursor = connection.execute(
                "UPDATE request_item SET product_id = ? WHERE id = ?",
                (product_id, item_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Позиция заявки не найдена.")
