import unittest

from normalizer import normalize_request
from schemas import PurchaseRequest, RequestItem
from validator import validate_request


def validate_item(**kwargs):
    request = PurchaseRequest(items=[RequestItem(**kwargs)])
    return validate_request(normalize_request(request))


class ValidatorTests(unittest.TestCase):
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

    def test_preserves_no_model_generated_questions_or_warnings(self):
        request = PurchaseRequest(
            items=[RequestItem(product="швеллер", size_designation="12П", quantity=1, unit="т")],
            clarification_questions=["вопрос модели"],
            warnings=["предупреждение модели"],
            missing_fields=["ошибка модели"],
        )

        result = validate_request(normalize_request(request))

        self.assertEqual(result.missing_fields, [])
        self.assertEqual(result.clarification_questions, [])
        self.assertEqual(result.warnings, [])
