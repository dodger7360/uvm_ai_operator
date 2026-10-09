import sqlite3
import unittest
from unittest.mock import Mock, patch

from schemas import PurchaseRequest, RequestItem
from workbench import NormalizationWorkbench, WorkbenchResult


class NormalizationWorkbenchTests(unittest.TestCase):
    def test_runs_extraction_normalization_and_validation(self):
        raw_request = "Арматура 12 А500С — 2 т"
        request = PurchaseRequest(items=[RequestItem(
            product="арматура",
            product_group="rebar",
            size_designation="12",
            diameter_mm=12,
            grade="А500С",
            quantity=2,
            unit="т",
            source_text="Арматура 12 А500С — 2 т",
        )])
        ai_service = Mock()
        ai_service.parse_request.return_value = request

        result = NormalizationWorkbench(ai_service).process(raw_request)

        ai_service.parse_request.assert_called_once_with(raw_request)
        self.assertIsInstance(result, WorkbenchResult)
        self.assertTrue(result.is_valid)
        self.assertEqual(result.questions, ())
        self.assertEqual(result.current_request.items[0].diameter_mm, 12)

    def test_preserves_full_raw_request_and_item_source_text(self):
        raw_request = "Заявка для Москвы:\nАрматура 12 А500С — 2 т"
        item_source = "Арматура 12 А500С — 2 т"
        request = PurchaseRequest(items=[RequestItem(
            product="арматура",
            product_group="rebar",
            size_designation="12",
            diameter_mm=12,
            grade="А500С",
            quantity=2,
            unit="т",
            source_text=item_source,
        )])
        ai_service = Mock()
        ai_service.parse_request.return_value = request

        result = NormalizationWorkbench(ai_service).process(raw_request)

        self.assertEqual(result.raw_request, raw_request)
        self.assertEqual(len(result.current_request.items), 1)
        self.assertEqual(result.current_request.items[0].source_text, item_source)

    def test_exposes_validator_questions_separately_from_current_request(self):
        request = PurchaseRequest(items=[RequestItem(
            product="арматура",
            product_group="rebar",
            grade="А500С",
            quantity=2,
            unit="т",
            source_text="Арматура А500С — 2 т",
        )])
        ai_service = Mock()
        ai_service.parse_request.return_value = request

        result = NormalizationWorkbench(ai_service).process("Арматура А500С — 2 т")

        self.assertFalse(result.is_valid)
        self.assertEqual(
            result.questions,
            ("Позиция 1: уточните диаметр.",),
        )
        self.assertEqual(result.questions, tuple(result.current_request.clarification_questions))

    def test_does_not_use_sqlite(self):
        request = PurchaseRequest()
        ai_service = Mock()
        ai_service.parse_request.return_value = request

        with patch.object(sqlite3, "connect") as connect:
            NormalizationWorkbench(ai_service).process("Заявка")

        connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
