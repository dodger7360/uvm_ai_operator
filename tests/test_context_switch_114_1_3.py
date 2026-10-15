"""Final context routing checks; isolated SQLite and deterministic AI stubs."""
from decimal import Decimal
import pytest
from database import Database
from telegram_service import TelegramService
from repositories.telegram_repository import TelegramRepository
from tests.test_dialog_context_114 import context, PIPE, MASS
from tests.test_dialog_review_114 import working_snapshot


def restore(tmp_path, ai, assistant):
    return TelegramService(ai, TelegramRepository(Database(tmp_path/'dialog.sqlite3')),
                           reliable=True, assistant=assistant)


def both(tmp_path):
    service, ai, repo, assistant = context(tmp_path)
    service.process(1, PIPE)
    service.process(1, MASS)
    return service, ai, repo, assistant


@pytest.mark.parametrize('question', ['Расскажи про шахматы', 'Какие комбинации в покере?',
    'Объясни правила покера', 'Какая погода в Екатеринбурге?'])
@pytest.mark.parametrize('working', [False, True])
def test_free_chat_preserves_both_pending_states(tmp_path, question, working):
    service, ai, repo, assistant = context(tmp_path)
    if working: service.process(1, PIPE)
    service.process(1, MASS)
    before = working_snapshot(repo, 1) if working else None
    calc = repo.load_calculation(1).model_dump_json()
    service.process(1, question)
    assert assistant.answer.call_args.args[0] == question
    assert assistant.answer.call_args.kwargs['current'] is None
    assert repo.load_calculation(1).model_dump_json() == calc
    assert (working_snapshot(repo, 1) if working else repo.load(1)) == before
    service.process(1, 'Длина 11,7 м' if working else '11.7м')
    item = repo.load_calculation(1).items[0]
    assert item.length_mm == 11700 and item.quantity == Decimal('405') and item.diameter_mm == 12
    assert (working_snapshot(repo, 1) if working else repo.load(1)) == before


@pytest.mark.parametrize('target', ['к расчёту', 'к заявке'])
@pytest.mark.parametrize('restart', [False, True])
def test_ambiguous_answer_asks_target_and_applies_buffer_once(tmp_path, target, restart):
    service, ai, repo, assistant = both(tmp_path)
    before = working_snapshot(repo, 1)
    calc = repo.load_calculation(1).model_dump_json()
    messages = service.process(1, '11.7м')
    assert 'расчёт' in ' '.join(messages).casefold() and 'заявк' in ' '.join(messages).casefold()
    assert working_snapshot(repo, 1) == before
    assert repo.load_calculation(1).model_dump_json() == calc
    if restart: service = restore(tmp_path, ai, assistant)
    service.process(1, target)
    if target == 'к расчёту':
        assert working_snapshot(repo, 1) == before
        assert repo.load_calculation(1).items[0].length_mm == 11700
    else:
        assert repo.load(1).items[0].wall_thickness_mm == 11700  # no invented size tolerance
        assert repo.load_calculation(1).model_dump_json() == calc
    work_after = working_snapshot(repo, 1)
    calc_after = repo.load_calculation(1).model_dump_json()
    service.process(1, target)
    assert working_snapshot(repo, 1) == work_after
    assert repo.load_calculation(1).model_dump_json() == calc_after
    ai.interpret_message.assert_not_called()


@pytest.mark.parametrize('reference', ['Уточнение текущей', 'Вернёмся к заявке', 'Что у нас в заявке?'])
@pytest.mark.parametrize('restart', [False, True])
def test_explicit_working_reference_keeps_calculation_unchanged(tmp_path, reference, restart):
    service, ai, repo, assistant = both(tmp_path)
    calc = repo.load_calculation(1).model_dump_json()
    before = working_snapshot(repo, 1)
    messages = service.process(1, reference)
    assert working_snapshot(repo, 1) == before
    assert repo.load_calculation(1).model_dump_json() == calc
    if '?' in reference:
        assert assistant.answer.call_args.kwargs['current'].request_id == repo.load(1).request_id
    else: assert 'стенк' in ' '.join(messages).casefold()
    if restart: service = restore(tmp_path, ai, assistant)
    service.process(1, '6 мм')
    assert repo.load(1).items[0].wall_thickness_mm == 6
    assert repo.load_calculation(1).model_dump_json() == calc
    service.process(1, 'Длина 11,7 м')
    assert repo.load_calculation(1).items[0].length_mm == 11700
    assert repo.load(1).items[0].wall_thickness_mm == 6


def test_explicit_wall_label_only_changes_working_request(tmp_path):
    service, ai, repo, assistant = both(tmp_path)
    calc = repo.load_calculation(1).model_dump_json()
    service.process(1, 'Стенка 6 мм')
    assert repo.load(1).items[0].wall_thickness_mm == 6
    assert repo.load_calculation(1).model_dump_json() == calc


def test_choice_does_not_survive_changed_working_version(tmp_path):
    service, ai, repo, assistant = both(tmp_path)
    service.process(1, '11.7м')
    active = repo.load(1)
    active.items[0].quantity = Decimal('21')
    repo.save(1, active)
    before = working_snapshot(repo, 1)
    calc = repo.load_calculation(1).model_dump_json()
    messages = service.process(1, 'к расчёту')
    assert 'устарел' in ' '.join(messages).casefold()
    assert working_snapshot(repo, 1) == before
    assert repo.load_calculation(1).model_dump_json() == calc


def test_choice_is_user_scoped_and_survives_free_chat(tmp_path):
    service, ai, repo, assistant = both(tmp_path)
    service.process(1, '11.7м')
    before = working_snapshot(repo, 1)
    calc = repo.load_calculation(1).model_dump_json()
    service.process(2, 'к расчёту')
    service.process(1, 'Расскажи про шахматы')
    assert working_snapshot(repo, 1) == before
    assert repo.load_calculation(1).model_dump_json() == calc
    service.process(1, 'к расчёту')
    assert repo.load_calculation(1).items[0].length_mm == 11700
    assert repo.load(2) is None and repo.load_calculation(2) is None


def test_completed_calculation_does_not_intercept_working_reply(tmp_path):
    service, ai, repo, assistant = both(tmp_path)
    service.process(1, 'Длина 11,7 м')
    calc = repo.load_calculation(1).model_dump_json()
    service.process(1, 'Стенка 6 мм')
    assert repo.load(1).items[0].wall_thickness_mm == 6
    assert repo.load_calculation(1).model_dump_json() == calc

@pytest.mark.parametrize('target', ['к расчёту', 'к заявке'])
def test_ambiguous_quantity_uses_existing_quantity_parser(tmp_path, target):
    service, ai, repo, assistant = context(tmp_path)
    service.process(1, 'Уголок 50х50х5 Ст3')
    service.process(1, 'Сколько весит арматура ф12 А240?')
    work = working_snapshot(repo, 1)
    calc = repo.load_calculation(1).model_dump_json()
    service.process(1, '120 шт')
    assert working_snapshot(repo, 1) == work
    assert repo.load_calculation(1).model_dump_json() == calc
    service.process(1, target)
    if target == 'к расчёту':
        assert repo.load_calculation(1).items[0].quantity == Decimal('120')
        assert working_snapshot(repo, 1) == work
    else:
        assert repo.load(1).items[0].quantity == Decimal('120')
        assert repo.load(1).items[0].unit == 'шт'
        assert repo.load_calculation(1).model_dump_json() == calc


def test_restore_both_then_resume_work_and_calculation_separately(tmp_path):
    service, ai, repo, assistant = both(tmp_path)
    work, calc = working_snapshot(repo, 1), repo.load_calculation(1).model_dump_json()
    service = restore(tmp_path, ai, assistant)
    assert working_snapshot(repo, 1) == work and repo.load_calculation(1).model_dump_json() == calc
    service.process(1, 'Уточнение текущей')
    service.process(1, 'Стенка 6 мм')
    assert repo.load_calculation(1).model_dump_json() == calc
    work = working_snapshot(repo, 1)
    service = restore(tmp_path, ai, assistant)
    service.process(1, '11.7м')
    assert repo.load_calculation(1).items[0].length_mm == 11700
    assert working_snapshot(repo, 1) == work


def test_new_spec_discards_pending_target_selection(tmp_path):
    service, ai, repo, assistant = both(tmp_path)
    old_request = repo.load(1).request_id
    service.process(1, '11.7м')
    service.process(1, PIPE.replace('20 т', '3 т'))
    assert repo.load(1).request_id != old_request
    work = working_snapshot(repo, 1)
    calc = repo.load_calculation(1).model_dump_json()
    service.process(1, 'к заявке')
    assert working_snapshot(repo, 1) == work
    assert repo.load_calculation(1).model_dump_json() == calc
    service.process(1, '6 мм')
    assert repo.load(1).items[0].wall_thickness_mm == 6
    assert repo.load(1).items[0].quantity == Decimal('3')


def test_choice_rejects_changed_calculation_version(tmp_path):
    service, ai, repo, assistant = both(tmp_path)
    service.process(1, '11.7м')
    calc = repo.load_calculation(1)
    calc.items[0].quantity = Decimal('406')
    repo.save_calculation(1, calc)
    before = working_snapshot(repo, 1), calc.model_dump_json()
    messages = service.process(1, 'к заявке')
    assert 'устарел' in ' '.join(messages).casefold()
    assert (working_snapshot(repo, 1), repo.load_calculation(1).model_dump_json()) == before


def test_access_revocation_and_changed_floor_hide_calc_and_routing(tmp_path):
    from unittest.mock import Mock
    from repositories.access_repository import AuthorizedRequestRepository
    from dialog_context import RoutingState
    service, ai, repo, assistant = context(tmp_path)
    service.process(1, MASS)
    calc = repo.load_calculation(1)
    access = Mock()
    access.get.return_value = {'status': 'approved', 'context_floor': 0}
    authorized = AuthorizedRequestRepository(repo, access)
    authorized.save_routing(1, RoutingState(request_focus='work'))
    assert authorized.load_calculation(1).calculation_id == calc.calculation_id
    access.get.return_value = {'status': 'revoked', 'context_floor': 0}
    for action in [lambda: authorized.load_calculation(1), lambda: authorized.save_calculation(1, calc),
                   lambda: authorized.load_routing(1), lambda: authorized.save_routing(1, RoutingState())]:
        with pytest.raises(PermissionError): action()
    access.get.return_value = {'status': 'approved', 'context_floor': 50}
    assert authorized.load_calculation(1) is None
    assert authorized.load_routing(1).request_focus is None
    assert repo.load_calculation(1).calculation_id == calc.calculation_id


def test_reapproval_without_working_history_does_not_restore_old_calculation(tmp_path):
    from repositories.access_repository import AccessRepository, AuthorizedRequestRepository
    service, ai, repo, assistant = context(tmp_path)
    access = AccessRepository(repo.database)
    access.bootstrap_configured_admin(99)
    access.request_access({'id': 1, 'first_name': 'Synthetic'})
    access.transition(99, 1, 'approve', access.get(1)['revision'], notify=lambda _: True)
    service.repository = AuthorizedRequestRepository(repo, access)
    service.process(1, MASS)
    original = service.repository.load_calculation(1)
    from dialog_context import RoutingState
    service.repository.save_routing(1, RoutingState(request_focus='synthetic'))
    service.repository.save_dialog(1, 'Synthetic question', 'Synthetic answer')
    assert original is not None and repo.load(1) is None
    access.transition(99, 1, 'revoke', access.get(1)['revision'])
    with pytest.raises(PermissionError): service.repository.load_calculation(1)
    access.transition(99, 1, 'approve', access.get(1)['revision'], notify=lambda _: True)
    assert access.get(1)['context_floor'] == 0  # no procurement history to advance the floor
    assert service.repository.load_calculation(1) is None
    assert service.repository.load_routing(1).request_focus is None
    assert service.repository.load_dialog(1) == []
    service.process(1, MASS.replace('405', '120'))
    restored = AuthorizedRequestRepository(TelegramRepository(Database(tmp_path/'dialog.sqlite3')), access)
    assert restored.load_calculation(1).items[0].quantity == Decimal('120')
    assert repo.load_calculation(1).items[0].quantity == Decimal('405')  # archived, not deleted

def test_explicit_working_edit_preserves_pending_calculation(tmp_path):
    from tests.test_dialog_context_114 import ANGLE
    from telegram_service import MessagePlan
    from request_operations import Edit
    service, ai, repo, assistant = context(tmp_path)
    service.process(1, ANGLE)
    active = repo.load(1)
    service.process(1, MASS)
    calc = repo.load_calculation(1).model_dump_json()
    ai.interpret_message.return_value = MessagePlan(intent='edit', edits=[
        Edit(item_id=active.items[0].item_id, field='quantity', value='7')])
    service.process(1, 'Измени количество на 7 т')
    assert repo.load(1).items[0].quantity == Decimal('7')
    assert repo.load(1).request_id == active.request_id
    assert repo.load_calculation(1).model_dump_json() == calc
    service.process(1, '11.7м')
    assert repo.load_calculation(1).items[0].length_mm == 11700
    assert repo.load(1).items[0].quantity == Decimal('7')
