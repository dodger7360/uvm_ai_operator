"""SQLite database setup for the application's local storage layer."""

from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Iterator, Optional, Union

from metal_reference.schema import SCHEMA as METAL_REFERENCE_SCHEMA

PathLike = Union[str, Path]


SCHEMA = """
CREATE TABLE IF NOT EXISTS request (
    id INTEGER PRIMARY KEY,
    source_text TEXT NOT NULL,
    city TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS product (
    id INTEGER PRIMARY KEY,
    identity_key TEXT NOT NULL UNIQUE,
    product TEXT NOT NULL,
    product_group TEXT NOT NULL,
    size_designation TEXT,
    grade TEXT,
    diameter_mm REAL,
    thickness_mm REAL,
    width_mm REAL,
    length_mm REAL,
    height_mm REAL,
    side_a_mm REAL,
    side_b_mm REAL,
    wall_thickness_mm REAL,
    form TEXT,
    gost_tu TEXT,
    processing TEXT
);

CREATE TABLE IF NOT EXISTS request_item (
    id INTEGER PRIMARY KEY,
    request_id INTEGER NOT NULL REFERENCES request(id) ON DELETE RESTRICT,
    position INTEGER NOT NULL,
    source_text TEXT NOT NULL,
    product_id INTEGER REFERENCES product(id) ON DELETE RESTRICT,
    normalized_product_json TEXT,
    required_quantity_scaled INTEGER NOT NULL,
    required_unit TEXT NOT NULL,
    source_unit TEXT,
    UNIQUE(request_id, position)
);

CREATE TABLE IF NOT EXISTS warehouse (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    city TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inventory (
    id INTEGER PRIMARY KEY,
    warehouse_id INTEGER NOT NULL REFERENCES warehouse(id) ON DELETE RESTRICT,
    product_id INTEGER NOT NULL REFERENCES product(id) ON DELETE RESTRICT,
    quantity_scaled INTEGER NOT NULL CHECK(quantity_scaled >= 0),
    unit TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(warehouse_id, product_id)
);
"""


class Database:
    """Opens short-lived, transaction-scoped SQLite connections."""

    def __init__(self, path: Optional[PathLike] = None) -> None:
        self.path = Path(path) if path is not None else (
            Path.home() / "uvm_ai_operator" / "operator.sqlite3"
        )

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.path))
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connection() as connection:
            connection.executescript(SCHEMA + METAL_REFERENCE_SCHEMA)
