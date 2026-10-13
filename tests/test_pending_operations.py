"""Offline integration: real AIService/SDK transport, service and SQLite.

Only the external HTTP response is simulated. No Telegram/OpenAI network calls.
"""
from collections import deque
from decimal import Decimal
import json
from pathlib import Path
import sqlite3

import httpx2
import pytest
from openai import DefaultHttpxClient, OpenAI

from ai import AIService
from clarifications import PendingClarification, question_state
from database import Database
from diagnostics import PipelineError
from repositories.telegram_repository import TelegramRepository
from request_operations import RequestOperation, UNSUPPORTED_MESSAGE
from schemas import PurchaseRequestExtraction, RequestItem
from telegram_service import TelegramService, MessagePlan, Edit


SOURCE = 'Лист 6×1500×6000 Ст3 — 4 шт'
FIXTURES = Path(__file__).parent / 'fixtures'


@pytest.fixture
def context(tmp_path):
    responses = deque()
    calls = []
    def respond(request):
        calls.append(json.loads(request.content))
        assert responses, 'Unexpected AI call'
        return httpx2.Response(200, json={
            'id': 'offline-test', 'object': 'chat.completion', 'created': 0,
            'model': 'gpt-6-luna', 'choices': [{'index': 0, 'finish_reason': 'stop',
                'message': {'role': 'assistant', 'content': responses.popleft().model_dump_json(), 'refusal': None}}]})
    with DefaultHttpxClient(transport=httpx2.MockTransport(respond)) as http:
        client = OpenAI(api_key='offline-test', http_client=http, max_retries=0)
        ai = object.__new__(AIService)
        ai._client = client
        repo = TelegramRepository(Database(tmp_path / 'requests.sqlite3'))
        service = TelegramService(ai, repo)
        responses.append(PurchaseRequestExtraction(items=[RequestItem(
            product='Лист', product_group='sheet', size_designation='6×1500×6000',
            grade='Ст3', quantity='4', unit='шт', source_text=SOURCE)]))
        service.process(1, SOURCE)
        yield service, repo, responses, calls
        assert not responses, 'Unused HTTP responses'


def propose(context, user=1, **kwargs):
    service, repo, responses, _ = context
    active = repo.load(user)
    responses.append(MessagePlan(intent='clarify', question='Подтвердите замену марки на 09Г2С?',
        edits=[Edit(item_id=active.items[0].item_id, field='grade', value='09Г2С')], **kwargs))
    service.process(user, 'Хочу согласовать замену марки')
    return repo.load(user)


def history_count(repo):
    with repo.database.connection() as c:
        return c.execute('SELECT COUNT(*) FROM telegram_history').fetchone()[0]


def test_yes_executes_saved_edits_once_across_restart(context):
    service, repo, _, calls = context
    before = propose(context)
    pending = before.pending_operation
    assert pending.user_id == 1 and pending.request_id == before.request_id
    assert pending.expected_answer == 'confirmation'
    calls_before = len(calls)
    service = TelegramService(service.ai, TelegramRepository(repo.database))
    service.process(1, 'Да!')
    after = repo.load(1)
    assert after.items[0].grade == '09Г2С'
    assert after.pending_operation is None
    assert after.items[0].quantity == Decimal('4') and after.items[0].unit == 'шт'
    assert after.items[0].original == before.items[0].original
    assert after.source_text == SOURCE
    assert len(calls) == calls_before
    count = history_count(repo)
    restarted = TelegramService(service.ai, TelegramRepository(repo.database))
    assert 'Нет ожидающего' in restarted.process(1, 'Да')[0]
    assert history_count(repo) == count


@pytest.mark.parametrize('answer', ['Нет', 'отмена', '/cancel', 'отмени операцию'])
def test_cancel_preserves_data_and_does_not_reappear(context, answer):
    service, repo, _, _ = context
    before = propose(context)
    assert 'отменена' in service.process(1, answer)[0]
    after = repo.load(1)
    assert after.items == before.items and after.source_text == before.source_text
    assert after.pending_operation is None and after.pending_clarification is None
    assert 'Нет ожидающего' in service.process(1, 'Да')[0]


def test_no_cancels_legacy_confirmation_without_recreating_it(context):
    service, repo, _, _ = context
    active = repo.load(1)
    active.pending_clarification = question_state('Верно, что требуется 5 шт.?', active)
    repo.save(1, active)
    service.process(1, 'Нет')
    assert repo.load(1).items[0].quantity == Decimal('4')
    assert repo.load(1).pending_clarification is None
    assert 'Нет ожидающего' in service.process(1, 'Да')[0]


def test_unknown_scope_question_is_persisted_and_continued(context):
    service, repo, responses, calls = context
    responses.append(MessagePlan(intent='clarify', question='Какие позиции и какую марку изменить?'))
    service.process(1, 'Хочу исправить марку')
    active = repo.load(1)
    assert active.pending_operation.expected_answer == 'details'
    assert 'конкретные' in service.process(1, 'Да')[0]
    assert repo.load(1).pending_operation is not None
    responses.append(MessagePlan(intent='edit', edits=[Edit(item_id=active.items[0].item_id, field='grade', value='09Г2С')]))
    service.process(1, 'Первую позицию, марка 09Г2С')
    assert repo.load(1).items[0].grade == '09Г2С'
    assert repo.load(1).pending_operation is None
    sent = calls[-1]['messages'][-1]['content']
    assert 'Хочу исправить марку' in sent and 'Какие позиции' in sent


def test_empty_edit_question_also_has_state(context):
    service, repo, responses, _ = context
    responses.append(MessagePlan(intent='edit', question='Укажите полную марку.'))
    service.process(1, 'Хочу поменять сталь')
    assert repo.load(1).pending_operation.question == 'Укажите полную марку.'


def test_new_21_position_request_replaces_pending_and_preserves_sources(context):
    service, repo, responses, _ = context
    old = propose(context)
    text = (FIXTURES / 'mixed_21_original.txt').read_text().strip()
    responses.append(PurchaseRequestExtraction.model_validate_json((FIXTURES / 'mixed_21_extraction.json').read_text()))
    service.process(1, text)
    active = repo.load(1)
    assert active.request_id != old.request_id
    assert len(active.items) == 21
    assert active.source_text == text
    assert all(item.source_text and item.original.source_text == item.source_text for item in active.items)
    assert active.pending_operation is None
    assert 'Нет ожидающего' in service.process(1, 'Да')[0]
    assert all(item.unit == 'т' for item in active.items)


@pytest.mark.parametrize('stale', ['request', 'user', 'attributes'])
def test_stale_operation_cannot_be_applied(context, stale):
    service, repo, _, _ = context
    active = propose(context)
    if stale == 'request': active.pending_operation.request_id = 'another-request'
    if stale == 'user': active.pending_operation.user_id = 2
    if stale == 'attributes': active.items[0].grade = 'С245'
    repo.save(1, active)
    with pytest.raises(PipelineError, match='другой или изменённой'):
        service.process(1, 'Да')
    assert repo.load(1) == active


def test_stale_legacy_clarification_is_rejected(context):
    service, repo, _, _ = context
    active = repo.load(1)
    active.pending_clarification = PendingClarification(request_id='old', item_id=active.items[0].item_id,
        field='quantity', kind='confirmation', question='Верно?', candidate={'quantity': '5'})
    repo.save(1, active)
    with pytest.raises(PipelineError, match='другой заявке'):
        service.process(1, 'Да')
    assert repo.load(1) == active


def test_two_users_have_separate_confirmations(context):
    service, repo, responses, _ = context
    responses.append(PurchaseRequestExtraction(items=[RequestItem(product='Лист', product_group='sheet',
        size_designation='6×1500×6000', grade='Ст3', quantity=4, unit='шт', source_text=SOURCE)]))
    service.process(2, SOURCE)
    propose(context, 1)
    second = propose(context, 2)
    service.process(1, 'Да')
    assert repo.load(2) == second
    service.process(2, 'Нет')
    assert repo.load(1).items[0].grade == '09Г2С'
    assert repo.load(2).items[0].grade == 'Ст3'


def test_sqlite_failure_rolls_back_execution_and_keeps_pending_for_retry(context):
    service, repo, _, _ = context
    before = propose(context)
    count = history_count(repo)
    with repo.database.connection() as c:
        c.execute("CREATE TRIGGER fail_update BEFORE UPDATE ON telegram_active BEGIN SELECT RAISE(ABORT, 'test failure'); END")
    with pytest.raises(sqlite3.IntegrityError): service.process(1, 'Да')
    assert repo.load(1) == before and history_count(repo) == count
    with repo.database.connection() as c: c.execute('DROP TRIGGER fail_update')
    restarted = TelegramService(service.ai, TelegramRepository(repo.database))
    restarted.process(1, 'Да')
    assert repo.load(1).pending_operation is None
    assert history_count(repo) == count + 1


def test_error_before_execution_does_not_drop_state(context):
    service, repo, _, _ = context
    active = propose(context)
    active.pending_operation.operation.edits[0].item_id = 'missing'
    repo.save(1, active)
    with pytest.raises(PipelineError): service.process(1, 'Да')
    assert repo.load(1) == active


@pytest.mark.parametrize('command', [
    'Все переведи в тонны и объедини дубли',
    'Штуки тоже переведи в тонны', 'Рассчитай теоретическую массу',
    'Переведи лист 6×1500×6000 4 шт в тонны',
    'Пожалуйста, переведи лист 6×1500×6000 4 шт в тонны'])
def test_unsupported_command_never_creates_fake_confirmation(context, command):
    service, repo, _, calls = context
    before = repo.load(1)
    count = len(calls)
    assert service.process(1, command) == [UNSUPPORTED_MESSAGE]
    assert repo.load(1) == before
    assert len(calls) == count
    assert 'Нет ожидающего' in service.process(1, 'Да')[0]


def test_unsupported_operation_does_not_consume_existing_pending(context):
    service, repo, _, _ = context
    before = propose(context)
    response = service.process(1, 'Суммируй одинаковые позиции')
    assert response[0].endswith('4 шт. = 1,696 т')
    assert response[1] == before.pending_operation.question
    assert repo.load(1) == before
    service.process(1, 'Да')
    assert repo.load(1).items[0].grade == '09Г2С'


@pytest.mark.parametrize('question', [
    'Объединять позиции с одинаковыми товаром, размером и маркой, суммируя количества после перевода в тонны?',
    'Вы хотите объединить одинаковые позиции в одну, сложив количества и удалив дублирующие строки?',
    'Для позиции 1 укажите вес одного листа или общий тоннаж: какое количество в тоннах указать?'])
def test_model_unsupported_question_is_not_a_quantity_clarification(context, question):
    service, repo, responses, _ = context
    before = repo.load(1)
    responses.append(MessagePlan(intent='clarify', question=question))
    assert service.process(1, 'Хочу дополнительную операцию') == [UNSUPPORTED_MESSAGE]
    assert repo.load(1) == before
    assert 'Нет ожидающего' in service.process(1, '1,6956')[0]


@pytest.mark.parametrize('kind', ['convert_units', 'calculate_mass'])
def test_structured_unsupported_operation_does_not_execute_edits(context, kind):
    service, repo, responses, _ = context
    before = repo.load(1)
    responses.append(MessagePlan(intent='operation', operation=RequestOperation(kind=kind,
        edits=[Edit(item_id=before.items[0].item_id, field='quantity', value='1.6956')])))
    assert service.process(1, 'Хочу дополнительную операцию') == [UNSUPPORTED_MESSAGE]
    assert repo.load(1) == before


def test_legacy_json_compatibility_and_sqlite_schema_unchanged(context):
    service, repo, _, _ = context
    with repo.database.connection() as c:
        schema = c.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        old = json.loads(repo.load(1).model_dump_json())
        for field in ('pending_operation', 'clarification_cancelled'): old.pop(field)
        c.execute('UPDATE telegram_active SET request_json=? WHERE user_id=1', (json.dumps(old),))
    legacy = repo.load(1)
    assert legacy.pending_operation is None and not legacy.clarification_cancelled
    propose(context)
    service.process(1, 'Да')
    with repo.database.connection() as c:
        assert [tuple(row) for row in schema] == [tuple(row) for row in c.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name")]
        assert c.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert not c.execute('PRAGMA foreign_key_check').fetchall()


def test_existing_quantity_edit_and_restore_keep_source_units(context):
    service, repo, responses, _ = context
    original = repo.load(1)
    service.process(1, 'В позиции 1 измени количество на 5 шт')
    assert repo.load(1).items[0].quantity == Decimal('5')
    active = repo.load(1)
    responses.append(MessagePlan(intent='edit', edits=[Edit(item_id=active.items[0].item_id, field='quantity', restore=True)]))
    service.process(1, 'Верни исходное количество')
    assert repo.load(1).items == original.items
    assert repo.load(1).source_text == SOURCE


def test_new_request_prompt_clears_old_operation(context):
    service, repo, _, calls = context
    before = propose(context)
    service.process(1, 'новую')
    assert repo.load(1).pending_operation is None
    assert repo.load(1).request_id == before.request_id
    count = len(calls)
    service.process(1, 'Да')
    assert len(calls) == count
    assert repo.load(1).items == before.items


def test_multiple_detail_answers_survive_restart(context):
    service, repo, responses, calls = context
    responses.append(MessagePlan(intent='clarify', question='Какие позиции?'))
    service.process(1, 'Хочу исправить марку')
    responses.append(MessagePlan(intent='clarify', question='Какая марка?'))
    service.process(1, 'Первую')
    active = repo.load(1)
    assert active.pending_operation.answers == ['Первую']
    service = TelegramService(service.ai, TelegramRepository(repo.database))
    responses.append(MessagePlan(intent='edit', edits=[Edit(item_id=active.items[0].item_id, field='grade', value='09Г2С')]))
    service.process(1, '09Г2С')
    sent = calls[-1]['messages'][-1]['content']
    assert 'Первую' in sent and '09Г2С' in sent
    assert repo.load(1).items[0].grade == '09Г2С'


def test_cannot_return_question_when_sqlite_save_failed(context):
    service, repo, responses, _ = context
    before = repo.load(1)
    responses.append(MessagePlan(intent='clarify', question='Какие позиции?'))
    with repo.database.connection() as c:
        c.execute("CREATE TRIGGER fail_question BEFORE UPDATE ON telegram_active BEGIN SELECT RAISE(ABORT, 'test failure'); END")
    with pytest.raises(sqlite3.IntegrityError): service.process(1, 'Хочу исправить марку')
    assert repo.load(1) == before


def test_structured_field_edit_uses_existing_executor(context):
    service, repo, responses, _ = context
    active = repo.load(1)
    responses.append(MessagePlan(intent='operation', operation=RequestOperation(kind='edit_fields',
        edits=[Edit(item_id=active.items[0].item_id, field='grade', value='09Г2С')])))
    service.process(1, 'Исправь сталь')
    assert repo.load(1).items[0].grade == '09Г2С'


def test_structured_cancel_clears_pending_without_changing_items(context):
    service, repo, responses, _ = context
    active = propose(context)
    responses.append(MessagePlan(intent='operation', operation=RequestOperation(kind='cancel')))
    service.process(1, 'Измени решение: откажемся от предложения')
    assert repo.load(1).pending_operation is None
    assert repo.load(1).items == active.items


def test_validator_question_has_persisted_state(context):
    service, repo, responses, _ = context
    source = 'Лист Ст3 — 4 шт'
    responses.append(PurchaseRequestExtraction(items=[RequestItem(product='Лист', product_group='sheet',
        grade='Ст3', quantity=4, unit='шт', source_text=source)]))
    output = service.process(1, source)
    active = repo.load(1)
    assert active.pending_operation and active.pending_operation.question in output
    assert active.pending_operation.request_id == active.request_id
    assert 'конкретные' in service.process(1, 'Да')[0]
    assert repo.load(1).items[0].quantity == Decimal('4')


def test_concurrent_users_confirm_independent_operations(context):
    from concurrent.futures import ThreadPoolExecutor
    service, repo, responses, _ = context
    responses.append(PurchaseRequestExtraction(items=[RequestItem(product='Лист', product_group='sheet',
        size_designation='6×1500×6000', grade='Ст3', quantity=4, unit='шт', source_text=SOURCE)]))
    service.process(2, SOURCE)
    propose(context, 1)
    propose(context, 2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda user: service.process(user, 'Да'), [1, 2]))
    assert len(results) == 2
    assert all(repo.load(user).items[0].grade == '09Г2С' for user in [1, 2])
    assert all(repo.load(user).pending_operation is None for user in [1, 2])


def test_existing_sqlite_access_and_procurement_data_survive(context):
    from repositories.access_repository import AccessRepository
    service, repo, _, _ = context
    repo.database.initialize()
    access = AccessRepository(repo.database)
    access.bootstrap_configured_admin(99)
    with repo.database.connection() as c:
        c.execute("INSERT INTO warehouse(name,city) VALUES('Test warehouse','Test city')")
        before = [tuple(row) for row in c.execute('SELECT * FROM telegram_access_user')]
        schema = [tuple(row) for row in c.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name")]
    propose(context)
    service.process(1, 'Да')
    service = TelegramService(service.ai, TelegramRepository(repo.database))
    assert access.is_admin(99)
    with repo.database.connection() as c:
        assert before == [tuple(row) for row in c.execute('SELECT * FROM telegram_access_user')]
        assert schema == [tuple(row) for row in c.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name")]
        assert c.execute('SELECT name FROM warehouse').fetchone()[0] == 'Test warehouse'
        assert c.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


def test_typed_cancel_can_be_confirmed_without_becoming_field_edit(context):
    service, repo, responses, _ = context
    before = propose(context)
    responses.append(MessagePlan(intent='clarify', question='Подтвердите отмену предложения?',
        operation=RequestOperation(kind='cancel')))
    service.process(1, 'Измени решение: уточним отказ')
    active = repo.load(1)
    assert active.pending_operation.operation.kind == 'cancel'
    assert active.pending_operation.expected_answer == 'confirmation'
    service.process(1, 'Да')
    assert repo.load(1).pending_operation is None
    assert repo.load(1).items == before.items
