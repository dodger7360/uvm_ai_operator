"""Offline regressions of the reported Telegram dialogues, with real SDK parsing."""
from collections import deque
from decimal import Decimal
import json

import httpx2
import pytest
from openai import DefaultHttpxClient, OpenAI

from ai import AIService
from database import Database
from repositories.telegram_repository import TelegramRepository
from schemas import PurchaseRequestExtraction, RequestItem
from telegram_service import TelegramService, MessagePlan, Edit
from diagnostics import PipelineError, SemanticExtractionError
from formatter import format_normalized_request, ordered_consolidation
from request_operations import request_fingerprint
from workbench import NormalizationWorkbench


LINES = [
    'Арматура 12 А500С 2 т',
    'Арматура 12 А500С 350 кг',
    'Труба проф. 40*40*3 1200 кг',
    'Труба проф. 40х40х3 0,8 т',
    'Лист 6*1500*6000 ст3 4 шт',
    'Уголок 100*100*4 ст3 500 кг',
]
SOURCE = '\n'.join(LINES)
EXPECTED = ('Труба профильная 40×40×3 — 2 т\n'
            'Лист 6×1500×6000 Ст3 — 4 шт. = 1,696 т\n'
            'Уголок 100×100×4 Ст3 — 0,5 т\n'
            'Арматура А500С Ø12 — 2,35 т')


def six_rows():
    facts = [
        ('Арматура', 'rebar', '12', 'А500С', '2', 'т'),
        ('Арматура', 'rebar', '12', 'А500С', '350', 'кг'),
        ('Труба проф.', 'profile_pipe', '40*40*3', None, '1200', 'кг'),
        ('Труба проф.', 'profile_pipe', '40х40х3', None, '0.8', 'т'),
        ('Лист', 'sheet', '6*1500*6000', 'Ст3', '4', 'шт'),
        ('Уголок', 'angle', '100*100*4', 'Ст3', '500', 'кг'),
    ]
    return PurchaseRequestExtraction(items=[RequestItem(product=p, product_group=g,
        size_designation=s, grade=grade, quantity=q, unit=u, source_text=line)
        for (p, g, s, grade, q, u), line in zip(facts, LINES)])


@pytest.fixture
def context(tmp_path):
    responses, calls = deque(), []
    def respond(request):
        calls.append(json.loads(request.content))
        assert responses, 'Unexpected AI call in a deterministic dialogue'
        return httpx2.Response(200, json={'id': 'offline-test', 'object': 'chat.completion',
            'created': 0, 'model': 'gpt-6-luna', 'choices': [{'index': 0,
            'finish_reason': 'stop', 'message': {'role': 'assistant',
            'content': responses.popleft().model_dump_json(), 'refusal': None}}]})
    with DefaultHttpxClient(transport=httpx2.MockTransport(respond)) as http:
        ai = object.__new__(AIService)
        ai._client = OpenAI(api_key='offline-test', http_client=http, max_retries=0)
        repo = TelegramRepository(Database(tmp_path / 'requests.sqlite3'))
        yield TelegramService(ai, repo), repo, responses, calls
        assert not responses, 'Unused offline responses'


def start(context):
    service, repo, responses, _ = context
    responses.append(six_rows())
    assert service.process(1, SOURCE) == [EXPECTED]
    return repo.load(1)


def test_a_grade_edit_targets_all_sources_of_display_group(context):
    service, repo, _, calls = context
    before = start(context)
    answer = service.process(1, 'В первой позиции допиши ст3')
    assert answer[0].startswith('Труба профильная 40×40×3 Ст3 — 2 т')
    after = repo.load(1)
    pipes = [i for i in after.items if i.product_group == 'profile_pipe']
    assert [i.grade for i in pipes] == ['Ст3', 'Ст3']
    assert [i.original for i in after.items] == [i.original for i in before.items]
    assert [i for i in after.items if i.product_group != 'profile_pipe'] == [
        i for i in before.items if i.product_group != 'profile_pipe']
    assert len(calls) == 1


def test_b_diameter_first_is_a_persisted_display_setting(context):
    service, repo, _, calls = context
    before = start(context)
    result = service.process(1, 'Арматура напиши диаметр вначале')
    assert result[0].endswith('Арматура Ø12 А500С — 2,35 т')
    assert repo.load(1).items == before.items
    restarted = TelegramService(service.ai, TelegramRepository(repo.database))
    assert restarted.process(1, 'Покажи текущую заявку')[0].endswith('Арматура Ø12 А500С — 2,35 т')
    assert len(calls) == 1


def test_c_pasted_mass_is_not_the_original_piece_quantity(context):
    service, repo, responses, _ = context
    text = 'Лист 6×1500×6000 Ст3 — 4 шт. = 1,696 т'
    responses.append(PurchaseRequestExtraction(items=[RequestItem(product='Лист',
        product_group='sheet', size_designation='6×1500×6000', grade='Ст3',
        quantity='1.696', unit='т', source_text=text)]))
    assert service.process(1, text) == [text]
    item = repo.load(1).items[0]
    assert item.quantity == Decimal('4') and item.unit == 'шт'
    assert item.original.quantity == Decimal('4') and item.original.unit == 'шт'
    assert item.mass_calculation.total_t == Decimal('1.6956')


def test_d_alternative_steel_confirmation_survives_sqlite_restart(context):
    service, repo, _, calls = context
    result = start_alternatives(context)
    assert 'альтернатив' in result[-1]
    assert repo.load(1).pending_operation is not None
    assert not repo.load(1).is_valid
    restarted = TelegramService(service.ai, TelegramRepository(repo.database))
    restarted.process(1, 'да')
    after = repo.load(1)
    assert len(after.items) == 3 and after.pending_operation is None
    assert after.is_valid and after.clarification_answers == ['да']
    assert all(i.grade == 'Ст20 (или 09Г2С)' for i in after.items)
    assert {i.quantity for i in after.items} == {Decimal('83.704'), Decimal('2.1084'), Decimal('40.87')}
    assert all(i.unit == 'пог. м' and i.original.unit == 'пог. м' for i in after.items)
    assert len(calls) == 1


def start_alternatives(context):
    service, repo, responses, calls = context
    rows = [f'Труба стальная электросварная прямошовная {size} ст.20/09Г2С {q} пог.м'
            for size, q in [('920х10', '83,704'), ('530х12', '2,1084'), ('273х5', '40,87')]]
    text = 'Наименование, технические характеристики Количество Ед. изм.\n' + '\n'.join(rows)
    responses.append(PurchaseRequestExtraction(items=[RequestItem(product='Труба э/с',
        product_group='round_pipe', size_designation=size, grade='Ст20/09Г2С',
        quantity=q.replace(',', '.'), unit='пог. м', source_text=row)
        for size, q, row in zip(['920х10', '530х12', '273х5'], ['83,704', '2,1084', '40,87'], rows)]))
    return service.process(1, text)


def test_e_show_current_does_not_extract_or_change_data(context):
    service, repo, _, calls = context
    before = start(context)
    assert service.process(1, 'Покажи текущую заявку') == [EXPECTED]
    assert repo.load(1) == before and len(calls) == 1


@pytest.mark.parametrize('reply', ['Для суммы', 'К обеим', 'обеим', 'да'])
def test_a_pending_group_grade_proposal_completes_without_new_extraction(context, reply):
    service, repo, responses, calls = context
    before = start(context)
    pipe = next(i for i in before.items if i.product_group == 'profile_pipe')
    responses.append(MessagePlan(intent='clarify', question='Применить Ст3 к обеим исходным строкам?',
        edits=[Edit(item_id=pipe.item_id, field='grade', value='Ст3')]))
    service.process(1, 'В первой позиции поставь марку Ст3')
    assert len(repo.load(1).pending_operation.operation.edits) == 2
    restarted = TelegramService(service.ai, TelegramRepository(repo.database))
    assert restarted.process(1, reply)[0].startswith('Труба профильная 40×40×3 Ст3 — 2 т')
    assert repo.load(1).pending_operation is None
    assert 'Нет ожидающего' in restarted.process(1, 'К обеим')[0]
    assert len(calls) == 2 and repo.load(1).source_text == SOURCE


def test_a_two_scope_answers_preserve_command_across_handler_restarts(context):
    service, repo, responses, calls = context
    before = start(context)
    responses.append(MessagePlan(intent='clarify', question='К каким исходным строкам применить Ст3?'))
    service.process(1, 'В первой позиции поставь марку Ст3')
    responses.append(MessagePlan(intent='clarify', question='Применить Ст3 к обеим исходным строкам?'))
    service = TelegramService(service.ai, TelegramRepository(repo.database))
    service.process(1, 'Для суммы')
    assert repo.load(1).pending_operation.answers == ['Для суммы']
    pipe = next(i for i in before.items if i.product_group == 'profile_pipe')
    responses.append(MessagePlan(intent='edit', edits=[Edit(item_id=pipe.item_id, field='grade', value='Ст3')]))
    service = TelegramService(service.ai, TelegramRepository(repo.database))
    assert service.process(1, 'К обеим')[0].startswith('Труба профильная 40×40×3 Ст3 — 2 т')
    sent = calls[-1]['messages'][-1]['content']
    assert 'В первой позиции' in sent and 'Для суммы' in sent and 'К обеим' in sent
    assert len(calls) == 4 and repo.load(1).pending_operation is None


def test_a_model_grade_edit_is_scoped_to_entire_display_group(context):
    service, repo, responses, _ = context
    before = start(context)
    pipe = next(i for i in before.items if i.product_group == 'profile_pipe')
    responses.append(MessagePlan(intent='edit', edits=[Edit(item_id=pipe.item_id, field='grade', value='Ст3')]))
    service.process(1, 'В первой позиции поставь марку Ст3')
    assert all(i.grade == 'Ст3' for i in repo.load(1).items if i.product_group == 'profile_pipe')


def test_a_wrong_model_target_is_rejected_even_with_valid_item_id(context):
    service, repo, responses, _ = context
    before = start(context)
    wrong = next(i for i in before.items if i.product_group == 'angle')
    for _ in range(2):
        responses.append(MessagePlan(intent='edit', edits=[Edit(item_id=wrong.item_id, field='grade', value='09Г2С')]))
    with pytest.raises(SemanticExtractionError):
        service.process(1, 'В первой позиции поставь марку Ст3')
    assert repo.load(1) == before


def test_a_quantity_of_merged_row_never_chooses_or_distributes_a_source(context):
    service, repo, _, calls = context
    before = start(context)
    response = service.process(1, 'В первой позиции измени количество на 5 т')
    assert 'исходные строки' in response[0]
    for answer in ['Для суммы', 'К обеим']:
        assert service.process(1, answer) == response
    assert repo.load(1).items == before.items and len(calls) == 1
    service.process(1, 'В исходной позиции 3 измени количество на 1500 кг')
    after = repo.load(1)
    assert format_normalized_request(after).startswith('Труба профильная 40×40×3 — 2,3 т')
    assert next(i for i in after.items if i.source_position == 3).original.quantity == Decimal('1200')


def test_a_conflicting_characteristics_are_not_broadcast_as_one_group(context):
    service, repo, responses, _ = context
    draft = six_rows()
    draft.items[3].source_text = LINES[3].replace('0,8 т', 'ГОСТ 30245 0,8 т')
    responses.append(draft)
    service.process(1, SOURCE.replace(LINES[3], draft.items[3].source_text))
    assert len([g for g in ordered_consolidation(repo.load(1).items) if g.item.product_group == 'profile_pipe']) == 2
    service.process(1, 'В первой позиции допиши Ст3')
    pipes = [i for i in repo.load(1).items if i.product_group == 'profile_pipe']
    assert sum(i.grade == 'Ст3' for i in pipes) == 1
    assert next(i for i in pipes if i.gost_tu).grade is None


@pytest.mark.parametrize('reply,expected_grade', [('да', 'А500'), ('нет', 'А500С'), ('отмена', 'А500С')])
def test_b_changed_grade_in_template_requires_explicit_confirmation(context, reply, expected_grade):
    service, repo, _, calls = context
    before = start(context)
    service.process(1, 'Арматура напиши диаметр вначале')
    result = service.process(1, 'Пропиши арматура ф12 а500 - 2,35 т')
    assert 'А500 вместо А500С' in result[0]
    assert all(i.grade == 'А500С' for i in repo.load(1).items if i.product_group == 'rebar')
    service = TelegramService(service.ai, TelegramRepository(repo.database))
    service.process(1, reply)
    after = repo.load(1)
    assert all(i.grade == expected_grade for i in after.items if i.product_group == 'rebar')
    assert [i.original for i in after.items] == [i.original for i in before.items]
    assert all((a.quantity, a.unit) == (b.quantity, b.unit) for a, b in zip(after.items, before.items))
    assert len(calls) == 1 and after.pending_operation is None


def test_b_template_with_same_grade_only_changes_presentation(context):
    service, repo, _, calls = context
    before = start(context)
    assert service.process(1, 'Пропиши арматура ф12 а500с - 2,35 т')[0].endswith('Арматура Ø12 А500С — 2,35 т')
    assert repo.load(1).items == before.items and len(calls) == 1


def test_b_changed_quantity_in_template_is_not_a_cosmetic_edit(context):
    service, repo, _, calls = context
    before = start(context)
    assert 'Количество' in service.process(1, 'Пропиши арматура ф12 а500с - 2,36 т')[0]
    assert repo.load(1) == before and len(calls) == 1


def test_b_model_can_only_request_a_typed_display_setting(context):
    service, repo, responses, _ = context
    before = start(context)
    responses.append(MessagePlan(intent='display', display_format='rebar_diameter_first'))
    assert service.process(1, 'Для арматуры сначала размер, потом марка')[0].endswith('Арматура Ø12 А500С — 2,35 т')
    assert repo.load(1).items == before.items
    responses.append(MessagePlan(intent='display', display_format='rebar_grade_first'))
    assert service.process(1, 'Верни порядок обозначений арматуры')[0].endswith('Арматура А500С Ø12 — 2,35 т')


def test_c_repeated_normalization_preserves_pieces_and_exact_mass(context):
    service, repo, responses, _ = context
    text = 'Лист 6×1500×6000 Ст3 — 4 шт. = 1,696 т'
    for _ in range(2):
        responses.append(PurchaseRequestExtraction(items=[RequestItem(product='Лист', product_group='sheet',
            size_designation='6×1500×6000', grade='Ст3', quantity='1.696', unit='т', source_text=text)]))
        assert service.process(1, text) == [text]
        item = repo.load(1).items[0]
        assert item.quantity == Decimal('4') and item.mass_calculation.total_t == Decimal('1.6956')
        assert item.source_text == text and item.original.source_text == text
    service.process(1, 'В первой позиции измени количество на 5 шт')
    item = repo.load(1).items[0]
    assert item.quantity == Decimal('5') and item.unit == 'шт'
    assert item.original.quantity == Decimal('4') and item.original.unit == 'шт'
    assert item.mass_calculation.total_t == Decimal('2.1195')


def test_c_two_pasted_sheet_rows_consolidate_without_losing_sources(context):
    service, repo, responses, _ = context
    row = 'Лист 6×1500×6000 Ст3 — 4 шт. = 1,696 т'
    responses.append(PurchaseRequestExtraction(items=[RequestItem(product='Лист', product_group='sheet',
        size_designation='6×1500×6000', grade='Ст3', quantity='4', unit='шт', source_text=row) for _ in range(2)]))
    assert service.process(1, row + '\n' + row) == ['Лист 6×1500×6000 Ст3 — 8 шт. = 3,391 т']
    active = repo.load(1)
    assert len(active.items) == 2 and all(i.quantity == 4 and i.unit == 'шт' for i in active.items)
    assert sum(i.mass_calculation.total_t for i in active.items) == Decimal('3.3912')


def test_c_model_fragment_may_omit_only_the_rendered_mass_annotation(context):
    service, repo, responses, _ = context
    text = 'Лист 6×1500×6000 Ст3 — 4 шт. = 1,696 т'
    responses.append(PurchaseRequestExtraction(items=[RequestItem(product='Лист', product_group='sheet',
        size_designation='6×1500×6000', grade='Ст3', quantity='4', unit='шт', source_text=text.split(' = ')[0])]))
    assert service.process(1, text) == [text]
    assert repo.load(1).items[0].source_text == text


def test_c_workbench_uses_same_literal_quantity_rule(context):
    service, _, responses, _ = context
    text = 'Лист 6×1500×6000 Ст3 — 4 шт. = 1,696 т'
    responses.append(PurchaseRequestExtraction(items=[RequestItem(product='Лист', product_group='sheet',
        size_designation='6×1500×6000', grade='Ст3', quantity='1.696', unit='т', source_text=text)]))
    item = NormalizationWorkbench(service.ai).process(text).current_request.items[0]
    assert item.quantity == Decimal('4') and item.unit == 'шт'
    assert item.mass_calculation.total_t == Decimal('1.6956')


@pytest.mark.parametrize('answer', ['да', 'нет', 'обеим', 'К обеим', 'Для суммы'])
def test_e_short_answer_without_pending_never_changes_or_extracts(context, answer):
    service, repo, _, calls = context
    before = start(context)
    assert 'Нет ожидающего' in service.process(1, answer)[0]
    assert repo.load(1) == before and len(calls) == 1


def test_e_second_display_position_and_multiple_corrections(context):
    service, repo, _, _ = context
    before = start(context)
    service.process(1, 'Во второй позиции поменяй количество на 5 т')
    sheet = next(i for i in repo.load(1).items if i.product_group == 'sheet')
    assert sheet.quantity == 5 and sheet.unit == 'т'
    assert sheet.original.quantity == 4 and sheet.original.unit == 'шт'
    service.process(1, 'В первой позиции допиши Ст3')
    service.process(1, 'В первой позиции замени марку на 09Г2С')
    after = repo.load(1)
    assert all(i.grade == '09Г2С' for i in after.items if i.product_group == 'profile_pipe')
    assert after.source_text == before.source_text
    assert [i.original for i in after.items] == [i.original for i in before.items]


def test_e_model_cannot_turn_an_explicit_edit_into_a_new_request(context):
    service, repo, responses, _ = context
    before = start(context)
    responses.append(MessagePlan(intent='new'))
    answer = service.process(1, 'В первой позиции добавь обычную сталь')
    assert 'новая заявка не создавалась' in answer[0]
    assert repo.load(1) == before


def test_e_undo_is_explicitly_unsupported_not_a_cancel_or_new_request(context):
    service, repo, _, calls = context
    before = start(context)
    assert 'пока не поддерживается' in service.process(1, 'Отмени последнее изменение')[0]
    assert repo.load(1) == before and len(calls) == 1


def test_legacy_json_defaults_and_fingerprint_are_compatible(context):
    service, repo, _, _ = context
    active = start(context)
    before = request_fingerprint(active)
    payload = active.model_dump()
    payload.pop('rebar_diameter_first')
    with repo.database.connection() as c:
        from pydantic_core import to_json
        c.execute('UPDATE telegram_active SET request_json=? WHERE user_id=1', (to_json(payload).decode(),))
        tables_before = c.execute('SELECT sql FROM sqlite_master ORDER BY name').fetchall()
    restored = repo.load(1)
    assert restored.rebar_diameter_first is False and request_fingerprint(restored) == before
    assert TelegramService(service.ai, TelegramRepository(repo.database)).process(1, 'Покажи текущую заявку') == [EXPECTED]
    with repo.database.connection() as c:
        assert c.execute('SELECT sql FROM sqlite_master ORDER BY name').fetchall() == tables_before
        assert c.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


@pytest.mark.parametrize('answer', ['нет', 'отмена', '/cancel'])
def test_d_declining_alternatives_does_not_assign_or_erase_source_facts(context, answer):
    service, repo, _, calls = context
    start_alternatives(context)
    before = repo.load(1)
    restarted = TelegramService(service.ai, TelegramRepository(repo.database))
    assert 'отменена' in restarted.process(1, answer)[0]
    after = repo.load(1)
    assert after.items == before.items and after.source_text == before.source_text
    assert after.pending_operation is None and all(i.grade is None for i in after.items)
    assert 'Нет ожидающего' in restarted.process(1, 'Да')[0]
    assert len(calls) == 1


@pytest.mark.parametrize('answer', ['Да!', 'верно', 'подтверждаю'])
def test_d_short_confirmations_apply_only_literal_alternative_sets(context, answer):
    service, repo, _, _ = context
    start_alternatives(context)
    before = repo.load(1)
    service.process(1, answer)
    after = repo.load(1)
    assert all(i.grade == 'Ст20 (или 09Г2С)' for i in after.items)
    assert [(i.source_text, i.quantity, i.unit, i.original) for i in after.items] == [
        (i.source_text, i.quantity, i.unit, i.original) for i in before.items]
    assert all(i.mass_calculation is None for i in after.items)
    assert all(i.unit == 'пог. м' for i in after.items)
    assert ' = ' not in format_normalized_request(after)


def test_d_tampered_grade_proposal_is_not_executed(context):
    service, repo, _, _ = context
    start_alternatives(context)
    active = repo.load(1)
    active.pending_operation.operation.edits[0].value = 'Ст3 (или А500С)'
    repo.save(1, active)
    with pytest.raises(PipelineError) as error:
        service.process(1, 'Да')
    assert error.value.code == 'invalid_grade_confirmation'
    assert repo.load(1) == active


def test_d_stale_confirmation_cannot_modify_changed_request(context):
    service, repo, _, _ = context
    start_alternatives(context)
    active = repo.load(1)
    active.items[0].quantity = Decimal('99')
    repo.save(1, active)
    with pytest.raises(PipelineError) as error:
        service.process(1, 'Да')
    assert error.value.code == 'stale_operation'
    assert repo.load(1) == active


def test_d_other_session_cannot_answer_pending_question(context):
    service, repo, _, calls = context
    start_alternatives(context)
    before = repo.load(1)
    service.process(2, 'да')
    assert repo.load(2) is None and repo.load(1) == before and len(calls) == 1


def test_d_model_cannot_self_confirm_the_human_question(context):
    service, repo, responses, _ = context
    start_alternatives(context)
    before = repo.load(1)
    responses.append(MessagePlan(intent='operation', operation=before.pending_operation.operation))
    assert 'нужно подтвердить' in service.process(1, 'Укажи марки труб')[0]
    assert repo.load(1) == before


@pytest.mark.parametrize('reply,grade', [('да', '09Г2С'), ('нет', None)])
def test_a_unstated_model_grade_must_be_confirmed(context, reply, grade):
    service, repo, responses, _ = context
    before = start(context)
    pipe = next(i for i in before.items if i.product_group == 'profile_pipe')
    responses.append(MessagePlan(intent='edit', edits=[Edit(item_id=pipe.item_id, field='grade', value='09Г2С')]))
    question = service.process(1, 'В первой позиции поставь обычную марку')[0]
    assert 'не указанную явно' in question
    assert repo.load(1).items == before.items
    service.process(1, reply)
    assert all(i.grade == grade for i in repo.load(1).items if i.product_group == 'profile_pipe')


def test_a_unstated_model_grade_without_position_must_be_confirmed(context):
    service, repo, responses, _ = context
    before = start(context)
    pipe = next(i for i in before.items if i.product_group == 'profile_pipe')
    responses.append(MessagePlan(intent='edit', edits=[Edit(item_id=pipe.item_id, field='grade', value='09Г2С')]))
    question = service.process(1, 'Измени марку')[0]
    assert 'исходными позициями' in question
    assert repo.load(1).items == before.items
    assert repo.load(1).pending_operation is not None


def test_new_request_replaces_pending_state_but_preserves_history(context):
    service, repo, responses, _ = context
    start_alternatives(context)
    old = repo.load(1)
    responses.append(six_rows())
    assert service.process(1, SOURCE) == [EXPECTED]
    new = repo.load(1)
    assert new.request_id != old.request_id and new.pending_operation is None
    with repo.database.connection() as c:
        assert c.execute('SELECT count(*) FROM telegram_history').fetchone()[0] == 2
    assert 'Нет ожидающего' in service.process(1, 'да')[0]


def test_display_command_does_not_clear_a_pending_technical_proposal(context):
    service, repo, _, _ = context
    start(context)
    service.process(1, 'Пропиши арматура ф12 а500 - 2,35 т')
    before = repo.load(1)
    service.process(1, 'Арматура напиши диаметр вначале')
    after = repo.load(1)
    assert after.pending_operation == before.pending_operation and after.items == before.items
    service.process(1, 'да')
    assert all(i.grade == 'А500' for i in repo.load(1).items if i.product_group == 'rebar')


def test_pasted_annotation_does_not_relax_missing_source_rows(context):
    service, repo, responses, _ = context
    before = start(context)
    row = 'Лист 6×1500×6000 Ст3 — 4 шт. = 1,696 т'
    for _ in range(2):
        responses.append(PurchaseRequestExtraction(items=[RequestItem(product='Лист', product_group='sheet',
            size_designation='6×1500×6000', grade='Ст3', quantity='4', unit='шт', source_text=row)]))
    with pytest.raises(SemanticExtractionError) as error:
        service.process(1, row + '\n' + row)
    assert error.value.code == 'missing_positions' and repo.load(1) == before


def test_pasted_annotation_cannot_hide_a_missing_standard(context):
    service, repo, responses, _ = context
    before = start(context)
    row = 'Лист 6×1500×6000 Ст3 ГОСТ 19903 — 4 шт. = 1,696 т'
    for _ in range(2):
        responses.append(PurchaseRequestExtraction(items=[RequestItem(product='Лист', product_group='sheet',
            size_designation='6×1500×6000', grade='Ст3', quantity='4', unit='шт',
            source_text='Лист 6×1500×6000 Ст3')]))
    with pytest.raises(SemanticExtractionError):
        service.process(1, row)
    assert repo.load(1) == before


@pytest.mark.parametrize('answer', ['да', 'нет', 'обеим'])
def test_empty_session_short_answer_has_no_invented_context(context, answer):
    service, repo, _, calls = context
    assert 'Нет ожидающего' in service.process(1, answer)[0]
    assert repo.load(1) is None and calls == []


def test_model_grade_edit_is_canonical_without_losing_originals(context):
    service, repo, responses, _ = context
    before = start(context)
    pipe = next(i for i in before.items if i.product_group == 'profile_pipe')
    responses.append(MessagePlan(intent='edit', edits=[Edit(item_id=pipe.item_id, field='grade', value='ст3')]))
    assert service.process(1, 'В первой позиции поставь марку Ст3')[0].startswith('Труба профильная 40×40×3 Ст3 — 2 т')
    assert [i.original for i in repo.load(1).items] == [i.original for i in before.items]
