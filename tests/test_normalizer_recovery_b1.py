"""B1 acceptance reproduction: intentionally red until quantity/unit repair.

Only the model transport is controlled. Real parsing, source validation,
normalization, routing, persistence and clarification remain enabled.
"""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

from ai import AIService
from database import Database
from repositories.telegram_repository import TelegramRepository
from schemas import PurchaseRequestExtraction, RequestItem
from telegram_service import TelegramService
import telegram_service


ROWS = (
    ('20Ш2', '35087-2024', '10.700'),
    ('30Ш2', '35087-2024', '186.698'),
    ('40Ш2', 'Р 57837-2017', '127.097'),
    ('45Ш2', 'Р 57837-2017', '56.669'),
)
SOURCE = '\n'.join(
    f'Балка {size} С355-5 {standard} — {amount.replace(".", ",")}'
    for size, standard, amount in ROWS
)


def test_four_beams_keep_amounts_then_accept_shared_tonnes(tmp_path, monkeypatch):
    extracted = PurchaseRequestExtraction(items=[
        RequestItem(product='Балка', product_group='beam', size_designation=size,
                    grade='С355-5', quantity=Decimal(amount), unit=None,
                    source_text=source)
        for (size, _standard, amount), source in zip(ROWS, SOURCE.splitlines())
    ])
    # No invented ГОСТ prefix: abbreviated identifiers remain in literal source.
    ai = AIService.__new__(AIService)  # No SDK client, credentials or .env loading.
    ai._invoke = Mock(return_value=SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(refusal=None, parsed=extracted))]))
    observed = []
    real_exact = telegram_service.exact_source_attributes

    def observe(item, *args, **kwargs):
        before = item.quantity
        result = real_exact(item, *args, **kwargs)
        observed.append((item.size_designation, before, item.quantity, item.unit))
        return result

    monkeypatch.setattr(telegram_service, 'exact_source_attributes', observe)
    repo = TelegramRepository(Database(tmp_path / 'b1-synthetic-only.sqlite3'))
    service = TelegramService(ai, repo, reliable=True, commercial_view=True)
    initial_messages = service.process(900001, SOURCE)
    initial = repo.load(900001)
    final_messages = service.process(900001, 'тонны')
    final = repo.load(900001)

    assert ai._invoke.call_count == 1
    assert ai._invoke.call_args.args[0] == 'extraction'
    assert initial is not None and len(initial.items) == 4
    assert {i.source_text for i in initial.items} == set(SOURCE.splitlines())
    expected = {size: Decimal(amount) for size, _, amount in ROWS}
    assert {i.size_designation: i.quantity for i in initial.items} == expected, (
        'B1 first failure: exact_source_attributes erased model quantities. '
        f'before/after={observed!r}; initial_reply={initial_messages!r}; '
        f'tonnes_reply={final_messages!r}; '
        f'final={[(i.size_designation, i.quantity, i.unit) for i in final.items]!r}'
    )
    assert initial.pending_clarification is not None
    assert initial.pending_clarification.field == 'unit'
    assert all(i.unit is None for i in initial.items)
    assert {i.size_designation: i.quantity for i in final.items} == expected
    assert all(i.unit == 'т' and i.grade == 'С355-5' for i in final.items)
    assert sum((i.quantity for i in final.items), Decimal(0)) == Decimal('381.164')
    assert sorted(i.source_position for i in final.items) == [1, 2, 3, 4]
    assert final.pending_clarification is None
