"""Offline volume estimates, shared projection and legacy JSON compatibility."""
from decimal import Decimal, localcontext
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from consolidation import consolidate_items
from database import Database
from formatter import format_item, format_normalized_request
from normalizer import normalize_request
from quantity import calculate_theoretical_mass, normalize_quantity
from repositories.telegram_repository import TelegramRepository
from request_operations import request_fingerprint
from schemas import PurchaseRequest, PurchaseRequestExtraction, RequestItem
from telegram_service import ActiveRequest, TelegramService
from validator import validate_request
from workbench import NormalizationWorkbench, format_workbench_result

SOURCE = 'Лист 6×1500×6000 Ст3 — 4 шт.'
EXPECTED = 'Лист 6×1500×6000 Ст3 — 4 шт. = 1,696 т'


def sheet(count='4', size='6×1500×6000', grade='Ст3', **updates):
    data = dict(product='Лист', product_group='sheet', size_designation=size,
                grade=grade, quantity=count, unit='шт', source_unit='шт.',
                source_text=f'Лист {size} {grade} — {count} шт.')
    data.update(updates)
    item = RequestItem(**data)
    normalize_request(PurchaseRequest(items=[item]))
    return item


@pytest.fixture
def context(tmp_path):
    repo = TelegramRepository(Database(tmp_path / 'isolated.sqlite3'))
    ai = Mock()
    ai.parse_request.side_effect = lambda *args, **kwargs: PurchaseRequest(items=[sheet()])
    return TelegramService(ai, repo), ai, repo


def test_four_sheets_exact_calculation_and_display():
    item = sheet()
    mass = item.mass_calculation
    assert mass.status == 'estimate'
    assert mass.density_kg_m3 == Decimal('7850')
    assert mass.dimensions_mm == dict(thickness_mm=Decimal('6'), width_mm=Decimal('1500'), length_mm=Decimal('6000'))
    assert mass.per_piece_kg == Decimal('423.9')
    assert mass.total_kg == Decimal('1695.6')
    assert mass.total_t == Decimal('1.6956')
    assert mass.input_quantity == item.quantity == Decimal('4')
    assert mass.input_unit == item.unit == 'шт'
    assert mass.source_unit == 'шт.'
    assert mass.method == 'solid_rectangular_sheet_volume_density_v1'
    assert mass.assumption_id == 'sheet_st3_rectangular_density_7850_v1'
    assert mass.basis.startswith('docs/METAL_REFERENCE.md#')
    assert format_normalized_request(PurchaseRequest(items=[item])) == EXPECTED
    assert RequestItem.model_validate_json(item.model_dump_json()).mass_calculation == mass
    assert json.loads(item.model_dump_json())['mass_calculation']['total_t'] == str(mass.total_t)


@pytest.mark.parametrize('count,total,display', [
    ('1', '0.4239', '0,424'), ('5', '2.1195', '2,12'), ('10', '4.239', '4,239'),
])
def test_counts_and_display_rounding(count, total, display):
    item = sheet(count)
    assert item.mass_calculation.total_t == Decimal(total)
    assert format_item(item, 1).endswith(f'{count} шт. = {display} т')
    assert item.quantity == Decimal(count)


@pytest.mark.parametrize('changes,reason', [
    ({'length_mm': None}, 'missing_geometry'),
    ({'quantity': None}, 'missing_quantity'),
    ({'grade': None}, 'unsupported_material'),
    ({'grade': 'Ст20'}, 'unsupported_material'),
    ({'grade': '12Х18Н10Т'}, 'unsupported_material'),
    ({'grade': 'Ст3 (или Ст20)'}, 'unsupported_material'),
    ({'product': 'Лист алюминиевый'}, 'unsupported_execution'),
    ({'product': 'Лист рифлёный'}, 'unsupported_execution'),
    ({'processing': 'оцинкованный'}, 'unsupported_execution'),
    ({'form': 'рулон'}, 'unsupported_execution'),
    ({'source_text': SOURCE+' с отверстиями'}, 'unsupported_execution'),
    ({'quantity': Decimal('0')}, 'invalid_quantity'),
    ({'quantity': Decimal('0.5')}, 'non_integer_piece_count'),
    ({'unit': 'м²'}, 'unsupported_unit'),
    ({'unit': 'т'}, 'unsupported_unit'),
    ({'unit': 'кг'}, 'unsupported_unit'),
    ({'thickness_mm': 0}, 'invalid_geometry'),
    ({'thickness_mm': -6}, 'invalid_geometry'),
    ({'thickness_mm': float('nan')}, 'invalid_geometry'),
    ({'thickness_mm': float('inf')}, 'invalid_geometry'),
    ({'thickness_mm': 8}, 'inconsistent_geometry'),
])
def test_unsupported_inputs_never_show_invented_mass(changes, reason):
    item = sheet()
    for name, value in changes.items():
        setattr(item, name, value)
    result = calculate_theoretical_mass(item)
    assert result.status == 'unavailable'
    assert result.reason == reason
    assert result.total_t is None
    assert ' = ' not in format_item(item, 1)


@pytest.mark.parametrize('category', ['rebar','round_pipe','profile_pipe','angle','channel','beam','round_bar','expanded_metal'])
def test_other_categories_are_not_estimated(category):
    item = sheet(product_group=category)
    assert calculate_theoretical_mass(item).reason == 'unsupported_category'
    assert item.mass_calculation is None


def test_geometry_change_recalculates_without_old_mass():
    item = sheet()
    item.thickness_mm = 8
    item.size_designation = '8×1500×6000'
    normalize_request(PurchaseRequest(items=[item]))
    assert item.mass_calculation.total_t == Decimal('2.2608')
    assert format_item(item, 1).endswith('4 шт. = 2,261 т')


def test_decimal_precision_independent_of_float_and_context():
    thickness = '6.0000000000000000000000001'
    count = '12345678901234567890123456789'
    # Exact expected rational arithmetic in a sufficiently large context.
    with localcontext() as context:
        context.prec = 120
        expected = Decimal(thickness) * Decimal('1500') * Decimal('6000') * Decimal('7850') * Decimal(count) / Decimal('1000000000000')
    with localcontext() as context:
        context.prec = 6
        item = sheet(count, size=thickness+'×1500×6000')
        assert item.mass_calculation.total_t == expected
        assert item.mass_calculation.dimensions_mm['thickness_mm'] == Decimal(thickness)
        assert ' = ' in format_item(item, 1)
    assert item.quantity == Decimal(count)


def test_consolidation_sums_pieces_and_recalculates_once():
    items = [sheet('1'), sheet('3')]
    snapshots = [item.model_dump_json() for item in items]
    for _ in range(3):
        normalize_request(PurchaseRequest(items=items))
        groups = consolidate_items(items)
        assert len(groups) == 1
        assert groups[0].source_indices == (1, 2)
        assert groups[0].item.quantity == Decimal('4')
        assert groups[0].item.mass_calculation.total_t == Decimal('1.6956')
        assert format_normalized_request(PurchaseRequest(items=items)) == EXPECTED
    assert [item.model_dump_json() for item in items] == snapshots
    assert sum(item.mass_calculation.total_t for item in items) == Decimal('1.6956')


def test_ai_schema_and_python_recalculation():
    assert 'mass_calculation' not in json.dumps(PurchaseRequestExtraction.model_json_schema())
    item = sheet()
    item.mass_calculation = item.mass_calculation.model_copy(update={'total_t': Decimal('999')})
    normalize_request(PurchaseRequest(items=[item]))
    assert item.mass_calculation.total_t == Decimal('1.6956')


def test_telegram_json_storage_quantity_edits_and_original(context):
    service, ai, repo = context
    assert service.process(1, SOURCE) == [EXPECTED]
    before = repo.load(1)
    saved = before.items[0]
    assert saved.quantity == saved.original.quantity == Decimal('4')
    assert saved.unit == saved.original.unit == 'шт'
    assert saved.source_text == saved.original.source_text == SOURCE
    assert saved.mass_calculation.total_t == Decimal('1.6956')
    assert service.process(1, 'В позиции 1 измени количество на 5 шт.') == ['Лист 6×1500×6000 Ст3 — 5 шт. = 2,12 т']
    changed = repo.load(1)
    assert changed.items[0].quantity == Decimal('5')
    assert changed.items[0].original.quantity == Decimal('4')
    assert changed.items[0].mass_calculation.total_t == Decimal('2.1195')
    assert changed.items[0].source_text == SOURCE
    with repo.database.connection() as connection:
        rows = connection.execute('SELECT request_json FROM telegram_history ORDER BY id').fetchall()
    assert len(rows) == 2
    assert ActiveRequest.model_validate_json(rows[0][0]) == before
    ai.parse_request.assert_called_once()


def test_grade_edit_removes_estimate_and_restoration_returns_it(context):
    service, ai, repo = context
    service.process(1, SOURCE)
    assert ' = ' not in service.process(1, 'В позиции 1 измени марку на Ст20')[0]
    assert repo.load(1).items[0].mass_calculation.status == 'unavailable'
    assert service.process(1, 'Верни исходную марку позиции 1') == [EXPECTED]


def test_quantity_clarification_calculates_mass(context):
    service, ai, repo = context
    item = sheet(); item.quantity = None; item.source_text = 'Лист 6×1500×6000 Ст3'
    ai.parse_request.side_effect = lambda *args, **kwargs: PurchaseRequest(items=[item.model_copy(deep=True)])
    assert ' = ' not in service.process(1, item.source_text)[0]
    assert repo.load(1).pending_clarification is not None
    assert service.process(1, '4')[0] == EXPECTED
    assert repo.load(1).items[0].quantity == Decimal('4')


def test_workbench_and_current_geometry_edits_share_calculator():
    ai = Mock(); ai.parse_request.side_effect = lambda *args, **kwargs: PurchaseRequest(items=[sheet()])
    result = NormalizationWorkbench(ai).process(SOURCE)
    assert result.is_valid
    assert EXPECTED in format_workbench_result(result)
    item = result.current_request.items[0]
    item.quantity = Decimal('5')
    assert '5 шт. = 2,12 т' in format_workbench_result(result)
    item.thickness_mm = 8; item.size_designation = '8×1500×6000'
    normalize_request(result.current_request)
    assert '8×1500×6000 Ст3 — 5 шт. = 2,826 т' in format_workbench_result(result)
    assert result.original_items[0].quantity == Decimal('4')
    assert result.original_items[0].thickness_mm == 6


def test_legacy_sqlite_load_is_read_only_and_pending_hash_stable(context):
    service, ai, repo = context
    service.process(1, SOURCE)
    data = json.loads(repo.load(1).model_dump_json())
    for item in data['items']:
        item.pop('mass_calculation'); item['original'].pop('mass_calculation')
    legacy = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
    with repo.database.connection() as connection:
        schema_before = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        connection.execute('UPDATE telegram_active SET request_json=? WHERE user_id=1', (legacy,))
    loaded = repo.load(1)
    assert loaded.items[0].mass_calculation is None
    fingerprint = request_fingerprint(loaded)
    normalize_request(loaded)
    assert request_fingerprint(loaded) == fingerprint
    assert loaded.items[0].mass_calculation.total_t == Decimal('1.6956')
    with repo.database.connection() as connection:
        assert connection.execute('SELECT request_json FROM telegram_active WHERE user_id=1').fetchone()[0] == legacy
        assert connection.execute("SELECT sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall() == schema_before


def test_real_21_position_request_output_remains_unchanged(context):
    from telegram_service import prepare_sheet_heading
    fixtures = Path(__file__).parent / 'fixtures'
    source = (fixtures / 'mixed_21_20261013_2109.txt').read_text().strip()
    draft = PurchaseRequest.model_validate_json((fixtures / 'mixed_21_extraction.json').read_text())
    service, ai, repo = context
    ai.parse_request.side_effect = lambda *args, **kwargs: draft.model_copy(deep=True)
    assert '\n'.join(service.process(1, source)) == (fixtures / 'mixed_21_expected.txt').read_text().strip()
    assert len(repo.load(1).items) == 21
    assert not any(i.mass_calculation and i.mass_calculation.status == 'estimate' for i in repo.load(1).items)


def test_kg_conversion_remains_exact_and_distinct_from_piece_mass():
    assert normalize_quantity('1000', 'кг') == (Decimal('1'), 'т')
    item = sheet('1000', unit='кг')
    assert format_normalized_request(PurchaseRequest(items=[item])) == 'Лист 6×1500×6000 Ст3 — 1 т'
    assert item.quantity == Decimal('1000') and item.unit == 'кг'
    assert item.mass_calculation.status == 'unavailable'


@pytest.mark.parametrize('size', ['-6×1500×6000', '6×-1500×6000', '6×1500×-6000',
                                  '6×1500×6000 или 6×2000×6000'])
def test_invalid_or_alternative_nominal_geometry_is_not_estimated(size):
    item = sheet(size=size)
    assert item.mass_calculation.status == 'unavailable'
    assert ' = ' not in format_item(item, 1)


def test_only_display_is_rounded_and_mass_ignores_tonnage_setting():
    item = sheet()
    mass = item.mass_calculation.model_dump_json()
    request = ActiveRequest(request_id='isolated', source_text=SOURCE,
                            items=[], tonnage_decimals=2)
    from telegram_service import ActiveItem
    request.items = [ActiveItem(**item.model_dump(), item_id='isolated', original=item.model_copy(deep=True))]
    assert format_normalized_request(request) == EXPECTED
    assert item.mass_calculation.model_dump_json() == mass
    assert item.mass_calculation.total_t == Decimal('1.6956')



def test_workbench_recovers_literal_piece_count_before_calculating():
    item = sheet()
    item.quantity = Decimal('9'); item.unit = 'т'
    ai = Mock(); ai.parse_request.return_value = PurchaseRequest(items=[item])
    result = NormalizationWorkbench(ai).process(SOURCE)
    assert result.current_request.items[0].quantity == Decimal('4')
    assert result.current_request.items[0].unit == 'шт'
    assert EXPECTED in format_workbench_result(result)


def test_existing_pending_operation_is_not_invalidated_by_mass_metadata(context):
    from request_operations import PendingOperation, RequestOperation
    service, ai, repo = context
    service.process(1, SOURCE)
    request = repo.load(1)
    request.pending_operation = PendingOperation(
        user_id=1, request_id=request.request_id, request_fingerprint=request_fingerprint(request),
        operation=RequestOperation(kind='edit_fields', edits=[
            dict(item_id=request.items[0].item_id, field='quantity', value='5')]),
        command='isolated test', question='Подтвердите изменение', expected_answer='confirmation')
    data = json.loads(request.model_dump_json())
    for item in data['items']:
        item.pop('mass_calculation'); item['original'].pop('mass_calculation')
    with repo.database.connection() as connection:
        connection.execute('UPDATE telegram_active SET request_json=? WHERE user_id=1',
                           (json.dumps(data, ensure_ascii=False),))
    assert service.process(1, 'Да') == ['Лист 6×1500×6000 Ст3 — 5 шт. = 2,12 т']
    assert repo.load(1).pending_operation is None
    assert repo.load(1).items[0].original.quantity == Decimal('4')
