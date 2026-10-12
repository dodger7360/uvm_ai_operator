import unittest
from unittest.mock import Mock

from schemas import PurchaseRequest, RequestItem
from workbench import NormalizationWorkbench, WorkbenchResult, main


class NormalizationWorkbenchCliTests(unittest.TestCase):
    def test_reads_multiline_request_and_displays_current_result(self):
        raw_request = "Нужна арматура 12 А500С\n2 тонны"
        request = PurchaseRequest(items=[RequestItem(
            product="арматура",
            product_group="rebar",
            size_designation="12",
            diameter_mm=12,
            grade="А500С",
            quantity=2,
            unit="т",
            source_text="арматура 12 А500С 2 тонны",
        )], is_valid=True)
        workbench = Mock(spec=NormalizationWorkbench)
        workbench.process.return_value = WorkbenchResult(
            raw_request=raw_request,
            current_request=request,
        )
        lines = iter(["Нужна арматура 12 А500С", "2 тонны", "/done"])
        output = []

        result = main(
            workbench=workbench,
            input_fn=lambda _prompt: next(lines),
            output_fn=output.append,
        )

        self.assertEqual(result, 0)
        workbench.process.assert_called_once_with(raw_request)
        rendered = "\n".join(output)
        self.assertIn("1. Арматура А500С Ø12 — 2 т", rendered)
        self.assertIn("Проверка: пройдена", rendered)
        self.assertIn("Вопросы/уточнения:\n- Нет", rendered)

    def test_eof_processes_collected_input(self):
        workbench = Mock(spec=NormalizationWorkbench)
        workbench.process.return_value = WorkbenchResult(
            raw_request="Арматура 12 А500С",
            current_request=PurchaseRequest(),
        )
        calls = iter(["Арматура 12 А500С", EOFError()])

        def read(_prompt):
            value = next(calls)
            if isinstance(value, Exception):
                raise value
            return value

        result = main(workbench=workbench, input_fn=read, output_fn=lambda _text: None)

        self.assertEqual(result, 0)
        workbench.process.assert_called_once_with("Арматура 12 А500С")

    def test_ctrl_c_exits_without_processing_partial_input(self):
        workbench = Mock(spec=NormalizationWorkbench)

        def interrupt(_prompt):
            raise KeyboardInterrupt

        output = []
        result = main(workbench=workbench, input_fn=interrupt, output_fn=output.append)

        self.assertEqual(result, 0)
        workbench.process.assert_not_called()
        self.assertIn("Ввод прерван.", output[-1])

    def test_empty_input_does_not_start_workbench(self):
        workbench = Mock(spec=NormalizationWorkbench)
        lines = iter(["/done"])
        output = []

        result = main(
            workbench=workbench,
            input_fn=lambda _prompt: next(lines),
            output_fn=output.append,
        )

        self.assertEqual(result, 0)
        workbench.process.assert_not_called()
        self.assertIn("Заявка не введена.", output[-1])


if __name__ == "__main__":
    unittest.main()
