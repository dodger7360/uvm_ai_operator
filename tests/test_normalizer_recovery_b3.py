"""Rejected structured extraction remains untrusted and request-local."""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock
import logging

import pytest

from ai import AIService
from database import Database
from repositories.telegram_repository import TelegramRepository
from schemas import PurchaseRequestExtraction, RequestItem
from telegram_service import TelegramService
from tests.test_normalizer_recovery_b2_real_input import SOURCE, ROWS, EXPECTED


def draft():
    return PurchaseRequestExtraction(items=[RequestItem(product='Балка', product_group='beam',
        size_designation=row.split()[1], grade='С355-5', length_requirement='12 000',
        quantity=Decimal(row.split()[-1].replace(',', '.')), source_text=row) for row in ROWS])


def setup_service(tmp_path, extraction):
    ai = AIService.__new__(AIService)
    ai._invoke = Mock(return_value=SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(refusal=None, parsed=extraction))]))
    repo = TelegramRepository(Database(tmp_path / 'b3-test-only.sqlite3'))
    return TelegramService(ai, repo, reliable=True, commercial_view=True), ai, repo


def test_three_literal_rows_survive_one_invalid_fragment(tmp_path, caplog):
    extraction = draft()
    extraction.items[-1].source_text = 'МОДЕЛЬ ПЕРЕПИСАЛА ИСХОДНУЮ СТРОКУ'
    service, ai, repo = setup_service(tmp_path, extraction)
    with caplog.at_level(logging.INFO, logger='operator_diagnostics'):
        messages = service.process(1, SOURCE)
    current = repo.load(1)
    assert len(current.items) == 3
    assert {i.source_text for i in current.items} == set(ROWS[:3])
    assert current.unprocessed_source == [ROWS[-1]]
    assert current.source_text == SOURCE
    assert current.fallback_status == 'partial' and not current.is_valid
    assert current.initial_extraction is None  # Rejected response is not persisted.
    assert sorted(i.source_position for i in current.items) == [1, 2, 3]
    for item in current.items:
        assert item.quantity == EXPECTED[item.size_designation]
        assert item.unit is None and item.grade == 'С355-5'
        assert item.length_requirement == '12 000' and item.length_mm is None
        assert item.original.source_text == item.source_text
    assert SOURCE in '\n'.join(messages)
    assert messages[0].startswith('Частичная нормализация; требуется проверка:')
    assert ai._invoke.call_count == 1
    assert not any(row in caplog.text for row in ROWS)
    assert 'МОДЕЛЬ ПЕРЕПИСАЛА' not in caplog.text
    with repo.database.connection() as connection:
        stored = connection.execute('SELECT request_json FROM telegram_history').fetchall()
    assert all('МОДЕЛЬ ПЕРЕПИСАЛА' not in row[0] for row in stored)


def test_all_literal_rows_keep_normal_path_and_shared_unit(tmp_path):
    service, ai, repo = setup_service(tmp_path, draft())
    service.process(1, SOURCE)
    assert repo.load(1).fallback_status is None
    service.process(1, 'тонны')
    current = repo.load(1)
    assert len(current.items) == 4 and current.fallback_status is None
    assert all(i.unit == 'т' for i in current.items)
    assert sum((i.quantity for i in current.items), Decimal(0)) == Decimal('381.164')
    assert ai._invoke.call_count == 1


@pytest.mark.parametrize('fault', ['invented', 'duplicate', 'conflict', 'standard', 'length', 'amount', 'size', 'processing', 'dimension'])
def test_recovery_rechecks_rows_and_never_counts_a_source_twice(tmp_path, fault):
    extraction = draft()
    if fault == 'invented':
        invented = extraction.items[-1].model_copy(deep=True)
        invented.source_text = 'Балка 99Ш2 С355-5 — 999 т'
        extraction.items.append(invented)
    elif fault in {'duplicate', 'conflict'}:
        duplicate = extraction.items[-1].model_copy(deep=True)
        if fault == 'conflict': duplicate.quantity = Decimal('999')
        extraction.items.append(duplicate)
    else:
        extraction.items[0].source_text = 'НЕСУЩЕСТВУЮЩИЙ ФРАГМЕНТ'
        if fault == 'standard': extraction.items[-1].gost_tu = 'ГОСТ 99999-2026'
        if fault == 'length': extraction.items[-1].length_mm = 12000
        if fault == 'amount': extraction.items[-1].quantity = Decimal('999')
        if fault == 'size': extraction.items[-1].size_designation = '99Ш2'
        if fault == 'processing': extraction.items[-1].processing = 'оцинкованный'
        if fault == 'dimension': extraction.items[-1].width_mm = 999
    service, _, repo = setup_service(tmp_path, extraction)
    service.process(1, SOURCE)
    current = repo.load(1)
    assert not current.is_valid and current.fallback_status in {'partial', 'original'}
    assert current.source_text == SOURCE
    assert len(current.items) == (4 if fault in {'invented', 'duplicate'} else 3 if fault == 'conflict' else 2)
    assert len({i.source_position for i in current.items}) == len(current.items)
    for item in current.items:
        assert item.source_text in ROWS
        assert item.quantity == EXPECTED[item.size_designation]
        assert item.length_mm is None and item.gost_tu is None
    assert set(i.source_text for i in current.items) | set(current.unprocessed_source) == set(ROWS)


def test_rejected_extractions_do_not_leak_across_messages_or_users(tmp_path):
    extraction = draft()
    extraction.items[-1].source_text = 'НЕСУЩЕСТВУЮЩИЙ ФРАГМЕНТ'
    service, ai, repo = setup_service(tmp_path, extraction)
    service.process(1, SOURCE)
    saved = repo.load(1).model_dump_json()
    ai._invoke.side_effect = RuntimeError('offline unavailable transport')
    # Same text cannot borrow previous captured rows after its operation ended.
    service.process(2, SOURCE)
    assert repo.load(2).items == []
    assert repo.load(2).unprocessed_source == ROWS
    assert repo.load(1).model_dump_json() == saved
    service.process(1, SOURCE)
    assert repo.load(1).items == []
    assert repo.load(1).source_text == SOURCE


def test_one_extracted_occurrence_cannot_cover_two_identical_original_rows(tmp_path):
    extraction = draft()
    extraction.items = [extraction.items[0]]
    service, _, repo = setup_service(tmp_path, extraction)
    source = ROWS[0] + '\n' + ROWS[0]
    service.process(1, source)
    current = repo.load(1)
    assert len(current.items) == 1
    assert current.items[0].source_position == 1
    assert current.unprocessed_source == [ROWS[0]]
    assert current.fallback_status == 'partial' and not current.is_valid
