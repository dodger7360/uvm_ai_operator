import sqlite3
import unittest
from unittest.mock import Mock, patch

from schemas import PurchaseRequest, RequestItem, SharedAttributeEvidence
from workbench import (
    NormalizationWorkbench,
    WorkbenchResult,
    format_workbench_result,
)


class NormalizationWorkbenchTests(unittest.TestCase):
    @staticmethod
    def _profile_pipe_request(
        raw_request="""Труба профильная
100х50х3 - 444 м.п.
40х40х2 - 60 м.п.
60х40х2 - 48 м.п.
ст3""",
        applies_to=(1, 2, 3),
    ):
        position_lines = [
            "100х50х3 - 444 м.п.",
            "40х40х2 - 60 м.п.",
            "60х40х2 - 48 м.п.",
        ]
        dimensions = [(100, 50, 3), (40, 40, 2), (60, 40, 2)]
        quantities = [444, 60, 48]
        request = PurchaseRequest(items=[
            RequestItem(
                product="труба профильная",
                product_group="profile_pipe",
                size_designation=line.split(" - ", 1)[0],
                width_mm=width,
                height_mm=height,
                wall_thickness_mm=wall,
                quantity=quantity,
                unit="м.п.",
                source_text=line,
            )
            for line, (width, height, wall), quantity
            in zip(position_lines, dimensions, quantities)
        ], shared_attributes=[SharedAttributeEvidence(
            attribute="grade",
            value="ст3",
            source_text="ст3",
            applies_to=list(applies_to),
        )])
        return raw_request, request

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

    def test_applies_trailing_shared_grade_to_all_three_positions(self):
        raw_request, request = self._profile_pipe_request()
        ai_service = Mock()
        ai_service.parse_request.return_value = request

        result = NormalizationWorkbench(ai_service).process(raw_request)

        self.assertTrue(result.is_valid)
        self.assertEqual([item.grade for item in result.current_request.items], [
            "Ст3", "Ст3", "Ст3",
        ])
        self.assertEqual(result.questions, ())
        self.assertEqual(format_workbench_result(result).count("Ст3"), 3)

    def test_shared_grade_does_not_replace_position_source_fragments(self):
        raw_request, request = self._profile_pipe_request()
        expected_fragments = [item.source_text for item in request.items]
        ai_service = Mock()
        ai_service.parse_request.return_value = request

        result = NormalizationWorkbench(ai_service).process(raw_request)

        self.assertEqual(
            [item.source_text for item in result.current_request.items],
            expected_fragments,
        )
        self.assertNotIn("ст3", " ".join(item.source_text for item in result.current_request.items))

    def test_shared_grade_provenance_and_targets_are_preserved(self):
        raw_request, request = self._profile_pipe_request()
        ai_service = Mock()
        ai_service.parse_request.return_value = request

        result = NormalizationWorkbench(ai_service).process(raw_request)

        self.assertEqual(len(result.current_request.confirmed_shared_attributes), 1)
        evidence = result.current_request.confirmed_shared_attributes[0]
        self.assertEqual(evidence.attribute, "grade")
        self.assertEqual(evidence.value, "ст3")
        self.assertEqual(evidence.source_text, "ст3")
        self.assertEqual(evidence.applies_to, [1, 2, 3])

    def test_unsubstantiated_item_grade_is_still_rejected(self):
        request = PurchaseRequest(items=[RequestItem(
            product="труба профильная", product_group="profile_pipe",
            size_designation="100х50х3", width_mm=100, height_mm=50,
            wall_thickness_mm=3, grade="Ст3", quantity=444, unit="м.п.",
            source_text="100х50х3 - 444 м.п.",
        )])
        ai_service = Mock()
        ai_service.parse_request.return_value = request

        result = NormalizationWorkbench(ai_service).process(
            "Труба профильная\n100х50х3 - 444 м.п."
        )

        self.assertIsNone(result.current_request.items[0].grade)
        self.assertTrue(any("марка не подтверждается" in warning
                            for warning in result.current_request.warnings))

    def test_ambiguous_shared_grade_scope_is_not_applied(self):
        raw_request, request = self._profile_pipe_request(applies_to=(1, 2))
        ai_service = Mock()
        ai_service.parse_request.return_value = request

        result = NormalizationWorkbench(ai_service).process(raw_request)

        self.assertEqual([item.grade for item in result.current_request.items], [
            None, None, None,
        ])
        self.assertEqual(result.current_request.confirmed_shared_attributes, [])
        self.assertFalse(result.is_valid)
        self.assertTrue(any("область применения" in question
                            for question in result.questions))

    def test_shared_grade_does_not_weaken_dimension_source_evidence(self):
        raw_request, request = self._profile_pipe_request()
        request.items[0].width_mm = 101
        ai_service = Mock()
        ai_service.parse_request.return_value = request

        result = NormalizationWorkbench(ai_service).process(raw_request)

        self.assertEqual(result.current_request.items[0].grade, "Ст3")
        self.assertFalse(result.is_valid)
        self.assertTrue(any("размера не подтверждается" in warning
                            for warning in result.current_request.warnings))


if __name__ == "__main__":
    unittest.main()
