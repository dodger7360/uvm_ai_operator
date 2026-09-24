import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ai import AIService, SYSTEM_PROMPT
from schemas import PurchaseRequest


class AIServiceTests(unittest.TestCase):
    def setUp(self):
        self.request = PurchaseRequest()
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

        self.assertIs(result, self.request)
        load_dotenv.assert_called_once_with()
        openai.assert_called_once()
        client.beta.chat.completions.parse.assert_called_once_with(
            model="gpt-5.6-luna",
            response_format=PurchaseRequest,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": "арматура 12"},
            ],
        )

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


if __name__ == "__main__":
    unittest.main()
