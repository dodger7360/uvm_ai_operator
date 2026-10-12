import json
import unittest

from pydantic import ValidationError

from schemas import PurchaseRequest, RequestItem


class RequestItemSchemaTests(unittest.TestCase):
    def test_new_fields_default_to_none(self):
        item = RequestItem()
        self.assertIsNone(item.length_type)
        self.assertIsNone(item.length_requirement)
        self.assertIsNone(item.wall_thickness_requirement)

    def test_legacy_data_can_be_created_and_loaded(self):
        legacy = {
            "product": "Арматура", "product_group": "rebar",
            "size_designation": "12", "grade": "А500С",
            "diameter_mm": 12, "length_mm": 11700,
            "quantity": 20, "unit": "т",
            "source_text": "Арматура 12 А500С длина 11,7 м — 20 т",
        }
        for item in (
            RequestItem(**legacy),
            RequestItem.model_validate(legacy),
            RequestItem.model_validate_json(json.dumps(legacy)),
        ):
            with self.subTest(item=item):
                for field, value in legacy.items():
                    self.assertEqual(getattr(item, field), value)
                self.assertIsNone(item.length_type)
                self.assertIsNone(item.length_requirement)
                self.assertIsNone(item.wall_thickness_requirement)
        request = PurchaseRequest.model_validate_json(
            json.dumps({"items": [legacy]})
        )
        self.assertEqual(request.items[0], RequestItem(**legacy))

    def test_accepts_supported_length_types(self):
        for value in ("measured", "unmeasured", "other", None):
            with self.subTest(value=value):
                self.assertEqual(RequestItem(length_type=value).length_type, value)

    def test_rejects_unknown_length_types(self):
        for value in ("unknown", "НД", "", "Measured"):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    RequestItem(length_type=value)

    def test_preserves_unmeasured_length_requirement(self):
        item = RequestItem(
            length_type="unmeasured", length_requirement="НД",
        )
        self.assertEqual(item.length_type, "unmeasured")
        self.assertEqual(item.length_requirement, "НД")
        self.assertIsNone(item.length_mm)

    def test_preserves_wall_requirement_without_numeric_thickness(self):
        item = RequestItem(
            wall_thickness_requirement="стенка минимально возможная",
        )
        self.assertEqual(
            item.wall_thickness_requirement, "стенка минимально возможная",
        )
        self.assertIsNone(item.wall_thickness_mm)

    def test_new_fields_round_trip(self):
        item = RequestItem(
            product="Труба профильная", size_designation="40×40",
            length_type="measured", length_requirement="длина 6 м",
            length_mm=6000,
            wall_thickness_requirement="стенка минимально возможная",
            quantity=100, unit="м",
            source_text="Труба профильная 40×40, длина 6 м, стенка минимально возможная — 100 м",
        )
        data = item.model_dump()
        self.assertEqual(data["length_type"], "measured")
        self.assertEqual(data["length_requirement"], "длина 6 м")
        self.assertEqual(
            data["wall_thickness_requirement"], "стенка минимально возможная",
        )
        self.assertEqual(RequestItem.model_validate(data), item)
        self.assertEqual(RequestItem.model_validate_json(item.model_dump_json()), item)
        request = PurchaseRequest(items=[item])
        self.assertEqual(
            PurchaseRequest.model_validate_json(request.model_dump_json()), request,
        )


if __name__ == "__main__":
    unittest.main()
