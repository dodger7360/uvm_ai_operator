"""Offline ingestion tests for A1, A3 and At800 /price snapshots."""

import tempfile
import unittest
from pathlib import Path

import pytest

from database import Database
from metal_reference.ingest_armatura_price import (
    BASE_CATEGORIES,
    DEFAULT_RESEARCH_DIR,
    ingest_armatura_price,
)


class ArmaturaPriceIngestionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp_dir.cleanup)
        self.database = Database(Path(self.temp_dir.name) / "armatura.sqlite3")
        self.database.initialize()

    @pytest.mark.legacy_23met
    def test_ingests_three_categories_and_snapshot_confirmed_assortment(self):
        counts = ingest_armatura_price(self.database, DEFAULT_RESEARCH_DIR)
        self.assertEqual(counts["families"], 1)
        self.assertEqual(counts["sources"], 3)
        self.assertEqual(counts["categories"], 3)
        self.assertEqual(counts["items"], 41)
        self.assertEqual(counts["dimensions"], 41)
        self.assertEqual(counts["standards"], 6)
        self.assertEqual(counts["variants"], 14)
        self.assertEqual(counts["properties"], 0)
        self.assertEqual(counts["aliases"], 0)

        expected_sizes = {
            "armatura_a1": {"6", "6.5", "8", "10", "12", "14", "16", "18",
                            "20", "22", "25", "28", "30", "32", "36", "40"},
            "armatura_a3": {"5", "5.8", "6", "7.8", "8", "10", "11", "12", "14",
                            "16", "18", "20", "22", "25", "27", "28", "30", "32", "36", "40"},
            "armatura_at800": {"10", "12", "14", "16", "18"},
        }
        with self.database.connection() as connection:
            family_categories = connection.execute(
                """SELECT f.code AS family_code, f.name AS family_name,
                          c.code, c.name, c.external_id, c.source_id
                   FROM reference_family f
                   JOIN reference_category c ON c.family_id = f.id
                   ORDER BY c.code"""
            ).fetchall()
            self.assertEqual(len(family_categories), 3)
            self.assertTrue(all(row["family_code"] == "rebar" for row in family_categories))
            self.assertTrue(all(row["family_name"] == "Арматура" for row in family_categories))
            for row in family_categories:
                self.assertEqual(row["external_id"], row["code"])
                self.assertIsNotNone(row["source_id"])
                actual = {
                    item["normalized_key"]
                    for item in connection.execute(
                        """SELECT i.normalized_key FROM reference_item i
                           JOIN reference_category c ON c.id = i.category_id
                           WHERE c.code = ?""",
                        (row["code"],),
                    )
                }
                self.assertEqual(actual, expected_sizes[row["code"]])

            dimensions = connection.execute(
                """SELECT c.code, i.normalized_key, d.parameter_type,
                          d.numeric_value, d.unit, d.raw_label, d.raw_value,
                          d.reference_variant_id, d.source_id, d.source_locator
                   FROM reference_dimension d
                   JOIN reference_item i ON i.id = d.reference_item_id
                   JOIN reference_category c ON c.id = i.category_id"""
            ).fetchall()
            self.assertEqual(len(dimensions), 41)
            for row in dimensions:
                self.assertEqual(row["parameter_type"], "nominal_diameter")
                self.assertEqual(row["unit"], "мм")
                self.assertEqual(float(row["numeric_value"]), float(row["normalized_key"]))
                self.assertEqual(row["raw_value"], row["normalized_key"])
                self.assertEqual(row["raw_label"], "Размеры")
                self.assertIsNone(row["reference_variant_id"])
                self.assertIsNotNone(row["source_id"])
                self.assertTrue(row["source_locator"].endswith("/12"))

            sources = connection.execute(
                """SELECT name, url_path FROM reference_source
                   WHERE source_type = 'local_html_snapshot'"""
            ).fetchall()
            self.assertEqual(len(sources), 3)
            self.assertTrue(all("tmp/23met_research/armatura_" in row["url_path"]
                                for row in sources))

    @pytest.mark.legacy_23met
    def test_same_size_has_distinct_grade_variants_and_category_scoped_items(self):
        ingest_armatura_price(self.database, DEFAULT_RESEARCH_DIR)
        with self.database.connection() as connection:
            items = connection.execute(
                """SELECT c.code, i.id, i.normalized_key, i.designation
                   FROM reference_item i
                   JOIN reference_category c ON c.id = i.category_id
                   WHERE i.normalized_key = '12' ORDER BY c.code"""
            ).fetchall()
            self.assertEqual(len(items), 3)
            self.assertEqual(len({row["id"] for row in items}), 3)
            self.assertEqual({row["code"] for row in items}, set(BASE_CATEGORIES))
            self.assertTrue(all(row["designation"] == "12" for row in items))

            a3_item_id = next(row["id"] for row in items if row["code"] == "armatura_a3")
            variants = connection.execute(
                """SELECT v.grade_class, s.designation AS standard
                   FROM reference_variant v
                   LEFT JOIN reference_standard s ON s.id = v.standard_id
                   WHERE v.reference_item_id = ? ORDER BY v.grade_class, s.designation""",
                (a3_item_id,),
            ).fetchall()
            by_grade = {}
            for row in variants:
                by_grade.setdefault(row["grade_class"], set()).add(row["standard"])
            self.assertEqual(
                set(by_grade),
                {"25Г2С", "А500", "А500С", "А550СК", "А600", "А800", "В500С"},
            )
            self.assertEqual(
                by_grade["А500С"], {"ГОСТ 34028-2016", "СТО АСЧМ 7-93"}
            )
            self.assertEqual(by_grade["А500"], {"ТУ 55791017-05-001-2019"})
            self.assertEqual(by_grade["А600"], {None})
            self.assertFalse(any(value in {"1ф", "2ф", "3ф"} for value in by_grade))

            a1_item_id = next(row["id"] for row in items if row["code"] == "armatura_a1")
            a1_grades = {
                row["grade_class"] for row in connection.execute(
                    "SELECT grade_class FROM reference_variant WHERE reference_item_id = ?",
                    (a1_item_id,),
                )
            }
            self.assertEqual(a1_grades, {"А240", "А240С", "ст3", "3пс/сп"})
            at800_item_id = next(
                row["id"] for row in items if row["code"] == "armatura_at800"
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM reference_variant WHERE reference_item_id = ?",
                    (at800_item_id,),
                ).fetchone()[0],
                0,
            )

    @pytest.mark.legacy_23met
    def test_only_price_reference_fields_are_stored_and_provenance_is_attached(self):
        ingest_armatura_price(self.database, DEFAULT_RESEARCH_DIR)
        with self.database.connection() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM reference_property").fetchone()[0], 0
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM reference_alias").fetchone()[0], 0
            )
            tables = {
                row["name"] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            self.assertTrue(
                {"price", "supplier", "availability", "market_offer"}.isdisjoint(tables)
            )
            for table in ("reference_category", "reference_item", "reference_variant",
                          "reference_dimension", "reference_standard"):
                columns = {
                    row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
                }
                self.assertTrue(
                    {"price", "supplier", "availability", "commercial_length"}.isdisjoint(
                        columns
                    )
                )
            self.assertEqual(
                connection.execute(
                    """SELECT COUNT(*) FROM reference_variant v
                       JOIN reference_source s ON s.id = v.source_id
                       WHERE s.source_type = 'local_html_snapshot'
                         AND v.source_locator IS NOT NULL"""
                ).fetchone()[0],
                14,
            )
            self.assertEqual(
                connection.execute(
                    """SELECT COUNT(*) FROM reference_standard
                       WHERE source_id IS NOT NULL AND source_locator IS NOT NULL"""
                ).fetchone()[0],
                6,
            )

    @pytest.mark.legacy_23met
    def test_repeated_import_is_idempotent(self):
        first = ingest_armatura_price(self.database, DEFAULT_RESEARCH_DIR)
        second = ingest_armatura_price(self.database, DEFAULT_RESEARCH_DIR)
        self.assertTrue(any(value > 0 for value in first.values()))
        self.assertTrue(all(value == 0 for value in second.values()))
        with self.database.connection() as connection:
            expected = {
                "reference_family": 1,
                "reference_source": 3,
                "reference_category": 3,
                "reference_item": 41,
                "reference_dimension": 41,
                "reference_standard": 6,
                "reference_variant": 14,
                "reference_property": 0,
                "reference_alias": 0,
            }
            for table, count in expected.items():
                self.assertEqual(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                    count,
                    table,
                )


if __name__ == "__main__":
    unittest.main()
