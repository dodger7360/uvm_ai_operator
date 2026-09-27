import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ai import AIService, SYSTEM_PROMPT
from schemas import PurchaseRequest, PurchaseRequestExtraction, RequestItem


class AIServiceTests(unittest.TestCase):
    def setUp(self):
        self.request = PurchaseRequestExtraction()
        message = SimpleNamespace(refusal=None, parsed=self.request)
        self.response = SimpleNamespace(
            choices=[SimpleNamespace(message=message)]
        )

    @patch("ai.load_dotenv")
    @patch("ai.OpenAI")
    def test_parse_request_sends_prompt_and_parses_structured_response(
        self, openai, load_dotenv
    ):
        client = Mock()
        client.beta.chat.completions.parse.return_value = self.response
        openai.return_value = client

        service = AIService()
        result = service.parse_request("арматура 12")

        self.assertEqual(result, PurchaseRequest())
        self.assertIsInstance(result, PurchaseRequest)
        load_dotenv.assert_called_once_with()
        openai.assert_called_once()
        client.beta.chat.completions.parse.assert_called_once_with(
            model="gpt-5.6-luna",
            response_format=PurchaseRequestExtraction,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": "арматура 12"},
            ],
        )

    @patch("ai.load_dotenv")
    @patch("ai.OpenAI")
    def test_extracts_multiple_items_with_individual_source_fragments(
        self, openai, _load_dotenv
    ):
        extraction = PurchaseRequestExtraction(items=[
            RequestItem(
                product="балка", product_group="beam", size_designation="25К1",
                quantity=2, unit="т", source_text="Балка 25К1 — 2 т",
            ),
            RequestItem(
                product="швеллер", product_group="channel", size_designation="12П",
                quantity=1, unit="т", source_text="Швеллер 12П — 1 т",
            ),
        ])
        client = Mock()
        client.beta.chat.completions.parse.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(refusal=None, parsed=extraction))]
        )
        openai.return_value = client

        result = AIService().parse_request("Балка 25К1 — 2 т\nШвеллер 12П — 1 т")

        self.assertEqual(len(result.items), 2)
        self.assertEqual(
            [item.source_text for item in result.items],
            ["Балка 25К1 — 2 т", "Швеллер 12П — 1 т"],
        )

    @patch("ai.load_dotenv")
    @patch("ai.OpenAI")
    def test_source_fragment_match_ignores_case_spacing_and_punctuation(
        self, openai, _load_dotenv
    ):
        extraction = PurchaseRequestExtraction(items=[RequestItem(
            product="балка", product_group="beam", size_designation="25К1",
            quantity=2, unit="т", source_text="  БАЛКА 25к1 ; 2т  ",
        )])
        client = Mock()
        client.beta.chat.completions.parse.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(refusal=None, parsed=extraction))]
        )
        openai.return_value = client

        result = AIService().parse_request("Балка 25К1 — 2 т")

        self.assertEqual(result.items[0].source_text, "  БАЛКА 25к1 ; 2т  ")

    @patch("ai.load_dotenv")
    @patch("ai.OpenAI")
    def test_rejects_item_source_text_not_present_in_user_input(
        self, openai, _load_dotenv
    ):
        extraction = PurchaseRequestExtraction(items=[RequestItem(
            product="балка", product_group="beam", size_designation="25К1",
            source_text="Балка 30К1 — 2 т",
        )])
        client = Mock()
        client.beta.chat.completions.parse.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(refusal=None, parsed=extraction))]
        )
        openai.return_value = client

        with self.assertRaisesRegex(ValueError, "не найден в тексте заявки"):
            AIService().parse_request("Балка 25К1 — 2 т")

    @patch("ai.load_dotenv")
    @patch("ai.OpenAI")
    def test_keeps_explicit_request_level_fields(self, openai, _load_dotenv):
        extraction = PurchaseRequestExtraction(
            city="Москва", delivery="самовывоз", deadline="до пятницы",
            customer="ООО Пример", comment="Позвонить перед отгрузкой",
        )
        client = Mock()
        client.beta.chat.completions.parse.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(refusal=None, parsed=extraction))]
        )
        openai.return_value = client

        result = AIService().parse_request(
            "В Москве самовывоз до пятницы, ООО Пример. Позвонить перед отгрузкой."
        )

        self.assertEqual(result.city, "Москва")
        self.assertEqual(result.delivery, "самовывоз")
        self.assertEqual(result.deadline, "до пятницы")
        self.assertEqual(result.customer, "ООО Пример")
        self.assertEqual(result.comment, "Позвонить перед отгрузкой")

    def test_extraction_schema_has_no_python_validation_fields(self):
        properties = PurchaseRequestExtraction.model_json_schema()["properties"]
        for field in ("missing_fields", "clarification_questions", "warnings", "is_valid"):
            self.assertNotIn(field, properties)

    @patch("ai.load_dotenv")
    @patch("ai.OpenAI")
    def test_parse_request_raises_for_refusal(self, openai, _load_dotenv):
        client = Mock()
        message = SimpleNamespace(refusal="запрос отклонён", parsed=None)
        client.beta.chat.completions.parse.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=message)]
        )
        openai.return_value = client

        with self.assertRaisesRegex(ValueError, "Модель отказалась"):
            AIService().parse_request("заявка")

    @patch("ai.load_dotenv")
    @patch("ai.OpenAI")
    def test_parse_request_raises_when_parsed_response_is_missing(
        self, openai, _load_dotenv
    ):
        client = Mock()
        message = SimpleNamespace(refusal=None, parsed=None)
        client.beta.chat.completions.parse.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=message)]
        )
        openai.return_value = client

        with self.assertRaisesRegex(ValueError, "структурированный ответ"):
            AIService().parse_request("заявка")

    @patch("ai.load_dotenv")
    @patch("ai.OpenAI")
    def test_parse_request_preserves_api_error(self, openai, _load_dotenv):
        client = Mock()
        error = RuntimeError("upstream API unavailable")
        client.beta.chat.completions.parse.side_effect = error
        openai.return_value = client

        with self.assertRaises(RuntimeError) as caught:
            AIService().parse_request("заявка")

        self.assertIs(caught.exception, error)


if __name__ == "__main__":
    unittest.main()
