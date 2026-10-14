"""Commercial view keeps source facts and requires scoped human decisions."""
from decimal import Decimal, localcontext
from pathlib import Path
import re
from unittest.mock import Mock

import pytest

from consolidation import consolidate_items
from database import Database
from formatter import format_normalized_request, request_consolidation
from normalizer import normalize_request
from repositories.telegram_repository import TelegramRepository
from schemas import PurchaseRequest, RequestItem
from source_fidelity import source_product_rows
from telegram_service import TelegramService, ActiveItem, ActiveRequest
from commercial import approve_group


FIXTURES = Path(__file__).parent / 'fixtures'


def source_item(grade='Ст3', amount='1', group='angle', size='50х50х5', **fields):
    name = 'Уголок' if group == 'angle' else 'Швеллер'
    standard = fields.pop('gost_tu', 'ГОСТ 8509-93' if group == 'angle' else 'ГОСТ 8240-97')
    source = f'{name} {size} {grade} {standard} — {amount} т'
    item = RequestItem(product=name, product_group=group, size_designation=size,
                       grade=grade, quantity=amount, unit='т', gost_tu=standard,
                       source_text=source, **fields)
    return normalize_request(PurchaseRequest(items=[item])).items[0]


@pytest.mark.parametrize(('grades', 'label'), [
    (('Ст3', 'Ст3'), 'ст3'), (('Ст3пс5', 'Ст3сп3'), 'ст3'),
    (('С255-4', 'Ст3сп5'), 'ст3'), (('С345-6', 'С355-4'), 'С355'),
    (('С345-3', 'С345-6'), 'С345'), (('С355-3', 'С355-6'), 'С355'),
])
def test_commercial_material_groups_do_not_rewrite_sources(grades, label):
    items = [source_item(grade, amount) for grade, amount in zip(grades, ['0.1234', '0.20005'])]
    before = [item.model_dump() for item in items]
    groups = consolidate_items(items, commercial=True)
    assert len(groups) == 1
    assert groups[0].item.grade == label
    assert groups[0].item.quantity == Decimal('0.32345')
    assert groups[0].source_indices == (1, 2)
    assert [item.model_dump() for item in groups[0].source_items] == before
    assert [item.model_dump() for item in items] == before


@pytest.mark.parametrize('grade', ['С345-6', 'С355-4', '09Г2С', 'Ст3кп5'])
def test_single_specific_technical_material_is_not_truncated(grade):
    assert consolidate_items([source_item(grade)], commercial=True)[0].item.grade == grade


@pytest.mark.parametrize(('a', 'b'), [
    (source_item('09Г2С'), source_item('С345-6')),
    (source_item('Ст3', group='channel', size='10У'), source_item('С255', group='channel', size='10П')),
    (source_item('Ст3', size='50х50х5'), source_item('С255', size='50х50х6')),
    (source_item('Ст3'), source_item('С255', processing='оцинкованный')),
    (source_item('Ст3'), source_item('С255', form='повышенная точность')),
    (source_item('Ст3'), source_item('С255', gost_tu='ГОСТ 8509-2022')),
    (source_item('С345-3', gost_tu='ГОСТ 27772-88, ГОСТ 8509-93'),
     source_item('С345-6', gost_tu='ГОСТ 27772-2015, ГОСТ 8509-93')),
])
def test_incompatible_products_and_document_editions_remain_separate(a, b):
    assert len(consolidate_items([a, b], commercial=True)) == 2


def extracted_source(text):
    items = []
    for row in source_product_rows(text):
        angle = 'угол' in row.casefold()
        size = re.search(r'\d+х\d+х\d+' if angle else r'\b\d+[ПУ]\b', row)[0]
        items.append(RequestItem(product='Уголок' if angle else 'Швеллер',
                                 product_group='angle' if angle else 'channel',
                                 size_designation=size, source_text=row))
    return PurchaseRequest(items=items)


def approved_reference_operation(active):
    from commercial import approve_group
    rows = source_product_rows((FIXTURES / 'p0_angles_channels_38.txt').read_text(encoding='utf-8'))
    # Explicit human-approved source members of this operation. These choices
    # are test data, never automatic production rules keyed on sizes or mass.
    by_source = {' '.join(item.source_text.split()): item for item in active.items}
    selected = lambda indices: [by_source[' '.join(rows[index].split())].item_id for index in indices]
    return [
        approve_group(active, selected([5, 14, 18, 19]), 'ст3',
                      selected([5])[0], reason='Согласованное коммерческое представление с сохранением Б и ГОСТ 535-05',
                      allow_unmapped_source=True),
        approve_group(active, selected([32, 33]), '09Г2С', selected([32])[0],
                      reason='Разовое решение пользователя для этой заявки; нормативная эквивалентность не утверждается'),
    ]


@pytest.mark.parametrize('flatten', [False, True])
def test_real_38_to_28_through_telegram_and_temporary_sqlite(tmp_path, flatten):
    source = (FIXTURES / 'p0_angles_channels_38.txt').read_text(encoding='utf-8')
    if flatten:
        source = '\n'.join(' '.join(row.split()) for row in source_product_rows(source))
    ai = Mock()
    ai.parse_request.side_effect = lambda text, **kwargs: extracted_source(text)
    repo = TelegramRepository(Database(tmp_path / 'commercial-test-only.sqlite3'))
    service = TelegramService(ai, repo, commercial_view=True)
    messages = service.process(1, source, commercial_decisions=approved_reference_operation)
    expected = (FIXTURES / 'p0_angles_channels_28_expected.txt').read_text(encoding='utf-8').strip()
    assert '\n'.join(messages) == expected
    assert len([line for line in expected.splitlines() if line.strip()]) == 28
    saved = repo.load(1)
    assert saved.source_text == source and len(saved.items) == 38
    assert saved.is_valid
    assert sorted(item.source_position for item in saved.items) == list(range(1, 39))
    assert sum((item.quantity for item in saved.items if item.product_group == 'angle'), Decimal(0)) == Decimal('59.326')
    assert sum((item.quantity for item in saved.items if item.product_group == 'channel'), Decimal(0)) == Decimal('146.029')
    assert sum((item.quantity for item in saved.items), Decimal(0)) == Decimal('205.355')
    row20 = next(item for item in saved.items if item.source_position == 20)
    assert row20.grade == 'ВСт3сп5' and row20.gost_tu == 'ГОСТ 8509-93, ГОСТ 535-05'
    assert row20.quantity == Decimal('0.193')
    assert format_normalized_request(saved) == expected  # Survives a repository reload.
    groups = request_consolidation(saved)
    assert len(groups) == 28
    assert sorted(position for group in groups for position in group.source_positions) == list(range(1, 39))
    assert len({key for group in groups for key in group.source_item_ids}) == 38
    assert sum((group.item.quantity for group in groups), Decimal(0)) == Decimal('205.355')
    assert sum((group.item.quantity for group in groups if group.item.product_group == 'angle'), Decimal(0)) == Decimal('59.326')
    assert sum((group.item.quantity for group in groups if group.item.product_group == 'channel'), Decimal(0)) == Decimal('146.029')
    assert len([member for group in groups for member in group.source_items]) == 38
    by_source_id = {item.item_id: item for item in saved.items}
    for group in groups:
        for key, member in zip(group.source_item_ids, group.source_items):
            assert member.model_dump() == RequestItem(**by_source_id[key].model_dump()).model_dump()
    ai.parse_request.assert_called_once()
    ai.interpret_message.assert_not_called()


def explicit_request():
    sources = [source_item('09Г2С', '1.2345', group='channel', size='30П',
                           gost_tu='ГОСТ 27772-88, ГОСТ 8240-97'),
               source_item('С345-6', '4.5678', group='channel', size='30П',
                           gost_tu='ГОСТ 27772-2015, ГОСТ 8240-97')]
    return ActiveRequest(request_id='operation-alpha', source_text='\n'.join(item.source_text for item in sources),
        commercial_view=True, items=[ActiveItem(**item.model_dump(), item_id=key,
            original=item.model_copy(deep=True), source_position=index)
            for index, (item, key) in enumerate(zip(sources, ['alpha', 'beta']), 1)])


def test_explicit_decision_is_not_bound_to_a_size_row_number_or_control_mass():
    request = explicit_request()
    before = request.model_dump()
    decision = approve_group(request, ['alpha', 'beta'], '09Г2С', 'alpha', reason='Явное разовое решение')
    assert len(consolidate_items(request.items, commercial=True)) == 2
    groups = consolidate_items(request.items, commercial=True, decisions=[decision], request=request)
    assert len(groups) == 1 and groups[0].item.quantity == Decimal('5.8023')
    assert groups[0].item.grade == '09Г2С'
    assert groups[0].item.gost_tu == 'ГОСТ 27772-88, ГОСТ 8240-97'
    assert {member.grade for member in groups[0].source_items} == {'09Г2С', 'С345-6'}
    assert request.model_dump() == before


def test_stale_approval_does_not_follow_source_edits():
    request = explicit_request()
    decision = approve_group(request, ['alpha', 'beta'], '09Г2С', 'alpha', reason='Разовое решение')
    request.items[1].quantity = Decimal('7')
    assert len(consolidate_items(request.items, commercial=True, decisions=[decision], request=request)) == 2


def test_approval_cannot_be_reused_in_another_request_or_overlap():
    request = explicit_request()
    decision = approve_group(request, ['alpha', 'beta'], '09Г2С', 'alpha', reason='Разовое решение')
    with pytest.raises(ValueError, match='несколько'):
        consolidate_items(request.items, commercial=True, decisions=[decision, decision], request=request)
    request.request_id = 'different-operation'
    with pytest.raises(ValueError, match='другой заявке'):
        consolidate_items(request.items, commercial=True, decisions=[decision], request=request)


def test_explicit_decision_cannot_override_incompatible_structured_execution():
    request = explicit_request()
    request.items[1].processing = 'оцинкованный'
    with pytest.raises(ValueError, match='несовместимые'):
        approve_group(request, ['alpha', 'beta'], '09Г2С', 'alpha', reason='Недопустимое решение')


def test_unknown_source_qualifiers_require_specific_explicit_approval():
    sources = [source_item('Ст3'), source_item('С255')]
    sources[1].source_text += ' повышенная точность'
    assert len(consolidate_items(sources, commercial=True)) == 2


def test_source_kilograms_and_exact_decimal_values_survive_commercial_conversion():
    sources = [source_item('С255', '0.2'), source_item('Ст3', '350')]
    sources[1].unit = sources[1].source_unit = 'кг'
    sources[1].source_text = sources[1].source_text.replace('350 т', '350 кг')
    before = [item.model_dump() for item in sources]
    groups = consolidate_items(sources, commercial=True)
    assert len(groups) == 1 and groups[0].item.quantity == Decimal('0.55')
    assert [member.model_dump() for member in groups[0].source_items] == before
    assert [item.model_dump() for item in sources] == before


@pytest.mark.parametrize('grade', ['Ст3', 'С255', 'Ст3сп5'])
def test_commercial_label_is_not_a_new_density_assumption(tmp_path, grade):
    text = f'Лист 6х1500х6000 {grade} — 4 шт'
    item = RequestItem(product='Лист', product_group='sheet', size_designation='6х1500х6000',
                       grade=grade, quantity='4', unit='шт', source_text=text)
    ai = Mock()
    ai.parse_request.side_effect = lambda *args, **kwargs: PurchaseRequest(items=[item.model_copy(deep=True)])
    repo = TelegramRepository(Database(tmp_path / 'mass-only-test.sqlite3'))
    messages = TelegramService(ai, repo, commercial_view=True).process(1, text)
    assert (' = 1,696 т' in messages[0]) == (grade == 'Ст3')
    assert repo.load(1).items[0].grade == grade


def test_reference_special_decisions_are_never_activated_implicitly(tmp_path):
    source = (FIXTURES / 'p0_angles_channels_38.txt').read_text(encoding='utf-8')
    ai = Mock()
    ai.parse_request.side_effect = lambda text, **kwargs: extracted_source(text)
    repo = TelegramRepository(Database(tmp_path / 'no-hidden-policy.sqlite3'))
    messages = TelegramService(ai, repo, commercial_view=True).process(1, source)
    saved = repo.load(1)
    assert saved.commercial_decisions == []
    groups = request_consolidation(saved)
    # Two explicit approvals are required: the unmatched Б execution and the
    # different 09Г2С/С345 materials. Neither is a default compatibility rule.
    assert len(groups) == 30
    assert sum((group.item.quantity for group in groups), Decimal(0)) == Decimal('205.355')
    assert '0,519 т' not in '\n'.join(messages)


def test_source_quantity_edit_recalculates_commercial_sum_without_changing_originals(tmp_path):
    sources = [source_item('С255', '2'), source_item('Ст3сп5', '3')]
    text = '\n'.join(item.source_text for item in sources)
    ai = Mock()
    ai.parse_request.side_effect = lambda *args, **kwargs: PurchaseRequest(items=[item.model_copy(deep=True) for item in sources])
    repo = TelegramRepository(Database(tmp_path / 'commercial-edit-test.sqlite3'))
    service = TelegramService(ai, repo, commercial_view=True)
    service.process(1, text)
    messages = service.process(1, 'В исходной позиции 2 измени количество на 4 т')
    assert ' — 6,000 т' in messages[0]
    saved = repo.load(1)
    second = next(item for item in saved.items if item.source_position == 2)
    assert second.grade == 'Ст3сп5' and second.quantity == Decimal('4')
    assert second.original.quantity == Decimal('3')
    assert second.source_text == sources[1].source_text
    assert len(request_consolidation(saved)) == 1
    ai.parse_request.assert_called_once()


def test_decimal_context_and_repeated_views_do_not_round_or_double_quantities():
    sources = [source_item('С255', '123456789.123456789'), source_item('Ст3', '0.000000001')]
    with localcontext() as context:
        context.prec = 5
        first = consolidate_items(sources, commercial=True)
        second = consolidate_items(sources, commercial=True)
    assert first[0].item.quantity == second[0].item.quantity == Decimal('123456789.123456790')
    assert first[0].source_indices == second[0].source_indices == (1, 2)
    assert sources[0].quantity == Decimal('123456789.123456789')


def test_commercial_policy_is_not_part_of_ai_extraction_schema():
    from schemas import PurchaseRequestExtraction
    schema = PurchaseRequestExtraction.model_json_schema()
    assert 'commercial_view' not in schema['properties']
    assert 'commercial_decisions' not in schema['properties']
