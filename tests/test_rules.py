import unittest

from rules import field_label, normalize_grade_rule, required_fields


class RulesTests(unittest.TestCase):
    def test_required_fields_for_product_groups(self):
        self.assertEqual(
            required_fields("profile_pipe"),
            ("width_mm", "height_mm", "wall_thickness_mm"),
        )
        self.assertEqual(
            required_fields("sheet"),
            ("thickness_mm", "width_mm", "length_mm"),
        )
        self.assertEqual(
            required_fields("round_pipe"),
            ("diameter_mm", "wall_thickness_mm"),
        )
        self.assertEqual(
            required_fields("rebar"),
            ("diameter_mm",),
        )

    def test_unknown_product_group_has_no_required_fields(self):
        self.assertEqual(required_fields("unknown"), ())
        self.assertEqual(required_fields(None), ())

    def test_field_labels(self):
        self.assertEqual(field_label("wall_thickness_mm"), "толщину стенки")
        self.assertEqual(field_label("diameter_mm"), "диаметр")
        self.assertEqual(field_label("size_designation"), "типоразмер")

    def test_normalizes_gs_to_09g2s(self):
        self.assertEqual(normalize_grade_rule("ГС"), "09Г2С")
        self.assertEqual(normalize_grade_rule("гс"), "09Г2С")

    def test_does_not_replace_steel_with_another_steel(self):
        self.assertEqual(normalize_grade_rule("Ст3"), "Ст3")
        self.assertEqual(normalize_grade_rule("09Г2С"), "09Г2С")
        self.assertEqual(normalize_grade_rule("С355"), "С355")

    def test_empty_grade_remains_empty(self):
        self.assertIsNone(normalize_grade_rule(None))
        self.assertEqual(normalize_grade_rule(""), "")


if __name__ == "__main__":
    unittest.main()