"""Stage 3: real engine/agent/storage, scripted GPT facts, no live AI claim.

The seven-sheet fixture was explicitly approved for publication by the owner
in this stage. Other inputs are synthetic. Commercial groups are deliberately
unrelated to display kinds/grades throughout these tests.
"""
from copy import deepcopy
from decimal import Decimal, localcontext
from pathlib import Path
import json
import re

import pytest

from database import Database
from steel_agent_v2 import Repository, RequestEngine, SteelAgent
from steel_agent_v2.contracts import AgentPlan, DisplayLayout, Reply
from steel_agent_v2.presentation import display_order, render
from steel_agent_v2.recovery import partial_from_proposal
from telegram_operator import safe_chunks
from test_steel_agent_v2 import Fake, edit, set_op
from test_steel_agent_v2_027_presentation import sample
from test_steel_agent_v2_queue import rig, admit, Executor
from test_steel_agent_v2_wire_schema import sdk_transport, subset_issues


APPROVED=Path(__file__).with_name('fixtures').joinpath('steel_v2_stage3_approved_sheets.txt').read_text(encoding='utf-8').rstrip('\n')
APPROVED_ORDER=['i4','i5','i3','i1','i6','i7','i2']


def hint(key,kind='sheet',grade='grade',name='size',role='thickness',unit='мм',component=None,separator=None):
    return dict(item_id=key,kind=kind,grade_name=grade,dimensions=[] if name is None else [
        dict(role=role,name=name,unit=unit,component=component,separator=separator)])


def approved_plan():
    lines=APPROVED.splitlines();offset=0;records=[];items=[]
    for n,line in enumerate(lines):
        records.append(dict(id='header' if n==0 else f's{n}',kind='header' if n==0 else 'product',
                            start=offset,end=offset+len(line)))
        offset+=len(line)+1
        if n==0:continue
        # Script the structured interpretation; Python does not learn a product
        # parser from this one example, nor do tests make claims about live GPT.
        raw_grade,thickness=line.split()[2:4]
        grade='Ст3' if raw_grade=='ст.3' else '09Г2С-15'
        size=thickness+'×1500×6000 мм'
        items.append(dict(id=f'i{n}',source_id=f's{n}',label=f'Лист г/к {size}, {grade}',
            characteristics=[dict(name='size',value=size,raw=thickness+' 1,5х6м',source_ids=[f's{n}']),
                             dict(name='grade',value=grade,raw=raw_grade,source_ids=[f's{n}'])],
            quantity='40',quantity_text='40',unit='т',unit_text='тоннах',unit_source_id='header'))
    return dict(intent='create_request',extraction=dict(records=records,items=items,
        groups=[dict(id='commercial',label='Все строки',rationale='Тестовая группа с сохранением различий',
                     item_ids=[i['id'] for i in items])]),display_layout=dict(
        items=[hint(i['id'],component=1,separator='×') for i in items],
        grade_order=[dict(kind='sheet',grades=['Ст3','09Г2С-15'])]))


def start(tmp_path,source=APPROVED,plan=None):
    plan=plan or approved_plan();fake=Fake(plan)
    repo=Repository(Database(tmp_path/'sorting.sqlite3'));agent=SteelAgent(fake,repo)
    reply=agent.handle(1,source,event_id='create')
    assert reply.status=='ok' and reply.calls==1
    return agent,repo,fake,reply


def assert_order(reply,state,expected):
    assert reply.display.item_ids==expected
    assert display_order(state,reply.display.layout)==expected
    lines=[line for line in reply.text.splitlines() if ' — ' in line]
    assert len(lines)==len(state.items)==len(set(expected))
    assert set(expected)=={i.id for i in state.items}
    by_id={i.id:i for i in state.items}
    # Each row is identified by its full current facts, not a rendered position number.
    assert lines==[render(state.model_copy(update={'groups':[state.groups[0].model_copy(
        update={'item_ids':[key]})], 'items':[by_id[key]],
        'originals':[i for i in state.originals if i.id==key]})).splitlines()[0] for key in expected]
    assert not any(re.match(r'\d+\. ',line) for line in lines)
    assert all(text not in reply.text for text in ('Количество:','Итого','Группа ',state.id))


def test_owner_seven_sheets_exact_result_original_ids_provenance_and_header_units(tmp_path):
    agent,repo,fake,reply=start(tmp_path);state=repo.load(1)
    assert_order(reply,state,APPROVED_ORDER)
    assert reply.text=='\n'.join([
        'Лист г/к 10×1500×6000 мм, Ст3 — 40 т',
        'Лист г/к 16×1500×6000 мм, Ст3 — 40 т',
        'Лист г/к 8×1500×6000 мм, 09Г2С-15 — 40 т',
        'Лист г/к 10×1500×6000 мм, 09Г2С-15 — 40 т',
        'Лист г/к 12×1500×6000 мм, 09Г2С-15 — 40 т',
        'Лист г/к 16×1500×6000 мм, 09Г2С-15 — 40 т',
        'Лист г/к 20×1500×6000 мм, 09Г2С-15 — 40 т'])
    assert 'ГОСТ' not in reply.text and '280' not in reply.text
    assert state.original_text==APPROVED and [i.id for i in state.items]==[f'i{n}' for n in range(1,8)]
    assert all(i.quantity==Decimal(40) and i.unit=='т' and i.unit_source_id=='header' for i in state.items)
    assert state.items==state.originals and state.revision==1 and not state.changes
    expected=RequestEngine().create(AgentPlan.model_validate(approved_plan()).extraction,APPROVED,'baseline')
    assert state.records==expected.records and state.originals==expected.originals and state.groups==expected.groups
    before=state.model_dump_json()
    fake.plans.append(dict(intent='show_request'))
    shown=agent.handle(1,'Покажи заявку',event_id='show')
    assert_order(shown,state,APPROVED_ORDER)
    assert repo.load(1).model_dump_json()==before
    assert repo.version(1,state.id,1).model_dump_json()==before
    assert len(repo.history(1,state.id))==4


def test_numeric_thickness_not_lexicographic_and_no_global_steel_ranking():
    rows=[('Лист',[('size',str(n)+' мм',str(n)+' мм'),('grade','09Г2С-15','09Г2С-15')],'1.250','т')
          for n in [20,10,2,16,8]]
    _,_,state=sample(rows)
    layout=DisplayLayout(items=[hint(i.id) for i in state.items])
    assert display_order(state,layout)==['i3','i5','i2','i4','i1']
    # No universal ranking is hidden in Python: only an explicit scoped order changes grade order.
    _,_,two=sample([rows[0],('Лист',[('size','2 мм','2 мм'),('grade','Ст3','Ст3')],'1','т')])
    plain=DisplayLayout(items=[hint(i.id) for i in two.items])
    assert display_order(two,plain)==['i1','i2']


def test_mixed_six_kinds_grouping_is_independent_of_commercial_groups(tmp_path):
    kinds=['sheet','pipe','angle','channel','beam','rebar','sheet','pipe']
    labels=['Лист','Труба','Уголок','Швеллер','Балка','Арматура','Лист','Труба']
    rows=[(label,[('size',str(n)+' мм',str(n)+' мм'),('grade','С245','С245')],str(n)+'.125','т')
          for n,label in enumerate(labels,1)]
    source,plan,_=sample(rows,groups=[['i8','i7'],['i1','i2','i3','i4','i5','i6']])
    plan['display_layout']=dict(items=[hint(f'i{n}',kind,role='thickness' if kind=='sheet' else 'size') for n,kind in enumerate(kinds,1)])
    _,repo,_,reply=start(tmp_path,source,plan);state=repo.load(1)
    expected=display_order(state,reply.display.layout)
    assert_order(reply,state,expected)
    assert len(expected)==8 and abs(expected.index('i1')-expected.index('i7'))==1
    assert abs(expected.index('i2')-expected.index('i8'))==1
    assert expected.index('i1')<expected.index('i7') and expected.index('i2')<expected.index('i8')
    assert state.groups[0].item_ids==['i8','i7']
    assert state.items==state.originals


def test_same_size_different_full_grades_and_standards_never_merge():
    rows=[('Лист',[('size','8 мм','8 мм'),('grade',g,g),('standard',s,s)],q,'т') for g,s,q in [
        ('09Г2С-15','ГОСТ ТЕСТ-1','1.01234567890123456789'),('09Г2С','ГОСТ ТЕСТ-1','2'),
        ('09Г2С-15','ГОСТ ТЕСТ-2','3'),('09Г2С-15','ГОСТ ТЕСТ-1','4')]]
    _,_,state=sample(rows);before=state.model_dump_json()
    layout=DisplayLayout(items=[hint(i.id) for i in state.items])
    assert display_order(state,layout)==['i1','i3','i4','i2']
    text=render(state,layout=layout)
    assert text.count(' — ')==4 and '1,01234567890123456789 т' in text
    assert 'ГОСТ ТЕСТ-1' in text and 'ГОСТ ТЕСТ-2' in text
    assert state.model_dump_json()==before


@pytest.mark.parametrize('missing',['kind','grade','size','bad_number','bad_component','bad_unit','bad_field','duplicate','foreign'])
def test_unknown_or_invalid_display_reference_never_drops_or_changes_item(missing):
    rows=[('Лист',[('size',size,size),('grade','Ст3','Ст3')],str(n),'т')
          for n,size in enumerate(['10 мм','2 мм','8 мм'],1)]
    _,_,state=sample(rows);before=state.model_dump_json()
    hints=[hint(i.id) for i in state.items]
    if missing=='kind':hints[0]['kind']=None
    if missing=='grade':hints[0]['grade_name']='not_present'
    if missing=='size':hints[0]['dimensions']=[]
    if missing=='bad_number':
        state=state.model_copy(deep=True);state.items[0]=state.items[0].model_copy(update={
            'characteristics':[state.items[0].characteristics[0].model_copy(update={'value':'10 или 12 мм'}),state.items[0].characteristics[1]]})
        before=state.model_dump_json()
    if missing=='bad_component':hints[0]['dimensions'][0].update(separator='×',component=2)
    if missing=='bad_unit':hints[0]['dimensions'][0]['unit']='м'
    if missing=='bad_field':hints[0]['dimensions'][0]['name']='not_present'
    if missing=='duplicate':hints.append(deepcopy(hints[0]))
    if missing=='foreign':hints.append(hint('another-users-item'))
    layout=DisplayLayout(items=hints)
    order=display_order(state,layout)
    assert len(order)==3 and set(order)=={'i1','i2','i3'}
    assert render(state,layout=layout).count(' — ')==3 and state.model_dump_json()==before


def test_numeric_components_units_and_role_conflicts_use_only_verified_numbers():
    _,_,state=sample([('Труба',[('size',v,v)],'1','т') for v in ['159×0,8 см','159×6 мм','159×0,01 м']])
    layout=DisplayLayout(items=[hint(i.id,'pipe',None,role='wall',unit=None,separator='×',component=2) for i in state.items])
    assert display_order(state,layout)==['i2','i1','i3']
    # Two different meanings cannot be compared just because both values are numbers.
    bad=layout.model_copy(deep=True)
    bad.items[0].dimensions[0]=bad.items[0].dimensions[0].model_copy(update={'role':'diameter'})
    assert display_order(state,bad)==['i1','i2','i3']
    with localcontext() as context:
        context.prec=2
        assert display_order(state,layout)==['i2','i1','i3']


def test_secondary_dimensions_only_after_primary_and_no_profile_index_guess():
    _,_,state=sample([('Лист',[('size',size,size),('grade','Ст3','Ст3')],'1','т')
                      for size in ['10×1000 мм','8×2000 мм','8×1000 мм']])
    hints=[hint(i.id,component=1,separator='×') for i in state.items]
    for h in hints:
        # Deliberately put width first: BR-005 must still prioritize thickness.
        h['dimensions'].insert(0,dict(role='width',name='size',unit='мм',separator='×',component=2))
    assert display_order(state,DisplayLayout(items=hints))==['i3','i2','i1']
    for h in hints:h['dimensions']=h['dimensions'][:1]
    assert display_order(state,DisplayLayout(items=hints))==['i1','i2','i3']
    _,_,profiles=sample([('Балка',[('size',size,size)],'1','т') for size in ['20Ш2','8Б1','10Б1']])
    layout=DisplayLayout(items=[hint(i.id,'beam',None,role='profile') for i in profiles.items])
    assert display_order(profiles,layout)==['i1','i2','i3']


def test_number_after_sort_edit_resort_reopen_undo_and_show_are_one_order(tmp_path):
    agent,repo,fake,first=start(tmp_path);old=repo.load(1)
    choose=dict(mode='display',positions=[3],cardinality='one',evidence='Позицию 3')
    operation=dict(kind='set',selection=choose,field='characteristic',name='size',
                   value='22×1500×6000 мм',evidence='22×1500×6000 мм')
    fake.plans.append(edit(old,[operation]))
    changed=agent.handle(1,'Позицию 3 размер 22×1500×6000 мм',event_id='resize')
    new=repo.load(1)
    assert new.items[2].characteristics[0].value=='22×1500×6000 мм'
    assert all(i==j for i,j in zip(old.items,new.items) if i.id!='i3')
    assert new.originals==old.originals and new.records==old.records and new.groups==old.groups
    assert_order(changed,new,['i4','i5','i1','i6','i7','i2','i3'])
    assert fake.plans==[] and fake.calls[-1][0]['display_numbers']['3']=='i3'
    repo=Repository(Database(tmp_path/'sorting.sqlite3'));fake=Fake(edit(new,[dict(kind='undo')]))
    agent=SteelAgent(fake,repo);undone=agent.handle(1,'Отмени',event_id='undo')
    restored=repo.load(1)
    assert restored.items==old.items and restored.revision==3 and restored.originals==old.originals
    assert_order(undone,restored,APPROVED_ORDER)
    fake.plans.append(dict(intent='show_request'))
    shown=agent.handle(1,'Покажи',event_id='reshow');assert_order(shown,restored,APPROVED_ORDER)
    fake.plans.append(edit(restored,[dict(kind='set_quantity',selection=choose,field='quantity',value='10',evidence='10',unit='т',unit_evidence='т')]))
    last=agent.handle(1,'Позицию 3 сделай 10 т',event_id='quantity')
    final=repo.load(1)
    assert final.items[2].quantity==Decimal(10)
    assert all(i==j for i,j in zip(restored.items,final.items) if i.id!='i3')
    assert_order(last,final,APPROVED_ORDER)
    assert repo.version(1,old.id,1)==old and repo.version(1,old.id,2)==new
    assert len(repo.history(1,old.id))==10
    # Duplicate event returns the exact stored payload/map, not a recomputed order or GPT call.
    cached=agent.handle(1,'Позицию 3 сделай 10 т',event_id='quantity')
    assert cached.model_dump_json()==last.model_dump_json() and len(fake.calls)==3


def test_grade_edit_regroups_using_current_values_not_cached_grade_values(tmp_path):
    agent,repo,fake,_=start(tmp_path);old=repo.load(1)
    select=dict(mode='where',predicates=[dict(field='label',value='Лист',match='token')],cardinality='all',evidence='Лист')
    operation=set_op(name='grade',value='С255',evidence='С255');operation.pop('target_id');operation['selection']=select
    fake.plans.append(edit(old,[operation]))
    reply=agent.handle(1,'Лист сделай С255',event_id='grades')
    new=repo.load(1)
    assert_order(reply,new,['i3','i1','i4','i6','i5','i7','i2'])
    assert all(next(c.value for c in i.characteristics if c.name=='grade')=='С255' for i in new.items)
    assert new.originals==old.originals and len(new.changes)==7 and new.revision==2


def test_third_position_after_resort_targets_the_new_third_row_then_undo(tmp_path):
    agent,repo,fake,_=start(tmp_path);old=repo.load(1)
    choice=dict(mode='display',positions=[3],cardinality='one',evidence='Позицию 3')
    fake.plans.append(edit(old,[dict(kind='set',selection=choice,field='characteristic',name='size',
                                  value='22×1500×6000 мм',evidence='22×1500×6000 мм')]))
    first=agent.handle(1,'Позицию 3 размер 22×1500×6000 мм',event_id='resize');resized=repo.load(1)
    assert first.display.item_ids[2]=='i1'
    fake.plans.append(edit(resized,[dict(kind='set_quantity',selection=choice,field='quantity',
                                      value='11',evidence='11',unit='т',unit_evidence='т')]))
    reply=agent.handle(1,'Позицию 3 сделай 11 т',event_id='new-third');new=repo.load(1)
    assert fake.calls[-1][0]['display_numbers']['3']=='i1'
    assert new.items[0].quantity==Decimal(11) and new.items[2].quantity==Decimal(40)
    assert all(i==j for i,j in zip(resized.items,new.items) if i.id!='i1')
    fake.plans.append(edit(new,[dict(kind='undo')]))
    back=agent.handle(1,'Отмени',event_id='undo');assert repo.load(1).items==resized.items
    assert back.display.item_ids==reply.display.item_ids


def test_forward_and_questions_use_same_sorted_position_mapping(tmp_path):
    agent,repo,fake,_=start(tmp_path);old=repo.load(1)
    fake.plans.append(dict(intent='show_request',view='forward'))
    forwarded=agent.handle(1,'Для пересылки',event_id='forward')
    assert_order(forwarded,old,APPROVED_ORDER)
    questions=old.model_copy(update={'questions':['Уточните i3 и i4']})
    text=render(questions,layout=forwarded.display.layout)
    assert 'Уточните позиция 3 и позиция 1' in text


def test_preupgrade_request_gets_layout_on_show_without_snapshot_or_version_change(tmp_path):
    p=approved_plan();layout=p.pop('display_layout');agent,repo,fake,first=start(tmp_path,plan=p)
    old=repo.load(1);assert first.display.layout is None
    fake.plans.append(dict(intent='show_request',display_layout=layout))
    shown=agent.handle(1,'Покажи',event_id='show-sorted')
    assert_order(shown,old,APPROVED_ORDER)
    assert repo.load(1)==old and fake.calls[-1][0]['last_display_layout'] is None
    repo=Repository(Database(tmp_path/'sorting.sqlite3'))
    assert repo.last_display(1,old).layout is not None and repo.last_display(2,old) is None


def test_two_users_with_same_item_ids_keep_separate_layouts_and_numbered_edits(tmp_path):
    agent,repo,fake,_=start(tmp_path);one=repo.load(1)
    second=approved_plan();second['display_layout']['grade_order']=[]
    fake.plans.append(second);shown=agent.handle(2,APPROVED,event_id='create')
    two=repo.load(2)
    assert shown.display.item_ids==['i3','i1','i6','i7','i2','i4','i5']
    assert repo.last_display(1,one).item_ids==APPROVED_ORDER
    choice=dict(mode='display',positions=[3],cardinality='one',evidence='Позицию 3')
    fake.plans.append(edit(two,[dict(kind='set_quantity',selection=choice,field='quantity',value='11',evidence='11',unit='т',unit_evidence='т')]))
    reply=agent.handle(2,'Позицию 3 сделай 11 т',event_id='second-edit')
    assert fake.calls[-1][0]['display_numbers']['3']=='i6'
    assert repo.load(2).items[5].quantity==Decimal(11) and repo.load(1)==one
    assert reply.display.item_ids==shown.display.item_ids


def test_failed_group_edit_does_not_apply_new_layout_or_partial_changes(tmp_path):
    agent,repo,fake,first=start(tmp_path);old=repo.load(1)
    bad=edit(old,[set_op(item='i4',name='grade',value='С255',evidence='С255'),
                  set_op(item='not-an-item',name='grade',value='С255',evidence='С255')])
    bad['display_layout']=dict(items=[hint(i.id,kind=None) for i in old.items])
    fake.plans.extend([bad,deepcopy(bad)])
    result=agent.handle(1,'С255',event_id='invalid-group')
    assert result.status=='failed' and result.display is None and repo.load(1)==old
    assert repo.last_display(1,old)==first.display
    assert repo.version(1,old.id,2) is None


@pytest.mark.parametrize('failure',['missing_header','wrong_record_kind','wrong_unit','wrong_text','no_unit','future_header'])
def test_header_unit_evidence_does_not_weaken_regular_or_source_validation(failure):
    p=approved_plan();item=p['extraction']['items'][0]
    if failure=='missing_header':item['unit_source_id']='missing'
    if failure=='wrong_record_kind':item['unit_source_id']='s2'
    if failure=='wrong_unit':item['unit']='кг'
    if failure=='wrong_text':item['unit_text']='килограммах'
    if failure=='no_unit':item['unit']=None
    if failure=='future_header':
        last=p['extraction']['records'][-1];last['kind']='header';item['unit_source_id']=last['id']
        p['extraction']['items']=p['extraction']['items'][:-1]
        p['extraction']['groups'][0]['item_ids']=p['extraction']['groups'][0]['item_ids'][:-1]
    with pytest.raises(ValueError):RequestEngine().create(AgentPlan.model_validate(p).extraction,APPROVED,'rejected')
    # Removing the pointer cannot use a unit from the header as a fallback.
    p=approved_plan();p['extraction']['items'][0].pop('unit_source_id')
    with pytest.raises(ValueError,match='unit_evidence'):RequestEngine().create(AgentPlan.model_validate(p).extraction,APPROVED,'rejected')


def test_partial_recovery_retains_header_provenance():
    p=approved_plan();p['extraction']['items'][0]['characteristics'].append(deepcopy(p['extraction']['items'][0]['characteristics'][0]))
    recovered=partial_from_proposal(p,APPROVED,'partial')
    assert len(recovered.items)==6 and any(r.id=='header' for r in recovered.records)
    assert all(i.unit=='т' and i.unit_source_id=='header' for i in recovered.items)
    RequestEngine().validate(recovered)


def test_real_sdk_schema_and_agent_use_one_coordinator_request(tmp_path):
    p=AgentPlan.model_validate(approved_plan()).model_dump(mode='json')
    repo=Repository(Database(tmp_path/'http.sqlite3'))
    with sdk_transport(p) as (transport,sent):
        reply=SteelAgent(transport,repo).handle(1,APPROVED,event_id='http')
    assert_order(reply,repo.load(1),APPROVED_ORDER)
    assert len(sent)==1 and subset_issues(sent[0]['text']['format']['schema'])==[]
    assert sent[0]['model']=='gpt-6-luna' and sent[0]['reasoning']=={'effort':'high'}


def test_durable_sorted_receipt_outbox_replay_and_unknown_delivery(rig):
    fake=Fake(approved_plan());admit(rig,100,text=APPROVED)
    executor=Executor(rig.queue,rig.access,lambda:rig.api,lambda:fake)
    executor.execute(rig.queue.claim());repo=Repository(rig.queue.database);old=repo.load(1)
    receipt=repo.last_display(1,old);assert receipt.item_ids==APPROVED_ORDER
    sent=[p['text'] for method,p in rig.api.calls if method=='sendMessage']
    assert len(sent)==1 and sent[0]==render(old,layout=receipt.layout)
    assert admit(rig,100,text=APPROVED)==rig.queue.cursor()
    assert rig.queue.claim() is None and len(fake.calls)==1
    choice=dict(mode='display',positions=[3],cardinality='one',evidence='Позицию 3')
    fake.plans.append(edit(old,[dict(kind='set_quantity',selection=choice,field='quantity',value='10',evidence='10',unit='т',unit_evidence='т')]))
    admit(rig,101,text='Позицию 3 сделай 10 т');rig.api.fail=True
    executor.execute(rig.queue.claim());new=repo.load(1)
    assert new.items[2].quantity==Decimal(10) and new.revision==2
    assert rig.queue.job(101)['state']=='delivery_unknown' and repo.last_display(1,new) is None
    assert repo.last_display(2,new) is None and repo.load(2) is None
    assert rig.queue.claim() is None and len(fake.calls)==2


def test_long_sorted_request_chunks_preserve_every_row_and_id():
    rows=[('Лист',[('size',str(n)+' мм',str(n)+' мм'),('grade','С245','С245')],str(n)+'.125','т') for n in range(160,0,-1)]
    _,_,state=sample(rows);layout=DisplayLayout(items=[hint(i.id) for i in state.items])
    before=state.model_dump_json();text=render(state,layout=layout);parts=safe_chunks(text,limit=1000)
    assert display_order(state,layout)==[f'i{n}' for n in range(160,0,-1)]
    assert len(parts)>1 and all(len(p)<=1000 for p in parts)
    assert ''.join(parts).replace('\n','')==text.replace('\n','')
    assert sum(p.count(' — ') for p in parts)==160 and state.model_dump_json()==before


def test_old_receipt_without_layout_stays_byte_compatible():
    old=Reply(text='old',status='ok',intent='show_request',display=dict(request_id='r',revision=1,item_ids=['i']))
    assert 'layout' not in json.loads(old.model_dump_json())['display']
    assert Reply.model_validate_json(old.model_dump_json())==old
