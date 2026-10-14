from decimal import Decimal

from types import SimpleNamespace

from clarifications import parse_bulk_quantity
from schemas import PurchaseRequest, RequestItem
from source_fidelity import PRODUCT_ROW, logical_source_rows, validate_source_coverage
from telegram_service import ActiveItem, ActiveRequest, TelegramService, exact_source_attributes
from formatter import format_normalized_request
from validator import _group_clarification_questions
from database import Database
from repositories.telegram_repository import TelegramRepository
from unittest.mock import Mock
from request_operations import PendingOperation, RequestOperation, request_fingerprint
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
    assert parse_bulk_quantity('все по 1 шт', type('Request', (), {'items': items})()) is None
    assert items[0].quantity is None and items[1].quantity == Decimal('0.2')


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

