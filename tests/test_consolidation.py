"""Derived-view invariants with actual services, SDK transport and isolated SQLite."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, localcontext
import json
from pathlib import Path
import sqlite3

import httpx2
import pytest
from openai import DefaultHttpxClient, OpenAI

from ai import AIService
from consolidation import consolidate_items
from database import Database
from diagnostics import PipelineError, SemanticExtractionError
from formatter import format_normalized_request, format_number
from normalizer import normalize_request
from product_identity import build_identity_key, consolidation_key
from quantity import normalize_quantity, sum_quantities, to_scaled_quantity, SQLITE_INTEGER_MAX
from repositories.access_repository import AccessRepository
from repositories.telegram_repository import TelegramRepository
from request_operations import RequestOperation, request_fingerprint
from schemas import PurchaseRequest, PurchaseRequestExtraction, RequestItem
from telegram_service import TelegramService, MessagePlan, Edit
from workbench import NormalizationWorkbench, format_workbench_result


LINES = [
    'Арматура 12 А500С 2 т',
    'Арматура А500С Ø12 350 кг',
    'Труба проф. 40*40*3 1200 кг',
    'Труба профильная 40х40х3 0,8 т',
    'Лист 6*1500*6000 ст3 4 шт',
    'Уголок 100*100*4 ст3 500 кг',
]
SOURCE = '\n'.join(LINES)
EXPECTED = ('Труба профильная 40×40×3 — 2 т\n'
            'Лист 6×1500×6000 Ст3 — 4 шт. = 1,696 т\n'
            'Уголок 100×100×4 Ст3 — 0,5 т\n'
            'Арматура А500С Ø12 — 2,35 т')


def draft():
    return PurchaseRequestExtraction(items=[
        RequestItem(product='Арматура', product_group='rebar', size_designation='12', grade='А500С', quantity='2', unit='т', source_text=LINES[0]),
        RequestItem(product='Арматура', product_group='rebar', size_designation='Ø12', grade='А500С', quantity='350', unit='кг', source_text=LINES[1]),
        RequestItem(product='Труба проф.', product_group='profile_pipe', size_designation='40*40*3', quantity='1200', unit='кг', source_text=LINES[2]),
        RequestItem(product='Труба профильная', product_group='profile_pipe', size_designation='40х40х3', quantity='0.8', unit='т', source_text=LINES[3]),
        RequestItem(product='Лист', product_group='sheet', size_designation='6*1500*6000', grade='Ст3', quantity='4', unit='шт', source_text=LINES[4]),
        RequestItem(product='Уголок', product_group='angle', size_designation='100*100*4', grade='Ст3', quantity='500', unit='кг', source_text=LINES[5]),
    ])


@pytest.fixture
def context(tmp_path):
    responses, calls = deque(), []
    def respond(request):
        calls.append(json.loads(request.content))
        assert responses, 'Unexpected AI call'
        return httpx2.Response(200, json={'id':'offline-test', 'object':'chat.completion', 'created':0,
            'model':'gpt-6-luna', 'choices':[{'index':0, 'finish_reason':'stop',
            'message':{'role':'assistant', 'content':responses.popleft().model_dump_json(), 'refusal':None}}]})
    with DefaultHttpxClient(transport=httpx2.MockTransport(respond)) as http:
        client = OpenAI(api_key='offline-test', http_client=http, max_retries=0)
        ai = object.__new__(AIService); ai._client = client
        repo = TelegramRepository(Database(tmp_path/'requests.sqlite3'))
        yield TelegramService(ai, repo), repo, responses, calls
        assert not responses, 'Unused test response'


def start(context, user=1):
    service, repo, responses, _ = context
    responses.append(draft())
    output = service.process(user, SOURCE)
    return output, repo.load(user)


def simple(quantity='2', unit='т', **fields):
    item = RequestItem(product='Арматура', product_group='rebar', size_designation='12',
        diameter_mm=12, grade='А500С', quantity=quantity, unit=unit,
        source_text=f'Арматура 12 А500С {quantity} {unit}')
    for field, value in fields.items(): setattr(item, field, value)
    return item


def test_six_sources_four_groups_in_telegram_and_workbench(context):
    service, repo, responses, calls = context
    output, active = start(context)
    assert output == [EXPECTED]
    assert len(active.items) == 6 and len(consolidate_items(active.items)) == 4
    assert active.source_text == SOURCE
    assert sorted(item.source_position for item in active.items) == list(range(1,7))
    by_position = {item.source_position:item for item in active.items}
    assert [by_position[n].source_text for n in range(1,7)] == LINES
    assert [(by_position[n].quantity, by_position[n].unit) for n in range(1,7)] == [
        (Decimal('2'),'т'), (Decimal('350'),'кг'), (Decimal('1200'),'кг'),
        (Decimal('0.8'),'т'), (Decimal('4'),'шт'), (Decimal('500'),'кг')]
    assert all(item.original.quantity == item.quantity and item.original.unit == item.unit for item in active.items)
    assert active.pending_clarification is None and active.pending_operation is None
    assert len(calls) == 1
    groups = consolidate_items(active.items)
    assert sorted(index for group in groups for index in group.source_indices) == list(range(1,7))
    assert set(item_id for group in groups for item_id in group.source_item_ids) == {item.item_id for item in active.items}
    assert {frozenset(group.source_positions) for group in groups} == {frozenset({3,4}), frozenset({5}), frozenset({6}), frozenset({1,2})}
    responses.append(draft())
    result = NormalizationWorkbench(service.ai).process(SOURCE)
    assert len(result.current_request.items) == 6 and len(result.consolidated_groups) == 4
    assert result.raw_request == SOURCE and result.is_valid
    formatted = format_workbench_result(result)
    for line in EXPECTED.splitlines(): assert line.rstrip('.') in formatted


@pytest.mark.parametrize('amount,unit,expected,canonical', [
    ('1200','кг','1.2','т'), ('350','кг','0.35','т'), ('500','кг','0.5','т'),
    ('1000','кг','1','т'), ('0.0015','т','0.0015','т'),
    ('5','шт.','5','шт'), ('12','пог. м','12','м')])
def test_exact_quantity_conversion(amount, unit, expected, canonical):
    converted, converted_unit = normalize_quantity(amount, unit)
    assert converted == Decimal(expected) and converted_unit == canonical
    assert normalize_quantity(converted, converted_unit) == (converted, converted_unit)


@pytest.mark.parametrize('first,second,unit,total', [
    ('2','350','кг','2.35'), ('0.8','1200','кг','2'),
    ('0.1','0.2','т','0.3')])
def test_exact_sums(first, second, unit, total):
    a = simple(first)
    b = simple(second, unit)
    groups = consolidate_items([a,b])
    assert len(groups) == 1 and groups[0].item.quantity == Decimal(total)
    assert a.quantity == Decimal(first) and b.quantity == Decimal(second)


@pytest.mark.parametrize('unit', ['шт','м'])
def test_compatible_non_mass_units_sum(unit):
    groups = consolidate_items([simple('4',unit), simple('6',unit)])
    assert len(groups) == 1 and groups[0].item.quantity == Decimal('10')
    assert groups[0].item.unit == unit


@pytest.mark.parametrize('units', [('шт','т'),('м','т'),('шт','м'),('м²','м')])
def test_incompatible_units_never_sum(units):
    assert len(consolidate_items([simple('2',units[0]), simple('3',units[1])])) == 2


@pytest.mark.parametrize('field,value', [
    ('grade','А400'), ('gost_tu','ГОСТ 34028-2016'), ('diameter_mm',14),
    ('size_designation','14'), ('processing','оцинкованная'), ('form','бухта'),
    ('length_type','unmeasured'), ('length_mm',6000),
    ('length_requirement','длина немерная'), ('wall_thickness_requirement','стенка не менее 3 мм')])
def test_every_material_difference_prevents_merge(field, value):
    a, b = simple(), simple()
    setattr(b, field, value)
    assert len(consolidate_items([a,b])) == 2


@pytest.mark.parametrize('product_a,product_b,group,size', [
    ('Лист Г/К','Лист Х/К','sheet','6×1500×6000'),
    ('Труба э/с','Труба бесшовная','round_pipe','159×6'),
    ('Труба профильная','Труба профильная оцинкованная','profile_pipe','40×40×3')])
def test_product_subtypes_are_part_of_identity(product_a,product_b,group,size):
    request = PurchaseRequest(items=[RequestItem(product=product,product_group=group,size_designation=size,
        quantity='1',unit='т',source_text=f'{product} {size} 1 т') for product in [product_a,product_b]])
    normalize_request(request)
    assert len(consolidate_items(request.items)) == 2


@pytest.mark.parametrize('suffix', ['исполнение 1', 'допуск 0,3 м', 'упаковка завода Х', 'сертификат EN 10204 3.1'])
def test_unmodelled_source_requirement_blocks_merge(suffix):
    a,b = simple(),simple()
    b.source_text += ' '+suffix
    assert len(consolidate_items([a,b])) == 2
    # Even identical omitted qualifiers are not treated as verified fields.
    a.source_text = b.source_text
    assert len(consolidate_items([a,b])) == 2


def test_alternative_steel_is_not_split_or_merged_with_one_choice():
    a = RequestItem(product='Труба',product_group='round_pipe',size_designation='159×6',
        grade='Ст3 (или Ст20)',quantity='1',unit='т',source_text='Труба 159×6 Ст3 (или Ст20) 1 т')
    b = a.model_copy(deep=True); b.grade='Ст3'; b.source_text='Труба 159×6 Ст3 1 т'
    request=normalize_request(PurchaseRequest(items=[a,b]))
    assert len(consolidate_items(request.items)) == 2
    assert a.grade == 'Ст3 (или Ст20)'


def test_full_equal_alternative_set_can_merge_as_one_position():
    a = RequestItem(product='Труба',product_group='round_pipe',size_designation='159×6',
        grade='Ст3 (или Ст20)',quantity='1',unit='т',source_text='Труба 159×6 Ст3 (или Ст20) 1 т')
    request=normalize_request(PurchaseRequest(items=[a,a.model_copy(deep=True)]))
    groups=consolidate_items(request.items)
    assert len(groups)==1 and groups[0].item.grade=='Ст3 (или Ст20)'
    assert groups[0].item.quantity==Decimal('2')


def test_existing_product_v1_is_not_the_only_identity():
    request=normalize_request(PurchaseRequest(items=[
        RequestItem(product='Лист Г/К',product_group='sheet',size_designation='6×1500×6000',quantity=1,unit='т',source_text='Лист Г/К 6×1500×6000 1 т'),
        RequestItem(product='Лист Х/К',product_group='sheet',size_designation='6×1500×6000',quantity=1,unit='т',source_text='Лист Х/К 6×1500×6000 1 т')]))
    assert build_identity_key(request.items[0]) == build_identity_key(request.items[1])
    assert consolidation_key(request.items[0]) != consolidation_key(request.items[1])


def test_source_edit_rebuilds_sum_and_preserves_other_contributions(context):
    service,repo,_,_ = context
    _,before=start(context)
    output=service.process(1,'В исходной позиции 2 измени количество на 500 кг')
    after=repo.load(1)
    assert output[0].endswith('Арматура А500С Ø12 — 2,5 т')
    assert next(item for item in after.items if item.source_position==1) == next(item for item in before.items if item.source_position==1)
    changed=next(item for item in after.items if item.source_position==2)
    assert changed.quantity==Decimal('500') and changed.unit=='кг'
    assert changed.original.quantity==Decimal('350') and changed.original.unit=='кг'
    assert changed.source_text==LINES[1]
    with repo.database.connection() as c:
        assert c.execute('SELECT COUNT(*) FROM telegram_history').fetchone()[0]==2


def test_display_group_edit_requires_source_selection_and_survives_restart(context):
    service,repo,_,_=context
    _,before=start(context)
    output=service.process(1,'В позиции 4 измени количество на 5 т')
    assert 'исходные строки' in output[0]
    assert repo.load(1).items==before.items
    assert repo.load(1).pending_operation is not None
    service=TelegramService(service.ai,TelegramRepository(repo.database))
    assert 'конкретные' in service.process(1,'Да')[0]
    assert repo.load(1).items==before.items
    service.process(1,'/cancel')
    assert repo.load(1).pending_operation is None
    assert 'Нет ожидающего' in service.process(1,'Да')[0]


def test_workbench_source_edit_updates_view_without_changing_original(context):
    service,_,responses,_=context
    responses.append(draft())
    result=NormalizationWorkbench(service.ai).process(SOURCE)
    source=result.current_request.items[1]
    source.quantity=Decimal('500')
    rebar=next(group for group in result.consolidated_groups if group.item.product_group=='rebar')
    assert rebar.item.quantity==Decimal('2.5')
    assert result.original_items[1].quantity==Decimal('350')
    assert result.raw_request==SOURCE and source.source_text==LINES[1]


def test_repeated_projection_and_normalization_never_double_amount(context):
    service,repo,responses,_=context
    _,active=start(context)
    before=active.model_dump_json()
    for _ in range(3):
        assert format_normalized_request(active)==EXPECTED
        assert len(consolidate_items(active.items))==4
    assert active.model_dump_json()==before
    responses.append(draft())
    assert service.process(1,SOURCE)==[EXPECTED]
    assert format_normalized_request(repo.load(1))==EXPECTED


def test_restart_and_simultaneous_users_preserve_source_state(context):
    service,repo,_,_=context
    start(context,1);start(context,2)
    second=repo.load(2)
    restarted=TelegramService(service.ai,TelegramRepository(repo.database))
    assert format_normalized_request(repo.load(1))==EXPECTED
    with ThreadPoolExecutor(max_workers=2) as pool:
        outputs=list(pool.map(lambda user:restarted.process(user,'В исходной позиции 2 измени количество на 500 кг'),[1,2]))
    assert all(output[0].endswith('2,5 т') for output in outputs)
    assert len(repo.load(2).items)==len(second.items)==6


def test_missing_source_still_fails_before_consolidation_in_both_interfaces(context):
    service,repo,responses,_=context
    start(context)
    before=repo.load(1)
    missing=draft(); missing.items.pop(1)
    responses.extend([missing,missing])
    with pytest.raises(SemanticExtractionError):service.process(1,SOURCE)
    assert repo.load(1)==before
    responses.append(missing)
    with pytest.raises(SemanticExtractionError):NormalizationWorkbench(service.ai).process(SOURCE)


def test_twenty_one_position_request_still_preserves_all_sources(context):
    service,repo,responses,_=context
    fixtures=Path(__file__).parent/'fixtures'
    text=(fixtures/'mixed_21_original.txt').read_text().strip()
    extraction=PurchaseRequestExtraction.model_validate_json((fixtures/'mixed_21_extraction.json').read_text())
    responses.append(extraction)
    output=service.process(1,text)
    active=repo.load(1)
    assert len(active.items)==21 and len(output[0].splitlines())==21
    assert active.source_text==text
    assert sorted(i for g in consolidate_items(active.items) for i in g.source_indices)==list(range(1,22))


def test_quantity_precision_beyond_decimal_context_and_sqlite_scale():
    amount=Decimal('123456789012345678901234567890.1234567890123456789')
    with localcontext() as ctx:
        ctx.prec=6
        converted,unit=normalize_quantity(amount,'кг')
        assert converted==Decimal('123456789012345678901234567.8901234567890123456789')
        assert sum_quantities([amount,Decimal('0.0000000000000000001')])==Decimal('123456789012345678901234567890.1234567890123456790')
    assert normalize_quantity('0.000000001','кг')==(Decimal('0.000000000001'),'т')
    with pytest.raises(ValueError,match='точность'):to_scaled_quantity('0.000000001','кг')
    assert to_scaled_quantity(str(SQLITE_INTEGER_MAX)+'e-6','т')[0]==SQLITE_INTEGER_MAX
    with pytest.raises(ValueError,match='диапазон'):to_scaled_quantity(str(SQLITE_INTEGER_MAX+1)+'e-6','т')


@pytest.mark.parametrize('value',['0','-1','NaN','Infinity','-Infinity'])
def test_invalid_quantities_are_never_summed(value):
    with pytest.raises(ValueError):normalize_quantity(value,'кг')
    if Decimal(value).is_finite():
        assert len(consolidate_items([simple(value),simple(value)]))==2
    else:
        with pytest.raises(ValueError): simple(value)


def test_schema_access_history_and_legacy_pending_compatibility(context):
    service,repo,responses,_=context
    repo.database.initialize();access=AccessRepository(repo.database);access.bootstrap_configured_admin(99)
    _,active=start(context)
    fingerprint=request_fingerprint(active)
    # Legacy fingerprint lacked source_position. It must remain the same.
    import hashlib
    legacy=json.loads(active.model_dump_json(include={'request_id','items'}))
    for item in legacy['items']:
        item.pop('source_position')
        item.pop('mass_calculation')
        item['original'].pop('mass_calculation')
    assert hashlib.sha256(json.dumps(legacy,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()==fingerprint
    original_tables=None
    with repo.database.connection() as c:
        original_tables=[tuple(row) for row in c.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name")]
        old=json.loads(active.model_dump_json())
        for item in old['items']:item.pop('source_position')
        c.execute('UPDATE telegram_active SET request_json=? WHERE user_id=1',(json.dumps(old),))
    assert format_normalized_request(repo.load(1))==EXPECTED
    assert access.is_admin(99)
    with repo.database.connection() as c:
        assert original_tables==[tuple(row) for row in c.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name")]
        assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert not c.execute('PRAGMA foreign_key_check').fetchall()
        assert c.execute('SELECT COUNT(*) FROM telegram_history').fetchone()[0]==1


def test_structured_merge_is_only_a_view_and_cannot_apply_fake_quantity(context):
    service,repo,responses,_=context
    _,active=start(context)
    responses.append(MessagePlan(intent='operation',operation=RequestOperation(kind='merge_items',
        edits=[Edit(item_id=active.items[0].item_id,field='quantity',value='900')])))
    assert service.process(1,'Хочу видеть итог')==[EXPECTED]
    assert repo.load(1)==active


def test_ordinal_group_number_cannot_choose_a_source_arbitrarily(context):
    service,repo,_,_=context
    _,active=start(context)
    output=service.process(1,'В четвертой позиции измени количество на 5 т')
    assert 'исходные строки' in output[0]
    assert repo.load(1).items==active.items
    service.process(1,'Нет')
    assert repo.load(1).pending_operation is None
    output=service.process(1,'В второй исходной позиции измени количество на 500 кг')
    assert output[0].endswith('2,5 т')


def test_yes_no_cancel_and_repeated_yes_after_grouping(context):
    service,repo,responses,_=context
    _,active=start(context)
    target=next(item for item in active.items if item.source_position==2)
    plan=MessagePlan(intent='clarify',question='Подтвердите правку исходной строки?',
        edits=[Edit(item_id=target.item_id,field='quantity',value='500')])
    responses.append(plan)
    service.process(1,'Хочу согласовать изменение')
    service.process(1,'Нет')
    assert repo.load(1).items==active.items
    responses.append(plan)
    service.process(1,'Хочу согласовать изменение')
    restarted=TelegramService(service.ai,TelegramRepository(repo.database))
    reply = restarted.process(1,'Да')[0]
    assert 'исходными позициями' in reply
    assert repo.load(1).items == active.items
    restarted.process(1,'Отмена')
    assert repo.load(1).pending_operation is None
    assert repo.load(1).items==active.items


def test_display_order_matches_provenance_in_workbench(context):
    service,_,responses,_=context
    responses.append(draft())
    result=NormalizationWorkbench(service.ai).process(SOURCE)
    first=result.consolidated_groups[0]
    assert first.item.product_group=='profile_pipe'
    assert first.source_indices==(3,4)
    assert result.current_request.items[0].product_group=='rebar'


def test_distinct_standards_and_requirements_with_identical_geometry():
    a=simple();b=simple()
    a.gost_tu='ГОСТ 34028-2016';b.gost_tu='СТО АСЧМ 7-93'
    a.source_text += ' '+a.gost_tu;b.source_text += ' '+b.gost_tu
    assert len(consolidate_items([a,b]))==2
    a=simple();b=simple()
    a.length_requirement='длина немерная';b.length_requirement='длина мерная'
    a.source_text += ' '+a.length_requirement;b.source_text += ' '+b.length_requirement
    assert len(consolidate_items([a,b]))==2


def test_sqlite_failure_does_not_commit_source_edit_or_sum(context):
    service,repo,_,_=context
    _,active=start(context)
    with repo.database.connection() as c:
        c.execute("CREATE TRIGGER fail_update BEFORE UPDATE ON telegram_active BEGIN SELECT RAISE(ABORT,'test'); END")
    with pytest.raises(sqlite3.IntegrityError):
        service.process(1,'В исходной позиции 2 измени количество на 500 кг')
    assert repo.load(1)==active
    assert format_normalized_request(repo.load(1))==EXPECTED
    with repo.database.connection() as c:
        assert c.execute('SELECT COUNT(*) FROM telegram_history').fetchone()[0]==1


def test_numeric_geometry_spellings_merge_without_removing_unknown_x():
    request=normalize_request(PurchaseRequest(items=[
        RequestItem(product='Труба проф.',product_group='profile_pipe',size_designation='40.0*40*3.0',quantity=1,unit='т',source_text='Труба проф. 40.0*40*3.0 1 т'),
        RequestItem(product='Труба профильная',product_group='profile_pipe',size_designation='40×40×3',quantity=1,unit='т',source_text='Труба профильная 40×40×3 1 т')]))
    assert len(consolidate_items(request.items))==1
    a,b=simple(),simple()
    a.processing=b.processing='исполнение'
    a.source_text += ' исполнение'
    b.source_text += ' исполнение X'
    assert len(consolidate_items([a,b]))==2


def test_input_source_numbers_do_not_depend_on_ai_extraction_order(context):
    service,repo,responses,_=context
    reversed_draft=draft();reversed_draft.items.reverse()
    responses.append(reversed_draft)
    assert service.process(1,SOURCE)==[EXPECTED]
    active=repo.load(1)
    assert [next(i for i in active.items if i.source_position==n).source_text for n in range(1,7)]==LINES
    assert service.process(1,'В исходной позиции 2 измени количество на 500 кг')[0].endswith('2,5 т')
    assert next(i for i in repo.load(1).items if i.source_position==1).quantity==Decimal('2')
    responses.append(reversed_draft)
    result=NormalizationWorkbench(service.ai).process(SOURCE)
    assert result.source_positions==(6,5,4,3,2,1)
    assert sorted(p for g in result.consolidated_groups for p in g.source_positions)==list(range(1,7))


def test_product_abbreviations_share_complete_profile_identity():
    request=normalize_request(PurchaseRequest(items=[RequestItem(product=product,product_group='profile_pipe',
        size_designation='40×40×3',quantity='1',unit='т',source_text=f'{product} 40×40×3 1 т')
        for product in ['Труба проф.', 'Труба проф', 'Труба профильная', 'Труба']]))
    assert len(consolidate_items(request.items))==1
    assert consolidate_items(request.items)[0].item.quantity==Decimal('4')


def test_equal_sort_keys_keep_actual_source_order_when_ai_shuffles(context):
    service,_,responses,_=context
    lines=['Арматура 12 А500С 1 т', 'Арматура 12 А400 1 т']
    items=[RequestItem(product='Арматура',product_group='rebar',size_designation='12',grade=grade,
        quantity=1,unit='т',source_text=line) for grade,line in zip(['А500С','А400'],lines)]
    responses.append(PurchaseRequestExtraction(items=list(reversed(items))))
    output=service.process(1,'\n'.join(lines))[0].splitlines()
    assert 'А500С' in output[0] and 'А400' in output[1]


def test_confirmed_equal_lengths_and_length_types_can_merge():
    from validator import validate_request
    items=[RequestItem(product='Труба профильная',product_group='profile_pipe',size_designation='40×40×3',
        length_type='measured',quantity=1,unit='т',
        source_text='Труба профильная 40×40×3 длина 6 м мерная — 1 т') for _ in range(2)]
    request=normalize_request(PurchaseRequest(items=items))
    assert validate_request(request).is_valid
    groups=consolidate_items(request.items)
    assert len(groups)==1 and groups[0].item.quantity==Decimal('2')
    assert groups[0].item.length_mm==6000 and groups[0].item.length_type=='measured'


def test_complete_new_list_with_sum_instruction_has_priority_over_old_request(context):
    service,repo,responses,_=context
    responses.append(PurchaseRequestExtraction(items=[simple()]))
    service.process(1,simple().source_text)
    old=repo.load(1)
    responses.append(draft())
    text=SOURCE+'\nСуммируй одинаковые позиции'
    assert service.process(1,text)==[EXPECTED]
    assert repo.load(1).request_id!=old.request_id
    assert repo.load(1).source_text==text and len(repo.load(1).items)==6


def test_negative_extracted_source_quantity_is_not_turned_positive(context):
    service,repo,responses,_=context
    _,active=start(context)
    source='Арматура 12 А500С -350 кг'
    responses.append(PurchaseRequestExtraction(items=[RequestItem(product='Арматура',product_group='rebar',
        size_designation='12',grade='А500С',quantity='-350',unit='кг',source_text=source)]))
    with pytest.raises(PipelineError,match='положительным'):
        service.process(1,source)
    assert repo.load(1)==active
