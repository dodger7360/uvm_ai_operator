import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from database import Database
from product_identity import build_identity_key
from quantity import to_scaled_quantity
from repositories import (
    InventoryRepository,
    ProductRepository,
    RequestRepository,
    WarehouseRepository,
)
from schemas import PurchaseRequest, RequestItem


class SQLiteStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database = Database(Path(self.temp_dir.name) / "test.sqlite3")
        self.database.initialize()
        self.requests = RequestRepository(self.database)
        self.products = ProductRepository(self.database)
        self.warehouses = WarehouseRepository(self.database)
        self.inventory = InventoryRepository(self.database)

    def _table_names(self):
        with self.database.connection() as connection:
            return {
                row["name"] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }

    def _request_with_rebar(self, quantity=20, unit="т"):
        return PurchaseRequest(
            city="Москва",
            items=[RequestItem(
                product="арматура",
                product_group="rebar",
                diameter_mm=12,
                grade="А500",
                quantity=quantity,
                unit=unit,
                source_unit=unit,
                source_text="арматура 12 А500 20 тонн",
                interpreted_fields=["diameter_mm"],
            )],
        )

    def test_creates_all_five_tables_and_enables_foreign_keys(self):
        self.assertTrue(
            {"request", "request_item", "product", "warehouse", "inventory"}
            .issubset(self._table_names())
        )
        with self.database.connection() as connection:
            self.assertEqual(
                connection.execute("PRAGMA foreign_keys").fetchone()[0], 1
            )

    def test_request_item_stores_valid_product_snapshot_and_can_be_linked_later(self):
        request_id = self.requests.create(
            "Нужна арматура 12 А500 20 тонн в Москве",
            self._request_with_rebar(),
        )
        item = self.requests.get_items(request_id)[0]
        self.assertEqual(item["source_text"], "арматура 12 А500 20 тонн")
        self.assertIsNone(item["product_id"])
        snapshot = json.loads(item["normalized_product_json"])
        self.assertEqual(snapshot["product_group"], "rebar")
        self.assertEqual(snapshot["diameter_mm"], 12.0)
        self.assertEqual(snapshot["grade"], "А500")
        self.assertNotIn("quantity", snapshot)
        self.assertNotIn("unit", snapshot)
        self.assertNotIn("source_text", snapshot)
        self.assertNotIn("interpreted_fields", snapshot)

        product_id = self.products.create(self._request_with_rebar().items[0])
        self.requests.link_product(item["id"], product_id)
        self.assertEqual(
            self.requests.get_items(request_id)[0]["product_id"], product_id
        )
        with self.database.connection() as connection:
            saved_request = connection.execute(
                "SELECT source_text, city FROM request WHERE id = ?", (request_id,)
            ).fetchone()
        self.assertEqual(saved_request["source_text"], "Нужна арматура 12 А500 20 тонн в Москве")
        self.assertEqual(saved_request["city"], "Москва")

    def test_identity_is_deterministic_and_product_key_is_unique(self):
        item = self._request_with_rebar().items[0]
        same_item = RequestItem(
            product="Арматура", product_group=item.product_group,
            diameter_mm=item.diameter_mm, grade=item.grade,
        )
        self.assertEqual(build_identity_key(item), build_identity_key(same_item))
        self.products.create(item)
        with self.assertRaises(sqlite3.IntegrityError):
            self.products.create(same_item)

    def test_distinct_grades_and_pipe_wall_thickness_have_distinct_keys(self):
        rebar_a500 = RequestItem(
            product="арматура", product_group="rebar", diameter_mm=12, grade="А500"
        )
        rebar_a500c = RequestItem(
            product="арматура", product_group="rebar", diameter_mm=12, grade="А500С"
        )
        self.assertNotEqual(build_identity_key(rebar_a500), build_identity_key(rebar_a500c))

        pipe_4 = RequestItem(
            product="труба", product_group="round_pipe",
            diameter_mm=108, wall_thickness_mm=4,
        )
        pipe_5 = RequestItem(
            product="труба", product_group="round_pipe",
            diameter_mm=108, wall_thickness_mm=5,
        )
        self.assertNotEqual(build_identity_key(pipe_4), build_identity_key(pipe_5))

    def test_incomplete_sheet_cannot_get_identity_or_be_saved_as_product(self):
        sheet = RequestItem(
            product="лист", product_group="sheet", thickness_mm=10
        )
        with self.assertRaisesRegex(ValueError, "width_mm, length_mm"):
            build_identity_key(sheet)
        with self.assertRaisesRegex(ValueError, "width_mm, length_mm"):
            self.products.create(sheet)

    def test_quantity_scaling_uses_exact_integer_units(self):
        self.assertEqual(to_scaled_quantity(20, "т"), (20_000_000, "т"))
        self.assertEqual(to_scaled_quantity(7000, "кг"), (7_000_000, "т"))
        self.assertEqual(to_scaled_quantity(500.5, "кг"), (500_500, "т"))
        self.assertEqual(to_scaled_quantity("1.234", "м"), (1234, "м"))
        self.assertEqual(to_scaled_quantity(5, "шт"), (5, "шт"))
        with self.assertRaisesRegex(ValueError, "точность"):
            to_scaled_quantity("1.5", "шт")

    def test_request_quantity_is_saved_scaled_and_item_product_is_optional(self):
        request_id = self.requests.create("Арматура", self._request_with_rebar())
        item = self.requests.get_items(request_id)[0]
        self.assertEqual(item["required_quantity_scaled"], 20_000_000)
        self.assertEqual(item["required_unit"], "т")
        self.assertIsNone(item["product_id"])

    def test_warehouse_product_and_inventory_are_related(self):
        product_id = self.products.create(self._request_with_rebar().items[0])
        warehouse_id = self.warehouses.create("Основной склад", "Москва")
        inventory_id = self.inventory.set_balance(warehouse_id, product_id, 7, "т")
        rows = self.inventory.list_for_product(product_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], inventory_id)
        self.assertEqual(rows[0]["quantity_scaled"], 7_000_000)
        self.assertEqual(rows[0]["unit"], "т")

    def test_inventory_rejects_negative_quantity_and_duplicate_pair(self):
        product_id = self.products.create(self._request_with_rebar().items[0])
        warehouse_id = self.warehouses.create("Основной склад", "Москва")
        with self.assertRaises(ValueError):
            self.inventory.set_balance(warehouse_id, product_id, -1, "т")
        self.inventory.set_balance(warehouse_id, product_id, 7, "т")
        with self.assertRaises(sqlite3.IntegrityError):
            with self.database.connection() as connection:
                connection.execute(
                    """INSERT INTO inventory
                    (warehouse_id, product_id, quantity_scaled, unit, updated_at)
                    VALUES (?, ?, ?, ?, ?)""",
                    (warehouse_id, product_id, 1, "т", "2026-01-01T00:00:00+00:00"),
                )

    def test_inventory_unit_cannot_change_for_existing_warehouse_product(self):
        product_id = self.products.create(self._request_with_rebar().items[0])
        warehouse_id = self.warehouses.create("Основной склад", "Москва")
        self.inventory.set_balance(warehouse_id, product_id, 7, "т")
        with self.assertRaisesRegex(ValueError, "изменить единицу"):
            self.inventory.set_balance(warehouse_id, product_id, 100, "м")
        # kg is converted first and therefore remains the same canonical unit.
        self.inventory.set_balance(warehouse_id, product_id, 7000, "кг")
        self.assertEqual(self.inventory.list_for_product(product_id)[0]["unit"], "т")

    def test_foreign_key_violations_and_deletes_are_restricted(self):
        with self.assertRaises(sqlite3.IntegrityError):
            with self.database.connection() as connection:
                connection.execute(
                    """INSERT INTO request_item
                    (request_id, position, source_text, required_quantity_scaled, required_unit)
                    VALUES (?, ?, ?, ?, ?)""",
                    (999, 1, "позиция", 1, "шт"),
                )

        product_id = self.products.create(self._request_with_rebar().items[0])
        warehouse_id = self.warehouses.create("Основной склад", "Москва")
        self.inventory.set_balance(warehouse_id, product_id, 2, "т")
        with self.assertRaises(sqlite3.IntegrityError):
            with self.database.connection() as connection:
                connection.execute("DELETE FROM product WHERE id = ?", (product_id,))
        with self.assertRaises(sqlite3.IntegrityError):
            with self.database.connection() as connection:
                connection.execute("DELETE FROM warehouse WHERE id = ?", (warehouse_id,))


if __name__ == "__main__":
    unittest.main()
