"""Quantity evidence and request-bound shared unit clarification, offline only."""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ai import AIService
from database import Database
from diagnostics import PipelineError
from repositories.telegram_repository import TelegramRepository
from schemas import PurchaseRequestExtraction, RequestItem
from telegram_service import TelegramService, exact_source_attributes


@pytest.mark.parametrize('tail,claimed,expected', [
    ('— 10,700', '10.700', '10.700'),
    ('— 10.700', '10.700', '10.700'),
    ('— 1 234,5678', '1234.5678', '1234.5678'),
    ('— 10,700', '99', None),
    ('— 10,700 или 20,700', '10.700', None),
    ('', '20', None),
    ('ГОСТ 35087-2024', '2024', None),
    ('длина — 12000', '12000', None),
    ('толщина — 5', '5', None),
    ('длина изделия — 12000', '12000', None),
    ('марка стали — 355', '355', None),
    ('— 80х40х3', '3', None),
    ('— 10,700 — 20,700', '20.700', None),
])
def test_unitless_quantity_requires_unambiguous_amount_evidence(tail, claimed, expected):
    item = RequestItem(product='Балка', product_group='beam', size_designation='20Ш2',
                       grade='С355-5', quantity=claimed,
                       source_text=f'Балка 20Ш2 С355-5 {tail}')
    exact_source_attributes(item)
    assert item.quantity == (Decimal(expected) if expected is not None else None)
    assert item.unit is None


def build_service(tmp_path):
    ai = AIService.__new__(AIService)
    ai._invoke = Mock()
    repo = TelegramRepository(Database(tmp_path / 'synthetic-b2.sqlite3'))
    return TelegramService(ai, repo, reliable=True, commercial_view=True), ai, repo


def feed(service, ai, user_id, rows):
    text = '\n'.join(row.source_text for row in rows)
    ai._invoke.return_value = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        refusal=None, parsed=PurchaseRequestExtraction(items=rows)))])
    service.process(user_id, text)
    return service.repository.load(user_id)


def beam(size, amount, unit=None):
    return RequestItem(product='Балка', product_group='beam', size_designation=size,
        grade='С355-5', quantity=amount, unit=unit,
        source_text=f'Балка {size} С355-5 — {amount}' + (f' {unit}' if unit else ''))


def test_shared_unit_preserves_known_units_sources_and_other_user_after_reload(tmp_path):
    service, ai, repo = build_service(tmp_path)
    other = feed(service, ai, 2, [beam('10Ш2', '7', 'кг')]).model_dump_json()
    before = feed(service, ai, 1, [beam('20Ш2', '10.700'), beam('30Ш2', '2'),
                                  beam('40Ш2', '500', 'кг')])
    assert len(before.pending_clarification.unit_item_ids) == 2
    # Round-trip persisted state and recreate the service before answering.
    service = TelegramService(ai, repo, reliable=True, commercial_view=True)
    calls = ai._invoke.call_count
    reply = service.process(1, 'тонны')
    after = repo.load(1)
    assert ai._invoke.call_count == calls
    assert repo.load(2).model_dump_json() == other
    assert after.pending_clarification is None
    assert not any('Укажите единицу' in str(message) for message in reply)
    old = {item.item_id: item for item in before.items}
    for item in after.items:
        original = old[item.item_id]
        assert item.model_dump(exclude={'unit', 'source_unit'}) == original.model_dump(exclude={'unit', 'source_unit'})
        assert item.unit == (original.unit or 'т')
    assert after.source_text == before.source_text
    assert after.is_valid
    unchanged = after.model_dump_json()
    service.process(1, 'тонны')
    assert repo.load(1).model_dump_json() == unchanged
    assert ai._invoke.call_count == calls


@pytest.mark.parametrize('reply', ['тонны или кг', 'тонны, наверное кг', '2 т'])
def test_shared_unit_does_not_accept_ambiguous_answer_or_replace_amount(tmp_path, reply):
    service, ai, repo = build_service(tmp_path)
    before = feed(service, ai, 1, [beam('20Ш2', '10'), beam('30Ш2', '20')])
    service.process(1, reply)
    after = repo.load(1)
    assert [(i.quantity, i.unit) for i in after.items] == [(i.quantity, i.unit) for i in before.items]
    assert after.pending_clarification is not None


def test_stale_shared_question_cannot_apply_to_another_request(tmp_path):
    service, ai, repo = build_service(tmp_path)
    before = feed(service, ai, 1, [beam('20Ш2', '10'), beam('30Ш2', '20')])
    stale = before.pending_clarification.model_copy(deep=True)
    newer = feed(service, ai, 1, [beam('40Ш2', '30'), beam('45Ш2', '40')])
    unchanged = newer.model_dump_json()
    newer.pending_clarification = stale
    with pytest.raises(PipelineError):
        service._answer_clarification(1, newer, 'тонны')
    assert repo.load(1).model_dump_json() == unchanged


def test_shared_question_requires_unchanged_request_fingerprint(tmp_path):
    service, ai, repo = build_service(tmp_path)
    current = feed(service, ai, 1, [beam('20Ш2', '10'), beam('30Ш2', '20')])
    current.items[0].quantity = Decimal('99')
    with pytest.raises(PipelineError):
        service._answer_clarification(1, current, 'тонны')
    assert repo.load(1).items[0].quantity == Decimal('10')


def test_model_cannot_invent_unit_or_unconfirmed_amount_in_real_pipeline(tmp_path):
    service, ai, repo = build_service(tmp_path)
    rows = [beam('20Ш2', '10'), beam('30Ш2', '20')]
    rows[0].unit = 'т'  # Source contains a number, but no unit.
    rows[1].quantity = Decimal('999')  # Source says 20, not 999.
    current = feed(service, ai, 1, rows)
    assert [(i.quantity, i.unit) for i in current.items] == [(Decimal('10'), None), (None, None)]
    assert current.pending_clarification.unit_item_ids == []


def test_missing_amount_does_not_create_shared_unit_scope(tmp_path):
    service, ai, repo = build_service(tmp_path)
    unknown = beam('30Ш2', '20')
    unknown.quantity = None
    unknown.source_text = 'Балка 30Ш2 С355-5'
    current = feed(service, ai, 1, [beam('20Ш2', '10'), unknown])
    assert current.pending_clarification.unit_item_ids == []
    service.process(1, 'тонны')
    after = repo.load(1)
    assert next(i for i in after.items if i.size_designation == '30Ш2').unit is None
