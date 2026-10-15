"""Dialog routing and persistence: isolated SQLite, no Telegram or paid API."""
from decimal import Decimal
from unittest.mock import Mock
import json
import pytest
from database import Database
from repositories.telegram_repository import TelegramRepository
from schemas import RequestItem, PurchaseRequest
from telegram_service import TelegramService, MessagePlan
from work_assistant import AssistantAnswer, WorkAssistantService, needs_search
from request_operations import PendingOperation, RequestOperation, request_fingerprint
from tests.test_telegram_operator import assistant_client, response

ANGLE = 'Уголок 50х50х5 Ст3 — 2 т'
PIPE = 'Труба профильная 140х140 Ст3 — 20 т'
MASS = 'Сколько весит 405 шт арматуры ф12 А240?'


def context(tmp_path):
    repo = TelegramRepository(Database(tmp_path/'dialog.sqlite3'))
    ai, assistant = Mock(), Mock()
    assistant.answer.return_value = AssistantAnswer('Ответ собеседника')
    def extract(text, **kw):
        if 'арматур' in text:
            item = RequestItem(product='Арматура', product_group='rebar', diameter_mm=12, grade='А240', source_text=text)
        elif 'Труба' in text:
            item = RequestItem(product='Труба профильная', product_group='profile_pipe', size_designation='140х140', width_mm=140, height_mm=140, source_text=text)
        else:
            item = RequestItem(product='Уголок', product_group='angle', size_designation='50х50х5', source_text=text)
        return PurchaseRequest(items=[item])
    ai.parse_request.side_effect = extract
    return TelegramService(ai, repo, reliable=True, assistant=assistant), ai, repo, assistant


def test_mass_length_reply_resumes_bound_calculation(tmp_path):
    service, ai, repo, assistant = context(tmp_path)
    messages = service.process(1, MASS)
    assert any('длин' in m.casefold() for m in messages)
    original = repo.load_calculation(1)
    assert repo.load(1) is None
    assert original.items[0].quantity == Decimal('405')
    assistant.answer.return_value = AssistantAnswer('При 0,888 кг/м: 405 × 11,7 × 0,888 / 1000 = 4,207788 т.')
    messages = service.process(1, '11.7м')
    calculation = repo.load_calculation(1)
    assert calculation.calculation_id == original.calculation_id
    assert calculation.items[0].length_mm == 11700
    assert calculation.items[0].quantity == Decimal('405')
    assert calculation.source_text == MASS and repo.load(1) is None
    assert any('4,207788' in m for m in messages)
    current = repo.load_calculation(1)
    assert current.items[0].diameter_mm == 12 and current.items[0].length_mm == 11700
    assert current.result.total_t == Decimal('4.207788')
    assistant.answer.assert_not_called()
    ai.parse_request.assert_not_called()



@pytest.mark.parametrize('reply', ['40 мм', 'Стенка 40 мм'])
def test_numeric_wall_confirmation_does_not_become_new_request(tmp_path, reply):
    service, ai, repo, assistant = context(tmp_path)
    service.process(1, PIPE)
    before = repo.load(1)
    messages = service.process(1, reply)
    active = repo.load(1)
    assert active.request_id == before.request_id
    assert active.items[0].wall_thickness_mm == 40
    assert active.items[0].quantity == Decimal('20')
    assert 'Это новая заявка' not in '\n'.join(messages)
    assert ai.parse_request.call_count == 1


@pytest.mark.parametrize('question', ['Какие комбинации в покере?', 'Привет', 'Расскажи про шахматы'])
def test_chat_bypasses_pending_and_does_not_receive_request(tmp_path, question):
    service, ai, repo, assistant = context(tmp_path)
    service.process(1, PIPE)
    before = repo.load(1).model_dump_json()
    service.process(1, question)
    assert repo.load(1).model_dump_json() == before
    assert assistant.answer.call_args.kwargs['current'] is None
    service.process(1, 'Стенка 6 мм')
    assert repo.load(1).items[0].wall_thickness_mm == 6
    ai.parse_request.assert_called_once()


@pytest.mark.parametrize('command', ['Уточнение', 'Уточнение текущей'])
def test_clarification_command_repeats_specific_question_not_generic_loop(tmp_path, command):
    service, ai, repo, assistant = context(tmp_path)
    service.process(1, PIPE)
    before = repo.load(1).request_id
    messages = service.process(1, command)
    assert any('стенк' in m.casefold() for m in messages)
    service.process(1, '6 мм')
    assert repo.load(1).request_id == before
    assert repo.load(1).items[0].wall_thickness_mm == 6


def test_new_request_excludes_old_context_and_retains_history(tmp_path):
    service, ai, repo, assistant = context(tmp_path)
    service.process(1, PIPE)
    old_id = repo.load(1).request_id
    assistant.answer.return_value = AssistantAnswer('Ранее обсуждалась '+PIPE)
    service.process(1, 'Какая стенка у этой трубы?')
    service.process(1, ANGLE)
    latest = repo.load(1)
    assert latest.request_id != old_id
    assert latest.pending_operation is None and latest.pending_clarification is None
    service.process(1, 'Что указано в текущей заявке?')
    args = assistant.answer.call_args.kwargs
    assert args['current'].request_id == latest.request_id
    assert PIPE not in json.dumps(args.get('history', []), ensure_ascii=False)
    with repo.database.connection() as c:
        assert c.execute('SELECT COUNT(*) FROM telegram_history WHERE request_id=?',(old_id,)).fetchone()[0]


def test_append_preserves_request_id_and_sources(tmp_path):
    service, ai, repo, assistant = context(tmp_path)
    service.process(1, ANGLE)
    before = repo.load(1)
    service.process(1, 'Дополни заявку: ' + PIPE)
    active = repo.load(1)
    assert active.request_id == before.request_id and len(active.items) == 2
    assert active.items[0].item_id in {item.item_id for item in before.items} or active.items[1].item_id in {item.item_id for item in before.items}
    assert sum(item.quantity for item in active.items) == Decimal('22')


def test_chat_history_survives_restart_is_user_scoped_and_bounded(tmp_path):
    service, ai, repo, assistant = context(tmp_path)
    for n in range(12): service.process(1, f'Расскажи про шахматы {n}')
    service = TelegramService(ai, TelegramRepository(Database(tmp_path/'dialog.sqlite3')), reliable=True, assistant=assistant)
    service.process(1, 'Объясни подробнее')
    history = assistant.answer.call_args.kwargs['history']
    assert 10 <= len(history) <= 20
    assert any('шахматы 11' in m['content'] for m in history)
    service.process(2, 'Привет')
    assert not assistant.answer.call_args.kwargs.get('history')


@pytest.mark.parametrize('text', ['Какая погода в Екатеринбурге?', 'Кто в России производит такую трубу?', 'Какие заводы выпускают трубу 630х6?'])
def test_current_information_uses_search_without_edit(tmp_path, text):
    service, ai, repo, assistant = context(tmp_path)
    service.process(1, PIPE)
    before = repo.load(1).model_dump_json()
    service.process(1, text)
    assert needs_search(text)
    assert repo.load(1).model_dump_json() == before
    current = assistant.answer.call_args.kwargs['current']
    assert (current is not None) == ('такую' in text)


def test_ambiguous_short_reply_keeps_pending(tmp_path):
    service, ai, repo, assistant = context(tmp_path)
    service.process(1, PIPE)
    before = repo.load(1).model_dump_json()
    messages = service.process(1, 'ну')
    assert repo.load(1).model_dump_json() == before
    assert any('стенк' in m.casefold() for m in messages)


def test_pending_geometry_restores_after_restart(tmp_path):
    service, ai, repo, assistant = context(tmp_path)
    service.process(1, PIPE)
    request_id = repo.load(1).request_id
    service = TelegramService(ai, TelegramRepository(Database(tmp_path/'dialog.sqlite3')), reliable=True, assistant=assistant)
    service.process(1, '6 мм')
    assert repo.load(1).request_id == request_id
    assert repo.load(1).items[0].wall_thickness_mm == 6



def test_explicit_40mm_confirmation_keeps_original_geometry(tmp_path):
    service, ai, repo, assistant = context(tmp_path)
    source = 'Труба профильная 140х140х40 Ст3 — 20 т'
    ai.parse_request.return_value = PurchaseRequest(items=[RequestItem(product='Труба профильная',
        product_group='profile_pipe',width_mm=140,height_mm=140,wall_thickness_mm=40,
        size_designation='140х140х40',source_text=source)])
    ai.parse_request.side_effect = None
    service.process(1,source)
    active = repo.load(1)
    service._begin_clarification(1,active,source,MessagePlan(intent='clarify',question='Подтвердите, действительно ли стенка 40 мм?'))
    service.process(1,'40 мм')
    active = repo.load(1)
    assert active.items[0].wall_thickness_mm == active.items[0].original.wall_thickness_mm == 40
    assert active.detail_answers[-1].value_mm == Decimal('40')
    assert active.detail_answers[-1].request_id == active.request_id


def test_uncertain_prose_classified_once_without_normalization(tmp_path):
    from dialog_context import DialogClassification
    service, ai, repo, assistant = context(tmp_path)
    service.process(1,PIPE)
    before = repo.load(1).model_dump_json()
    ai.classify_dialog.return_value = DialogClassification(intent='general_chat')
    service.process(1,'Наверное поговорим о стратегии турнира')
    assert repo.load(1).model_dump_json() == before
    assert assistant.answer.call_args.kwargs['current'] is None
    ai.classify_dialog.assert_called_once()
    ai.parse_request.assert_called_once()


def test_history_retains_old_exchanges_without_schema_migration(tmp_path):
    service, ai, repo, assistant = context(tmp_path)
    with repo.database.connection() as c:
        schema_before = list(c.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name"))
    for n in range(15):service.process(1,f'Расскажи про покер {n}')
    with repo.database.connection() as c:
        schema_after = list(c.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name"))
        entries = c.execute("SELECT COUNT(*) FROM telegram_state WHERE key LIKE 'dialog:1:0:%' AND key NOT LIKE '%sequence'").fetchone()[0]
    assert schema_after == schema_before and entries == 15
    assert len(repo.load_dialog(1)) == 20


def test_model_payload_separates_free_history_and_request(tmp_path):
    client = assistant_client()
    assistant = WorkAssistantService(client)
    assistant.answer('Какие ещё комбинации?', user_id=1, history=[{'role':'user','content':'Покер'}])
    payload = json.loads(client.responses.create.call_args.kwargs['input'])
    assert payload['request_context'] is None
    assert payload['current_technical_items'] is None
    assert payload['dialog_history'] == [{'role':'user','content':'Покер'}]
    assert client.responses.create.call_args.kwargs['model'] == 'gpt-6-luna'
    assert client.responses.create.call_args.kwargs['reasoning'] == {'effort':'low'}



def test_pending_detail_has_stable_binding_and_original_question(tmp_path):
    service, ai, repo, assistant = context(tmp_path)
    messages = service.process(1,PIPE)
    active = repo.load(1)
    pending = active.pending_detail
    assert pending.request_id == active.request_id and pending.item_id == active.items[0].item_id
    assert pending.field == 'wall_thickness_mm'
    assert pending.question == active.pending_operation.question
    assert pending.fingerprint == request_fingerprint(active)
    service.process(1,'6 мм')
    active = repo.load(1)
    assert active.items[0].original.wall_thickness_mm is None
    assert active.detail_answers[-1].question == pending.question
    assert active.user_status == 'Готова'


def test_stale_detail_cannot_apply_to_changed_request(tmp_path):
    service, ai, repo, assistant = context(tmp_path)
    service.process(1,PIPE)
    active = repo.load(1)
    active.pending_detail.request_id = 'other-request'
    repo.save(1,active)
    before = repo.load(1).model_dump_json()
    service.process(1,'40 мм')
    assert repo.load(1).model_dump_json() == before


def test_bare_dimension_cannot_pick_one_of_two_positions(tmp_path):
    from telegram_service import ActiveItem, ActiveRequest
    service, ai, repo, assistant = context(tmp_path)
    original = RequestItem(product='Труба профильная',product_group='profile_pipe',width_mm=140,height_mm=140,quantity='1',unit='т',source_text=PIPE)
    active = ActiveRequest(request_id='multiple',source_text=PIPE,items=[ActiveItem(**original.model_dump(),item_id=str(n),original=original.model_copy(deep=True),source_position=n+1) for n in range(2)],missing_fields=['items[0].wall_thickness_mm','items[1].wall_thickness_mm'])
    active.pending_operation = PendingOperation(user_id=1,request_id=active.request_id,request_fingerprint=request_fingerprint(active),operation=RequestOperation(kind='edit_fields'),command=PIPE,question='Укажите стенку каждой исходной позиции: 1 и 2.',expected_answer='details')
    repo.save(1,active)
    before = repo.load(1).model_dump_json()
    assert 'каждой' in service.process(1,'6')[0]
    assert repo.load(1).model_dump_json() == before
    ai.interpret_message.assert_not_called()


def test_authorized_chat_storage_requires_access_and_respects_epoch(tmp_path):
    from repositories.access_repository import AuthorizedRequestRepository
    repo = TelegramRepository(Database(tmp_path/'auth.sqlite3'))
    access = Mock()
    access.get.return_value = {'status':'approved','context_floor':0}
    authorized = AuthorizedRequestRepository(repo,access)
    authorized.save_dialog(1,'hello','reply')
    assert len(authorized.load_dialog(1)) == 2
    access.get.return_value = {'status':'approved','context_floor':50}
    assert authorized.load_dialog(1) == []
    access.get.return_value = {'status':'revoked','context_floor':50}
    with pytest.raises(PermissionError):authorized.load_dialog(1)
    with pytest.raises(PermissionError):authorized.save_dialog(1,'blocked','blocked')
    assert len(repo.load_dialog(1)) == 2  # Old history was hidden, not deleted.



@pytest.mark.parametrize('text', ['Какая погода этой осенью?', 'Что означает эта комбинация в покере?'])
def test_general_pronoun_does_not_leak_request_context(tmp_path,text):
    service, ai, repo, assistant = context(tmp_path)
    service.process(1,PIPE)
    before = repo.load(1).model_dump_json()
    service.process(1,text)
    assert assistant.answer.call_args.kwargs['current'] is None
    assert repo.load(1).model_dump_json() == before
