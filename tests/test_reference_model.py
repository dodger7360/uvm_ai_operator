"""Validation fixtures for the generic Metal Reference relational model."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from database import Database


class MetalReferenceModelTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database = Database(Path(self.temp_dir.name) / "reference.sqlite3")
        self.database.initialize()
        self._load_fixtures()

    def _load_fixtures(self):
        # Only values already present in project research or explicitly supplied
        # as example designations in the task are used here.
        with self.database.connection() as connection:
            examples = (
                ("rebar_a3", "Арматура А3", "rebar", "Арматура", "12", "12"),
                ("beam", "Балка", "beam", "Балка", "20б1", "20Б1"),
                ("channel", "Швеллер", "channel", "Швеллер", "10п", "10П"),
                ("sheet_hot_rolled", "Лист/рулон г/к", "sheet", "Лист/рулон г/к", "16x1500x6000", "16×1500×6000"),
                ("rectangular_pipe", "Труба прямоугольная", "pipe", "Труба", "100x50x4", "100×50×4"),
            )
            self.items = {}
            self.categories = {}
            self.families = {}
            for code, name, family_code, family_name, key, designation in examples:
                if family_code not in self.families:
                    self.families[family_code] = connection.execute(
                        "INSERT INTO reference_family (code, name) VALUES (?, ?)",
                        (family_code, family_name),
                    ).lastrowid
                category_id = connection.execute(
                    "INSERT INTO reference_category (family_id, code, name) VALUES (?, ?, ?)",
                    (self.families[family_code], code, name),
                ).lastrowid
                self.categories[code] = category_id
                item_id = connection.execute(
                    """INSERT INTO reference_item
                    (category_id, normalized_key, designation)
                    VALUES (?, ?, ?)""",
                    (category_id, key, designation),
                ).lastrowid
                self.items[code] = item_id

            standards = {}
            for designation, document_type, year in (
                ("ГОСТ Р 52544-2006", "ГОСТ", 2006),
                ("ГОСТ Р 57837-2017", "ГОСТ", 2017),
                ("ГОСТ 26020-83", "ГОСТ", 1983),
                ("ГОСТ 8240-97", "ГОСТ", 1997),
            ):
                standards[designation] = connection.execute(
                    """INSERT INTO reference_standard
                    (designation, document_type, year)
                    VALUES (?, ?, ?)""",
                    (designation, document_type, year),
                ).lastrowid

            rebar_variant = self._variant(
                connection, self.items["rebar_a3"], "В500С",
                standards["ГОСТ Р 52544-2006"],
            )
            beam_variant = self._variant(
                connection, self.items["beam"], standard_id=standards["ГОСТ Р 57837-2017"]
            )
            beam_26020_variant = self._variant(
                connection, self.items["beam"], standard_id=standards["ГОСТ 26020-83"]
            )
            channel_variant = self._variant(
                connection, self.items["channel"], standard_id=standards["ГОСТ 8240-97"]
            )
            sheet_variant = self._variant(
                connection, self.items["sheet_hot_rolled"], "Ст08"
            )
            self.variants = {
                "rebar_a3": rebar_variant,
                "beam": beam_variant,
                "beam_gost_26020": beam_26020_variant,
                "channel": channel_variant,
                "sheet_hot_rolled": sheet_variant,
                "rectangular_pipe": None,
            }

            self._dimension(connection, self.items["rebar_a3"], "nominal_diameter", 12, "мм")
            self._property(connection, rebar_variant, "cross_section_area", 113.1, "мм²")
            self._property(connection, rebar_variant, "mass_per_length", 0.888, "кг/м",
                           "Номинальная масса 1 м", "0,888 кг")

            # The diagram values are known, but their named geometric mapping is
            # not established in the research. Keep them as generic dimensions.
            self._dimension(connection, beam_variant, "diagram_dimension", 200, "мм",
                            "Размерная метка схемы", "200 мм", variant=True)
            self._property(connection, beam_variant, "cross_section_area", 27.16,
                           "см²", "Площадь поперечного сечения", "27,16 см²")
            self._property(connection, beam_variant, "mass_per_length", 21.3,
                           "кг/м", "Масса 1 м", "21,3 кг")
            self._property(connection, beam_26020_variant, "cross_section_area", 28.49,
                           "см²", "Площадь поперечного сечения", "28,49 см²")
            self._property(connection, beam_26020_variant, "mass_per_length", 22.4,
                           "кг/м", "Масса 1 м", "22,4 кг")

            self._dimension(connection, channel_variant, "diagram_dimension", 100, "мм",
                            "Размерная метка схемы", "100 мм", variant=True)
            self._property(connection, channel_variant, "cross_section_area", 10.9,
                           "см²", "Площадь поперечного сечения", "10,9 см²")
            self._property(connection, channel_variant, "mass_per_length", 8.59,
                           "кг/м", "Масса 1 м", "8,59 кг")

            for param, value in (
                ("thickness", 16), ("width", 1500), ("length", 6000),
            ):
                self._dimension(connection, self.items["sheet_hot_rolled"], param,
                                value, "мм")
            self._property(connection, sheet_variant, "density", 7.871, "т/м³",
                           "Выбранная сталь Ст08", "7871 кг/м³",
                           "Контекст выбора Ст08 в калькуляторе листа")

            for param, value in (("width", 100), ("height", 50), ("wall_thickness", 4)):
                self._dimension(connection, self.items["rectangular_pipe"], param,
                                value, "мм")

            aliases = (
                ("rebar_a3", "Арматура А3 ф12", "12"),
                ("beam", "Балка 20Б1", "20б1"),
                ("channel", "Швеллер 10П", "10п"),
                ("sheet_hot_rolled", "Лист/рулон г/к 16×1500×6000", "16x1500x6000"),
                ("rectangular_pipe", "Труба прямоугольная 100×50×4", "100x50x4"),
            )
            for code, raw_value, normalized_value in aliases:
                connection.execute(
                    """INSERT INTO reference_alias
                    (reference_item_id, raw_value, normalized_value, alias_type)
                    VALUES (?, ?, ?, ?)""",
                    (self.items[code], raw_value, normalized_value, "request_phrase"),
                )

    @staticmethod
    def _variant(connection, item_id, grade_class=None, standard_id=None):
        return connection.execute(
            """INSERT INTO reference_variant
            (reference_item_id, grade_class, standard_id)
            VALUES (?, ?, ?)""",
            (item_id, grade_class, standard_id),
        ).lastrowid

    @staticmethod
    def _dimension(connection, owner_id, parameter_type, numeric_value, unit,
                   raw_label=None, raw_value=None, variant=False):
        connection.execute(
            """INSERT INTO reference_dimension
            (reference_item_id, reference_variant_id, parameter_type, numeric_value,
             unit, raw_label, raw_value)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (None if variant else owner_id, owner_id if variant else None,
             parameter_type, numeric_value, unit, raw_label, raw_value),
        )

    @staticmethod
    def _property(connection, owner_id, property_type, numeric_value, unit,
                  raw_label=None, raw_value=None, context=None):
        connection.execute(
            """INSERT INTO reference_property
            (reference_variant_id, property_type, numeric_value, unit,
             raw_label, raw_value, context)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (owner_id, property_type, numeric_value, unit, raw_label, raw_value, context),
        )

    def test_five_examples_use_categories_items_and_only_supported_variants(self):
        with self.database.connection() as connection:
            rows = connection.execute(
                """SELECT DISTINCT c.code, i.id
                FROM reference_category c
                JOIN reference_item i ON i.category_id = c.id
                LEFT JOIN reference_variant v ON v.reference_item_id = i.id
                ORDER BY c.code"""
            ).fetchall()
            self.assertEqual(len(rows), 5)
            self.assertEqual(
                {row["code"] for row in rows},
                set(self.items),
            )
            pipe_variants = connection.execute(
                """SELECT COUNT(*) FROM reference_variant
                WHERE reference_item_id = ?""",
                (self.items["rectangular_pipe"],),
            ).fetchone()[0]
            self.assertEqual(pipe_variants, 0)

    def test_rebar_normative_variant_contains_confirmed_dimensions_and_mass(self):
        with self.database.connection() as connection:
            values = connection.execute(
                """SELECT d.parameter_type, d.numeric_value, d.unit,
                          p.property_type, p.numeric_value AS property_value,
                          s.designation
                FROM reference_variant v
                JOIN reference_dimension d ON d.reference_item_id = v.reference_item_id
                JOIN reference_property p ON p.reference_variant_id = v.id
                JOIN reference_standard s ON s.id = v.standard_id
                WHERE v.id = ? AND p.property_type = 'mass_per_length'""",
                (self.variants["rebar_a3"],),
            ).fetchone()
        self.assertEqual(values["parameter_type"], "nominal_diameter")
        self.assertEqual(values["numeric_value"], 12)
        self.assertEqual(values["unit"], "мм")
        self.assertEqual(values["property_value"], 0.888)
        self.assertEqual(values["designation"], "ГОСТ Р 52544-2006")

    def test_family_category_item_diameter_and_mark_have_separate_levels(self):
        with self.database.connection() as connection:
            row = connection.execute(
                """SELECT f.code AS family_code, c.code AS category_code,
                          i.normalized_key, d.numeric_value AS diameter,
                          v.grade_class
                FROM reference_family f
                JOIN reference_category c ON c.family_id = f.id
                JOIN reference_item i ON i.category_id = c.id
                JOIN reference_dimension d ON d.reference_item_id = i.id
                JOIN reference_variant v ON v.reference_item_id = i.id
                WHERE c.code = 'rebar_a3' AND d.parameter_type = 'nominal_diameter'"""
            ).fetchone()
            family_constraint = list(connection.execute(
                "PRAGMA foreign_key_list(reference_category)"
            ))
            family_categories = connection.execute(
                """SELECT f.code AS family_code, c.code AS category_code
                FROM reference_family f
                JOIN reference_category c ON c.family_id = f.id
                WHERE c.code IN ('beam', 'channel') ORDER BY c.code"""
            ).fetchall()
        self.assertEqual(
            (row["family_code"], row["category_code"], row["normalized_key"],
             row["diameter"], row["grade_class"]),
            ("rebar", "rebar_a3", "12", 12, "В500С"),
        )
        self.assertTrue(any(entry["table"] == "reference_family" for entry in family_constraint))
        self.assertNotIn("В500С", row["normalized_key"])
        self.assertEqual(
            [(entry["family_code"], entry["category_code"]) for entry in family_categories],
            [("beam", "beam"), ("channel", "channel")],
        )

    def test_family_and_category_foreign_key_and_uniqueness_constraints(self):
        with self.database.connection() as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO reference_family (code, name) VALUES (?, ?)",
                    ("rebar", "Повтор семейства"),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO reference_category (code, name) VALUES (?, ?)",
                    ("category_without_family", "Без семейства"),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO reference_category (family_id, code, name) VALUES (?, ?, ?)",
                    (-1, "category_with_unknown_family", "Неизвестное семейство"),
                )

    def test_beam_keeps_distinct_normative_variants_for_different_mass(self):
        with self.database.connection() as connection:
            rows = connection.execute(
                """SELECT s.designation, p.numeric_value
                FROM reference_variant v
                JOIN reference_standard s ON s.id = v.standard_id
                JOIN reference_property p ON p.reference_variant_id = v.id
                WHERE v.reference_item_id = ? AND p.property_type = 'mass_per_length'
                ORDER BY s.designation""",
                (self.items["beam"],),
            ).fetchall()
        self.assertEqual(
            [(row["designation"], row["numeric_value"]) for row in rows],
            [("ГОСТ 26020-83", 22.4), ("ГОСТ Р 57837-2017", 21.3)],
        )

    def test_beam_and_channel_share_property_shape_and_keep_standards_separate(self):
        with self.database.connection() as connection:
            results = connection.execute(
                """SELECT i.designation, s.designation AS standard, p.numeric_value,
                          p.unit
                FROM reference_item i
                JOIN reference_variant v ON v.reference_item_id = i.id
                JOIN reference_standard s ON s.id = v.standard_id
                JOIN reference_property p ON p.reference_variant_id = v.id
                WHERE p.property_type = 'mass_per_length'
                  AND i.designation IN ('20Б1', '10П')
                ORDER BY i.designation, s.designation"""
            ).fetchall()
        self.assertEqual(
            [(row["designation"], row["standard"], row["numeric_value"])
             for row in results],
            [("10П", "ГОСТ 8240-97", 8.59),
             ("20Б1", "ГОСТ 26020-83", 22.4),
             ("20Б1", "ГОСТ Р 57837-2017", 21.3)],
        )
        self.assertEqual([row["unit"] for row in results], ["кг/м"] * 3)
        with self.database.connection() as connection:
            raw_dimensions = connection.execute(
                """SELECT d.raw_value FROM reference_dimension d
                JOIN reference_variant v ON v.id = d.reference_variant_id
                WHERE v.id IN (?, ?) ORDER BY d.raw_value""",
                (self.variants["channel"], self.variants["beam"]),
            ).fetchall()
        self.assertEqual([row["raw_value"] for row in raw_dimensions], ["100 мм", "200 мм"])

    def test_sheet_density_is_contextual_and_pipe_has_only_known_dimensions(self):
        with self.database.connection() as connection:
            sheet_density = connection.execute(
                """SELECT p.numeric_value, p.unit, p.raw_value, p.context,
                          v.grade_class, v.standard_id
                FROM reference_variant v
                JOIN reference_property p ON p.reference_variant_id = v.id
                WHERE p.property_type = 'density'"""
            ).fetchone()
            pipe = connection.execute(
                """SELECT d.parameter_type, d.numeric_value, d.unit
                FROM reference_dimension d
                WHERE d.reference_item_id = ? ORDER BY d.parameter_type""",
                (self.items["rectangular_pipe"],),
            ).fetchall()
        self.assertEqual(sheet_density["numeric_value"], 7.871)
        self.assertEqual(sheet_density["unit"], "т/м³")
        self.assertEqual(sheet_density["raw_value"], "7871 кг/м³")
        self.assertEqual(sheet_density["grade_class"], "Ст08")
        self.assertIsNone(sheet_density["standard_id"])
        self.assertEqual(
            [(row["parameter_type"], row["numeric_value"]) for row in pipe],
            [("height", 50), ("wall_thickness", 4), ("width", 100)],
        )

    def test_each_user_designation_resolves_through_an_alias(self):
        with self.database.connection() as connection:
            aliases = connection.execute(
                """SELECT c.code, i.normalized_key, a.raw_value, a.normalized_value
                FROM reference_alias a
                JOIN reference_item i ON i.id = a.reference_item_id
                JOIN reference_category c ON c.id = i.category_id"""
            ).fetchall()
        self.assertEqual(len(aliases), 5)
        self.assertTrue(all(row["raw_value"] and row["normalized_value"] for row in aliases))
        self.assertEqual(
            {(row["code"], row["normalized_key"], row["normalized_value"])
             for row in aliases},
            {
                ("rebar_a3", "12", "12"),
                ("beam", "20б1", "20б1"),
                ("channel", "10п", "10п"),
                ("sheet_hot_rolled", "16x1500x6000", "16x1500x6000"),
                ("rectangular_pipe", "100x50x4", "100x50x4"),
            },
        )

    def test_external_ids_are_source_scoped_and_separate_from_domain_keys(self):
        with self.database.connection() as connection:
            source_id = connection.execute(
                "INSERT INTO reference_source (source_type, name) VALUES (?, ?)",
                ("catalog", "23MET"),
            ).lastrowid
            second_source_id = connection.execute(
                "INSERT INTO reference_source (source_type, name) VALUES (?, ?)",
                ("catalog", "Other source"),
            ).lastrowid
            category_id = connection.execute(
                """INSERT INTO reference_category
                (family_id, code, name, source_id, external_id) VALUES (?, ?, ?, ?, ?)""",
                (self.families["rebar"], "rebar_a3_internal", "Арматура А3", source_id, "armatura_a3"),
            ).lastrowid
            other_category_id = connection.execute(
                """INSERT INTO reference_category
                (family_id, code, name, source_id, external_id) VALUES (?, ?, ?, ?, ?)""",
                (self.families["rebar"], "rebar_a3_other_source", "Арматура А3", second_source_id, "armatura_a3"),
            ).lastrowid
            connection.execute(
                """INSERT INTO reference_item
                (category_id, normalized_key, designation, source_id, external_id)
                VALUES (?, ?, ?, ?, ?)""",
                (category_id, "12-mm", "Ø12", source_id, "12"),
            )
            connection.execute(
                """INSERT INTO reference_item
                (category_id, normalized_key, designation, source_id, external_id)
                VALUES (?, ?, ?, ?, ?)""",
                (other_category_id, "12-mm", "Ø12", second_source_id, "12"),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO reference_category
                    (family_id, code, name, source_id, external_id) VALUES (?, ?, ?, ?, ?)""",
                    (self.families["rebar"], "duplicate_source_mapping", "Duplicate", source_id, "armatura_a3"),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO reference_item
                    (category_id, normalized_key, designation, source_id, external_id)
                    VALUES (?, ?, ?, ?, ?)""",
                    (category_id, "12-mm-other", "Ø12 duplicate", source_id, "12"),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO reference_item
                    (category_id, normalized_key, designation, external_id)
                    VALUES (?, ?, ?, ?)""",
                    (category_id, "missing-source", "Missing source", "external"),
                )
            saved = connection.execute(
                """SELECT code, external_id FROM reference_category
                WHERE id = ?""",
                (category_id,),
            ).fetchone()
        self.assertEqual(saved["code"], "rebar_a3_internal")
        self.assertEqual(saved["external_id"], "armatura_a3")

    def test_provenance_source_and_locator_columns_exist_on_fact_tables(self):
        expected = {
            "reference_variant", "reference_dimension", "reference_property",
            "reference_standard", "reference_alias",
        }
        with self.database.connection() as connection:
            for table in expected:
                columns = {
                    row["name"]
                    for row in connection.execute(f"PRAGMA table_info({table})")
                }
                self.assertTrue({"source_id", "source_locator"}.issubset(columns), table)

            source_id = connection.execute(
                "INSERT INTO reference_source (source_type, name) VALUES (?, ?)",
                ("catalog", "23MET"),
            ).lastrowid
            row = connection.execute(
                """INSERT INTO reference_dimension
                (reference_item_id, parameter_type, unit, raw_value,
                 source_id, source_locator)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (self.items["sheet_hot_rolled"], None, None, "размер из снимка",
                 source_id, "/price/list_gk/4?filter=1500x6000"),
            )
            self.assertEqual(row.rowcount, 1)
            saved = connection.execute(
                "SELECT source_id, source_locator FROM reference_dimension ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(saved["source_id"], source_id)
        self.assertEqual(saved["source_locator"], "/price/list_gk/4?filter=1500x6000")

    def test_dimension_preserves_untyped_raw_value_and_requires_unit_for_numeric_value(self):
        with self.database.connection() as connection:
            cursor = connection.execute(
                """INSERT INTO reference_dimension
                (reference_item_id, parameter_type, numeric_value, unit,
                 raw_label, raw_value)
                VALUES (?, NULL, NULL, NULL, ?, ?)""",
                (self.items["beam"], "Размерная метка схемы", "200 мм"),
            )
            saved = connection.execute(
                """SELECT parameter_type, numeric_value, unit, raw_label, raw_value
                FROM reference_dimension WHERE id = ?""",
                (cursor.lastrowid,),
            ).fetchone()
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO reference_dimension
                    (reference_item_id, numeric_value, unit)
                    VALUES (?, ?, NULL)""",
                    (self.items["beam"], 200),
                )
        self.assertIsNone(saved["parameter_type"])
        self.assertIsNone(saved["numeric_value"])
        self.assertIsNone(saved["unit"])
        self.assertEqual(saved["raw_label"], "Размерная метка схемы")
        self.assertEqual(saved["raw_value"], "200 мм")

    def test_alias_can_belong_to_one_of_three_owner_types_and_values_can_repeat(self):
        with self.database.connection() as connection:
            category_ids = [
                row["id"] for row in connection.execute(
                    "SELECT id FROM reference_category ORDER BY id LIMIT 2"
                )
            ]
            variant_id = self.variants["rebar_a3"]
            for category_id in category_ids:
                connection.execute(
                    """INSERT INTO reference_alias
                    (reference_category_id, raw_value, normalized_value, alias_type)
                    VALUES (?, ?, ?, ?)""",
                    (category_id, "alias probe", "alias probe", "category_candidate"),
                )
            connection.execute(
                """INSERT INTO reference_alias
                (reference_item_id, raw_value, normalized_value, alias_type)
                VALUES (?, ?, ?, ?)""",
                (self.items["rebar_a3"], "alias probe", "alias probe", "item_candidate"),
            )
            connection.execute(
                """INSERT INTO reference_alias
                (reference_variant_id, raw_value, normalized_value, alias_type)
                VALUES (?, ?, ?, ?)""",
                (variant_id, "alias probe", "alias probe", "variant_candidate"),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO reference_alias
                    (raw_value, normalized_value, alias_type)
                    VALUES (?, ?, ?)""",
                    ("ownerless", "ownerless", "probe"),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO reference_alias
                    (reference_category_id, reference_item_id,
                     raw_value, normalized_value, alias_type)
                    VALUES (?, ?, ?, ?, ?)""",
                    (category_ids[0], self.items["rebar_a3"],
                     "multi-owner", "multi-owner", "probe"),
                )
            count = connection.execute(
                "SELECT COUNT(*) FROM reference_alias WHERE normalized_value = 'alias probe'"
            ).fetchone()[0]
        self.assertEqual(count, 4)


if __name__ == "__main__":
    unittest.main()
