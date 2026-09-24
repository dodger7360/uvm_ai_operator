"""Persistence operations for warehouses."""

import sqlite3
from typing import List

from database import Database


class WarehouseRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create(self, name: str, city: str) -> int:
        with self.database.connection() as connection:
            cursor = connection.execute(
                "INSERT INTO warehouse (name, city) VALUES (?, ?)", (name, city)
            )
            return int(cursor.lastrowid)

    def list_by_city(self, city: str) -> List[sqlite3.Row]:
        with self.database.connection() as connection:
            return connection.execute(
                "SELECT * FROM warehouse WHERE city = ? ORDER BY id", (city,)
            ).fetchall()
