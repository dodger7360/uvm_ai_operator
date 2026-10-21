"""Release boot smoke: current entrypoint/queue/SQLite; no external API calls."""
from threading import Event
from decimal import Decimal
from unittest.mock import Mock
import json

from database import Database
from repositories.access_repository import AccessRepository
from repositories.telegram_repository import TelegramRepository
from steel_agent_v2 import Repository
from steel_agent_v2.queue import DurableQueue
from steel_agent_v2.transport import OpenAITransport
from test_steel_agent_v2 import Fake, SOURCE, proposal
import telegram_bot as bot


def schema(database):
    with database.connection() as connection:
        return [tuple(row) for row in connection.execute(
            'SELECT type,name,sql FROM sqlite_master ORDER BY type,name')]


def test_release_boot_with_isolated_sqlite_preserves_schema(tmp_path, monkeypatch):
    path = tmp_path / 'access-and-offset.sqlite3'
    database = Database(path)
    mirror = TelegramRepository(database)
    access = AccessRepository(database)
    access.bootstrap_configured_admin(1)
    # These opaque historical payloads must never be parsed by the v2 worker.
    with database.connection() as connection:
        connection.execute('INSERT INTO telegram_active VALUES(1,?)', ('opaque old active',))
        connection.execute('INSERT INTO telegram_history(user_id,request_id,request_json) VALUES(1,?,?)',
                           ('old', 'opaque old history'))
        rights = [tuple(row) for row in connection.execute('SELECT * FROM telegram_access_user')]
    v2_database = Database(tmp_path / 'v2.sqlite3')
    Repository(v2_database)
    DurableQueue(v2_database)
    original_schema, v2_schema = schema(database), schema(v2_database)
    (tmp_path / 'telegram-handler.json').write_text(
        json.dumps({'handler':'v2', 'database':'v2.sqlite3'}), encoding='utf-8')
    fake = Fake(proposal())
    monkeypatch.setattr(OpenAITransport, 'from_environment', classmethod(lambda cls, **kwargs: fake))
    monkeypatch.setenv('OPERATOR_DB', str(path))
    monkeypatch.setenv('ADMIN_TELEGRAM_ID', '1')
    api = Mock()
    replies, offsets = [], []

    def call(method, **payload):
        if method == 'getMe': return {'username':'offline_test_bot'}
        if method == 'getWebhookInfo': return {}
        if method == 'getUpdates':
            offsets.append(payload['offset'])
            if len(offsets) == 1:
                assert payload['offset'] == 0
                return [{'update_id':1, 'message':{'chat':{'type':'private','id':1},
                    'from':{'id':1,'first_name':'Offline'}, 'text':SOURCE}}]
            Event().wait(.01)
            return []
        if method == 'sendMessage':
            assert payload['parse_mode'] == 'HTML'
            replies.append(payload['text'])
            (tmp_path / 'telegram.maintenance').touch()
            return {'message_id':7}
        return {}

    api.call.side_effect = call
    monkeypatch.setattr(bot, 'TelegramAPI', lambda _:api)
    assert bot.run_bot(tmp_path, 'offline-placeholder') == 0
    assert len(replies) == 1 and 'С245' in replies[0] and 'Ст3' in replies[0]
    current = Repository(v2_database).load(1)
    assert current.original_text == SOURCE and current.revision == 1 and len(current.items) == 2
    assert [(item.quantity,item.unit) for item in current.items] == [(Decimal('1.25'),'т'),(Decimal('750'),'кг')]
    assert '1,25 т' in replies[0] and '0,75 т' in replies[0]
    assert len(fake.calls) == 1 and not fake.search_calls
    assert mirror.get_offset() == 2 and access.admin_id() == 1
    assert schema(database) == original_schema and schema(v2_database) == v2_schema
    with database.connection() as connection:
        assert connection.execute('SELECT request_json FROM telegram_active').fetchone()[0] == 'opaque old active'
        assert connection.execute('SELECT request_json FROM telegram_history').fetchone()[0] == 'opaque old history'
        assert [tuple(row) for row in connection.execute('SELECT * FROM telegram_access_user')] == rights
        assert connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    queue = DurableQueue(v2_database)
    assert queue.cursor() == 2 and queue.job(1)['state'] == 'delivered'
    with v2_database.connection() as connection:
        assert connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert connection.execute('SELECT count(*) FROM steel_v2_version').fetchone()[0] == 1
        assert connection.execute('SELECT count(*) FROM steel_v2_turn').fetchone()[0] == 1
        outbox=connection.execute('SELECT state,message_id,payload FROM steel_v2_outbox').fetchone()
        assert outbox['state']=='delivered' and outbox['message_id']==7
        assert json.loads(outbox['payload'])['text']==replies[0]
    assert not (tmp_path / 'telegram.ready').exists()
