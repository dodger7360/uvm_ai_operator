"""Persistence operations for current warehouse inventory balances."""

from datetime import datetime, timezone
from typing import List
import sqlite3

from database import Database
from quantity import to_scaled_quantity


class InventoryRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def set_balance(self, warehouse_id: int, product_id: int, quantity, unit: str) -> int:
        scaled, canonical_unit = to_scaled_quantity(quantity, unit, allow_zero=True)
        updated_at = datetime.now(timezone.utc).isoformat()
        with self.database.connection() as connection:
            existing = connection.execute(
                """SELECT id, unit FROM inventory
                WHERE warehouse_id = ? AND product_id = ?""",
                (warehouse_id, product_id),
            ).fetchone()
            if existing is not None:
                if existing["unit"] != canonical_unit:
                    raise ValueError(
                        "Нельзя изменить единицу существующего остатка "
                        "для той же пары склад/товар."
                    )
                connection.execute(
                    """UPDATE inventory SET quantity_scaled = ?, updated_at = ?
                    WHERE id = ?""",
                    (scaled, updated_at, existing["id"]),
                )
                return int(existing["id"])

            cursor = connection.execute(
                """INSERT INTO inventory
                (warehouse_id, product_id, quantity_scaled, unit, updated_at)
                VALUES (?, ?, ?, ?, ?)""",
                (warehouse_id, product_id, scaled, canonical_unit, updated_at),
            )
            return int(cursor.lastrowid)

    def list_for_product(self, product_id: int) -> List[sqlite3.Row]:
        with self.database.connection() as connection:
            return connection.execute(
                "SELECT * FROM inventory WHERE product_id = ? ORDER BY warehouse_id",
                (product_id,),
            ).fetchall()
