"""PR22 blocker: independently explicit dimensions cannot be hidden by MassSpec.

Only synthetic products, supplied coordinator plans and temporary SQLite; no API.
"""
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path

import pytest

from database import Database
from steel_agent_v2 import SteelAgent, Repository
from steel_agent_v2.engine import EngineError
from steel_agent_v2.presentation import render
from steel_agent_v2.theoretical_mass import (
    applicable, calculate, classification, refresh, validate_annotations, validated_for_display,
)
from test_steel_agent_v2 import Fake, edit, set_op
from test_steel_agent_v2_027_presentation import sample
from test_steel_agent_v2_theoretical_mass import sheet_plan, pipe_plan
from test_steel_agent_v2_queue import rig, admit, Executor


CASES=json.loads((Path(__file__).parent/'fixtures/steel_v2_stage7_cases.json').read_text(encoding='utf-8'))['cases']
ALIASES=['wall','wall_mm','wall_thickness','thickness','thickness_mm','толщина стенки',
         'толщина_стенки_мм','стенка','толщина','explicit_wall']


def pipe(wall='3 мм',name='wall'):
    source,plan,_=sample([('Труба профильная',[
        ('profile','40×40×3','40×40×3'),('grade','Ст3','Ст3'),('length','6м','6м'),
        (name,wall,wall)],'430','шт')])
    hint=deepcopy(CASES[1]['plan']['mass_specs'][0]);hint.pop('standard_name')
    plan['mass_specs']=[hint]
    return source,plan


def run(tmp_path,source,plan):
    repo=Repository(Database(tmp_path/'dimensions.sqlite3'));fake=Fake(plan);agent=SteelAgent(fake,repo)
    reply=agent.handle(1,source,event_id='create')
    assert reply.status in {'ok','partial'},reply.diagnostics
    assert len(fake.calls)==1 and repo.load(1).original_text==source
    return agent,repo,fake,reply


@pytest.mark.parametrize('name',ALIASES)
@pytest.mark.parametrize('value,expected',[('5 мм',None),('3 мм','8.6688')])
def test_unreferenced_explicit_wall_agrees_with_profile_or_blocks_calculation(tmp_path,name,value,expected):
    source,plan=pipe(value,name);_,repo,_,reply=run(tmp_path,source,plan);state=repo.load(1)
    mass=state.theoretical_mass[0]
    assert mass.tonnes==(Decimal(expected) if expected else None)
    assert state.items==state.originals and state.items[0].quantity==430
    if expected is None:
        assert mass.status=='unavailable' and 'размер' in mass.reason.casefold()
        assert '≈8,669' not in reply.text and '5 мм' in reply.text


@pytest.mark.parametrize('value',['0,3 см','0,003 м'])
def test_equivalent_dimension_units_are_checked_with_decimal(tmp_path,value):
    source,plan=pipe(value);_,repo,_,_=run(tmp_path,source,plan)
    assert repo.load(1).theoretical_mass[0].tonnes==Decimal('8.6688')


@pytest.mark.parametrize('value',[None,'3 или 5 мм','5 неизвестных_единиц','3'])
def test_unestablished_dimension_compatibility_preserves_product_without_mass(tmp_path,value):
    source,plan=pipe('3 мм');fact=plan['extraction']['items'][0]['characteristics'][-1]
    # A supplied extraction may preserve unknown/ambiguous information verbatim.
    fact['value']=value
    if value=='3':fact['raw']='3'
    _,repo,_,_=run(tmp_path,source,plan)
    assert repo.load(1).theoretical_mass[0].status=='unavailable'
    assert repo.load(1).items[0].quantity==430


@pytest.mark.parametrize('role,value,expected',[
    ('width','41 мм',None),('width','40 мм','8.6688'),
    ('height','39 мм',None),('height','40 мм','8.6688'),
    ('ширина','41 мм',None),('высота','40 мм','8.6688'),
])
def test_width_and_height_are_checked_even_when_gpt_uses_only_profile(tmp_path,role,value,expected):
    source,plan=pipe(value,role);_,repo,_,_=run(tmp_path,source,plan)
    assert repo.load(1).theoretical_mass[0].tonnes==(Decimal(expected) if expected else None)


@pytest.mark.parametrize('role,value,expected',[('diameter','160 мм',None),('диаметр','159 мм','5.4336')])
def test_round_pipe_diameter_cannot_be_hidden_by_composite_size(tmp_path,role,value,expected):
    source,plan=pipe_plan()
    source+='; '+value
    plan['extraction']['records'][0]['end']=len(source)
    item=plan['extraction']['items'][0]
    item['characteristics'].append(dict(name=role,value=value,raw=value,source_ids=[item['source_id']]))
    _,repo,_,_=run(tmp_path,source,plan)
    assert repo.load(1).theoretical_mass[0].tonnes==(Decimal(expected) if expected else None)


@pytest.mark.parametrize('value,expected',[('3 мм',None),('2 мм','28.6034375')])
def test_sheet_thickness_is_independent_of_gpt_component_pointer(tmp_path,value,expected):
    plan=sheet_plan()
    # Existing helper uses a different safe source; its exact initial source is fixed.
    from test_steel_agent_v2_theoretical_mass import SHEET
    source=SHEET+'; толщина '+value
    plan['extraction']['records'][0]['end']=len(source)
    item=plan['extraction']['items'][0]
    item['characteristics'].append(dict(name='толщина',value=value,raw=value,source_ids=[item['source_id']]))
    _,repo,_,_=run(tmp_path,source,plan)
    assert repo.load(1).theoretical_mass[0].tonnes==(Decimal(expected) if expected else None)


def test_agreeing_dimension_from_another_source_record_is_allowed(tmp_path):
    source,plan=pipe();item=plan['extraction']['items'][0]
    source+='\nОбщая стенка: 3 мм'
    start=source.index('Общая стенка')
    plan['extraction']['records'].append(dict(id='header',kind='note',start=start,end=len(source)))
    item['characteristics'][-1]['source_ids']=['header']
    _,repo,_,_=run(tmp_path,source,plan)
    assert repo.load(1).theoretical_mass[0].tonnes==Decimal('8.6688')
    assert repo.load(1).items[0].characteristics[-1].source_ids==['header']


@pytest.mark.parametrize('resubmit_spec',[True,False])
def test_edit_old_spec_restart_readonly_and_undo_keep_independent_facts(tmp_path,resubmit_spec):
    source,plan=pipe();agent,repo,fake,_=run(tmp_path,source,plan);before=repo.load(1)
    patched=edit(before,[set_op(field='characteristic',name='wall',value='5 мм',evidence='5 мм')])
    if resubmit_spec:patched['mass_specs']=deepcopy(plan['mass_specs'])
    fake.plans.append(patched)
    reply=agent.handle(1,'Стенка теперь 5 мм',event_id='edit')
    assert reply.status=='ok',reply.diagnostics
    changed=repo.load(1)
    assert changed.theoretical_mass[0].tonnes is None
    assert 'размер' in changed.theoretical_mass[0].reason.casefold()
    assert changed.originals==before.originals and changed.original_text==source
    assert changed.items[0].quantity==430 and changed.revision==2
    assert repo.load(2) is None
    reopened=Repository(Database(tmp_path/'dimensions.sqlite3'))
    next_fake=Fake(dict(intent='show_request'),dict(intent='show_request',view='explanation'))
    next_agent=SteelAgent(next_fake,reopened)
    for event in ['show','explain']:
        shown=next_agent.handle(1,'Покажи',event_id=event)
        assert '≈8,669' not in shown.text and reopened.load(1)==changed
    next_fake.plans.append(edit(changed,[dict(kind='undo')]))
    undone=next_agent.handle(1,'Отмени',event_id='undo')
    restored=reopened.load(1)
    assert undone.status=='ok' and restored.revision==3
    assert restored.items==before.items and restored.theoretical_mass==before.theoretical_mass
    assert reopened.version(1,before.id,2)==changed
    assert reopened.last_display(1,restored).item_ids==['i1']
    assert len(fake.calls)==2 and len(next_fake.calls)==3


def test_historical_wrong_mass_cannot_survive_validation_display_or_refresh(tmp_path):
    source,plan=pipe();agent,repo,_,_=run(tmp_path,source,plan);good=repo.load(1)
    item=good.items[0].model_copy(update={'characteristics':[
        c.model_copy(update={'value':'5 мм'}) if c.name=='wall' else c for c in good.items[0].characteristics]})
    mass=good.theoretical_mass[0];hint=mass.spec
    damaged_mass=mass.model_copy(update={'classification':classification(item,hint)})
    bad=good.model_copy(update={'items':[item],'theoretical_mass':[damaged_mass]})
    assert not applicable(mass.reference,item,hint)
    assert calculate(item,hint,damaged_mass.classification,[mass.reference]).tonnes is None
    with pytest.raises(EngineError,match='invalid_or_stale_mass_annotation'):validate_annotations(bad)
    assert refresh(bad,catalog=agent.engine.mass_catalog).theoretical_mass[0].tonnes is None
    safe=validated_for_display(bad)
    assert safe.items==bad.items and safe.originals==good.originals and safe.theoretical_mass[0].tonnes is None
    original_payload=bad.model_dump_json()
    for view in ['normalized','explanation']:
        assert '≈8,669' not in render(bad,view,expert=True)
    assert bad.model_dump_json()==original_payload
    # Simulate an already persisted bad annotation made by the previous version.
    with repo.database.connection() as c:
        c.execute('UPDATE steel_v2_active SET payload=? WHERE user_id=1',(original_payload,))
        c.execute('UPDATE steel_v2_version SET payload=? WHERE user_id=1 AND revision=1',(original_payload,))
    reopened=Repository(Database(tmp_path/'dimensions.sqlite3'))
    fake=Fake(dict(intent='show_request',view='explanation'))
    assert '≈8,669' not in SteelAgent(fake,reopened).handle(1,'Объясни',event_id='reopened').text
    assert reopened.load(1).model_dump_json()==original_payload
    # Even undo to a historical bad calculated annotation restores goods only.
    current=reopened.load(1)
    fake.plans.append(edit(current,[set_op(field='quantity',name=None,value='7',evidence='7')]))
    next_agent=SteelAgent(fake,reopened)
    assert next_agent.handle(1,'Количество 7',event_id='seven').status=='ok'
    fake.plans.append(edit(reopened.load(1),[dict(kind='undo')]))
    undone=next_agent.handle(1,'Отмени',event_id='undo-old')
    restored=reopened.load(1)
    assert undone.status=='ok' and restored.items==bad.items and restored.originals==good.originals
    assert restored.theoretical_mass[0].tonnes is None and restored.revision==3
    assert reopened.version(1,good.id,1).model_dump_json()==original_payload


@pytest.mark.parametrize('name,value,expected',[
    ('wall_mm','3','8.6688'),('wall_mm','5',None),('wall_m','0.003','8.6688'),
    ('wall_cm','0,3 см','8.6688'),
])
def test_units_explicit_in_structured_field_name_are_respected(tmp_path,name,value,expected):
    source,plan=pipe(value,name);_,repo,_,_=run(tmp_path,source,plan)
    assert repo.load(1).theoretical_mass[0].tonnes==(Decimal(expected) if expected else None)


@pytest.mark.parametrize('name,value',[('wall_cm','3 мм'),('wall_mm','0,3 см')])
def test_contradictory_unit_declarations_are_not_silently_prioritized(tmp_path,name,value):
    source,plan=pipe(value,name);_,repo,_,_=run(tmp_path,source,plan)
    state=repo.load(1)
    assert state.theoretical_mass[0].tonnes is None and state.items==state.originals


@pytest.mark.parametrize('value,expected',[('40×40×3 мм','8.6688'),('40×40×5 мм',None)])
def test_secondary_structured_size_is_not_silently_ignored(tmp_path,value,expected):
    source,plan=pipe(value,'size');_,repo,_,_=run(tmp_path,source,plan)
    assert repo.load(1).theoretical_mass[0].tonnes==(Decimal(expected) if expected else None)


def test_conflict_response_is_durable_and_delivery_unknown_never_replays_gpt(rig):
    from steel_agent_v2.queue import DurableQueue
    source,plan=pipe('5 мм');fake=Fake(plan);rig.api.fail=True
    admit(rig,701,text=source)
    Executor(rig.queue,rig.access,lambda:rig.api,lambda:fake).execute(rig.queue.claim())
    repo=Repository(rig.queue.database);state=repo.load(1)
    assert state.theoretical_mass[0].tonnes is None and state.items[0].quantity==430
    assert rig.queue.job(701)['state']=='delivery_unknown' and len(fake.calls)==1
    with rig.queue.database.connection() as c:
        outbox=c.execute('SELECT payload FROM steel_v2_outbox WHERE update_id=701').fetchall()
        turns=c.execute('SELECT COUNT(*) FROM steel_v2_turn').fetchone()[0]
    assert outbox and turns==1
    assert all('≈8,669' not in json.loads(row[0])['text'] for row in outbox)
    reopened=DurableQueue(rig.queue.database);reopened.recover()
    rig.api.fail=False
    before=list(rig.api.calls)
    reopened.deliver(701,rig.api,rig.access)
    admit(rig,701,text=source)
    assert rig.queue.claim() is None and rig.api.calls==before and len(fake.calls)==1
    assert repo.load(1)==state
