"""Synthetic procurement fixtures only; no production messages or API calls."""
from dataclasses import replace
from decimal import Decimal, localcontext
from unittest.mock import Mock
import pytest

from consolidation import consolidate_items, validate_consolidation
from database import Database
from formatter import format_normalized_request
from normalization_policy import GradeRule
from normalizer import normalize_request
from product_identity import consolidation_key
from quantity import calculate_reference_mass
from repositories.access_repository import AccessRepository
from repositories.normalization_policy_repository import NormalizationPolicyRepository
from repositories.telegram_repository import TelegramRepository
from schemas import PurchaseRequest, RequestItem
from telegram_service import TelegramService
from workbench import NormalizationWorkbench


def row(n=0, quantity='1', unit='т', grade='Ст3', standard=None):
    size = f'{50+n}×{50+n}×5'
    return RequestItem(product='Уголок', product_group='angle', size_designation=size,
        grade=grade, quantity=quantity, unit=unit, gost_tu=standard,
        source_text=f'Уголок {size} {grade} {standard or ""} — {quantity} {unit}')


def normalized(items):
    return normalize_request(PurchaseRequest(items=items)).items


def rule(**updates):
    data = dict(rule_id='synthetic-angle', version=1, product_group='angle',
                size_designation='50×50×5', gost_tu=None, from_grade='Ст3сп5',
                to_grade='Ст3', basis='Synthetic test approval; not a normative equivalence')
    return GradeRule(**(data | updates))


@pytest.fixture
def store(tmp_path):
    db = Database(tmp_path/'isolated.sqlite3')
    access = AccessRepository(db)
    access.bootstrap_configured_admin(99)
    return NormalizationPolicyRepository(db, access)


@pytest.mark.parametrize('count,unique', [(40,40),(40,20),(40,15),(100,15)])
@pytest.mark.parametrize('interface', ['telegram','workbench'])
def test_large_requests(count, unique, interface, tmp_path):
    # Non-adjacent repeats and mixed units. Every source contributes exactly 1 t.
    items = [row(i % unique, '1000' if i%2 else '1', 'кг' if i%2 else 'т') for i in range(count)]
    text = '\n'.join(i.source_text for i in items)
    ai = Mock()
    ai.parse_request.side_effect = lambda *a, **k: PurchaseRequest(items=[i.model_copy(deep=True) for i in items])
    if interface == 'telegram':
        repo = TelegramRepository(Database(tmp_path/'telegram.sqlite3'))
        result = TelegramService(ai, repo).process(1, text)
        current = repo.load(1)
        assert len('\n'.join(result).splitlines()) == unique
        groups = consolidate_items(current.items)
    else:
        result = NormalizationWorkbench(ai).process(text)
        current = result.current_request
        groups = result.consolidated_groups
    assert len(current.items) == count and len(groups) == unique
    assert sorted(i for g in groups for i in g.source_indices) == list(range(1,count+1))
    assert sum(g.item.quantity for g in groups) == Decimal(count)
    for group in groups:
        assert group.item.quantity == len(group.source_indices)
    ai.interpret_message.assert_not_called()


@pytest.mark.parametrize('fault', ['missing','double','amount','grade','unit','standard','links'])
def test_independent_group_validation_detects_corruption(fault):
    items = normalized([row(),row()])
    groups = consolidate_items(items)
    group = groups[0]
    if fault == 'missing':
        groups = []
    elif fault == 'double':
        groups = [group,group]
    elif fault == 'links':
        groups = [replace(group,source_positions=())]
    else:
        broken = group.item.model_copy(deep=True)
        setattr(broken, {'amount':'quantity','standard':'gost_tu'}.get(fault,fault),
                {'amount':Decimal('3'),'grade':'Ст20','unit':'м','standard':'ГОСТ 123-00'}[fault])
        groups = [replace(group,item=broken)]
    with pytest.raises(ValueError):
        validate_consolidation(items,groups)


def test_rules_require_admin_version_and_regression(store):
    store.propose(rule())
    assert not store.active_rules()
    with pytest.raises(PermissionError):
        store.decide('synthetic-angle',1,1,approve=True,regression_evidence='test run')
    with pytest.raises(ValueError):
        store.decide('synthetic-angle',1,99,approve=True)
    store.decide('synthetic-angle',1,99,approve=True,regression_evidence='test_large_normalization.py on test SHA')
    assert store.active_rules() == (rule(),)
    with pytest.raises(ValueError):
        store.decide('synthetic-angle',1,99,approve=True,regression_evidence='again')
    store.propose(rule(version=2))
    store.decide('synthetic-angle',2,99,approve=True,regression_evidence='version 2 regression')
    assert [r.version for r in store.active_rules()] == [2]
    store.decide('synthetic-angle',2,99,approve=False)
    assert not store.active_rules()


def test_approved_rule_merges_view_only_and_survives_restart(store):
    items = normalized([row(grade='Ст3сп5'), row(quantity='1500',unit='кг')])
    before = [i.model_dump_json() for i in items]
    assert len(consolidate_items(items)) == 2
    store.propose(rule())
    assert len(consolidate_items(items,approved_rules=store.active_rules())) == 2
    store.decide('synthetic-angle',1,99,approve=True,regression_evidence='synthetic regression')
    restarted = NormalizationPolicyRepository(store.database,store.access)
    groups = consolidate_items(items,approved_rules=restarted.active_rules())
    assert len(groups)==1 and groups[0].item.quantity==Decimal('2.5')
    assert groups[0].item.grade=='Ст3'
    assert groups[0].applied_rules == ('synthetic-angle@1',)
    assert [i.model_dump_json() for i in items] == before
    assert consolidate_items(items,approved_rules=restarted.active_rules())==groups


@pytest.mark.parametrize('change', ['size','standard','unknown','grade'])
def test_rule_scope_does_not_expand(change):
    item = row(grade='Ст3сп5', n=1 if change=='size' else 0,
               standard='ГОСТ 8509-93' if change=='standard' else None)
    if change=='unknown': item.source_text += ' с покрытием заказчика'
    if change=='grade': item = row(grade='Ст20')
    group = consolidate_items(normalized([item]),approved_rules=(rule(),))[0]
    assert group.item.grade != 'Ст3' and not group.applied_rules


def test_conflicting_rules_fail_closed():
    with pytest.raises(ValueError,match='Конфликт'):
        consolidate_items(normalized([row(grade='Ст3сп5')]),
            approved_rules=(rule(),rule(rule_id='conflict',to_grade='Ст20')))


@pytest.mark.parametrize('unit,rate,amount,total', [('м','2.345','100','0.234500'),('шт','12.34','5','0.06170'),
    ('м','0.0000000000001','123456789012345678901234567890','12345678901234.5678901234567890')])
def test_known_reference_mass_exact_and_demand_immutable(unit,rate,amount,total):
    item = normalized([row(quantity=amount,unit=unit)])[0]
    before = item.model_dump_json()
    with localcontext() as ctx:
        ctx.prec = 6
        result = calculate_reference_mass(item,kg_per_unit=rate,reference_unit=unit,
            basis='synthetic verified specification',expected_identity=consolidation_key(item))
    assert result.total_t == Decimal(total)
    assert result.reliability == 'verified_reference'
    assert result.input_quantity == Decimal(amount)
    assert item.model_dump_json() == before


@pytest.mark.parametrize('bad', ['unit','identity','basis','rate','missing','fractional'])
def test_incomplete_reference_never_invents_mass(bad):
    item = normalized([row(quantity='1.5' if bad=='fractional' else '2',unit='шт')])[0]
    identity = consolidation_key(item)
    if bad=='missing': item.quantity=None
    result = calculate_reference_mass(item, kg_per_unit='NaN' if bad=='rate' else '2',
        reference_unit='м' if bad=='unit' else 'шт', basis='' if bad=='basis' else 'test',
        expected_identity=() if bad=='identity' else identity)
    assert result.status=='unavailable' and result.total_t is None


def test_supplier_mass_is_separate_from_customer_requirement():
    item = normalized([row(quantity='3',unit='шт')])[0]
    before = item.model_dump_json()
    result = calculate_reference_mass(item, kg_per_unit='15',reference_unit='шт',basis='synthetic supplier specification',
        expected_identity=consolidation_key(item),supplier_actual=True)
    assert result.total_kg==Decimal('45') and result.reliability=='supplier_actual'
    assert item.model_dump_json()==before


def test_group_edit_rebuilds_duplicates_without_changing_other_products(tmp_path):
    items = [row(grade='Ст3сп5'),row(),RequestItem(product='Швеллер',product_group='channel',
        size_designation='10П',grade='Ст3сп5',quantity=1,unit='т',source_text='Швеллер 10П Ст3сп5 1 т')]
    ai=Mock(); ai.parse_request.return_value=PurchaseRequest(items=items)
    repo=TelegramRepository(Database(tmp_path/'edits.sqlite3'))
    service=TelegramService(ai,repo)
    service.process(1,'\n'.join(i.source_text for i in items))
    before=repo.load(1)
    output=service.process(1,'Во всех уголках замени Ст3сп5 на Ст3')
    current=repo.load(1)
    assert len(current.items)==3 and len(output[0].splitlines())==2
    assert next(i for i in current.items if i.product_group=='channel').grade=='Ст3сп5'
    assert [i.original for i in current.items]==[i.original for i in before.items]
    assert len(consolidate_items(current.items))==2
    service.process(1,'Восстанови исходную марку исходной позиции 1')
    assert len(consolidate_items(repo.load(1).items))==3
    ai.interpret_message.assert_not_called()


def test_policy_same_view_in_telegram_and_workbench(store):
    store.propose(rule());store.decide('synthetic-angle',1,99,approve=True,regression_evidence='test')
    items=[row(),row(grade='Ст3сп5')];text='\n'.join(i.source_text for i in items)
    ai=Mock();ai.parse_request.side_effect=lambda *a,**k: PurchaseRequest(items=[i.model_copy(deep=True) for i in items])
    repo=TelegramRepository(store.database)
    output=TelegramService(ai,repo,policy_repository=store).process(1,text)
    result=NormalizationWorkbench(ai,store).process(text)
    assert len(output[0].splitlines())==len(result.consolidated_groups)==1
    assert result.consolidated_groups[0].item.quantity==Decimal('2')
    assert {i.grade for i in repo.load(1).items}=={'Ст3','Ст3сп5'}


@pytest.mark.parametrize('fault',['missing','additional','double'])
def test_large_source_fidelity_before_any_grouping(fault,tmp_path):
    items=[row(i%15) for i in range(40)]
    text='\n'.join(i.source_text for i in items)
    extracted=[i.model_copy(deep=True) for i in items]
    if fault=='missing': extracted.pop()
    elif fault=='additional': extracted.append(row(99))
    else: extracted[-1]=row(0)
    ai=Mock();ai.parse_request.side_effect=lambda *a,**k:PurchaseRequest(items=extracted)
    repo=TelegramRepository(Database(tmp_path/'fidelity.sqlite3'))
    with pytest.raises(ValueError): TelegramService(ai,repo).process(1,text)
    assert repo.load(1) is None
    with pytest.raises(ValueError): NormalizationWorkbench(ai).process(text)


def test_rule_does_not_expand_to_other_execution_or_length():
    for field,value in [('form','холодногнутый'),('processing','оцинкованный'),('length_mm',6000),('length_type','measured')]:
        item=normalized([row(grade='Ст3сп5')])[0]
        setattr(item,field,value)
        group=consolidate_items([item],approved_rules=(rule(),))[0]
        assert group.item.grade=='Ст3сп5' and not group.applied_rules


def test_suggestions_are_not_equivalence_decisions(tmp_path):
    from normalization_policy import grade_merge_candidates
    items=normalized([row(),row(grade='Ст3сп5'),row(grade='Ст20',standard='ГОСТ 8509-93')])
    assert grade_merge_candidates(items)==[((1,'Ст3'),(2,'Ст3сп5'))]
    ai=Mock();ai.parse_request.return_value=PurchaseRequest(items=items)
    repo=TelegramRepository(Database(tmp_path/'suggestions.sqlite3'))
    service=TelegramService(ai,repo)
    service.process(1,'\n'.join(i.source_text for i in items))
    before=repo.load(1).model_dump_json()
    output=service.process(1,'Объедини все одинаковые уголки')
    assert 'для согласования' in '\n'.join(output)
    assert repo.load(1).model_dump_json()==before
    assert len(consolidate_items(repo.load(1).items))==3


def test_unknown_singleton_cannot_lose_requirement():
    items=normalized([row()]);items[0].source_text += ' специальная упаковка'
    group=consolidate_items(items)[0]
    broken=group.item.model_copy(deep=True);broken.source_text='Уголок'
    with pytest.raises(ValueError):validate_consolidation(items,[replace(group,item=broken)])


def test_duplicate_source_identity_is_rejected():
    from telegram_service import ActiveItem
    item=normalized([row()])[0]
    active=ActiveItem(**item.model_dump(),item_id='synthetic-id',source_position=1,original=item)
    with pytest.raises(ValueError):consolidate_items([active,active.model_copy(deep=True)])


def test_conditional_grade_edit_only_approved_scope(store):
    store.propose(rule());store.decide('synthetic-angle',1,99,approve=True,regression_evidence='test')
    items=[row(grade='Ст3сп5'),row(n=1,grade='Ст3сп5')]
    ai=Mock();ai.parse_request.return_value=PurchaseRequest(items=items)
    repo=TelegramRepository(store.database)
    service=TelegramService(ai,repo,policy_repository=store)
    service.process(1,'\n'.join(i.source_text for i in items))
    service.process(1,'Поменяй Ст3сп5 на Ст3 там, где это разрешено')
    current=repo.load(1)
    assert [i.grade for i in current.items]==['Ст3','Ст3сп5']
    assert all(i.original.grade=='Ст3сп5' for i in current.items)
    ai.interpret_message.assert_not_called()


def test_history_keeps_rule_snapshot_while_new_response_honors_revocation(store):
    store.propose(rule());store.decide('synthetic-angle',1,99,approve=True,regression_evidence='test')
    items=[row(),row(grade='Ст3сп5')]
    ai=Mock();ai.parse_request.return_value=PurchaseRequest(items=items)
    repo=TelegramRepository(store.database);service=TelegramService(ai,repo,policy_repository=store)
    output=service.process(1,'\n'.join(i.source_text for i in items))
    historic=repo.load(1)
    assert historic.normalization_rules == [rule()]
    store.decide('synthetic-angle',1,99,approve=False)
    assert format_normalized_request(historic)==output[0]
    refreshed=service.process(1,'Объедини одинаковые позиции')
    assert len(refreshed[0].splitlines())==2
    assert repo.load(1).items==historic.items


def test_different_units_are_not_suggested_for_grade_only_merge():
    from normalization_policy import grade_merge_candidates
    items=normalized([row(),row(grade='Ст3сп5',unit='м')])
    assert grade_merge_candidates(items)==[]


def test_grade_approval_does_not_prove_sheet_density(store):
    from workbench import format_workbench_result
    sheet=RequestItem(product='Лист',product_group='sheet',size_designation='6×1500×6000',
        grade='Ст3сп5',quantity='2',unit='шт',source_text='Лист 6×1500×6000 Ст3сп5 2 шт')
    allowed=rule(product_group='sheet',size_designation='6×1500×6000',length_mm=6000)
    store.propose(allowed);store.decide(allowed.rule_id,allowed.version,99,approve=True,regression_evidence='test')
    ai=Mock();ai.parse_request.side_effect=lambda *a,**k:PurchaseRequest(items=[sheet.model_copy(deep=True)])
    repo=TelegramRepository(store.database)
    output=TelegramService(ai,repo,policy_repository=store).process(1,sheet.source_text)
    assert '=' not in output[0]
    result=NormalizationWorkbench(ai,store).process(sheet.source_text)
    assert '=' not in format_workbench_result(result)
    assert result.consolidated_groups[0].item.mass_calculation.status=='unavailable'


def test_existing_commercial_mode_keeps_source_snapshots_and_rule_versions():
    items=normalized([row(grade='Ст3сп5'),row()])
    groups=consolidate_items(items,commercial=True,approved_rules=(rule(),))
    assert len(groups)==1 and groups[0].item.quantity==2
    assert [i.grade for i in groups[0].source_items]==['Ст3сп5','Ст3']
    assert groups[0].applied_rules==('synthetic-angle@1',)


def test_conditional_edit_can_select_subset_of_commercial_group(store):
    store.propose(rule());store.decide('synthetic-angle',1,99,approve=True,regression_evidence='test')
    items=[row(grade='Ст3сп5'),row()]
    ai=Mock();ai.parse_request.return_value=PurchaseRequest(items=items)
    repo=TelegramRepository(store.database)
    service=TelegramService(ai,repo,commercial_view=True,policy_repository=store)
    service.process(1,'\n'.join(i.source_text for i in items))
    service.process(1,'Поменяй Ст3сп5 на Ст3 там где разрешено')
    assert [i.grade for i in repo.load(1).items]==['Ст3','Ст3']
    assert repo.load(1).pending_operation is None
    ai.interpret_message.assert_not_called()


def test_display_edit_uses_previously_shown_policy_snapshot(store):
    # A newly approved rule must not renumber the user's last displayed rows.
    items=[row(),row(grade='Ст3сп5'),row(n=1)]
    ai=Mock();ai.parse_request.return_value=PurchaseRequest(items=items)
    repo=TelegramRepository(store.database)
    service=TelegramService(ai,repo,policy_repository=store)
    service.process(1,'\n'.join(i.source_text for i in items))
    store.propose(rule());store.decide('synthetic-angle',1,99,approve=True,regression_evidence='test')
    service.process(1,'В позиции 2 измени количество на 7 т')
    current=repo.load(1)
    assert next(i for i in current.items if i.source_position==2).quantity==Decimal(7)
    assert next(i for i in current.items if i.source_position==3).quantity==Decimal(1)
