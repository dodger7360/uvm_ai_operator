"""Professional rendering of supplied synthetic facts, not live model evaluation."""
from copy import deepcopy
from decimal import Decimal

import pytest

from steel_agent_v2 import Repository, RequestEngine, SteelAgent
from steel_agent_v2.contracts import AgentPlan
from steel_agent_v2.presentation import render
from telegram_operator import safe_chunks
from test_steel_agent_v2 import Fake, edit, set_op
from test_steel_agent_v2_queue import rig, admit, Executor


def sample(rows=None, groups=None, questions=(), extra=None):
    # Each row: label, (name, value, raw) facts, exact amount, original unit.
    rows = rows or [('Лист', [('size', '8×1500×6000 мм', '8×1500×6000 мм'),
                            ('grade', 'С245', 'С245'),
                            ('standard', 'ГОСТ 19903-2015', 'ГОСТ 19903-2015')], '1.23456789', 'т')]
    records, items, lines = [], [], []
    offset = 0
    for n, (label, facts, amount, unit) in enumerate(rows, 1):
        raw_amount = amount.replace('.', ',') if amount is not None else 'количество не указано'
        line = label + ' ' + ' '.join(f[2] for f in facts) + ' — ' + raw_amount + (' ' + unit if unit else '')
        lines.append(line)
        source_id = f's{n}'
        records.append(dict(id=source_id, kind='product', start=offset, end=offset + len(line)))
        items.append(dict(id=f'i{n}', source_id=source_id, label=label,
                          characteristics=[dict(name=name, value=value, raw=raw, source_ids=[source_id])
                                           for name, value, raw in facts],
                          quantity=amount, unit=unit, quantity_text=raw_amount if amount is not None else None,
                          unit_text=unit))
        offset += len(line) + 1
    if extra:
        kind, line = extra
        lines.append(line)
        records.append(dict(id='extra', kind=kind, start=offset, end=offset + len(line)))
    members = groups or [[i['id'] for i in items]]
    plan = dict(intent='create_request', extraction=dict(records=records, items=items,
        groups=[dict(id=f'g{n}', label='Внутренняя коммерческая группа', item_ids=ids,
                     rationale='Внутреннее обоснование GPT', differences=['Служебное пояснение различий'])
                for n, ids in enumerate(members, 1)], questions=list(questions)))
    source = '\n'.join(lines)
    state = RequestEngine().create(AgentPlan.model_validate(plan).extraction, source, 'synthetic-027')
    return source, plan, state


@pytest.mark.parametrize('view', ['normalized', 'forward'])
def test_single_professional_line_without_service_text(view):
    _, _, state = sample()
    assert render(state, view) == 'Лист 8×1500×6000 мм; С245; ГОСТ 19903-2015 — 1,23456789 т'
    assert state.revision == 1


@pytest.mark.parametrize('view', ['normalized', 'forward'])
def test_different_and_duplicate_items_remain_separate_without_group_comments(view):
    rows = [('Уголок', [('size', '50×50×5 мм', '50×50×5 мм'), ('grade', 'Ст3сп', 'Ст3сп'),
                       ('standard', 'ГОСТ 8509-93', 'ГОСТ 8509-93')], '2.125', 'т'),
            ('Уголок', [('size', '50×50×5 мм', '50×50×5 мм'), ('grade', '09Г2С', '09Г2С'),
                       ('standard', 'ГОСТ 19281-2014', 'ГОСТ 19281-2014')], '317.50', 'кг'),
            ('Уголок', [('size', '50×50×5 мм', '50×50×5 мм'), ('grade', 'Ст3сп', 'Ст3сп'),
                       ('standard', 'ГОСТ 8509-93', 'ГОСТ 8509-93')], '2.125', 'т')]
    _, _, state = sample(rows, groups=[['i1', 'i2'], ['i3']])
    before = state.model_dump_json()
    text = render(state, view)
    assert text.splitlines() == [
        'Уголок 50×50×5 мм; Ст3сп; ГОСТ 8509-93 — 2,125 т',
        'Уголок 50×50×5 мм; 09Г2С; ГОСТ 19281-2014 — 317,5 кг',
        'Уголок 50×50×5 мм; Ст3сп; ГОСТ 8509-93 — 2,125 т']
    for forbidden in ('версия', 'Группа', 'Количество:', 'взаимозаменяемость', 'Служебное', 'Внутренн', state.id, 'i1', 'g1'):
        assert forbidden not in text
    assert state.model_dump_json() == before
    detail = render(state, 'explanation')
    assert 'Внутреннее обоснование GPT' in detail and 'Служебное пояснение различий' in detail
    assert 'взаимозаменяемость не подтверждена' in detail


@pytest.mark.parametrize('field,raw', [('Марка стали', 'марка не указана'), ('ГОСТ', 'ГОСТ не указан')])
def test_explicit_unknown_characteristics_and_questions_stay_visible(field, raw):
    _, _, state = sample([('Швеллер', [('Размер', '12П', '12П'), (field, None, raw)], '4.125', 'т')],
                         questions=[f'Уточните {field} позиции i1'])
    text = render(state)
    assert field + ': не уточнено' in text
    assert 'Уточните' in text and 'позиция 1' in text
    assert 'i1' not in text and 'Заявка требует уточнения.' in text
    assert 'Не требуется уточнений' not in text


def test_missing_unrepresented_grade_or_standard_is_not_invented_or_declared_complete():
    _, _, state = sample([('Труба', [('Размер', '159×6 мм', '159×6 мм')], '18', 'м')])
    assert not RequestEngine.partial(state)  # Not proof of professional completeness.
    text = render(state)
    assert text == 'Труба 159×6 мм — 18 м'
    for forbidden in ('ГОСТ', 'Ст20', 'Э/С', 'Б/Ш', 'Не требуется уточнений'):
        assert forbidden not in text


@pytest.mark.parametrize('quantity,unit,expected', [
    ('4.25', None, '4,25 (единица не уточнена)'),
    (None, 'шт', 'количество не уточнено шт'),
    ('4.25', 'упак', '4,25 упак')])
def test_unknown_quantity_or_unit_kept_without_tonne_conversion(quantity, unit, expected):
    _, _, state = sample([('Изделие', [], quantity, unit)])
    assert expected in render(state)
    assert not any(line.endswith(' т') for line in render(state).splitlines())


@pytest.mark.parametrize('amount', ['0.000000000000000000123456789', '123456789012345678901234567890.123456789', '0', '10.25000'])
def test_decimal_never_rounds_or_uses_exponent(amount):
    _, _, state = sample([('Лист', [], amount, 'кг')])
    text = render(state)
    rendered = text.split(' — ')[1].removesuffix(' кг')
    assert Decimal(rendered.replace(',', '.')) == Decimal(amount)
    assert 'E' not in rendered and 'e' not in rendered
    assert state.items[0].quantity == Decimal(amount)


def test_all_norms_and_technical_conditions_survive_and_label_fact_is_not_repeated():
    facts = [('size', '159×6 мм', '159×6 мм'), ('grade', 'Ст20', 'Ст20'),
             ('norm1', 'ГОСТ 10704-91', 'ГОСТ 10704-91'),
             ('norm2', 'ГОСТ 10705-80', 'ГОСТ 10705-80'), ('spec', 'ТУ 1-2', 'ТУ 1-2'),
             ('length', '6 м', '6 м'), ('execution', 'электросварная', 'электросварная')]
    _, _, state = sample([('Труба Ст20', facts, '7.625', 'т')])
    text = render(state)
    for _, value, _ in facts:
        assert value in text
    assert text.count('Ст20') == 1
    assert text.endswith('7,625 т')


def test_city_notes_and_unresolved_are_distinguished():
    rows = [('Лист', [('city', 'Екатеринбург', 'Екатеринбург')], '1', 'т')]
    _, _, state = sample(rows, extra=('note', 'Внутренняя заметка'))
    assert render(state).endswith('Город: Екатеринбург')
    assert 'Внутренняя заметка' not in render(state)
    _, _, unresolved = sample(rows, extra=('unresolved', 'Условие требует разбора'))
    assert 'Не обработано: Условие требует разбора' in render(unresolved)
    assert 'Заявка требует уточнения.' in render(unresolved)


@pytest.mark.parametrize('view', ['normalized', 'forward', 'explanation', 'original', 'totals'])
def test_render_does_not_mutate_originals_groups_or_history(view):
    _, _, state = sample()
    before = deepcopy(state.model_dump(mode='json'))
    render(state, view)
    assert state.model_dump(mode='json') == before


def test_long_real_queue_response_has_all_lines_and_durable_parts_in_order(rig):
    rows = [('Лист', [('size', f'{n}×1500×6000 мм', f'{n}×1500×6000 мм'),
                       ('grade', 'С245', 'С245')], str(Decimal(n) / 4), 'т') for n in range(1, 121)]
    source, plan, _ = sample(rows)
    fake = Fake(plan)
    admit(rig, 101, text=source)
    Executor(rig.queue, rig.access, lambda: rig.api, lambda: fake).execute(rig.queue.claim())
    state = Repository(rig.queue.database).load(1)
    text = render(state,expert=True)
    parts = [payload['text'] for method, payload in rig.api.calls if method == 'sendMessage']
    assert len(parts) > 1 and ''.join(parts) == text
    assert all(len(p.encode('utf-16-le')) // 2 <= 3200 for p in parts)
    assert all(p.endswith('\n') for p in parts[:-1])
    assert sum(len(p.splitlines()) for p in parts) == 120
    assert len(fake.calls) == 1 and rig.queue.job(101)['state'] == 'delivered'
    assert state.original_text == source and len(state.items) == 120


@pytest.mark.parametrize('text', ['abc\ndef\nghi', ('Сталь 🔩 ' * 900) + '\nКонец', 'А' * 6401, 'a\r\nb\r\n' * 800], ids=['lines', 'oversized-emoji-line', 'oversized-line', 'crlf'])
def test_chunking_never_loses_characters_or_exceeds_utf16_limit(text):
    parts = safe_chunks(text)
    assert ''.join(parts) == text
    assert all(part and len(part.encode('utf-16-le')) // 2 <= 3200 for part in parts)


def test_create_show_edit_undo_use_current_compact_data_with_one_plan_each(tmp_path):
    from database import Database
    source, plan, _ = sample()
    repo = Repository(Database(tmp_path / 'presentation.sqlite3'))
    fake = Fake(plan)
    agent = SteelAgent(fake, repo)
    created = agent.handle(1, source, event_id='create')
    first = repo.load(1)
    fake.plans = [edit(first, [set_op('i1', field='quantity', value='7.625', evidence='7.625')])]
    changed = agent.handle(1, 'Количество 7.625', event_id='edit')
    assert changed.text.endswith('7,625 т') and repo.load(1).revision == 2
    fake.plans = [edit(repo.load(1), [dict(kind='undo')])]
    restored = agent.handle(1, 'Отмени', event_id='undo')
    before = repo.load(1).model_dump_json()
    fake.plans = [dict(intent='show_request', view='normalized')]
    shown = agent.handle(1, 'Покажи', event_id='show')
    assert shown.text == restored.text == created.text
    assert repo.load(1).model_dump_json() == before and repo.load(1).revision == 3
    assert repo.version(1, first.id, 1).original_text == source
    assert repo.load(1).groups == first.groups and len(fake.calls) == 4
