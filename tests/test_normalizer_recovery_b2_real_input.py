"""Exact original beam export, real pipeline, only model transport replaced."""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ai import AIService
from database import Database
from repositories.telegram_repository import TelegramRepository
from schemas import PurchaseRequestExtraction, RequestItem
from telegram_service import TelegramService, exact_source_attributes


SOURCE = '''Балка 45Ш2 12 000 С355-5 Р 57837-2017 56,669

Балка 30Ш2 12 000 С355-5 35087-2024 186,698

Балка 40Ш2 12 000 С355-5 Р 57837-2017 127,097

Балка 20Ш2 12 000 С355-5 35087-2024 10,700'''
ROWS = [line for line in SOURCE.splitlines() if line]
EXPECTED = {'45Ш2': Decimal('56.669'), '30Ш2': Decimal('186.698'),
            '40Ш2': Decimal('127.097'), '20Ш2': Decimal('10.700')}


@pytest.mark.parametrize('model_keeps_raw_length', [True, False])
def test_exact_original_beams_and_shared_tonnes(tmp_path, model_keeps_raw_length):
    extraction = PurchaseRequestExtraction(items=[RequestItem(
        product='Балка', product_group='beam', size_designation=row.split()[1],
        grade='С355-5', length_requirement='12 000' if model_keeps_raw_length else None, length_mm=None,
        quantity=Decimal(row.split()[-1].replace(',', '.')), unit=None,
        source_text=row) for row in ROWS])
    ai = AIService.__new__(AIService)
    ai._invoke = Mock(return_value=SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(refusal=None, parsed=extraction))]))
    repo = TelegramRepository(Database(tmp_path / 'real-input-test.sqlite3'))
    service = TelegramService(ai, repo, reliable=True, commercial_view=True)
    service.process(1, SOURCE)
    before = repo.load(1)
    messages = service.process(1, 'тонны')
    after = repo.load(1)
    assert len(before.items) == len(after.items) == 4
    assert {item.size_designation: item.quantity for item in before.items} == EXPECTED, (
        [(item.size_designation, item.quantity, item.unit) for item in before.items])
    assert all(item.unit is None for item in before.items)
    assert {item.size_designation: item.quantity for item in after.items} == EXPECTED
    assert all(item.unit == 'т' and item.grade == 'С355-5' for item in after.items)
    assert sum((item.quantity for item in after.items), Decimal(0)) == Decimal('381.164')
    assert before.source_text == after.source_text == SOURCE
    assert sorted(item.source_position for item in after.items) == [1, 2, 3, 4]
    assert len({item.item_id for item in after.items}) == 4
    assert {item.source_text for item in after.items} == set(ROWS)
    assert {item.original.source_text for item in after.items} == set(ROWS)
    # Existing rules require a source unit or the reviewed angle/channel export.
    # Neither licenses mm for this beam export; keep the literal length unknown.
    assert all(item.length_requirement == '12 000' and item.length_mm is None
               and item.original.length_requirement == '12 000' for item in after.items)
    assert all(item.gost_tu is None for item in after.items)  # No fabricated ГОСТ prefix.
    assert after.pending_clarification is None
    assert not any('Укажите общую единицу' in str(message) for message in messages)
    assert ai._invoke.call_count == 1
    assert ai._invoke.call_args.args[0] == 'extraction'
    assert ai._invoke.call_args.args[2][1]['content'] == SOURCE


@pytest.mark.parametrize('source,claimed', [
    ('Балка 45Ш2 12 000 С355-5 Р 57837-2017 56,669', '12000'),
    ('Балка 45Ш2 12 000 С355-5 Р 57837-2017 56,669', '2017'),
    ('Балка 45Ш2 12 000 С355-5 Р 57837-2017 56,669', '57837'),
    ('Балка 45Ш2 12 000 С355-5 Р 57837-2017 56,669', '355'),
    ('Балка 45Ш2 12 000 С355-5 Р 57837-2017 56,669', '45'),
    ('Балка 45Ш2 12 000 С355-5 Р 57837-2017', '2017'),
    ('Балка 45Ш2 12 000 С355-5 Р 57837-2017 56,669 10,700', '10.700'),
    ('Балка 45Ш2 12 000 С355-5 Р 57837-2017 56,669 или 10,700', '10.700'),
    ('Балка 45Ш2 12 000 С355-5 Р 57837-2017 12000', '12000'),
    ('Балка 45Ш2 12 000', '12000'),
    ('Балка 45Ш2 С355-5', '355'),
])
def test_technical_numbers_and_ambiguous_endings_are_not_amounts(source, claimed):
    item = RequestItem(product='Балка', product_group='beam', size_designation='45Ш2',
                       grade='С355-5', quantity=claimed, source_text=source)
    exact_source_attributes(item)
    assert item.quantity is None and item.unit is None


def test_beam_length_millimetres_are_not_confirmed_from_unitless_column(tmp_path):
    ai = AIService.__new__(AIService)
    item = RequestItem(product='Балка', product_group='beam', size_designation='45Ш2',
        grade='С355-5', length_mm=12000, quantity='56.669', source_text=ROWS[0])
    ai._invoke = Mock(return_value=SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(refusal=None, parsed=PurchaseRequestExtraction(items=[item])))]))
    repo = TelegramRepository(Database(tmp_path / 'unconfirmed-length.sqlite3'))
    TelegramService(ai, repo, reliable=True, commercial_view=True).process(1, ROWS[0])
    current = repo.load(1)
    assert not current.is_valid
    assert 'items[0].length_mm' in current.missing_fields
    assert current.items[0].length_requirement == '12 000'
