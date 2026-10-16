"""Explicit global grade edits, real router/consolidation and temporary SQLite."""
from decimal import Decimal

import pytest

from formatter import request_consolidation
from tests.test_real_114_1_4 import service_at, ANGLES
from tests.test_dialog_review_114 import working_snapshot

COMMANDS = ['С245 замени на Ст3 везде', 'С245 замени на сталь 3 везде',
            'Везде С245 замени на Ст3', 'Замени С245 на Ст3 во всей заявке']
BEFORE = {1: 'Ст3сп5', 2: 'С245-12', 3: 'С245', 4: 'С255-12', 5: 'С245', 6: 'С245'}
AFTER = BEFORE | {3: 'Ст3', 5: 'Ст3', 6: 'Ст3'}


def grades(request):
    return {item.source_position: item.grade for item in request.items}


@pytest.mark.parametrize('command', COMMANDS)
def test_exact_targets_for_every_natural_command_and_reload(tmp_path, command):
    from database import Database
    from repositories.telegram_repository import TelegramRepository
    from telegram_service import TelegramService
    service, ai, repo, assistant = service_at(tmp_path)
    service.process(1, '\n'.join(ANGLES))
    before = repo.load(1)
    assert grades(before) == BEFORE
    ai.reset_mock(); assistant.reset_mock()
    reply = service.process(1, command)
    after = repo.load(1)
    assert grades(after) == AFTER, (reply, grades(after), ai.mock_calls, assistant.mock_calls)
    assert not ai.mock_calls and not assistant.mock_calls
    assert '3, 5, 6' in '\n'.join(reply) and '1, 2, 4' in '\n'.join(reply)
    previous = {item.item_id: item for item in before.items}
    for item in after.items:
        assert item.model_dump(exclude={'grade'}) == previous[item.item_id].model_dump(exclude={'grade'})
    assert after.request_id == before.request_id and after.source_text == before.source_text
    groups = request_consolidation(after)
    assert sorted(n for group in groups for n in group.source_positions) == [1, 2, 3, 4, 5, 6]
    assert sum((group.item.quantity for group in groups), Decimal(0)) == Decimal('4.3618')
    saved = working_snapshot(repo, 1)
    repo = TelegramRepository(Database(tmp_path / 'isolated.sqlite3'))
    service = TelegramService(ai, repo, reliable=True, commercial_view=True, assistant=assistant)
    assert working_snapshot(repo, 1) == saved
    service.process(1, command)
    assert working_snapshot(repo, 1) == saved  # Repetition is a read-only no-op.
    for position in (3, 5, 6):
        service.process(1, f'Верни исходную марку исходной позиции {position}')
    assert grades(repo.load(1)) == BEFORE


@pytest.mark.parametrize('command', [
    'С245 и родственные замени на Ст3 везде',
    'С245 со всеми индексами замени на Ст3 везде',
    'Замени С245 на Ст3 и С255 во всей заявке',
    'Замени С245 на Ст3, включая все родственные обозначения',
])
def test_ambiguous_grade_scope_never_changes_technical_items(tmp_path, command):
    service, ai, repo, assistant = service_at(tmp_path)
    service.process(1, '\n'.join(ANGLES))
    before = repo.load(1)
    ai.reset_mock(); assistant.reset_mock()
    reply = service.process(1, command)
    after = repo.load(1)
    assert after.items == before.items and after.source_text == before.source_text
    assert 'Уточните' in '\n'.join(reply)
    assert not ai.mock_calls and not assistant.mock_calls


def test_global_selector_changes_only_exact_sources_inside_a_merged_display_group(tmp_path):
    from tests.test_commercial_consolidation import source_item
    from telegram_service import ActiveItem, ActiveRequest
    service, ai, repo, assistant = service_at(tmp_path)
    rows = [source_item('С345-6', '1'), source_item('С345-4', '2'), source_item('С345-6', '3')]
    current = ActiveRequest(request_id='synthetic-group', commercial_view=True,
        source_text='\n'.join(item.source_text for item in rows), items=[
            ActiveItem(**row.model_dump(), item_id=f'source-{n}', source_position=n, original=row.model_copy(deep=True))
            for n, row in enumerate(rows, 1)])
    repo.save(1, current)
    assert len(request_consolidation(current)) == 1
    repo.save(2, current.model_copy(update={'request_id': 'other-request'}, deep=True))
    other = working_snapshot(repo, 2)
    reply = service.process(1, 'Замени С345-6 на Ст3 во всей заявке')
    after = repo.load(1)
    assert grades(after) == {1: 'Ст3', 2: 'С345-4', 3: 'Ст3'}, reply
    assert len(request_consolidation(after)) == 2
    assert sum((g.item.quantity for g in request_consolidation(after)), Decimal(0)) == Decimal('6')
    assert working_snapshot(repo, 2) == other
    assert not ai.mock_calls and not assistant.mock_calls


@pytest.mark.parametrize('fault', ['missing', 'extra', 'replacement', 'duplicate'])
def test_incomplete_or_injected_edit_set_is_rejected_atomically(tmp_path, fault):
    from telegram_service import local_edit_plan
    from diagnostics import SemanticExtractionError
    service, ai, repo, assistant = service_at(tmp_path)
    service.process(1, '\n'.join(ANGLES))
    current = repo.load(1)
    before = working_snapshot(repo, 1)
    command = COMMANDS[0]
    plan = local_edit_plan(command, current)
    assert plan is not None and len(plan.edits) == 3
    if fault == 'missing':
        plan.edits.pop()
    elif fault == 'extra':
        plan.edits.append(plan.edits[0].model_copy(update={
            'item_id': next(item.item_id for item in current.items if item.source_position == 2)}))
    elif fault == 'replacement':
        plan.edits[-1].value = '09Г2С'
    else:
        plan.edits.append(plan.edits[0].model_copy(deep=True))
    with pytest.raises(SemanticExtractionError):
        service._edit(1, current, plan, command=command)
    assert working_snapshot(repo, 1) == before


def test_latin_lookalike_is_not_a_match_and_exact_category_is_independent(tmp_path):
    service, ai, repo, assistant = service_at(tmp_path)
    service.process(1, '\n'.join(ANGLES))
    before = working_snapshot(repo, 1)
    ai.reset_mock(); assistant.reset_mock()
    service.process(1, 'C245 замени на Ст3 везде')  # Latin C, not Cyrillic С.
    assert working_snapshot(repo, 1) == before
    service.process(1, 'С245-12 замени на Ст3 везде')
    assert grades(repo.load(1)) == BEFORE | {2: 'Ст3'}
    assert not ai.mock_calls and not assistant.mock_calls


def test_all_targets_in_one_exact_duplicate_group_are_changed(tmp_path):
    from tests.test_commercial_consolidation import source_item
    from telegram_service import ActiveItem, ActiveRequest
    service, ai, repo, assistant = service_at(tmp_path)
    rows = [source_item('С245', '1'), source_item('С245', '2')]
    current = ActiveRequest(request_id='exact-group', commercial_view=True,
        source_text='\n'.join(item.source_text for item in rows), items=[
            ActiveItem(**row.model_dump(), item_id=f'source-{n}', source_position=n, original=row.model_copy(deep=True))
            for n, row in enumerate(rows, 1)])
    repo.save(1, current)
    assert len(request_consolidation(current)) == 1
    service.process(1, COMMANDS[1])
    after = repo.load(1)
    assert grades(after) == {1: 'Ст3', 2: 'Ст3'}
    assert len(request_consolidation(after)) == 1
    assert request_consolidation(after)[0].source_positions == (1, 2)
    assert request_consolidation(after)[0].item.quantity == Decimal('3')
    assert not ai.mock_calls and not assistant.mock_calls
