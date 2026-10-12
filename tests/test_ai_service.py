import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ai import AIService, SYSTEM_PROMPT
from schemas import (
    PurchaseRequest,
    PurchaseRequestExtraction,
    RequestItem,
    SharedAttributeEvidence,
)


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
    def test_preserves_length_and_wall_requirements_from_model(
        self, openai, _load_dotenv
    ):
        items = [
            RequestItem(
                product="Арматура", product_group="rebar",
                size_designation="12", grade="А500С", diameter_mm=12,
                length_requirement="11,7", quantity=20, unit="т",
                source_text="Арматура 12 А500С 11,7 — 20 т",
            ),
            RequestItem(
                product="Труба профильная", product_group="profile_pipe",
                size_designation="40×40", width_mm=40, height_mm=40,
                wall_thickness_requirement="стенка минимально возможная",
                quantity=100, unit="м",
                source_text="Труба профильная 40×40, стенка минимально возможная — 100 м",
            ),
            RequestItem(
                product="Труба", product_group="round_pipe",
                size_designation="76×3,5 НД", diameter_mm=76,
                wall_thickness_mm=3.5, length_type="unmeasured",
                length_requirement="НД", quantity=4, unit="т",
                source_text="Труба 76×3,5 НД — 4 т",
            ),
            RequestItem(
                product="Труба", length_type="measured",
                length_requirement="мерная длина 6 м", length_mm=6000,
                source_text="Труба, мерная длина 6 м",
            ),
            RequestItem(
                product="Труба", length_type="other",
                length_requirement="кратная длина",
                source_text="Труба, кратная длина",
            ),
        ]
        extraction = PurchaseRequestExtraction(items=items)
        client = Mock()
        client.beta.chat.completions.parse.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(
                refusal=None, parsed=extraction,
            ))]
        )
        openai.return_value = client
        source = "\n".join(item.source_text for item in items)

        result = AIService().parse_request(source)

        self.assertEqual(result.items, items)
        self.assertEqual(result.items[2].length_type, "unmeasured")
        self.assertEqual(result.items[2].length_requirement, "НД")
        self.assertEqual(
            result.items[1].wall_thickness_requirement,
            "стенка минимально возможная",
        )
        self.assertIsNone(result.items[1].wall_thickness_mm)
        self.assertIsNone(result.items[1].length_mm)
        self.assertIsNone(result.items[0].length_mm)
        self.assertIsNone(result.items[0].length_type)
        client.beta.chat.completions.parse.assert_called_once()
        sent = client.beta.chat.completions.parse.call_args.kwargs
        self.assertEqual(sent["messages"][0]["content"], SYSTEM_PROMPT)
        self.assertEqual(sent["messages"][1]["content"], source)
        self.assertEqual(sent["response_format"], PurchaseRequestExtraction)

    @patch("ai.load_dotenv")
    @patch("ai.OpenAI")
    def test_accepts_legacy_response_without_new_fields(
        self, openai, _load_dotenv
    ):
        source = "Лист 6×1500×6000 — 2 шт"
        extraction = PurchaseRequestExtraction.model_validate({
            "items": [{
                "product": "Лист", "product_group": "sheet",
                "size_designation": "6×1500×6000", "thickness_mm": 6,
                "width_mm": 1500, "length_mm": 6000,
                "quantity": 2, "unit": "шт", "source_text": source,
            }],
        })
        client = Mock()
        client.beta.chat.completions.parse.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(
                refusal=None, parsed=extraction,
            ))]
        )
        openai.return_value = client

        result = AIService().parse_request(source)

        self.assertEqual(result.items, extraction.items)
        self.assertEqual(result.items[0].length_mm, 6000)
        self.assertIsNone(result.items[0].length_type)
        self.assertIsNone(result.items[0].length_requirement)
        self.assertIsNone(result.items[0].wall_thickness_requirement)
        client.beta.chat.completions.parse.assert_called_once()

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
        for field in (
            "missing_fields", "clarification_questions", "warnings", "is_valid",
            "confirmed_shared_attributes",
        ):
            self.assertNotIn(field, properties)
        self.assertIn("shared_attributes", properties)

    @patch("ai.load_dotenv")
    @patch("ai.OpenAI")
    def test_rejects_shared_attribute_evidence_not_present_in_raw_request(
        self, openai, _load_dotenv
    ):
        extraction = PurchaseRequestExtraction(shared_attributes=[
            SharedAttributeEvidence(
                attribute="grade", value="ст3", source_text="сталь ст3",
                applies_to=[1],
            ),
        ])
        client = Mock()
        client.beta.chat.completions.parse.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(
                refusal=None, parsed=extraction,
            ))]
        )
        openai.return_value = client

        with self.assertRaisesRegex(ValueError, "общего атрибута"):
            AIService().parse_request("Труба 40х40х2\nст3")

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
