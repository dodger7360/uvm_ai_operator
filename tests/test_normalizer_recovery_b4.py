"""Read-only request commands through real routing and isolated SQLite."""
from decimal import Decimal

import pytest

from formatter import format_normalized_request
from telegram_service import TelegramService
from tests.test_real_114_1_4 import service_at, ANGLES, MASS
from tests.test_dialog_review_114 import working_snapshot


@pytest.mark.parametrize('restart', [False, True])
def test_real_six_rows_short_commands_are_local_and_read_only(tmp_path, restart):
    service, ai, repo, assistant = service_at(tmp_path)
    service.process(1, '\n'.join(ANGLES))
    before = working_snapshot(repo, 1)
    expected = format_normalized_request(repo.load(1))
    ai.reset_mock()
    assistant.reset_mock()
    if restart:
        from database import Database
        from repositories.telegram_repository import TelegramRepository
        repo = TelegramRepository(Database(tmp_path / 'isolated.sqlite3'))
        service = TelegramService(ai, repo, reliable=True, commercial_view=True, assistant=assistant)
    normalized = service.process(1, 'Нормализуй')
    tonnage = service.process(1, 'Какой общий тоннаж?')
    shown = service.process(1, 'Покажи текущую заявку')
    assert working_snapshot(repo, 1) == before
    assert normalized[0] == expected, (normalized, tonnage, shown, assistant.mock_calls, ai.mock_calls)
    assert tonnage == ['Общий тоннаж заявки: 4,3618 т.']
    assert shown[0] == expected
    assert not ai.mock_calls and not assistant.mock_calls


@pytest.mark.parametrize('command', ['Нормализуй', 'Покажи заявку', 'Текущая заявка',
                                     'Какой общий тоннаж?', 'Общий вес заявки?'])
def test_commands_without_request_do_not_call_ai(tmp_path, command):
    service, ai, repo, assistant = service_at(tmp_path)
    messages = service.process(1, command)
    assert 'Активной заявки нет' in ' '.join(messages)
    assert repo.load(1) is None
    assert not ai.mock_calls and not assistant.mock_calls


@pytest.mark.parametrize('unit', [None, 'шт', 'м', 'м²'])
def test_unknown_mass_is_not_a_complete_total(tmp_path, unit):
    service, ai, repo, assistant = service_at(tmp_path)
    service.process(1, '\n'.join(ANGLES))
    current = repo.load(1)
    current.items[0].unit = unit
    repo.save(1, current)
    before = working_snapshot(repo, 1)
    ai.reset_mock(); assistant.reset_mock()
    messages = service.process(1, 'Общий вес заявки?')
    assert 'не полный тоннаж' in ' '.join(messages)
    assert 'Общий тоннаж заявки:' not in ' '.join(messages)
    assert working_snapshot(repo, 1) == before
    assert not ai.mock_calls and not assistant.mock_calls


def test_tonnes_and_kg_sum_source_items_once(tmp_path):
    service, ai, repo, assistant = service_at(tmp_path)
    service.process(1, '\n'.join(ANGLES))
    current = repo.load(1)
    for item in current.items[:3]:
        item.quantity /= Decimal('1000')
        item.unit = 'т'
    repo.save(1, current)
    before = working_snapshot(repo, 1)
    assert service.process(1, 'Общий вес заявки?') == ['Общий тоннаж заявки: 4,3618 т.']
    assert working_snapshot(repo, 1) == before


def test_partial_display_and_mass_keep_unprocessed_source_visible(tmp_path):
    service, ai, repo, assistant = service_at(tmp_path)
    source = ANGLES[0] + '\nНеизвестный товар ZZZ — 7 шт'
    service.process(1, source)
    before = working_snapshot(repo, 1)
    ai.reset_mock(); assistant.reset_mock()
    for command in ('Нормализуй', 'Покажи заявку', 'Какой общий тоннаж?'):
        text = '\n'.join(service.process(1, command))
        assert 'Частичная' in text and 'ZZZ' in text
        assert 'Общий тоннаж заявки:' not in text
        assert working_snapshot(repo, 1) == before
    assert not ai.mock_calls and not assistant.mock_calls


def test_all_without_bound_group_is_a_read_only_question(tmp_path):
    service, ai, repo, assistant = service_at(tmp_path)
    service.process(1, '\n'.join(ANGLES))
    before = working_snapshot(repo, 1)
    ai.reset_mock(); assistant.reset_mock()
    reply = service.process(1, 'Все')
    assert 'Что применить ко всем' in ' '.join(reply)
    assert working_snapshot(repo, 1) == before
    assert not ai.mock_calls and not assistant.mock_calls


def test_request_commands_do_not_change_independent_calculation_or_other_user(tmp_path):
    service, ai, repo, assistant = service_at(tmp_path)
    service.process(1, '\n'.join(ANGLES))
    service.process(2, ANGLES[0])
    other = working_snapshot(repo, 2)
    service.process(1, MASS)
    calculation = repo.load_calculation(1).model_dump_json()
    request = working_snapshot(repo, 1)
    ai.reset_mock(); assistant.reset_mock()
    for command in ('Нормализуй', 'Общий вес заявки?', 'Текущая заявка'):
        service.process(1, command)
    assert repo.load_calculation(1).model_dump_json() == calculation
    assert working_snapshot(repo, 1) == request
    assert working_snapshot(repo, 2) == other
    assert not ai.mock_calls and not assistant.mock_calls


def test_all_uses_existing_group_question_without_changing_values(tmp_path):
    from tests.test_normalizer_recovery_b2 import build_service, feed, beam
    service, ai, repo = build_service(tmp_path)
    current = feed(service, ai, 1, [beam('20Ш2', '10'), beam('30Ш2', '20')])
    before = working_snapshot(repo, 1)
    calls = ai._invoke.call_count
    for command in ('Все', 'Нормализуй', 'Покажи заявку'):
        reply = service.process(1, command)
        assert current.pending_clarification.question in '\n'.join(reply)
        assert working_snapshot(repo, 1) == before
    service.process(1, 'тонны')
    assert all(item.unit == 'т' for item in repo.load(1).items)
    assert ai._invoke.call_count == calls


def test_display_keeps_explicit_commercial_decision_and_pending_operation(tmp_path):
    from commercial import approve_group
    from tests.test_commercial_consolidation import explicit_request
    from request_operations import PendingOperation, RequestOperation, request_fingerprint
    service, ai, repo, assistant = service_at(tmp_path)
    current = explicit_request()
    current.commercial_decisions = [approve_group(current, ['alpha', 'beta'], '09Г2С', 'alpha', reason='Synthetic approval')]
    current.pending_operation = PendingOperation(user_id=1, request_id=current.request_id,
        request_fingerprint=request_fingerprint(current), operation=RequestOperation(kind='edit_fields'),
        command='Synthetic clarification', question='Уточните исполнение.', expected_answer='details')
    repo.save(1, current)
    before = working_snapshot(repo, 1)
    expected = format_normalized_request(current)
    for command in ('Нормализуй', 'Покажи текущую заявку', 'Общий вес заявки?'):
        reply = service.process(1, command)
        if command != 'Общий вес заявки?':
            assert reply == [expected, 'Вопросы по заявке:\n' + current.pending_operation.question]
        assert working_snapshot(repo, 1) == before
    assert not ai.mock_calls and not assistant.mock_calls


def test_no_known_mass_and_missing_quantity_never_produce_zero_full_total(tmp_path):
    service, ai, repo, assistant = service_at(tmp_path)
    service.process(1, ANGLES[0])
    current = repo.load(1)
    current.items[0].quantity = None
    repo.save(1, current)
    before = working_snapshot(repo, 1)
    ai.reset_mock(); assistant.reset_mock()
    reply = service.process(1, 'Общий вес заявки?')
    assert 'Тоннаж заявки пока неизвестен' in ' '.join(reply)
    assert 'Общий тоннаж заявки:' not in ' '.join(reply)
    assert working_snapshot(repo, 1) == before
    assert not ai.mock_calls and not assistant.mock_calls


def test_confirmed_quantity_edit_is_not_lost_by_display_or_sum(tmp_path):
    service, ai, repo, assistant = service_at(tmp_path)
    service.process(1, '\n'.join(ANGLES))
    service.process(1, 'В исходной позиции 2 измени количество на 2 т')
    current = repo.load(1)
    target = next(i for i in current.items if i.source_position == 2)
    assert target.quantity == Decimal('2') and target.unit == 'т'
    before = working_snapshot(repo, 1)
    ai.reset_mock(); assistant.reset_mock()
    for command in ('Нормализуй', 'Какой общий тоннаж?', 'Покажи заявку'):
        service.process(1, command)
        assert working_snapshot(repo, 1) == before
    assert not ai.mock_calls and not assistant.mock_calls
