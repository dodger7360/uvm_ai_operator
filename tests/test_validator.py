import unittest

from normalizer import normalize_request
from schemas import PurchaseRequest, RequestItem
from validator import validate_request


def validate_item(**kwargs):
    kwargs.setdefault(
        "source_text",
        " ".join(
            str(kwargs[field])
            for field in (
                "product", "size_designation", "grade", "quantity", "unit"
            )
            if kwargs.get(field) is not None
        ),
    )
    request = PurchaseRequest(items=[RequestItem(**kwargs)])
    return validate_request(normalize_request(request))


class ValidatorTests(unittest.TestCase):
    def _round_length(self, source, **fields):
        return validate_item(
            product="Труба", product_group="round_pipe", size_designation="76×3,5",
            diameter_mm=76, wall_thickness_mm=3.5, quantity=4, unit="т",
            source_text=source, **fields,
        )

    def _profile_wall(self, source, **fields):
        return validate_item(
            product="Труба профильная", product_group="profile_pipe",
            size_designation="40×40", width_mm=40, height_mm=40,
            quantity=100, unit="м", source_text=source, **fields,
        )

    def test_confirms_unmeasured_length(self):
        for text in ("НД", "немерная длина"):
            with self.subTest(text=text):
                result = self._round_length(
                    f"Труба 76×3,5 {text} — 4 т",
                    length_type="unmeasured", length_requirement=text,
                )
                self.assertTrue(result.is_valid)
                self.assertEqual(result.items[0].length_requirement, text)
                self.assertIsNone(result.items[0].length_mm)

    def test_rejects_unconfirmed_or_negated_unmeasured_length(self):
        for text in ("", "не НД", "без НД", "мерная длина 6 м"):
            with self.subTest(text=text):
                result = self._round_length(
                    f"Труба 76×3,5 {text} — 4 т",
                    length_type="unmeasured", length_requirement="НД",
                )
                self.assertFalse(result.is_valid)
                self.assertIn("items[0].length_type", result.missing_fields)
                self.assertEqual(result.items[0].length_type, "unmeasured")

    def test_numeric_length_does_not_prove_measured_type(self):
        result = self._round_length(
            "Труба 76×3,5 длина 6 м — 4 т",
            length_mm=6000, length_type="measured",
        )
        self.assertFalse(result.is_valid)
        self.assertIn("items[0].length_type", result.missing_fields)
        self.assertNotIn("items[0].length_mm", result.missing_fields)

    def test_confirms_explicit_measured_and_other_types(self):
        for kind, text in (("measured", "мерная длина 6 м"), ("other", "кратная длина")):
            with self.subTest(kind=kind):
                result = self._round_length(
                    f"Труба 76×3,5 {text} — 4 т",
                    length_type=kind, length_requirement=text,
                )
                self.assertTrue(result.is_valid)
        result = self._round_length("Труба 76×3,5 — 4 т", length_type="other")
        self.assertFalse(result.is_valid)

    def test_checks_converted_length_units(self):
        for text, value in (("11,7 м", 11700), ("11700 мм", 11700), ("1170 см", 11700)):
            with self.subTest(text=text):
                result = self._round_length(
                    f"Труба 76×3,5 длина {text} — 4 т",
                    length_mm=value, length_requirement=f"длина {text}",
                )
                self.assertTrue(result.is_valid)
                self.assertIsNone(result.items[0].length_type)

    def test_rejects_wrong_or_unconfirmed_numeric_length(self):
        for source, value in (
            ("Труба 76×3,5 длина 11,7 м — 4 т", 6000),
            ("Труба 76×3,5 11,7 — 4 т", 11700),
            ("Труба 76×3,5 — 11700 м", 11700),
            ("Труба 76×3,5 длина 6 м или 12 м — 4 т", 6000),
            ("Труба 76×3,5 длина -6 м — 4 т", 6000),
            ("Труба 76×3,5 не длина 6 м — 4 т", 6000),
        ):
            with self.subTest(source=source):
                result = self._round_length(source, length_mm=value)
                self.assertFalse(result.is_valid)
                self.assertIn("items[0].length_mm", result.missing_fields)

    def test_sheet_geometric_length_remains_valid(self):
        result = validate_item(
            product="Лист", product_group="sheet", size_designation="16 1500×6000",
            thickness_mm=16, width_mm=1500, length_mm=6000,
            quantity=10, unit="т", source_text="Лист 16 1500×6000 — 10 т",
        )
        self.assertTrue(result.is_valid)
        self.assertIsNone(result.items[0].length_type)

    def test_accepts_confirmed_minimum_wall_without_selecting_thickness(self):
        source = "Труба профильная 40×40, стенка минимально возможная — 100 м"
        result = self._profile_wall(source, wall_thickness_requirement="стенка минимально возможная")
        self.assertTrue(result.is_valid)
        self.assertEqual(result.missing_fields, [])
        self.assertEqual(result.warnings, [])
        self.assertIsNone(result.items[0].wall_thickness_mm)
        self.assertEqual(result.items[0].source_text, source)

    def test_profile_without_wall_still_requires_clarification(self):
        result = self._profile_wall("Труба профильная 40×40 — 100 м")
        self.assertFalse(result.is_valid)
        self.assertEqual(result.missing_fields, ["items[0].wall_thickness_mm"])
        self.assertEqual(result.clarification_questions, ["Позиция 1: уточните толщину стенки."])

    def test_arbitrary_or_unconfirmed_wall_requirement_is_not_sufficient(self):
        for text, requirement in (
            ("", "стенка минимально возможная"),
            ("стенка по согласованию", "стенка по согласованию"),
            ("не стенка минимально возможная", "стенка минимально возможная"),
        ):
            with self.subTest(text=text):
                result = self._profile_wall(
                    f"Труба профильная 40×40, {text} — 100 м",
                    wall_thickness_requirement=requirement,
                )
                self.assertFalse(result.is_valid)
                self.assertIn("items[0].wall_thickness_mm", result.missing_fields)

    def test_wall_requirement_cannot_hide_dimension_contradictions(self):
        for source, wall in (
            ("Труба профильная 40×40, стенка минимально возможная — 100 м", 2),
            ("Труба профильная 40×50, стенка минимально возможная — 100 м", None),
            ("Труба профильная 40×40×3, стенка минимально возможная — 100 м", 2),
        ):
            with self.subTest(source=source):
                result = self._profile_wall(
                    source, wall_thickness_mm=wall,
                    wall_thickness_requirement="стенка минимально возможная",
                )
                self.assertFalse(result.is_valid)
                self.assertTrue(any("размера" in warning for warning in result.warnings))

    def test_conflicting_types_and_unconfirmed_length_requirement_need_clarification(self):
        result = self._round_length(
            "Труба 76×3,5 НД, мерная длина 6 м — 4 т",
            length_type="unmeasured", length_requirement="НД",
        )
        self.assertFalse(result.is_valid)
        result = self._round_length(
            "Труба 76×3,5 — 4 т", length_requirement="длина 11,7 м",
        )
        self.assertFalse(result.is_valid)
        self.assertIn("items[0].length_requirement", result.missing_fields)

    def test_bare_length_requirement_requires_unit(self):
        result = validate_item(
            product="Арматура", product_group="rebar", size_designation="12",
            grade="А500С", quantity=20, unit="т", length_requirement="11,7",
            source_text="Арматура 12 А500С 11,7 — 20 т",
        )
        self.assertFalse(result.is_valid)
        self.assertIn("Позиция 1: уточните единицу измерения длины изделия.", result.clarification_questions)
        self.assertEqual(result.items[0].length_requirement, "11,7")
        self.assertIsNone(result.items[0].length_mm)

    def test_unscoped_request_context_cannot_confirm_item_length(self):
        source = "Труба 76×3,5 — 4 т"
        request = PurchaseRequest(items=[RequestItem(
            product="Труба", product_group="round_pipe", diameter_mm=76,
            wall_thickness_mm=3.5, quantity=4, unit="т", source_text=source,
            length_type="unmeasured", length_requirement="НД",
        )])
        result = validate_request(request, raw_request=source + "\nНД")
        self.assertFalse(result.is_valid)

    def test_negated_requirement_without_length_type_is_not_confirmed(self):
        result = self._round_length(
            "Труба 76×3,5 не НД — 4 т", length_requirement="НД",
        )
        self.assertFalse(result.is_valid)
        self.assertIn("items[0].length_requirement", result.missing_fields)

    def test_validation_preserves_order_source_and_is_repeatable(self):
        items = [
            RequestItem(
                product="Труба", product_group="round_pipe", diameter_mm=76,
                wall_thickness_mm=3.5, quantity=4, unit="т",
                source_text="Труба 76×3,5 НД — 4 т",
                length_type="unmeasured", length_requirement="НД",
            ),
            RequestItem(
                product="Арматура", product_group="rebar", diameter_mm=12,
                grade="А500С", quantity=20, unit="т", length_requirement="11,7",
                source_text="Арматура 12 А500С 11,7 — 20 т",
            ),
        ]
        original = [item.model_dump() for item in items]
        request = validate_request(PurchaseRequest(items=items))
        self.assertEqual([item.model_dump() for item in request.items], original)
        snapshot = request.model_dump()
        self.assertEqual(validate_request(request).model_dump(), snapshot)

    def test_does_not_ask_for_known_standard_or_derived_dimensions(self):
        items = [
            RequestItem(product="труба", size_designation="40×40×4", quantity=1, unit="т"),
            RequestItem(product="швеллер", size_designation="12П", quantity=1, unit="т"),
            RequestItem(product="балка", size_designation="25К1", quantity=2, unit="т"),
            RequestItem(product="уголок", size_designation="90×90×6", quantity=0.3, unit="т"),
            RequestItem(product="арматура", grade="А1", size_designation="20", quantity=0.115, unit="т"),
        ]

        request = PurchaseRequest(items=items)
        result = validate_request(normalize_request(request))

        self.assertEqual(result.missing_fields, [])
        self.assertEqual(result.clarification_questions, [])

    def test_asks_only_for_information_needed_to_understand_position(self):
        pipe = validate_item(product="труба", size_designation="57", quantity=1, unit="т")
        sheet = validate_item(product="лист", size_designation="6", quantity=2, unit="шт")
        rebar = validate_item(product="арматура", grade="А500С", quantity=3, unit="т")

        self.assertEqual(pipe.missing_fields, ["items[0].wall_thickness_mm"])
        self.assertEqual(sheet.missing_fields, ["items[0].width_mm", "items[0].length_mm"])
        self.assertEqual(rebar.missing_fields, ["items[0].diameter_mm"])

    def test_rejects_unknown_product_group(self):
        result = validate_item(
            product="металл",
            product_group="unknown",
            quantity=1,
            unit="т",
        )

        self.assertEqual(result.missing_fields, ["items[0].product_group"])
        self.assertEqual(result.clarification_questions, ["Позиция 1: уточните тип товара."])

    def test_rebar_requires_diameter_and_grade(self):
        without_diameter = validate_item(
            product="арматура",
            grade="А500С",
            quantity=1,
            unit="т",
        )
        without_grade = validate_item(
            product="арматура",
            size_designation="12",
            quantity=1,
            unit="т",
        )

        self.assertEqual(without_diameter.missing_fields, ["items[0].diameter_mm"])
        self.assertEqual(without_grade.missing_fields, ["items[0].grade"])

    def test_non_positive_quantity_blocks_validation(self):
        for quantity in (0, -1):
            with self.subTest(quantity=quantity):
                result = validate_item(
                    product="швеллер",
                    size_designation="12П",
                    quantity=quantity,
                    unit="т",
                )

                self.assertEqual(result.missing_fields, ["items[0].quantity"])
                self.assertEqual(result.warnings, [])
                self.assertFalse(result.is_valid)

    def test_unsupported_unit_blocks_validation(self):
        result = validate_item(
            product="швеллер",
            size_designation="12П",
            quantity=1,
            unit="паллет",
        )

        self.assertEqual(result.missing_fields, ["items[0].unit"])
        self.assertEqual(
            result.clarification_questions,
            ["Позиция 1: уточните поддерживаемую единицу измерения."],
        )

    def test_unknown_unit_remains_unsupported_after_normalization(self):
        result = validate_item(
            product="труба профильная", product_group="profile_pipe",
            size_designation="40х40х2", width_mm=40, height_mm=40,
            wall_thickness_mm=2, quantity=1, unit="бухта",
        )

        self.assertEqual(result.items[0].unit, "бухта")
        self.assertEqual(result.missing_fields, ["items[0].unit"])

    def test_accepts_valid_rebar_position(self):
        result = validate_item(
            product="арматура",
            size_designation="12",
            grade="А500С",
            quantity=20,
            unit="т",
        )

        self.assertEqual(result.missing_fields, [])
        self.assertEqual(result.clarification_questions, [])
        self.assertEqual(result.warnings, [])
        self.assertTrue(result.is_valid)

    def test_preserves_no_model_generated_questions_or_warnings(self):
        request = PurchaseRequest(
            items=[RequestItem(
                product="швеллер", size_designation="12П", quantity=1, unit="т",
                source_text="Швеллер 12П — 1 т",
            )],
            clarification_questions=["вопрос модели"],
            warnings=["предупреждение модели"],
            missing_fields=["ошибка модели"],
        )

        result = validate_request(normalize_request(request))

        self.assertEqual(result.missing_fields, [])
        self.assertEqual(result.clarification_questions, [])
        self.assertEqual(result.warnings, [])

    def test_absent_grade_and_gost_are_not_invented_by_python(self):
        request = PurchaseRequest(items=[RequestItem(
            product="арматура", product_group="rebar", diameter_mm=12,
            quantity=1, unit="т", source_text="Арматура 12 — 1 т",
        )])

        result = validate_request(normalize_request(request))

        self.assertIsNone(result.items[0].grade)
        self.assertIsNone(result.items[0].gost_tu)
        self.assertEqual(result.missing_fields, ["items[0].grade"])
        self.assertFalse(result.is_valid)

    def test_unsubstantiated_model_grade_and_gost_are_discarded(self):
        request = PurchaseRequest(items=[RequestItem(
            product="арматура", product_group="rebar", diameter_mm=12,
            grade="А500С", gost_tu="ГОСТ 34028", quantity=1, unit="т",
            source_text="Арматура 12 — 1 т",
        )])

        result = validate_request(normalize_request(request))

        self.assertIsNone(result.items[0].grade)
        self.assertIsNone(result.items[0].gost_tu)
        self.assertEqual(result.missing_fields, ["items[0].grade"])
        self.assertEqual(len(result.warnings), 2)

    def test_confirmed_dimension_values_remain_valid(self):
        request = PurchaseRequest(items=[RequestItem(
            product="труба", product_group="round_pipe", diameter_mm=57,
            wall_thickness_mm=4, size_designation="57×4", quantity=1, unit="т",
            source_text="Труба 57×4 — 1 т",
        )])

        result = validate_request(normalize_request(request))

        self.assertEqual(result.items[0].diameter_mm, 57)
        self.assertEqual(result.items[0].wall_thickness_mm, 4)
        self.assertEqual(result.items[0].size_designation, "57×4")
        self.assertTrue(result.is_valid)

    def test_profile_pipe_dimensions_are_confirmed_as_an_ordered_size_sequence(self):
        examples = (
            ("100х50х3", 100, 50, 3),
            ("40x40x2", 40, 40, 2),
            ("60×40×2", 60, 40, 2),
        )
        for designation, width, height, wall in examples:
            with self.subTest(designation=designation):
                request = PurchaseRequest(items=[RequestItem(
                    product="труба профильная", product_group="profile_pipe",
                    size_designation=designation, width_mm=width,
                    height_mm=height, wall_thickness_mm=wall,
                    quantity=10, unit="м",
                    source_text=f"Труба профильная {designation} — 10 м",
                )])

                result = validate_request(normalize_request(request))

                self.assertTrue(result.is_valid)
                self.assertEqual(result.warnings, [])

    def test_dimension_values_cannot_be_confirmed_by_unrelated_numbers(self):
        request = PurchaseRequest(items=[RequestItem(
            product="труба профильная", product_group="profile_pipe",
            size_designation="100х50х4", width_mm=100, height_mm=50,
            wall_thickness_mm=3, quantity=3, unit="м",
            source_text="Труба профильная 100х50х4 — 3 м",
        )])

        result = validate_request(normalize_request(request))

        self.assertFalse(result.is_valid)
        self.assertTrue(any(
            "размера не подтверждается" in warning for warning in result.warnings
        ))

    def test_confirms_sheet_dimensions_with_legacy_space_separator(self):
        request = PurchaseRequest(items=[RequestItem(
            product="лист", product_group="sheet",
            size_designation="6 1500*6000", thickness_mm=6,
            width_mm=1500, length_mm=6000, quantity=2, unit="шт",
            source_text="Лист 6 1500*6000 — 2 шт",
        )])

        result = validate_request(normalize_request(request))

        self.assertTrue(result.is_valid)
        self.assertEqual(result.warnings, [])

    def test_required_dimension_absent_from_source_blocks_validation(self):
        request = PurchaseRequest(items=[RequestItem(
            product="труба", product_group="round_pipe", diameter_mm=57,
            wall_thickness_mm=4, size_designation="57×4", quantity=1, unit="т",
            source_text="Труба 57 — 1 т",
        )])

        result = validate_request(normalize_request(request))

        self.assertFalse(result.is_valid)
        self.assertEqual(result.missing_fields, [])

    def test_missing_item_source_text_blocks_validation(self):
        request = PurchaseRequest(items=[RequestItem(
            product="швеллер", product_group="channel", size_designation="12П",
            quantity=1, unit="т",
        )])

        result = validate_request(normalize_request(request))

        self.assertFalse(result.is_valid)
        self.assertEqual(result.missing_fields, [])
        self.assertIn("исходного фрагмента", result.warnings[0])

    def test_python_overwrites_model_validation_fields_and_preserves_item_source(self):
        request = PurchaseRequest(
            items=[RequestItem(
                product="балка", product_group="beam", size_designation="25К1",
                quantity=2, unit="т", source_text="Балка 25К1 — 2 т",
            )],
            missing_fields=["model value"],
            clarification_questions=["model question"],
            warnings=["model warning"],
            is_valid=False,
        )

        result = validate_request(normalize_request(request))

        self.assertEqual(result.items[0].source_text, "Балка 25К1 — 2 т")
        self.assertEqual(result.missing_fields, [])
        self.assertEqual(result.clarification_questions, [])
        self.assertEqual(result.warnings, [])
        self.assertTrue(result.is_valid)
