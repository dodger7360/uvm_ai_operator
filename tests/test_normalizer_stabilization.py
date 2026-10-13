"""Real request regressions with isolated SQLite and deterministic AI transports."""
from collections import Counter
from decimal import Decimal
import json
from pathlib import Path
import threading
import time
from unittest.mock import Mock, patch

import httpx2
import pytest
from openai import DefaultHttpxClient, OpenAI

from ai import AIService
from database import Database
from diagnostics import CURRENT, SemanticExtractionError, operation, http_attempt
from normalizer import normalize_request
from repositories.telegram_repository import TelegramRepository
from schemas import PurchaseRequest, PurchaseRequestExtraction, RequestItem
from telegram_bot import handle_update, TypingIndicator
from telegram_service import TelegramService, MessagePlan, Edit, prepare_sheet_heading
from validator import validate_request

FIXTURES = Path(__file__).parent / 'fixtures'


def pipes():
    text = (FIXTURES / 'pipes_5_original.txt').read_text().strip()
    return text, PurchaseRequest(items=[RequestItem(product='Труба', product_group='round_pipe',
        size_designation=line.split()[1], quantity=line.rsplit('-', 1)[1].split()[0], unit='т', source_text=line)
        for line in text.splitlines()])


def mixed():
    return ((FIXTURES / 'mixed_21_original.txt').read_text().strip(),
            PurchaseRequest.model_validate_json((FIXTURES / 'mixed_21_extraction.json').read_text()))


@pytest.fixture
def context(tmp_path):
    repo = TelegramRepository(Database(tmp_path / 'test.sqlite3'))
    ai = Mock()
    ai.parse_request.side_effect = lambda *args, **kwargs: pipes()[1]
    return TelegramService(ai, repo), ai, repo


def start(context, user=1):
    service, ai, repo = context
    service.process(user, pipes()[0])
    ai.reset_mock()
    return repo.load(user)


def test_five_pipes_through_authorized_handler_with_normalized_punctuation(context):
    service, ai, repo = context
    start(context)
    def parse(*args, **kwargs):
        draft = pipes()[1]
        for item in draft.items:
            item.source_text = item.source_text.replace('*', '×')
        return draft
    ai.parse_request.side_effect = parse
    api, access = Mock(), Mock()
    access.route.return_value = False
    access.allowed.return_value = True
    handle_update({'message': {'chat': {'type': 'private', 'id': 1}, 'from': {'id': 1}, 'text': pipes()[0]}}, service, api, access)
    replies = [call.kwargs['text'] for call in api.call.call_args_list if call.args[0] == 'sendMessage']
    assert '\n'.join(replies) == (FIXTURES / 'pipes_5_expected.txt').read_text().strip()
    assert len(repo.load(1).items) == 5
    ai.parse_request.assert_called_once()
    ai.interpret_message.assert_not_called()
    assert all(item.length_mm is None for item in repo.load(1).items)


def test_mixed_21_complete_and_scoped(context):
    service, ai, repo = context
    start(context)
    text, draft = mixed()
    ai.parse_request.side_effect = lambda *args, **kwargs: draft.model_copy(deep=True)
    result = service.process(1, text)
    assert '\n'.join(result) == (FIXTURES / 'mixed_21_expected.txt').read_text().strip()
    active = repo.load(1)
    assert active.is_valid
    assert len(active.items) == len('\n'.join(result).splitlines()) == 21
    assert Counter(item.product_group for item in active.items) == {
        'round_pipe': 5, 'round_bar': 2, 'beam': 1, 'angle': 1, 'sheet': 9, 'expanded_metal': 3}
    assert Counter(item.quantity for item in active.items) == Counter(item.quantity for item in draft.items)
    assert all(item.unit == 'т' for item in active.items)
    assert active.source_text == text
    sheets = [item for item in active.items if item.product_group == 'sheet']
    assert [item.thickness_mm for item in sheets] == [3, 4, 10, 12, 16, 16, 30, 50, 80]
    assert Counter(item.grade for item in sheets) == {'С245': 4, 'Ст09Г2С': 5}
    assert all((item.width_mm, item.length_mm, item.gost_tu) == (1500, 6000, 'ГОСТ 19903') for item in sheets)
    assert all(item.source_context and item.source_text[0].isdigit() for item in sheets)
    assert all(item.source_context is None for item in active.items if item.product_group != 'sheet')
    assert all(item.gost_tu == 'ТУ 36.26.11-5-89' for item in active.items if item.product_group == 'expanded_metal')
    ai.parse_request.assert_called_once()
    ai.interpret_message.assert_not_called()


def test_two_headers_after_other_products_and_blank_lines():
    prepared, records = prepare_sheet_heading(mixed()[0])
    assert len(records) == 9
    assert len(prepared.splitlines()) == 21
    assert sum('С245' in record[2] for record in records) == 4
    assert sum('09г2с' in record[2] for record in records) == 5
    assert prepared.splitlines()[-1].startswith('лист ПВЛ')


def test_sheet_block_ends_at_next_product():
    text = 'лист ГК ГОСТ 19903 С245 раскрой 1,5*6м:\n3 - 1 т\nБалка 30К1 С245 — 2 т'
    prepared, records = prepare_sheet_heading(text)
    assert len(records) == 1
    assert prepared.splitlines()[-1] == text.splitlines()[-1]


def test_unrecognized_numeric_sheet_row_is_not_silently_dropped(context):
    service, ai, repo = context
    old = start(context)
    text = 'лист ГК ГОСТ 19903 С245 раскрой 1,5*6м:\n3 - 1 т\n4 - 2 неизвестных'
    with pytest.raises(ValueError):
        service.process(1, text)
    assert repo.load(1).request_id == old.request_id
    ai.parse_request.assert_not_called()


def test_duplicates_keep_both_literal_source_rows(context):
    service, ai, repo = context
    text, draft = pipes()
    first = text.splitlines()[0]
    second = first.replace('*','×')
    ai.parse_request.side_effect = lambda *args, **kwargs: PurchaseRequest(items=[draft.items[0].model_copy(deep=True),draft.items[0].model_copy(deep=True)])
    service.process(1,first+'\n'+second)
    assert [item.source_text for item in repo.load(1).items] == [first,second]
    assert len({item.item_id for item in repo.load(1).items}) == 2


def test_unlisted_semantic_error_does_not_trigger_medium(context):
    service, ai, repo = context
    ai.parse_request.side_effect = SemanticExtractionError('unlisted_failure','test')
    with pytest.raises(SemanticExtractionError):service.process(1,pipes()[0])
    ai.parse_request.assert_called_once()


@pytest.mark.parametrize('missing', [0, 9, 13, 20])
def test_each_missing_mixed_position_blocks_save(context, missing):
    service, ai, repo = context
    old = start(context)
    text, draft = mixed()
    draft.items.pop(missing)
    ai.parse_request.side_effect = lambda *args, **kwargs: draft.model_copy(deep=True)
    with pytest.raises(SemanticExtractionError):
        service.process(1, text)
    assert ai.parse_request.call_count == 2
    assert repo.load(1).request_id == old.request_id


def test_duplicate_rows_with_punctuation_variants_require_two_items(context):
    service, ai, repo = context
    text, draft = pipes()
    line = text.splitlines()[0]
    ai.parse_request.side_effect = lambda *args, **kwargs: PurchaseRequest(items=[draft.items[0].model_copy(deep=True)])
    with pytest.raises(SemanticExtractionError):
        service.process(1, line+'\n'+line.replace('*','×'))


def test_greeting_new_choice_and_request_persist_across_restart(context):
    service, ai, repo = context
    old = start(context)
    assert 'новую' in service.process(1, 'как дела')[0]
    assert repo.load(1).dialog_state == 'choose_request'
    service = TelegramService(ai, TelegramRepository(repo.database))
    assert 'Пришлите новую заявку' in service.process(1, 'новую')[0]
    assert repo.load(1).request_id == old.request_id
    assert repo.load(1).dialog_state == 'awaiting_new'
    service.process(1, 'спасибо')
    ai.parse_request.assert_not_called()
    ai.interpret_message.assert_not_called()
    service.process(1, pipes()[0])
    assert repo.load(1).request_id != old.request_id
    assert repo.load(1).dialog_state == 'active'
    ai.parse_request.assert_called_once()


def test_model_dialog_choice_persists(context):
    service, ai, repo = context
    start(context)
    ai.interpret_message.return_value = MessagePlan(intent='clarify',question='Вы хотите изменить активную заявку или создать новую?')
    service.process(1, 'что будем делать')
    assert repo.load(1).dialog_state == 'choose_request'
    ai.reset_mock()
    service.process(1, 'новую')
    ai.parse_request.assert_not_called()
    ai.interpret_message.assert_not_called()


def test_dialog_states_isolated_between_users(context):
    service, ai, repo = context
    start(context, 1)
    second = start(context, 2)
    service.process(1, 'новую')
    assert repo.load(1).dialog_state == 'awaiting_new'
    assert repo.load(2).model_dump() == second.model_dump()


def test_non_request_without_context_never_calls_ai(context):
    service, ai, repo = context
    for text in ('как дела','новую','да','2'):
        service.process(1, text)
    assert repo.load(1) is None
    ai.parse_request.assert_not_called()
    ai.interpret_message.assert_not_called()


def test_failed_new_request_keeps_waiting_state_and_old_items(context):
    service, ai, repo = context
    old = start(context)
    service.process(1, 'новую')
    ai.parse_request.side_effect = TypeError('broken code')
    with pytest.raises(TypeError):service.process(1, pipes()[0])
    assert repo.load(1).request_id == old.request_id
    assert repo.load(1).dialog_state == 'awaiting_new'
    ai.parse_request.assert_called_once()


def test_waiting_new_request_does_not_reuse_old_pending_question(context):
    service, ai, repo = context
    active = start(context)
    active.items[0].quantity = None
    repo.save(1, active)
    service.process(1, 'новую')
    text = 'Труба профильная 70×70×5'
    ai.parse_request.side_effect = lambda *args, **kwargs: PurchaseRequest(items=[RequestItem(product='Труба профильная',product_group='profile_pipe',size_designation='70×70×5',source_text=text)])
    service.process(1, text)
    assert repo.load(1).request_id != active.request_id
    assert repo.load(1).items[0].size_designation == '70×70×5'
    ai.parse_request.assert_called_once()
    ai.interpret_message.assert_not_called()


def test_pvl_size_unit_is_formatted_without_altering_dimensions(context):
    service, ai, repo = context
    text, draft = mixed()
    for item in draft.items:
        if item.product_group == 'expanded_metal':
            item.size_designation += 'мм'
    ai.parse_request.side_effect = lambda *args, **kwargs: draft.model_copy(deep=True)
    service.process(1, text)
    assert [item.size_designation for item in repo.load(1).items if item.product_group == 'expanded_metal'] == ['508×450','508×718','508×1000']


def test_local_numeric_grade_edit_and_restore(context):
    service, ai, repo = context
    active = start(context)
    service.process(1, 'В позиции 1 измени марку на Ст20')
    assert repo.load(1).items[0].grade == 'Ст20'
    assert repo.load(1).items[0].original.grade == active.items[0].grade
    service.process(1, 'Верни исходную марку позиции 1')
    assert repo.load(1).items[0].grade == 'Ст3 (или Ст20)'
    ai.interpret_message.assert_not_called()
    ai.parse_request.assert_not_called()


@pytest.mark.parametrize('amount', ['0,004','0,0015'])
def test_local_quantity_edit_exact_decimal(context, amount):
    service, ai, repo = context
    start(context)
    service.process(1, f'В позиции 1 измени количество на {amount} т')
    assert repo.load(1).items[0].quantity == Decimal(amount.replace(',','.'))
    assert repo.load(1).items[0].unit == 'т'
    ai.interpret_message.assert_not_called()


def test_global_grade_edit_matches_full_category_only(context):
    service, ai, repo = context
    active = start(context)
    active.items[0].grade = 'С255'
    active.items[1].grade = 'С255-4'
    repo.save(1,active)
    service.process(1,'Везде С255 замени на Ст3')
    assert repo.load(1).items[0].grade == 'Ст3'
    assert repo.load(1).items[1].grade == 'С255-4'
    ai.interpret_message.assert_not_called()


def test_complex_edit_still_uses_ai(context):
    service, ai, repo = context
    start(context)
    ai.interpret_message.return_value = MessagePlan(intent='clarify',question='Укажите номера позиций.')
    service.process(1,'Замени марку, кроме первой позиции')
    ai.interpret_message.assert_called_once()


def test_typing_network_never_blocks_processing_or_exit():
    entered,release = threading.Event(),threading.Event()
    api=Mock()
    def call(*args,**kwargs):
        entered.set()
        release.wait(2)
    api.call.side_effect=call
    started=time.monotonic()
    try:
        with TypingIndicator(api,1) as indicator:
            assert entered.wait(1)
        assert time.monotonic()-started < 0.5
    finally:
        release.set()
        indicator.thread.join(1)
    assert not indicator.thread.is_alive()


def test_unauthorized_message_never_reaches_ai(context):
    service,ai,repo=context
    api,access=Mock(),Mock()
    access.route.return_value=True
    handle_update({'message':{'chat':{'type':'private','id':2},'from':{'id':2},'text':mixed()[0]}},service,api,access)
    ai.parse_request.assert_not_called()
    ai.interpret_message.assert_not_called()
    assert repo.load(2) is None


def test_stage_timings_include_routing_sqlite_validation_and_sorting(context):
    service,ai,repo=context
    with patch('diagnostics.emit') as emit:
        service.process(1,pipes()[0])
    event=next(call.kwargs for call in emit.call_args_list if call.args[0]=='operation_complete')
    assert {'routing','sqlite_load','sqlite_save','validation','normalization','sorting','formatting'} <= event['stage_ms'].keys()
    assert all(value>=0 for value in event['stage_ms'].values())
    assert pipes()[0] not in json.dumps(event,ensure_ascii=False)


def test_handler_error_is_recorded_even_when_reply_is_sent(context):
    service, ai, repo = context
    ai.parse_request.side_effect = TypeError('PRIVATE_TEXT_AND_SECRET')
    api, access = Mock(), Mock()
    access.route.return_value = False
    access.allowed.return_value = True
    with patch('diagnostics.emit') as emit:
        handle_update({'message': {'chat': {'type': 'private', 'id': 1}, 'from': {'id': 1}, 'text': pipes()[0]}},service,api,access)
    events = [call.kwargs for call in emit.call_args_list if call.args[0]=='operation_complete']
    assert len(events)==1 and events[0]['result']=='program_error'
    assert 'sending' in events[0]['stage_ms']
    assert 'PRIVATE_TEXT_AND_SECRET' not in json.dumps([call.kwargs for call in emit.call_args_list])
    ai.parse_request.assert_called_once()


def test_edit_context_excludes_source_history_and_keeps_positions(context):
    active=start(context)
    old=active.model_dump_json()
    compact=AIService._edit_context(active)
    data=json.loads(compact)
    assert len(compact)<len(old)
    assert [item['position'] for item in data['items']]==[1,2,3,4,5]
    assert 'source_text' not in compact and 'source_context' not in compact and 'initial_extraction' not in compact
    assert all(item['original']['grade']=='Ст3 (или Ст20)' for item in data['items'])


@pytest.mark.parametrize('standard', ['ГОСТ 8509', 'ГОСТ 8509-93'])
def test_equal_angle_short_designation_requires_explicit_standard(standard):
    item=RequestItem(product='Уголок',product_group='angle',size_designation='25×3',grade='С245',gost_tu=standard,quantity='0.965',unit='т',source_text=f'Угол 25*3 С245 {standard} - 0,965т')
    request=validate_request(normalize_request(PurchaseRequest(items=[item])))
    assert request.is_valid
    assert (item.side_a_mm,item.side_b_mm,item.thickness_mm)==(25,25,3)
    assert item.size_designation=='25×3'
    item=RequestItem(product='Уголок',product_group='angle',size_designation='25×3',quantity=1,unit='т',source_text='Угол 25*3 — 1 т')
    normalize_request(PurchaseRequest(items=[item]))
    assert item.side_b_mm is None


def test_sdk_retries_counted_with_mock_http_transport():
    text='Труба 25×2 — 0,004 т'
    draft=PurchaseRequestExtraction(items=[RequestItem(product='Труба',product_group='round_pipe',size_designation='25×2',quantity='0.004',unit='т',source_text=text)])
    attempts=[]
    def respond(request):
        attempts.append(request.headers.get('x-stainless-retry-count'))
        if len(attempts)==1:return httpx2.Response(500,json={'error':{'message':'temporary','type':'server_error'}})
        return httpx2.Response(200,json={'id':'test','object':'chat.completion','created':0,'model':'gpt-6-luna','choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':draft.model_dump_json(),'refusal':None}}]})
    with DefaultHttpxClient(transport=httpx2.MockTransport(respond),event_hooks={'request':[http_attempt]}) as http:
        client=OpenAI(api_key='test-only',http_client=http,max_retries=1)
        ai=object.__new__(AIService);ai._client=client
        with patch('openai._base_client.time.sleep'),operation() as trace:
            assert len(ai.parse_request(text).items)==1
        assert trace.calls==1 and trace.http_attempts==2 and trace.sdk_retries==1
        assert attempts==['0','1']
