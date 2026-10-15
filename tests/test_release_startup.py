"""Release boot smoke: real assembly/SQLite/handler; no external API calls."""
from unittest.mock import Mock

from database import Database
from repositories.access_repository import AccessRepository
from repositories.telegram_repository import TelegramRepository
from schemas import PurchaseRequest, RequestItem
import telegram_bot as bot


def test_release_boot_with_isolated_sqlite_preserves_schema(tmp_path, monkeypatch):
    path = tmp_path / 'isolated.sqlite3'
    database = Database(path)
    repository = TelegramRepository(database)
    access = AccessRepository(database)
    access.bootstrap_configured_admin(1)
    with database.connection() as connection:
        schema = [tuple(row) for row in connection.execute(
            'SELECT type,name,sql FROM sqlite_master ORDER BY type,name')]
    source = 'Уголок 50х50х5 Ст3 ГОСТ 8509-93 — 2 т'
    ai = Mock()
    ai.parse_request.return_value = PurchaseRequest(items=[RequestItem(
        product='Уголок', product_group='angle', size_designation='50х50х5', source_text=source)])
    client = Mock()
    client.with_options.return_value = client
    ai.assistant_transport = client
    monkeypatch.setattr(bot, 'AIService', lambda: ai)
    monkeypatch.setenv('OPERATOR_DB', str(path))
    monkeypatch.setenv('ADMIN_TELEGRAM_ID', '1')
    monkeypatch.setenv('OPENAI_MODEL', 'gpt-6-luna')
    monkeypatch.setenv('OPENAI_REASONING_EFFORT', 'low')
    api = Mock()
    replies = []

    def call(method, **payload):
        if method == 'getMe':
            return {'username': 'offline_test_bot'}
        if method == 'getWebhookInfo':
            return {}
        if method == 'getUpdates':
            assert payload['offset'] == 0
            return [{'update_id': 1, 'message': {
                'chat': {'type': 'private', 'id': 1},
                'from': {'id': 1, 'first_name': 'Offline'}, 'text': source}}]
        if method == 'sendMessage':
            replies.append(payload['text'])
            (tmp_path / 'telegram.maintenance').touch()
        return {}

    api.call.side_effect = call
    monkeypatch.setattr(bot, 'TelegramAPI', lambda _: api)
    assert bot.run_bot(tmp_path, 'offline-placeholder') == 0
    assert replies == ['Уголок 50х50х5 ст3 ГОСТ 8509-93 — 2 т']
    assert repository.load(1).source_text == source
    assert repository.get_offset() == 2
    assert access.admin_id() == 1
    assert not (tmp_path / 'telegram.ready').exists()
    client.responses.create.assert_not_called()
    ai.parse_request.assert_called_once_with(source)
    with database.connection() as connection:
        current_schema = [tuple(row) for row in connection.execute(
            'SELECT type,name,sql FROM sqlite_master ORDER BY type,name')]
        additions = {'normalization_rule', 'normalization_rule_event', 'sqlite_autoindex_normalization_rule_1'}
        assert [row for row in current_schema if row[1] not in additions] == schema
        assert {row[1] for row in current_schema} - {row[1] for row in schema} == additions
        assert connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
