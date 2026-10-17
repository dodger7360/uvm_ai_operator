"""Prelaunch fault and worker integration checks; all IO is isolated/faked."""
from unittest.mock import Mock
import json

import pytest
import telegram_bot as bot
from test_steel_agent_v2 import SOURCE, Fake, proposal, edit, set_op
from database import Database
from steel_agent_v2 import SteelAgent, Repository
from steel_agent_v2.runtime import selection, connect
from repositories.telegram_repository import TelegramRepository
from repositories.access_repository import AccessRepository
from telegram_access import TelegramAccess
from steel_agent_v2.transport import OpenAITransport


def test_failed_presentation_then_readonly_repair_cannot_commit_edit(tmp_path, monkeypatch):
    import steel_agent_v2.agent as module
    repo = Repository(Database(tmp_path / 'v2.sqlite3'))
    fake = Fake(proposal())
    service = SteelAgent(fake, repo)
    service.handle(1, SOURCE, event_id='create')
    before = repo.load(1).model_dump_json()
    fake.plans = [edit(repo.load(1), [set_op()]), dict(intent='clarify', answer='Уточните команду')]
    monkeypatch.setattr(module, 'render', Mock(side_effect=ValueError('presentation failed')))
    reply = service.handle(1, 'Ст3', event_id='edit')
    assert reply.intent == 'clarify'
    assert repo.load(1).model_dump_json() == before
    assert repo.version(1, repo.load(1).id, 2) is None


def configure(root, value):
    (root / 'telegram-handler.json').write_text(json.dumps(value), encoding='utf-8')


def test_selection_defaults_legacy_and_persists_without_environment(tmp_path):
    assert selection(tmp_path) == ('legacy', None)
    configure(tmp_path, {'handler':'v2', 'database':'steel-agent-v2.sqlite3'})
    assert selection(tmp_path) == ('v2', tmp_path / 'steel-agent-v2.sqlite3')
    configure(tmp_path, {'handler':'legacy'})
    assert selection(tmp_path) == ('legacy', None)


@pytest.mark.parametrize('config', [[], {}, {'handler':'typo'}, {'handler':'v2'},
    {'handler':'v2','database':''}, {'handler':'v2','database':True},
    {'handler':'legacy','database':'operator.sqlite3'}, {'handler':'v2','database':'x','token':'forbidden'}])
def test_invalid_selection_stops_before_network_and_database(tmp_path, monkeypatch, config):
    configure(tmp_path, config)
    api = Mock()
    db = Mock(side_effect=AssertionError('no database writes'))
    monkeypatch.setattr(bot, 'TelegramAPI', lambda _:api)
    monkeypatch.setattr(bot, 'Database', db)
    assert bot.run_bot(tmp_path, 'offline') == 7
    api.call.assert_not_called()
    db.assert_not_called()
    assert not (tmp_path / 'telegram.ready').exists()


def test_malformed_selection_never_falls_back_to_legacy(tmp_path, monkeypatch):
    (tmp_path / 'telegram-handler.json').write_text('{broken', encoding='utf-8')
    api = Mock()
    monkeypatch.setattr(bot, 'TelegramAPI', lambda _:api)
    assert bot.run_bot(tmp_path, 'offline') == 7
    api.call.assert_not_called()


def test_v2_worker_dispatch_replay_edit_offset_and_legacy_isolation(tmp_path, monkeypatch):
    legacy_path = tmp_path / 'operator.sqlite3'
    legacy = TelegramRepository(Database(legacy_path))
    permissions = AccessRepository(legacy.database)
    permissions.bootstrap_configured_admin(1)
    legacy.set_offset(41)
    with legacy.database.connection() as c:
        c.execute('INSERT INTO telegram_active VALUES(1,?)', ('legacy opaque payload',))
        c.execute('INSERT INTO telegram_history(user_id,request_id,request_json) VALUES(1,?,?)', ('old','legacy history'))
        old_rights = [tuple(r) for r in c.execute('SELECT * FROM telegram_access_user')]
    configure(tmp_path, {'handler':'v2','database':'steel-agent-v2.sqlite3'})
    fake = Fake(proposal(), lambda ctx, _:dict(intent='edit_request',
        expected_request_id=ctx['active_request']['id'], expected_revision=ctx['active_request']['revision'], operations=[set_op()]))
    monkeypatch.setattr(OpenAITransport, 'from_environment', classmethod(lambda cls:fake))
    monkeypatch.setattr(bot, 'AIService', Mock(side_effect=AssertionError('legacy AI must not be constructed')))
    monkeypatch.setenv('OPERATOR_DB', str(legacy_path))
    monkeypatch.setenv('OPENAI_REASONING_EFFORT','medium')
    monkeypatch.delenv('ADMIN_TELEGRAM_ID', raising=False)
    update = lambda n,text:dict(update_id=n, message={'chat':{'type':'private','id':1},'from':{'id':1},'text':text})
    polls=[]; sent=[]
    def call(method, **payload):
        if method=='getMe':return {'username':'offline'}
        if method=='getWebhookInfo':return {}
        if method=='getUpdates':
            polls.append(payload['offset'])
            assert len(polls)==1
            return [update(41,SOURCE),update(41,SOURCE),update(42,'Ст3')]
        if method=='sendMessage':
            sent.append(payload['text'])
            if len(sent)==3:(tmp_path/'telegram.maintenance').touch()
        return {}
    api=Mock();api.call.side_effect=call
    monkeypatch.setattr(bot, 'TelegramAPI', lambda _:api)
    assert bot.run_bot(tmp_path,'offline')==0
    assert polls==[41] and legacy.get_offset()==43
    assert len(fake.calls)==2 and len(sent)==3 and sent[0]==sent[1]
    assert fake.calls[0][0]['active_request'] is None
    assert Repository(Database(tmp_path/'steel-agent-v2.sqlite3')).load(1).revision==2
    assert bot.os.environ['OPENAI_REASONING_EFFORT']=='medium'
    with legacy.database.connection() as c:
        assert c.execute('SELECT request_json FROM telegram_active').fetchone()[0]=='legacy opaque payload'
        assert c.execute('SELECT request_json FROM telegram_history').fetchone()[0]=='legacy history'
        assert [tuple(r) for r in c.execute('SELECT * FROM telegram_access_user')]==old_rights
        assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    with Database(tmp_path/'steel-agent-v2.sqlite3').connection() as c:
        assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert c.execute('SELECT count(*) FROM steel_v2_turn').fetchone()[0]==2
    assert not (tmp_path/'telegram.ready').exists()


def test_runtime_legacy_db_path_is_rejected_without_mutating_data(tmp_path, monkeypatch):
    legacy = TelegramRepository(Database(tmp_path/'operator.sqlite3'))
    access = TelegramAccess(AccessRepository(legacy.database))
    before = legacy.database.path.read_bytes()
    monkeypatch.setattr(OpenAITransport, 'from_environment', classmethod(lambda cls:Fake()))
    with pytest.raises(ValueError):
        connect(legacy.database.path, access)
    assert legacy.database.path.read_bytes()==before


def test_sqlite_snapshot_is_reopenable_identical_and_does_not_overwrite(tmp_path):
    import sqlite3
    from scripts.v2_sqlite_snapshot import snapshot, metadata
    source=tmp_path/'source.sqlite3'; destination=tmp_path/'copy.sqlite3'; manifest=tmp_path/'manifest.json'
    with sqlite3.connect(source) as c:
        c.execute('CREATE TABLE history(id INTEGER PRIMARY KEY, text TEXT, raw BLOB)')
        c.execute('INSERT INTO history(text,raw) VALUES(?,?)', ('PRIVATE_REQUEST', b'PRIVATE_BLOB_CONTENT'))
        c.commit()
        expected=metadata(c)
    before=source.read_bytes()
    snapshot(source,destination,manifest)
    with sqlite3.connect(destination) as c:assert metadata(c)==expected
    assert source.read_bytes()==before
    assert 'PRIVATE_REQUEST' not in manifest.read_text() and b'PRIVATE_BLOB_CONTENT'.hex() not in manifest.read_text()
    with pytest.raises(FileExistsError):snapshot(source,destination,manifest)


def test_sqlite_snapshot_rejects_missing_source_without_creating_database(tmp_path):
    from scripts.v2_sqlite_snapshot import snapshot
    with pytest.raises(ValueError):snapshot(tmp_path/'missing',tmp_path/'copy',tmp_path/'manifest')
    assert not list(tmp_path.iterdir())


def test_reapproval_restores_preserved_v2_context_for_same_telegram_id(tmp_path):
    """Prompt 3 accepts preserved context after reapproval; no legacy epoch parity."""
    from steel_agent_v2.telegram import build_service
    legacy=TelegramRepository(Database(tmp_path/'legacy.sqlite3'))
    rights=AccessRepository(legacy.database)
    rights.bootstrap_configured_admin(1)
    rights.request_access({'id':2,'first_name':'Offline'})
    rights.transition(1,2,'approve',rights.get(2)['revision'],notify=lambda _:True)
    service=build_service(Fake(proposal()),database_path=tmp_path/'v2.sqlite3',access=TelegramAccess(rights))
    service.handle(2,SOURCE,event_id='old')
    before=service.repository.load(2)
    rights.transition(1,2,'revoke',rights.get(2)['revision'])
    with pytest.raises(PermissionError):service.repository.load(2)
    rights.transition(1,2,'approve',rights.get(2)['revision'],notify=lambda _:True)
    assert service.repository.load(2)==before
