"""Stage-15 reproductions, updated to acceptance regressions for stage 16.

Supplied plans prove Python behavior, not the unknown live rejected GPT payload.
No production IO or network. Original failures are recorded in report 015.
"""
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import telegram_bot as bot
from database import Database
from repositories.telegram_repository import TelegramRepository
from steel_agent_v2 import Repository, SteelAgent
from steel_agent_v2.engine import totals
from steel_agent_v2.telegram import handle_update
from steel_agent_v2.transport import OpenAITransport
from test_steel_agent_v2 import Fake, SOURCE, proposal


BEAMS = (Path(__file__).parent/'fixtures'/'steel_v2_015_beams.txt').read_text(encoding='utf-8')


def beams_plan():
    records, items, groups = [], [], []
    offset = 0
    for n, line in enumerate(BEAMS.splitlines(keepends=True), 1):
        text = line.rstrip('\r\n')
        profile = text.split()[1]
        standard = 'Р 57837-2017' if 'Р 57837-2017' in text else '35087-2024'
        quantity = text.rsplit(' - ', 1)[1][:-1]
        sid, iid = f'r{n}', f'i{n}'
        records.append(dict(id=sid, kind='product', start=offset, end=offset+len(text)))
        items.append(dict(id=iid, source_id=sid, label='Балка', quantity=quantity.replace(',', '.'),
            unit='т', quantity_text=quantity, unit_text='т', characteristics=[
                dict(name=name, value=value, raw=value, source_ids=[sid])
                for name, value in [('profile', profile), ('grade', 'С355-5'), ('standard', standard)]]))
        groups.append(dict(id=f'g{n}', label='Балка '+profile, item_ids=[iid], rationale='Отдельная позиция'))
        offset += len(line)
    return dict(intent='create_request', extraction=dict(records=records, items=items, groups=groups))


def whitespace_plan():
    plan = beams_plan()
    newline = BEAMS.index('\n')
    plan['extraction']['records'].insert(1, dict(id='s2', kind='note', start=newline, end=newline+1))
    return plan


def unsafe_plan():
    plan=whitespace_plan()
    plan['extraction']['records'][1]['kind']='unresolved'
    return plan


def adapter(tmp_path, plans):
    repo = Repository(Database(tmp_path/'v2.sqlite3'))
    fake = Fake(*plans)
    service = SteelAgent(fake, repo)
    api = Mock()
    access = SimpleNamespace(route=lambda *args: False, allowed=lambda user: True)
    def send(number, text, user=1):
        handle_update(dict(update_id=number, message=dict(text=text,
            chat=dict(id=user, type='private'), **{'from':dict(id=user)})), service, api, access)
    return repo, fake, api, send


def test_literal_four_beams_pass_current_pipeline(tmp_path):
    repo, fake, api, send = adapter(tmp_path, [beams_plan()])
    send(1, BEAMS)
    state = repo.load(1)
    assert state.original_text == BEAMS and len(state.items) == len(state.originals) == 4
    assert [i.quantity for i in state.items] == list(map(Decimal, ['10.700','186.698','127.097','56.669']))
    assert totals(state.items) == ({'т':'381.164'}, [])
    assert [i.characteristics[0].value for i in state.items] == ['20Ш2','30Ш2','40Ш2','45Ш2']
    assert all(i.characteristics[1].value == 'С355-5' for i in state.items)
    assert [i.characteristics[2].value for i in state.items] == ['35087-2024','35087-2024','Р 57837-2017','Р 57837-2017']
    assert len(fake.calls) == 1


def test_safe_whitespace_record_is_normalized_without_paid_repair(tmp_path):
    repo, fake, api, send = adapter(tmp_path, [whitespace_plan(), beams_plan()])
    send(1, BEAMS)
    assert len(fake.calls)==1
    assert repo.load(1).original_text == BEAMS
    assert totals(repo.load(1).items) == ({'т':'381.164'}, [])


def test_persistent_rejection_preserves_original_and_previous_state_without_code_leak(tmp_path):
    repo, fake, api, send = adapter(tmp_path, [proposal(), unsafe_plan(), unsafe_plan()])
    send(1, SOURCE)
    before = repo.load(1).model_dump_json()
    send(2, BEAMS)
    assert repo.load(1).model_dump_json() == before
    assert 'empty_source_range:s2' not in api.call.call_args.kwargs['text']
    assert fake.calls[2][1]['diagnostics']==['empty_source_range:s2']
    with repo.database.connection() as c:
        row = c.execute("SELECT message,reply FROM steel_v2_turn WHERE event_id='telegram:2'").fetchone()
        assert row['message'] == BEAMS
        assert 'failed' in row['reply']
        assert c.execute('SELECT count(*) FROM steel_v2_inflight').fetchone()[0] == 0
        assert c.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


@pytest.mark.parametrize('damage', ['uncovered', 'duplicate_item', 'bad_product_range'])
def test_genuine_source_damage_is_not_silently_removed(tmp_path, damage):
    bad = beams_plan()
    if damage == 'uncovered':
        bad['extraction']['records'].pop()
        bad['extraction']['items'].pop()
        bad['extraction']['groups'].pop()
    elif damage == 'duplicate_item':
        item = deepcopy(bad['extraction']['items'][0]); item['id'] = 'duplicate'
        bad['extraction']['items'].append(item)
        bad['extraction']['groups'][0]['item_ids'].append('duplicate')
    else:
        record = bad['extraction']['records'][1]
        record.update(start=BEAMS.index('\n'), end=BEAMS.index('\n')+1)
    repo, fake, api, send = adapter(tmp_path, [bad, bad])
    send(1, BEAMS)
    assert repo.load(1) is None and len(fake.calls) == 2
    with repo.database.connection() as c:
        assert c.execute('SELECT message FROM steel_v2_turn').fetchone()[0] == BEAMS


def test_rejected_beams_followup_add_can_reproduce_schema_value_error(tmp_path):
    invalid_add = dict(intent='edit_request', operations=[])
    repo, fake, api, send = adapter(tmp_path, [proposal(), unsafe_plan(), unsafe_plan(),
        dict(intent='answer_question', answer='Список балок не вошёл в заявку.'), invalid_add, invalid_add,
        dict(intent='clarify', answer='Добавление новых позиций к текущей заявке пока не поддерживается. Сохраните список отдельно.')])
    send(1, SOURCE)
    before = repo.load(1).model_dump_json()
    send(2, BEAMS)
    send(3, 'А балки?')
    send(4, 'Добавь')
    assert 'schema:' not in api.call.call_args.kwargs['text']
    # Two invalid plans do not prove an append intent; no unrelated fallback claim.
    assert 'не поддерживается' not in api.call.call_args.kwargs['text']
    assert fake.calls[5][1]['diagnostics']==['schema::value_error']
    assert any(row['content'] == BEAMS for row in fake.calls[4][0]['history'])
    assert repo.load(1).model_dump_json() == before
    send(5, 'Добавь эти позиции')
    assert 'не поддерживается' in api.call.call_args.kwargs['text']
    assert repo.load(1).model_dump_json() == before
    # Existing clarify intent CAN express the safe limitation, without adding an operation.
    send(5, 'Как безопасно продолжить?')
    assert 'schema:' not in api.call.call_args.kwargs['text']
    assert repo.load(1).model_dump_json() == before
    reopened = Repository(Database(tmp_path/'v2.sqlite3'))
    assert reopened.load(1).model_dump_json() == before
    assert reopened.load(2) is None


def test_crashed_claim_is_durable_but_not_automatically_replayed(tmp_path):
    path = tmp_path/'v2.sqlite3'
    repo = Repository(Database(path))
    repo.begin(1, 'telegram:1', BEAMS)
    reopened = Repository(Database(path))
    fake = Fake(beams_plan())
    reply = SteelAgent(fake, reopened).handle(1, BEAMS, event_id='telegram:1')
    assert reply.status == 'busy' and not fake.calls
    assert reopened.load(1) is None
    with reopened.database.connection() as c:
        assert c.execute('SELECT message FROM steel_v2_inflight').fetchone()[0] == BEAMS


def test_delivery_failure_reuses_committed_reply_without_second_mutation(tmp_path):
    repo, fake, api, send = adapter(tmp_path, [beams_plan()])
    def fail_delivery(method, **payload):
        if method == 'sendMessage':raise bot.TelegramError(503)
        return {}
    api.call.side_effect = fail_delivery
    with pytest.raises(bot.TelegramError):send(1, BEAMS)
    before = repo.load(1).model_dump_json()
    api.call.side_effect = None
    send(1, BEAMS)
    assert repo.load(1).model_dump_json() == before and len(fake.calls) == 1
    with repo.database.connection() as c:
        assert c.execute('SELECT count(*) FROM steel_v2_turn').fetchone()[0] == 1
        assert c.execute('SELECT count(*) FROM steel_v2_version').fetchone()[0] == 1
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert tables == {'steel_v2_active','steel_v2_version','steel_v2_turn','steel_v2_inflight'}
    # Current repository has no independent durable delivery/chunk receipt state.


def test_actual_poll_loop_allows_second_user_before_first_finishes(tmp_path, monkeypatch):
    """Ported from the stage-15 red characterization to the required concurrent behavior."""
    entered_a, entered_b, release_a = Event(), Event(), Event()
    legacy = TelegramRepository(Database(tmp_path/'operator.sqlite3'))
    from repositories.access_repository import AccessRepository
    permissions=AccessRepository(legacy.database)
    permissions.bootstrap_configured_admin(1)
    permissions.request_access({'id':2})
    permissions.transition(1,2,'approve',1,notify=lambda _:True)
    legacy.set_offset(41)
    (tmp_path/'telegram-handler.json').write_text('{"handler":"v2","database":"v2.sqlite3"}', encoding='utf-8')
    monkeypatch.setenv('OPERATOR_DB', str(tmp_path/'operator.sqlite3'))
    monkeypatch.delenv('ADMIN_TELEGRAM_ID', raising=False)
    class Controlled:
        def plan(self, context, **kwargs):
            if context['message'] == 'A':
                entered_a.set()
                assert release_a.wait(5)
            else:
                entered_b.set()
            return dict(intent='free_chat', answer='Готово')
    monkeypatch.setattr(OpenAITransport, 'from_environment', classmethod(lambda cls, **kwargs: Controlled()))
    monkeypatch.setattr(bot, 'TelegramAccess', lambda repository: SimpleNamespace(
        repository=repository, route=lambda *a: False, allowed=lambda u: True))
    def call(method, **payload):
        if method == 'getMe':return {'username':'offline'}
        if method == 'getUpdates':
            return [dict(update_id=40+n, message=dict(text=text, chat=dict(id=n, type='private'),
                    **{'from':dict(id=n)})) for n, text in [(1,'A'),(2,'B')]]
        if method == 'sendMessage' and payload['chat_id'] == 2:
            (tmp_path/'telegram.maintenance').touch()
        return {'message_id':payload.get('chat_id',1)}
    api = Mock(); api.call.side_effect = call
    monkeypatch.setattr(bot, 'TelegramAPI', lambda _:api)
    outcome = []
    def worker():
        try:outcome.append(bot.run_bot(tmp_path, 'offline'))
        except BaseException as error:outcome.append(error)
    thread = Thread(target=worker)
    thread.start()
    try:
        assert entered_a.wait(5)
        assert entered_b.wait(3)
        assert not release_a.is_set()
    finally:
        (tmp_path/'telegram.maintenance').touch()
        release_a.set()
        thread.join(5)
    assert not thread.is_alive() and outcome == [0]
    assert entered_b.is_set() and legacy.get_offset() == 43
