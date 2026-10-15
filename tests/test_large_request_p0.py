from decimal import Decimal

from types import SimpleNamespace

from clarifications import parse_bulk_quantity, parse_dimension_answers
from schemas import PurchaseRequest, RequestItem
from source_fidelity import PRODUCT_ROW, logical_source_rows, validate_source_coverage
from telegram_service import ActiveItem, ActiveRequest, TelegramService, exact_source_attributes
from formatter import format_normalized_request
from validator import _group_clarification_questions
from database import Database
from repositories.telegram_repository import TelegramRepository
from unittest.mock import Mock
from request_operations import PendingOperation, RequestOperation, request_fingerprint
from clarifications import PendingClarification
from pathlib import Path
import pytest


def _request_item(source, **values):
    return RequestItem(source_text=source, **values)


def test_quoted_multiline_rows_are_counted_once_and_keep_source_mapping():
    source = (
        '"Уголки стальные горячекатаные равнополочные 100х100х8 / С255-4 ГОСТ 27772-2015\n'
        'ГОСТ 8509-93" 0,102 т\n'
        '"Швеллеры стальные горячекатаные 10У / С345-6 ГОСТ\n'
        '27772-2015" ГОСТ 8240-97 0,215 т'
    )
    rows = logical_source_rows(source)
    assert len(rows) == 2
    draft = PurchaseRequest(items=[_request_item(row) for row in rows])
    assert validate_source_coverage(draft, source) == [1, 2]


@pytest.mark.parametrize(('filename', 'expected'), [
    ('p0_angles_channels_38.txt', 38),
    ('p0_mixed_26.txt', 26),
    ('p0_mixed_26_normalized.txt', 26),
])
def test_real_large_spec_source_rows_have_complete_one_to_one_coverage(filename, expected):
    """Exact user-provided inputs, exercised with deterministic source fragments."""
    source = (Path(__file__).parent / 'fixtures' / filename).read_text(encoding='utf-8')
    rows = [row for row in logical_source_rows(source) if PRODUCT_ROW.search(row)]
    assert len(rows) == expected
    draft = PurchaseRequest(items=[_request_item(row) for row in rows])
    mapped = validate_source_coverage(draft, source)
    assert mapped == list(range(1, expected + 1))


def test_logical_source_rows_support_50_and_100_positions_without_merging():
    for count in (50, 100):
        rows = [f'Круг {index} Ст3 ГОСТ 2590-2006 — 1 т' for index in range(1, count + 1)]
        text = '\n'.join(rows)
        draft = PurchaseRequest(items=[_request_item(row) for row in rows])
        mapped = validate_source_coverage(draft, text)
        assert len(logical_source_rows(text)) == count
        assert mapped == list(range(1, count + 1))


def test_bulk_quantity_parses_all_and_source_position_ranges_without_rounding():
    items = [
        SimpleNamespace(source_text=f'Круг {size} Ст3', item_id=f'i{index}', source_position=index,
                        unit='шт', quantity=None)
        for index, size in enumerate((8, 10, 12, 14), 1)
    ]
    request = type('Request', (), {'items': items})()
    all_items = parse_bulk_quantity('все позиции по одной штуке', request)
    assert all_items == [(f'i{n}', Decimal('1'), 'шт') for n in range(1, 5)]
    ranged = parse_bulk_quantity('поз. 1–3 по 5 шт', request)
    assert ranged == [(f'i{n}', Decimal('5'), 'шт') for n in range(1, 4)]
    assert parse_bulk_quantity('каждого по 1 шт', request) == all_items


def test_bulk_quantity_refuses_incompatible_units_atomically():
    items = [
        SimpleNamespace(source_text='Круг 8 Ст3', item_id='i1', source_position=1, unit='шт', quantity=None),
        SimpleNamespace(source_text='Круг 10 Ст3', item_id='i2', source_position=2, unit='т', quantity=Decimal('0.2')),
    ]
    assert parse_bulk_quantity('все по 1 шт', type('Request', (), {'items': items})()) == []
    assert items[0].quantity is None and items[1].quantity == Decimal('0.2')


def test_bulk_quantity_requires_explicit_replacement_to_overwrite_and_supports_explicit_replace():
    item = SimpleNamespace(item_id='item-1', source_position=1, unit='шт', quantity=Decimal('5'))
    request = type('Request', (), {'items': [item]})()
    assert parse_bulk_quantity('все по 1 шт', request) == []
    assert parse_bulk_quantity('Замени количество у всех позиций на 1 шт.', request) == [
        ('item-1', Decimal('1'), 'шт')]


@pytest.mark.parametrize('value', ['0', '-2', '1e-400', 'abc'])
def test_dimension_parser_rejects_zero_negative_exponent_and_invalid_values(value):
    item = SimpleNamespace(source_position=3)
    request = SimpleNamespace(items=[item], missing_fields=['items[0].width_mm'])
    parsed = parse_dimension_answers(f'Поз. 3 ширина {value} мм', request)
    assert parsed == {}


def test_dimension_parser_keeps_decimal_comma_exact():
    item = SimpleNamespace(source_position=3)
    request = SimpleNamespace(items=[item], missing_fields=['items[0].width_mm'])
    assert parse_dimension_answers('Поз. 3 ширина 2,5 мм', request) == {
        3: {'width_mm': Decimal('2.5')}}


def test_repeated_field_clarifications_are_grouped_by_position():
    request = PurchaseRequest(items=[
        RequestItem(product='Труба профильная', product_group='profile_pipe',
                    width_mm=None, height_mm=None, wall_thickness_mm=None,
                    source_text='Труба профильная 40×40'),
        RequestItem(product='Лист', product_group='sheet',
                    thickness_mm=6, width_mm=None, length_mm=None,
                    source_text='Лист 6 мм'),
    ])
    questions = _group_clarification_questions(request, [
        'Позиция 1: уточните ширину.',
        'Позиция 1: уточните высоту.',
        'Позиция 1: уточните толщину стенки.',
        'Позиция 2: уточните ширину.',
        'Позиция 2: уточните длину.',
    ], [3, 5])
    joined = '\n'.join(questions)
    assert questions[0] == 'Требуются уточнения:'
    assert 'Поз. 3 — Труба профильная: ширину, высоту и толщину стенки.' in joined
    assert 'Поз. 5 — Лист: ширину и длину.' in joined


def test_open_quantity_clarification_accepts_bulk_piece_answer_atomically(tmp_path):
    repo = TelegramRepository(Database(tmp_path / 'bulk.sqlite3'))
    ai = Mock()
    service = TelegramService(ai, repo)
    rows = [f'Круг {size} Ст3' for size in (8, 10, 12, 14)]
    ai.parse_request.return_value = PurchaseRequest(items=[
        RequestItem(product='Круг', product_group='round_bar', size_designation=str(size),
                    diameter_mm=size, source_text=row)
        for size, row in zip((8, 10, 12, 14), rows)
    ])
    service.process(1, '\n'.join(rows))
    service.process(1, 'все позиции по одной штуке')
    saved = repo.load(1)
    assert [(item.quantity, item.unit) for item in saved.items] == [(Decimal('1'), 'шт')] * 4
    assert saved.pending_clarification is None


def test_technical_sheet_form_is_restored_and_rendered():
    source = 'Лист Х/К 0,8×1000×1500 12Х18Н10Т БТ-ПН-О ГОСТ 19904-90, ГОСТ 5582-75 — 29,58 т'
    item = RequestItem(product='Лист Х/К', product_group='sheet', size_designation='0,8×1000×1500',
                       grade=None, form=None, gost_tu=None, quantity='29,58', unit='т', source_text=source)
    exact_source_attributes(item)
    formatted = format_normalized_request(PurchaseRequest(items=[item]))
    assert item.form == 'БТ-ПН-О'
    assert formatted == 'Лист Х/К 0,8×1000×1500 12Х18Н10Т БТ-ПН-О ГОСТ 19904-90, ГОСТ 5582-75 — 29,58 т'


def test_grouped_dimension_answer_accepts_partial_followup_without_reasking_known_fields(tmp_path):
    repo = TelegramRepository(Database(tmp_path / 'grouped.sqlite3'))
    service = TelegramService(Mock(), repo)
    original = RequestItem(product='Труба профильная', product_group='profile_pipe',
                           size_designation='40×40', quantity='1', unit='м',
                           source_text='Труба профильная 40×40 — 1 м')
    item = ActiveItem(**original.model_dump(), item_id='item-3', original=original.model_copy(deep=True),
                       source_position=3)
    active = ActiveRequest(request_id='request-1', source_text=original.source_text,
                           items=[item], is_valid=False,
                           missing_fields=['items[2].width_mm', 'items[2].height_mm', 'items[2].wall_thickness_mm'])
    active.pending_operation = PendingOperation(
        user_id=1, request_id=active.request_id, request_fingerprint=request_fingerprint(active),
        operation=RequestOperation(kind='edit_fields'), command=active.source_text,
        question='Требуются уточнения:\nПоз. 3 — Труба профильная: ширину, высоту и толщину стенки.',
        expected_answer='details')
    repo.save(1, active)

    service.process(1, 'Поз. 3 ширина 40 мм, высота 40 мм')
    partial = repo.load(1)
    assert partial.items[0].width_mm == 40 and partial.items[0].height_mm == 40
    assert partial.pending_operation is not None
    assert 'толщину стенки' in partial.pending_operation.question
    assert 'ширину' not in partial.pending_operation.question and 'высоту' not in partial.pending_operation.question

    service.process(1, 'Поз. 3 толщина стенки 4 мм')
    completed = repo.load(1)
    assert completed.items[0].wall_thickness_mm == 4
    assert completed.missing_fields == [] and completed.is_valid
    assert completed.pending_operation is None


def test_bulk_quantity_conflict_does_not_silently_overwrite_existing_values(tmp_path):
    repo = TelegramRepository(Database(tmp_path / 'bulk_conflict.sqlite3'))
    service = TelegramService(Mock(), repo)
    first = RequestItem(product='\u041a\u0440\u0443\u0433', product_group='round_bar', size_designation='8',
                        diameter_mm=8, quantity='5', unit='\u0448\u0442')
    second = RequestItem(product='\u041a\u0440\u0443\u0433', product_group='round_bar', size_designation='10', diameter_mm=10)
    items = [
        ActiveItem(**first.model_dump(), item_id='item-1', original=first.model_copy(deep=True), source_position=1),
        ActiveItem(**second.model_dump(), item_id='item-2', original=second.model_copy(deep=True), source_position=2),
    ]
    active = ActiveRequest(request_id='bulk-conflict', source_text='spec', items=items,
                           pending_clarification=PendingClarification(request_id='bulk-conflict', item_id='item-2',
                               field='quantity', kind='number', question='quantity?'))
    repo.save(7, active)
    service.process(7, '\u0432\u0441\u0435 \u043f\u043e\u0437\u0438\u0446\u0438\u0438 \u043f\u043e 1 \u0448\u0442')
    saved = repo.load(7)
    assert saved.items[0].quantity == Decimal('5')
    assert saved.items[1].quantity is None
    assert saved.pending_clarification is not None or saved.pending_operation is not None
    service.process(7, '\u0417\u0430\u043c\u0435\u043d\u0438 \u043a\u043e\u043b\u0438\u0447\u0435\u0441\u0442\u0432\u043e \u0443 \u0432\u0441\u0435\u0445 \u043f\u043e\u0437\u0438\u0446\u0438\u0439 \u043d\u0430 1 \u0448\u0442')
    replaced = repo.load(7)
    assert [item.quantity for item in replaced.items] == [Decimal('1'), Decimal('1')]
    assert replaced.items[0].original.quantity == Decimal('5')
    assert replaced.clarification_answers[-1].startswith('\u0417\u0430\u043c\u0435\u043d\u0438')


def test_dimension_answers_use_source_positions_not_array_indexes(tmp_path):
    repo = TelegramRepository(Database(tmp_path / 'sparse_positions.sqlite3'))
    service = TelegramService(Mock(), repo)
    originals = [
        RequestItem(product='\u0422\u0440\u0443\u0431\u0430 \u043f\u0440\u043e\u0444\u0438\u043b\u044c\u043d\u0430\u044f', product_group='profile_pipe',
                    size_designation='80x80', quantity='1', unit='\u043c'),
        RequestItem(product='\u041b\u0438\u0441\u0442', product_group='sheet', size_designation='6', thickness_mm=6,
                    quantity='1', unit='\u0448\u0442'),
    ]
    items = [
        ActiveItem(**originals[0].model_dump(), item_id='pipe-3', original=originals[0].model_copy(deep=True), source_position=3),
        ActiveItem(**originals[1].model_dump(), item_id='sheet-7', original=originals[1].model_copy(deep=True), source_position=7),
    ]
    active = ActiveRequest(request_id='sparse', source_text='spec', items=items, is_valid=False,
                           missing_fields=['items[0].width_mm', 'items[0].height_mm', 'items[0].wall_thickness_mm',
                                           'items[1].width_mm', 'items[1].length_mm'])
    active.pending_operation = PendingOperation(user_id=7, request_id=active.request_id,
        request_fingerprint=request_fingerprint(active), operation=RequestOperation(kind='edit_fields'),
        command='spec', question='details?', expected_answer='details')
    repo.save(7, active)
    service.process(7, '\u041f\u043e\u0437. 3 \u0448\u0438\u0440\u0438\u043d\u0430 80 \u043c\u043c, \u0432\u044b\u0441\u043e\u0442\u0430 80 \u043c\u043c\n'
                   '\u041f\u043e\u0437. 7 \u0448\u0438\u0440\u0438\u043d\u0430 1500 \u043c\u043c')
    partial = repo.load(7)
    assert (partial.items[0].width_mm, partial.items[0].height_mm) == (80, 80)
    assert partial.items[1].width_mm == 1500
    assert partial.missing_fields == ['items[0].wall_thickness_mm', 'items[1].length_mm']
    assert not partial.is_valid
    service.process(7, '\u041f\u043e\u0437. 3 \u0442\u043e\u043b\u0449\u0438\u043d\u0430 \u0441\u0442\u0435\u043d\u043a\u0438 4 \u043c\u043c\n'
                   '\u041f\u043e\u0437. 7 \u0434\u043b\u0438\u043d\u0430 6000 \u043c\u043c')
    completed = repo.load(7)
    assert completed.items[0].wall_thickness_mm == 4
    assert completed.items[1].length_mm == 6000
    assert completed.missing_fields == [] and completed.is_valid


def test_dimension_answer_rejects_float_underflow_instead_of_marking_field_resolved(tmp_path):
    repo = TelegramRepository(Database(tmp_path / 'tiny_dimension.sqlite3'))
    service = TelegramService(Mock(), repo)
    original = RequestItem(product='\u041b\u0438\u0441\u0442', product_group='sheet', quantity='1', unit='\u0448\u0442')
    item = ActiveItem(**original.model_dump(), item_id='sheet-1', original=original.model_copy(deep=True), source_position=1)
    active = ActiveRequest(request_id='tiny', source_text='sheet', items=[item], is_valid=False,
                           missing_fields=['items[0].width_mm'])
    active.pending_operation = PendingOperation(user_id=7, request_id=active.request_id,
        request_fingerprint=request_fingerprint(active), operation=RequestOperation(kind='edit_fields'),
        command='sheet', question='width?', expected_answer='details')
    repo.save(7, active)
    service.process(7, '\u0448\u0438\u0440\u0438\u043d\u0430 0.' + '0' * 400 + '1 \u043c\u043c')
    saved = repo.load(7)
    assert not saved.is_valid
    assert 'items[0].width_mm' in saved.missing_fields
    service.process(7, '\u0448\u0438\u0440\u0438\u043d\u0430 -2 \u043c\u043c')
    rejected = repo.load(7)
    assert rejected.items[0].width_mm is None
    assert 'items[0].width_mm' in rejected.missing_fields and not rejected.is_valid
