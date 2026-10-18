"""Historical legacy effort regression; not the active v2 configuration."""
import os
from unittest.mock import Mock

import pytest

from ai import AIService
from schemas import PurchaseRequestExtraction
from .reference import telegram_bot
bot=telegram_bot()
from work_assistant import WorkAssistantService


@pytest.fixture
def deployment_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv('OPENAI_MODEL', 'gpt-6-luna')
    monkeypatch.setenv('OPENAI_REASONING_EFFORT', 'low')
    monkeypatch.setenv('OPENAI_FALLBACK_REASONING_EFFORT', 'medium')
    api = Mock()
    # Stop assembly before any database or real AI client is opened.
    api.call.side_effect = [{'username': 'offline'}, {'url': 'https://offline.invalid'}]
    database = Mock()
    monkeypatch.setattr(bot, 'TelegramAPI', lambda token: api)
    monkeypatch.setattr(bot, 'Database', database)
    assert bot.run_bot(tmp_path, 'offline-placeholder') == 4
    database.assert_not_called()


def test_startup_uses_high_for_primary_and_retry(deployment_runtime):
    assert os.environ['OPENAI_MODEL'] == 'gpt-6-luna'
    assert os.environ['OPENAI_REASONING_EFFORT'] == 'high'
    assert os.environ['OPENAI_FALLBACK_REASONING_EFFORT'] == 'high'


def test_primary_and_retry_sdk_payloads_use_high(deployment_runtime):
    ai = object.__new__(AIService)
    ai._client = Mock()
    ai._invoke('extraction', PurchaseRequestExtraction, [])
    ai._invoke('extraction', PurchaseRequestExtraction, [],
               reasoning_effort=os.environ['OPENAI_FALLBACK_REASONING_EFFORT'])
    calls = ai._client.beta.chat.completions.parse.call_args_list
    assert [call.kwargs['reasoning_effort'] for call in calls] == ['high', 'high']
    assert all(call.kwargs['model'] == 'gpt-6-luna' for call in calls)


def test_work_assistant_sdk_payload_uses_high():
    client = Mock()
    client.with_options.return_value = client
    client.responses.create.return_value = {
        'status': 'completed', 'output_text': 'Офлайн-ответ', 'output': []}
    assistant = WorkAssistantService(client)
    assert assistant.answer('Что такое прокат?', user_id=1).text == 'Офлайн-ответ'
    payload = client.responses.create.call_args.kwargs
    assert payload['model'] == 'gpt-6-luna'
    assert payload['reasoning'] == {'effort': 'high'}
