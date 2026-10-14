"""Source evidence regressions; no network or production storage."""
from decimal import Decimal
from pathlib import Path
import re
from unittest.mock import Mock

import pytest

from database import Database
from diagnostics import SemanticExtractionError
from normalizer import normalize_request
from repositories.telegram_repository import TelegramRepository
from schemas import PurchaseRequest, RequestItem
from source_fidelity import PRODUCT_ROW, logical_source_rows
from telegram_service import TelegramService, exact_source_attributes
from validator import validate_request
from source_attributes import parse_carbon_designation


FIXTURE = Path(__file__).parent / 'fixtures' / 'p0_angles_channels_38.txt'
ROW20 = 'Уголок Б 50х50х5 "ГОСТ8509-93/В Ст.3 сп.5\nГОСТ535-05" 0,193 т'


def checked(source, **attributes):
    item = RequestItem(product='Уголок', product_group='angle',
                       size_designation='50х50х5', quantity='0.193', unit='т',
                       source_text=source, **attributes)
    return validate_request(normalize_request(PurchaseRequest(items=[item]))).items[0]


@pytest.mark.parametrize(('source', 'expected'), [
    ('Уголок 50х50х5 Ст3 ГОСТ 8509-93 0,193 т', 'ГОСТ 8509-93'),
    ('Уголок 50х50х5 ГОСТ 8509-93 ГОСТ 535-05 Ст3 0,193 т', 'ГОСТ 8509-93, ГОСТ 535-05'),
    ('Уголок 50х50х5 ГОСТ8509-93/Ст3 ГОСТ535-05 0,193 т', 'ГОСТ 8509-93, ГОСТ 535-05'),
    ('Уголок 50х50х5 "ГОСТ8509-93/Ст3\nГОСТ535-05" 0,193 т', 'ГОСТ 8509-93, ГОСТ 535-05'),
    ('Уголок 50х50х5 ГОСТ 8509 - 93 Ст3 ГОСТ 535 - 05 0,193 т', 'ГОСТ 8509-93, ГОСТ 535-05'),
    ('Уголок 50х50х5 ТУ 14-1-1234-05 Ст3 СТО АСЧМ 20-93 0,193 т', 'ТУ 14-1-1234-05, СТО АСЧМ 20-93'),
    ('Уголок 50х50х5 СТО 12345-2020 Ст3 ГОСТ Р 12345-2021 0,193 т', 'СТО 12345-2020, ГОСТ Р 12345-2021'),
])
def test_each_standard_is_recovered_and_confirmed_independently(source, expected):
    item = RequestItem(product='Уголок', product_group='angle',
                       size_designation='50х50х5', source_text=source)
    exact_source_attributes(item)
    assert item.gost_tu == expected
    result = validate_request(normalize_request(PurchaseRequest(items=[item])))
    assert result.items[0].gost_tu == expected
    assert result.is_valid


@pytest.mark.parametrize('claim', [
    'ГОСТ 8509-93, ГОСТ 535-06',  # replaced year
    'ГОСТ 8509-93, ГОСТ 99999-05',  # absent standard
    'ГОСТ 8509-93, ГОСТ 535',  # truncated year
    'ГОСТ 850-93',  # partial number
    'ГОСТ 8509-9',
    'ГОСТ 8509-930',
    'ТУ 8509-93',  # different normative document type
    'ГОСТ 8509-93, выдуманный стандарт',
])
def test_one_real_standard_does_not_confirm_a_false_claim(claim):
    assert checked(ROW20, gost_tu=claim).gost_tu is None


@pytest.mark.parametrize('claim', [
    'ГОСТ 8509-93', 'ГОСТ 535-05',
    'ГОСТ535-05 / ГОСТ8509-93',
])
def test_both_row20_standards_have_independent_literal_evidence(claim):
    assert checked(ROW20, gost_tu=claim).gost_tu is not None


def test_quantity_unit_near_standard_cannot_confirm_a_truncated_document():
    source = 'Уголок 50х50х5 ГОСТ8509-93 т'
    assert checked(source, gost_tu='ГОСТ 8509').gost_tu is None
    assert checked(source, gost_tu='ГОСТ 8509-93').gost_tu is not None


@pytest.mark.parametrize(('source_grade', 'canonical'), [
    ('В Ст.3 сп.5', 'ВСт3сп5'),
    ('в ст . 3 СП . 5', 'ВСт3сп5'),
    ('Ст3пс5', 'Ст3пс5'), ('Ст3сп5', 'Ст3сп5'),
    ('Ст.3 сп.5', 'Ст3сп5'), ('09Г2С', '09Г2С'),
    ('С255-4', 'С255-4'), ('С345-6', 'С345-6'),
])
def test_full_technical_designation_is_recovered_without_commercial_aliases(source_grade, canonical):
    source = f'Уголок 50х50х5 {source_grade} ГОСТ8509-93 0,193 т'
    item = RequestItem(product='Уголок', product_group='angle',
                       size_designation='50х50х5', source_text=source, grade='Ст3')
    exact_source_attributes(item)
    result = validate_request(normalize_request(PurchaseRequest(items=[item])))
    assert result.items[0].grade == canonical
    assert result.items[0].source_text == source
    assert result.is_valid


@pytest.mark.parametrize('claim', ['Ст3', 'Ст3сп5', 'ВСт3', 'ВСт3сп', 'ВСт3сп6', 'ВСт3пс5', 'С345-6'])
def test_partial_or_substituted_material_designation_is_not_confirmed(claim):
    assert checked(ROW20, grade=claim).grade is None


@pytest.mark.parametrize(('source_grade', 'claim'), [
    ('Ст3', 'ВСт3сп5'), ('Ст3сп', 'Ст3сп5'),
    ('С255-4', 'С255'), ('С345-6', 'С345-3'), ('09Г2С', 'С345'),
])
def test_source_fragment_does_not_confirm_a_fuller_or_different_designation(source_grade, claim):
    assert checked(f'Уголок 50х50х5 {source_grade} 0,193 т', grade=claim).grade is None


def test_supply_group_grade_deoxidation_and_category_are_separate_components():
    designation = parse_carbon_designation('В Ст.3 сп.5')
    assert designation.supply_group == 'В'
    assert designation.steel_number == '3'
    assert designation.deoxidation == 'сп'
    assert designation.category == '5'
    assert designation.canonical() == 'ВСт3сп5'


@pytest.mark.parametrize('quantity', ['0,193 т', '14.143 т', '4 шт'])
def test_quantity_after_dash_is_not_part_of_standard_or_steel_category(quantity):
    source = f'Уголок 50х50х5 ГОСТ8509-93 Ст3сп - {quantity}'
    item = RequestItem(source_text=source)
    exact_source_attributes(item)
    assert item.grade == 'Ст3сп'
    assert item.gost_tu == 'ГОСТ 8509-93'
    source = f'Уголок 50х50х5 Ст3сп ГОСТ8509-93 - {quantity}'
    item = RequestItem(source_text=source)
    exact_source_attributes(item)
    assert item.gost_tu == 'ГОСТ 8509-93'


def test_piece_count_does_not_become_supply_category():
    item = RequestItem(source_text='Уголок 50х50х5 Ст3сп 4 шт')
    exact_source_attributes(item)
    assert item.grade == 'Ст3сп'
    assert item.quantity == Decimal('4')


def draft_from_literal_rows(text):
    """Deterministic AI stub emits product/size/source, Python recovers attributes."""
    items = []
    for row in logical_source_rows(text):
        if not PRODUCT_ROW.search(row):
            continue
        angle = bool(re.search('угол', row, re.I))
        pattern = r'\d+(?:[.,]\d+)?[хx×]\d+(?:[.,]\d+)?[хx×]\d+(?:[.,]\d+)?' if angle else r'\b\d+[ПУ]\b'
        size = re.search(pattern, row, re.I)
        assert size is not None
        items.append(RequestItem(product='Уголок' if angle else 'Швеллер',
                                 product_group='angle' if angle else 'channel',
                                 size_designation=size[0], source_text=row))
    return PurchaseRequest(items=items)


@pytest.mark.parametrize('text', [ROW20, FIXTURE.read_text(encoding='utf-8')], ids=['row20', 'all38'])
def test_telegram_process_saves_all_literal_positions_and_row20(tmp_path, text):
    repo = TelegramRepository(Database(tmp_path / 'test-only.sqlite3'))
    ai = Mock()
    ai.parse_request.side_effect = lambda value, **kwargs: draft_from_literal_rows(value)
    response = TelegramService(ai, repo).process(1, text)
    saved = repo.load(1)
    expected = 38 if text != ROW20 else 1
    assert saved is not None and saved.is_valid
    assert len(saved.items) == expected
    assert {item.source_position for item in saved.items} == set(range(1, expected + 1))
    row20 = next(item for item in saved.items if item.source_position == (20 if expected == 38 else 1))
    assert row20.quantity == Decimal('0.193') and row20.unit == 'т'
    assert row20.gost_tu == 'ГОСТ 8509-93, ГОСТ 535-05'
    assert row20.grade == 'ВСт3сп5'
    assert 'В Ст.3 сп.5' in row20.source_text
    assert row20.original.grade == row20.grade
    assert saved.source_text == text
    rows = [row for row in logical_source_rows(text) if PRODUCT_ROW.search(row)]
    assert all(item.source_text == rows[item.source_position - 1] for item in saved.items)
    assert len(saved.initial_extraction.items) == expected
    with repo.database.connection() as connection:
        assert connection.execute('SELECT count(*) FROM telegram_history').fetchone()[0] == 1
    rendered = '\n'.join(response)
    assert '0,193 т' in rendered
    assert 'ВСт3сп5' in rendered
    ai.parse_request.assert_called_once()
    ai.interpret_message.assert_not_called()


@pytest.mark.parametrize('invented', [{'grade': 'С345-6'}, {'gost_tu': 'ГОСТ 99999-05'}])
def test_telegram_process_does_not_save_invented_source_attributes(tmp_path, invented):
    repo = TelegramRepository(Database(tmp_path / 'rejected-test-only.sqlite3'))
    text = 'Уголок 50х50х5 — 0,193 т'
    ai = Mock()
    ai.parse_request.side_effect = lambda *args, **kwargs: PurchaseRequest(items=[
        RequestItem(product='Уголок', product_group='angle', size_designation='50х50х5',
                    quantity='0.193', unit='т', source_text=text, **invented)])
    with pytest.raises(SemanticExtractionError) as error:
        TelegramService(ai, repo).process(1, text)
    assert error.value.code == 'unsupported_attributes'
    assert repo.load(1) is None
