"""Persistence operations for canonical products."""

import sqlite3
from typing import Optional

from database import Database
from product_identity import IDENTITY_FIELDS, build_identity_key


PRODUCT_FIELDS = IDENTITY_FIELDS


class ProductRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create(self, item) -> int:
        identity_key = build_identity_key(item)
        product = getattr(item, "product", None)
        if not product or not product.strip():
            raise ValueError("Для сохранения Product нужно отображаемое название.")
        values = [getattr(item, field, None) for field in PRODUCT_FIELDS]
        fields = ["identity_key", "product", "product_group"] + list(PRODUCT_FIELDS)
        params = [identity_key, product.strip(), item.product_group] + values
        placeholders = ", ".join("?" for _ in fields)
        with self.database.connection() as connection:
            cursor = connection.execute(
                "INSERT INTO product ({}) VALUES ({})".format(
                    ", ".join(fields), placeholders
                ),
                params,
            )
            return int(cursor.lastrowid)

    def get_by_identity_key(self, identity_key: str) -> Optional[sqlite3.Row]:
        with self.database.connection() as connection:
            return connection.execute(
                "SELECT * FROM product WHERE identity_key = ?", (identity_key,)
            ).fetchone()

    def get_or_create(self, item) -> int:
        identity_key = build_identity_key(item)
        existing = self.get_by_identity_key(identity_key)
        if existing is not None:
            return int(existing["id"])
        try:
            return self.create(item)
        except sqlite3.IntegrityError:
            # A concurrent creator may have inserted the same unique key.
            existing = self.get_by_identity_key(identity_key)
            if existing is not None:
                return int(existing["id"])
            raise
