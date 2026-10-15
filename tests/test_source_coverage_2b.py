"""Offline source accounting and retry policy through real TelegramService."""
from decimal import Decimal
from pathlib import Path
import re
from unittest.mock import Mock

import pytest

from database import Database
from diagnostics import SemanticExtractionError
from repositories.telegram_repository import TelegramRepository
from schemas import PurchaseRequest, RequestItem
from source_fidelity import logical_source_rows, validate_source_coverage
from telegram_service import TelegramService


RAW38 = (Path(__file__).parent / 'fixtures' / 'p0_angles_channels_38.txt').read_text(encoding='utf-8')
ROWS38 = logical_source_rows(RAW38)
FLAT38 = '\n'.join(' '.join(row.split()) for row in ROWS38)
CIRCLE = 'Круг 12 Ст3 ГОСТ 2590-2006 — 1 т'
HEXAGON = 'Шестигранник 20 Ст3 ГОСТ 2879-2006 — 2 т'


def angle_draft(text):
    # Literal dimensions and names, not a fabricated position count. The
    # application performs source mapping, quantities and attribute recovery.
    rows = logical_source_rows(text)
    items = []
    for row in rows:
        angle = 'угол' in row.casefold()
        match = re.search(r'\d+х\d+х\d+' if angle else r'\b\d+[ПУ]\b', row)
        assert match
        items.append(RequestItem(product='Уголок' if angle else 'Швеллер',
                                 product_group='angle' if angle else 'channel',
                                 size_designation=match[0], source_text=row))
    return PurchaseRequest(items=items)


def bars(rows):
    return PurchaseRequest(items=[RequestItem(
        product='Круг' if row.startswith('Круг') else 'Шестигранник',
        product_group='round_bar' if row.startswith('Круг') else 'rod',
        size_designation='12' if row.startswith('Круг') else '20',
        diameter_mm=12 if row.startswith('Круг') else None, source_text=row)
        for row in rows])


def context(tmp_path):
    repo = TelegramRepository(Database(tmp_path / 'isolated-2b.sqlite3'))
    ai = Mock()
    return TelegramService(ai, repo), ai, repo


@pytest.mark.parametrize('text', [RAW38, FLAT38], ids=['wrapped38', 'flat38'])
@pytest.mark.parametrize('omit_first', [False, True], ids=['complete', 'retry-real-omission'])
def test_38_source_positions_attributes_and_retry_are_wrap_independent(tmp_path, text, omit_first):
    service, ai, repo = context(tmp_path)

    def extract(value, **kwargs):
        draft = angle_draft(value)
        if omit_first and ai.parse_request.call_count == 1:
            draft.items.pop(19)  # Drop the real critical item, not a count-only stub.
        return draft

    ai.parse_request.side_effect = extract
    service.process(1, text)
    saved = repo.load(1)
    assert saved.is_valid and len(saved.items) == 38
    ordered = sorted(saved.items, key=lambda item: item.source_position)
    assert [item.source_position for item in ordered] == list(range(1, 39))
    assert len({item.item_id for item in ordered}) == 38
    expected_rows = logical_source_rows(text)
    assert [item.source_text for item in ordered] == expected_rows
    quantities = ['0.428', '0.103', '0.019', '2.22', '5.585', '0.317', '0.609', '7.213',
                  '19.935', '2.358', '0.102', '0.303', '5.924', '10.068', '0.073', '0.047',
                  '0.084', '1.2', '2.545', '0.193', '2.22', '0.086', '0.215', '0.562',
                  '0.279', '6.663', '0.104', '1.579', '0.102', '3.608', '0.513', '5.814',
                  '0.446', '0.073', '0.038', '51', '72.44', '0.287']
    assert [item.quantity for item in ordered] == list(map(Decimal, quantities))
    assert all(item.unit == 'т' for item in ordered)
    assert ordered[19].grade == 'ВСт3сп5'
    assert ordered[19].gost_tu == 'ГОСТ 8509-93, ГОСТ 535-05'
    assert ordered[32].grade == '09Г2С' and ordered[33].grade == 'С345-6'
    assert ai.parse_request.call_count == (2 if omit_first else 1)
    if omit_first:
        assert ai.parse_request.call_args_list[1].kwargs['reasoning_effort'] == 'medium'
    ai.interpret_message.assert_not_called()


def test_circle_and_hexagon_keep_literal_ids_characteristics_and_quantity(tmp_path):
    service, ai, repo = context(tmp_path)
    ai.parse_request.side_effect = lambda *args, **kwargs: bars([CIRCLE, HEXAGON])
    service.process(1, CIRCLE + '\n' + HEXAGON)
    saved = repo.load(1)
    ordered = sorted(saved.items, key=lambda item: item.source_position)
    assert [item.source_text for item in ordered] == [CIRCLE, HEXAGON]
    assert [item.quantity for item in ordered] == [Decimal('1'), Decimal('2')]
    assert [item.gost_tu for item in ordered] == ['ГОСТ 2590-2006', 'ГОСТ 2879-2006']
    ai.parse_request.assert_called_once()


def test_missing_hexagon_is_not_silently_accepted_and_previous_request_survives(tmp_path):
    service, ai, repo = context(tmp_path)
    ai.parse_request.side_effect = lambda *args, **kwargs: bars([CIRCLE])
    service.process(1, CIRCLE)
    before = repo.load(1).model_dump()
    ai.reset_mock()
    with pytest.raises(SemanticExtractionError) as error:
        service.process(1, CIRCLE + '\n' + HEXAGON)
    assert error.value.code == 'missing_positions'
    assert ai.parse_request.call_count == 2
    assert repo.load(1).model_dump() == before


@pytest.mark.parametrize('text', [
    'Уголок горячекатаный\nравнополочный 50х50х5 В Ст.3 сп.5\nГОСТ8509-93\nГОСТ535-05\n0,193 т',
    'Уголок горячекатаный равнополочный 50х50х5 В Ст.3 сп.5\nГОСТ\n8509-93\nГОСТ\n535-05\n0,193 т',
    'Уголок горячекатаный равнополочный 50х50х5 В Ст.3 сп.5 ГОСТ8509-93 ГОСТ535-05 0,193 т',
])
def test_unquoted_name_and_attribute_continuations_are_one_product(tmp_path, text):
    service, ai, repo = context(tmp_path)
    ai.parse_request.side_effect = lambda *args, **kwargs: PurchaseRequest(items=[RequestItem(
        product='Уголок', product_group='angle', size_designation='50х50х5', source_text=text)])
    service.process(1, text)
    saved = repo.load(1)
    assert saved.is_valid and len(saved.items) == 1
    assert saved.items[0].source_position == 1
    assert saved.items[0].quantity == Decimal('0.193')
    assert saved.items[0].grade == 'ВСт3сп5'
    assert saved.items[0].gost_tu == 'ГОСТ 8509-93, ГОСТ 535-05'
    ai.parse_request.assert_called_once()


def test_unknown_product_like_row_is_a_coverage_risk():
    text = CIRCLE + '\nСпецзаготовка 40×20 — 3 шт'
    with pytest.raises(SemanticExtractionError) as error:
        validate_source_coverage(bars([CIRCLE]), text)
    assert error.value.code == 'missing_positions'


def test_headings_notes_and_delivery_conditions_are_not_extra_products():
    text = ('Заявка на металлопрокат №12:\n' + CIRCLE
            + '\nПримечание: круг по ГОСТ 2590-2006 без покрытия'
            + '\nДоставка: 2 машины\nИтого: 1 т')
    assert validate_source_coverage(bars([CIRCLE]), text) == [1]


def test_supported_special_pvl_cannot_be_omitted():
    text = CIRCLE + '\nПВЛ 406 Ст3 — 2 т'
    with pytest.raises(SemanticExtractionError) as error:
        validate_source_coverage(bars([CIRCLE]), text)
    assert error.value.code == 'missing_positions'


@pytest.mark.parametrize('text', [RAW38, FLAT38], ids=['wrapped38', 'flat38'])
def test_persistent_real_omission_retries_once_and_saves_nothing(tmp_path, text):
    service, ai, repo = context(tmp_path)

    def extract(value, **kwargs):
        draft = angle_draft(value)
        draft.items.pop(19)
        return draft

    ai.parse_request.side_effect = extract
    with pytest.raises(SemanticExtractionError) as error:
        service.process(1, text)
    assert error.value.code == 'missing_positions'
    assert ai.parse_request.call_count == 2
    assert repo.load(1) is None


def test_numbered_products_are_not_unquoted_continuations():
    text = '1. ' + CIRCLE + '\n2. ' + HEXAGON
    draft = bars(['1. ' + CIRCLE, '2. ' + HEXAGON])
    assert validate_source_coverage(draft, text) == [1, 2]


def test_unknown_dimensioned_product_without_quantity_requires_clarification():
    with pytest.raises(SemanticExtractionError) as error:
        validate_source_coverage(bars([CIRCLE]), CIRCLE + '\nСпецзаготовка 40×20')
    assert error.value.code == 'missing_positions'


def test_unknown_numbered_product_is_not_silently_ignored():
    with pytest.raises(SemanticExtractionError) as error:
        validate_source_coverage(bars([CIRCLE]), CIRCLE + '\n2. Спецзаготовка 40×20 — 3 шт')
    assert error.value.code == 'missing_positions'


def test_flattened_fragment_cannot_cover_circle_and_hexagon():
    draft = bars([CIRCLE + ' ' + HEXAGON, HEXAGON])
    with pytest.raises(SemanticExtractionError) as error:
        validate_source_coverage(draft, CIRCLE + '\n' + HEXAGON)
    assert error.value.code == 'merged_positions'


@pytest.mark.parametrize('text', [RAW38, FLAT38], ids=['wrapped38', 'flat38'])
def test_empty_first_extraction_is_real_incompleteness_and_can_retry(tmp_path, text):
    service, ai, repo = context(tmp_path)
    ai.parse_request.side_effect = lambda value, **kwargs: (
        PurchaseRequest() if ai.parse_request.call_count == 1 else angle_draft(value))
    service.process(1, text)
    saved = repo.load(1)
    assert saved.is_valid and len(saved.items) == 38
    assert ai.parse_request.call_count == 2


def test_unknown_only_specification_reaches_source_coverage_not_a_greeting(tmp_path):
    service, ai, repo = context(tmp_path)
    ai.parse_request.return_value = PurchaseRequest()
    with pytest.raises(SemanticExtractionError) as error:
        service.process(1, 'Спецзаготовка 40×20 — 3 шт')
    assert error.value.code == 'missing_positions'
    assert ai.parse_request.call_count == 2
    assert repo.load(1) is None
