"""BR-010/011: scripted intent plans, real agent/SQLite, no live GPT claims."""
from copy import deepcopy
from decimal import Decimal, localcontext
import json
import re

import pytest
from database import Database
from steel_agent_v2 import Repository, SteelAgent
from steel_agent_v2.contracts import AgentPlan, Reply, Selection
from steel_agent_v2.engine import exact_add
from steel_agent_v2.presentation import display_order, render
from test_steel_agent_v2 import Fake, edit, set_op
from test_steel_agent_v2_027_presentation import sample
from test_steel_agent_v2_wire_schema import sdk_transport, subset_issues
from test_steel_agent_v2_queue import rig, admit, Executor

ROWS=[
 ('Лист',[('thickness','10 мм','10 мм'),('width','8 мм','8 мм'),('grade','С245','С245')],'1.125','т'),
 ('Швеллер',[('size','8П','8П'),('grade','С245','С245')],'17.25','кг'),
 ('Лист',[('thickness','8 мм','8 мм'),('grade','09Г2С','09Г2С')],'0.375','т'),
 ('Лист',[('thickness','3 мм','3 мм'),('grade','С245','С245')],'2.250','т'),
 ('Арматура',[('diameter','12 мм','12 мм'),('grade','А500С','А500С')],'2.125','т'),
 ('Труба',[('type','профильная','профильная'),('length','3000 мм','3000 мм'),('grade','Ст20','Ст20')],'0.625','т'),
 ('Труба',[('type','электросварная круглая','электросварная круглая'),('length','9000 мм','9000 мм'),('grade','Ст20','Ст20')],'120','м')]
GROUPS=[['i3','i1'],['i6','i7'],['i4'],['i2'],['i5']]


def seeded(tmp_path):
    source,plan,_=sample(ROWS,groups=GROUPS)
    repo=Repository(Database(tmp_path/'semantic.sqlite3'))
    fake=Fake(plan);agent=SteelAgent(fake,repo)
    reply=agent.handle(1,source,event_id='create')
    assert reply.status=='ok'
    return agent,repo,fake,source


def pred(field,value,name=None,match='equals'):
    return dict(field=field,value=value,name=name,match=match)


def sheets(extra=(),one=False,evidence='Лист'):
    return dict(mode='where',predicates=[pred('label','Лист',match='token'),*extra],
                cardinality='one' if one else 'all',evidence=evidence)


def measured(kind,selection,value='7',unit='т',name=None):
    return dict(kind=kind,selection=selection,field='characteristic' if kind=='set_length' else 'quantity',
                name=name,value=value,evidence=value,unit=unit,unit_evidence=unit)


def grade(selection):
    op=set_op(name='grade',value='Ст3',evidence='Ст3');op.pop('target_id')
    return dict(op,selection=selection)


def field(item,name):
    return next(c.value for c in item.characteristics if c.name==name)


def assert_untouched(old,new,changed_ids):
    assert old.original_text==new.original_text and old.originals==new.originals
    assert old.records==new.records and old.groups==new.groups
    assert [i.id for i in old.items]==[i.id for i in new.items]
    assert all(i==j for i,j in zip(old.items,new.items) if i.id not in changed_ids)


def test_group_grade_applies_to_every_sheet_without_word_all_and_undo(tmp_path):
    agent,repo,fake,_=seeded(tmp_path);old=repo.load(1)
    fake.plans.append(edit(old,[grade(sheets(evidence='Лист'))]))
    result=agent.handle(1,'Лист сделай Ст3',event_id='grade')
    new=repo.load(1)
    assert result.calls==1 and result.status=='ok'
    assert all(field(i,'grade')=='Ст3' for i in new.items if i.label=='Лист')
    assert_untouched(old,new,{'i1','i3','i4'})
    assert [i.quantity for i in old.items]==[i.quantity for i in new.items]
    assert new.revision==2 and len(new.changes)==3
    fake.plans.append(edit(new,[dict(kind='undo')]))
    agent.handle(1,'Отмени',event_id='undo')
    assert repo.load(1).items==old.items
    assert repo.version(1,old.id,2).items==new.items
    assert len(repo.history(1,old.id))==6


def test_sheet_eight_is_thickness_not_eighth_position_width_or_channel(tmp_path):
    agent,repo,fake,_=seeded(tmp_path);old=repo.load(1)
    choose=sheets([pred('characteristic','8 мм','thickness')],evidence='Лист 8')
    fake.plans.append(edit(old,[grade(choose)]))
    agent.handle(1,'Лист 8 сделай Ст3',event_id='eight')
    new=repo.load(1)
    assert field(new.items[2],'grade')=='Ст3'
    assert_untouched(old,new,{'i3'})


def test_all_matching_eight_mm_sheets_keep_other_technical_differences(tmp_path):
    rows=deepcopy(ROWS)
    rows[3]=('Лист',[('thickness','8 мм','8 мм'),('grade','С245','С245'),
                    ('standard','ТУ УЧЕБНЫЕ-2','ТУ УЧЕБНЫЕ-2')],'2.250','т')
    source,p,_=sample(rows,groups=GROUPS)
    repo=Repository(Database(tmp_path/'two-eight.sqlite3'));fake=Fake(p);agent=SteelAgent(fake,repo)
    agent.handle(1,source,event_id='create');state=repo.load(1)
    choose=sheets([pred('characteristic','8 мм','thickness')],evidence='Лист 8')
    fake.plans.append(edit(state,[grade(choose)]))
    agent.handle(1,'Лист 8 сделай Ст3',event_id='edit')
    new=repo.load(1)
    assert field(new.items[2],'grade')==field(new.items[3],'grade')=='Ст3'
    assert field(new.items[3],'standard')=='ТУ УЧЕБНЫЕ-2'
    assert new.items[2].quantity==state.items[2].quantity and new.items[3].quantity==state.items[3].quantity
    assert len(new.items)==7 and len(new.changes)==2


@pytest.mark.parametrize('separator',['×','х','*'])
def test_compound_size_component_cannot_confuse_width_with_thickness(tmp_path,separator):
    rows=[('Лист',[('size',separator.join(['10','8','6000 мм']),separator.join(['10','8','6000 мм'])),
                    ('grade','С245','С245')],'1','т'),
          ('Лист',[('size',separator.join(['8','1500','6000 мм']),separator.join(['8','1500','6000 мм'])),
                    ('grade','С245','С245')],'2','т'),
          ('Швеллер',[('size','8П','8П'),('grade','С245','С245')],'3','т')]
    source,p,_=sample(rows)
    repo=Repository(Database(tmp_path/'compound.sqlite3'));fake=Fake(p);agent=SteelAgent(fake,repo)
    agent.handle(1,source,event_id='create');state=repo.load(1)
    part=pred('characteristic','8','size',match='component')
    part.update(separator=separator,component=1)
    fake.plans.append(edit(state,[grade(sheets([part],evidence='Лист 8'))]))
    reply=agent.handle(1,'Лист 8 сделай Ст3',event_id='eight')
    new=repo.load(1)
    assert reply.status=='ok' and field(new.items[1],'grade')=='Ст3'
    assert new.items[0]==state.items[0] and new.items[2]==state.items[2]


def test_final_quantity_is_per_selected_item_not_request_total(tmp_path):
    agent,repo,fake,_=seeded(tmp_path);old=repo.load(1)
    fake.plans.append(edit(old,[measured('set_quantity',sheets(),value='50')]))
    result=agent.handle(1,'Лист в итоге нужен 50 т',event_id='fifty')
    new=repo.load(1)
    assert all(i.quantity==Decimal(50) and i.unit=='т' for i in new.items if i.label=='Лист')
    assert_untouched(old,new,{'i1','i3','i4'})
    assert '150 т' not in result.text and 'Количество:' not in result.text


def test_on_everything_is_full_atomic_selection(tmp_path):
    agent,repo,fake,_=seeded(tmp_path);old=repo.load(1)
    choose=dict(mode='all',cardinality='all',evidence='На всё')
    fake.plans.append(edit(old,[grade(choose)]))
    agent.handle(1,'На всё поставь Ст3',event_id='all')
    new=repo.load(1)
    assert all(field(i,'grade')=='Ст3' for i in new.items)
    assert len(new.items)==7 and len(new.changes)==7 and new.revision==2


def test_add_seven_increments_not_replaces_and_event_replay_does_not_add_twice(tmp_path):
    agent,repo,fake,_=seeded(tmp_path);old=repo.load(1)
    choose=dict(mode='where',predicates=[pred('label','Арматура',match='token')],
                cardinality='all',evidence='Арматуру')
    fake.plans.append(edit(old,[measured('add_quantity',choose)]))
    reply=agent.handle(1,'Арматуру добавь 7 т',event_id='add')
    new=repo.load(1)
    assert new.items[4].quantity==Decimal('9.125')
    assert_untouched(old,new,{'i5'})
    calls=len(fake.calls)
    assert agent.handle(1,'Арматуру добавь 7 т',event_id='add')==reply and len(fake.calls)==calls
    assert repo.load(1)==new


def test_profile_pipe_length_scales_in_python_and_round_pipe_stays_unchanged(tmp_path):
    agent,repo,fake,_=seeded(tmp_path);old=repo.load(1)
    choose=dict(mode='where',predicates=[pred('label','Труба',match='token'),
            pred('characteristic','профильная','type')],cardinality='all',evidence='Труба профильная')
    fake.plans.append(edit(old,[measured('set_length',choose,'6','м','length')]))
    reply=agent.handle(1,'Труба профильная длиной 6 м',event_id='length')
    new=repo.load(1)
    assert reply.calls==1 and field(new.items[5],'length')=='6000 мм'
    assert field(new.items[5],'grade')=='Ст20'
    assert_untouched(old,new,{'i6'})


def test_last_display_order_persists_after_show_edit_reopen_and_undo(tmp_path):
    agent,repo,fake,_=seeded(tmp_path);state=repo.load(1)
    expected=['i3','i1','i6','i7','i4','i2','i5']
    assert display_order(state)==expected
    assert repo.last_display(1,state).item_ids==expected
    assert not re.search(r'^\d+\. ',render(state),re.M)
    fake.plans.append(dict(intent='show_request'))
    agent.handle(1,'Покажи',event_id='show')
    assert fake.calls[-1][0]['display_numbers']['3']=='i6'
    repo=Repository(Database(tmp_path/'semantic.sqlite3'));agent=SteelAgent(fake,repo)
    choose=dict(mode='display',positions=[3],cardinality='one',evidence='Позицию 3')
    fake.plans.append(edit(state,[measured('set_quantity',choose,'10')]))
    agent.handle(1,'Позицию 3 сделай 10 т',event_id='three')
    new=repo.load(1)
    assert new.items[5].quantity==Decimal(10) and new.items[2]==state.items[2]
    assert_untouched(state,new,{'i6'})
    assert repo.last_display(1,new).item_ids==expected
    fake.plans.append(dict(intent='show_request',view='forward'))
    agent.handle(1,'Текстом',event_id='forward')
    assert fake.calls[-1][0]['display_numbers']['3']=='i6'
    fake.plans.append(edit(new,[dict(kind='undo')]))
    agent.handle(1,'Отмени',event_id='undo')
    assert repo.load(1).items==state.items
    assert repo.last_display(1,repo.load(1)).item_ids==expected


def test_missing_pre_upgrade_display_never_falls_back_to_source_order(tmp_path):
    agent,repo,fake,_=seeded(tmp_path);state=repo.load(1)
    # Simulate an old receipt before display metadata existed, only in temporary SQLite.
    with repo.database.connection() as c:
        c.execute("UPDATE steel_v2_turn SET reply=json_remove(reply,'$.display')")
    choose=dict(mode='display',positions=[3],cardinality='one',evidence='позиции 3')
    fake.plans.append(edit(state,[measured('set_quantity',choose,'10')]))
    reply=agent.handle(1,'В позиции 3 сделай 10 т',event_id='old')
    assert reply.intent=='clarify' and reply.calls==1 and repo.load(1)==state
    assert fake.calls[-1][0]['display_numbers']=={}
    fake.plans.append(dict(intent='show_request'))
    agent.handle(1,'Покажи',event_id='show')
    assert repo.last_display(1,state).item_ids[2]=='i6'


@pytest.mark.parametrize('kind',['set','set_quantity','add_quantity','set_length'])
def test_ambiguous_selection_keeps_value_and_operation_for_short_followup(tmp_path,kind):
    agent,repo,fake,_=seeded(tmp_path);state=repo.load(1)
    choose=sheets(one=True,evidence='одному листу')
    op=grade(choose) if kind=='set' else measured(kind,choose,'6' if kind=='set_length' else '7',
                                                  'м' if kind=='set_length' else 'т',
                                                  'length' if kind=='set_length' else None)
    command='Поставь одному листу Ст3' if kind=='set' else 'Поставь одному листу 6 м' if kind=='set_length' else 'Прибавь одному листу 7 т'
    fake.plans.append(edit(state,[op]))
    result=agent.handle(1,command,event_id='ambiguous')
    assert result.intent=='clarify' and result.calls==1 and repo.load(1)==state
    pending=repo.clarification(1,state)
    assert pending.operation.kind==kind and pending.proposal.target_ids==['i1','i3','i4']
    chosen=deepcopy(op);chosen['selection']=sheets([pred('characteristic','8 мм','thickness')],evidence='8')
    plan=edit(state,[chosen]);plan['clarification_event_id']='ambiguous';fake.plans.append(plan)
    reply=agent.handle(1,'Толщиной 8',event_id='answer')
    new=repo.load(1)
    assert reply.status=='ok' and reply.calls==1
    assert_untouched(state,new,{'i3'})
    if kind=='set':assert field(new.items[2],'grade')=='Ст3'
    if kind=='set_quantity':assert new.items[2].quantity==Decimal(7)
    if kind=='add_quantity':assert new.items[2].quantity==Decimal('7.375')
    if kind=='set_length':assert field(new.items[2],'length')=='6000 мм'


def test_intersected_conflicting_predicates_do_not_broaden_selection(tmp_path):
    agent,repo,fake,_=seeded(tmp_path);state=repo.load(1)
    choose=sheets([pred('characteristic','8 мм','thickness'),
                   pred('characteristic','С245','grade')],evidence='Лист 8 С245')
    fake.plans.append(edit(state,[grade(choose)]))
    result=agent.handle(1,'Лист 8 С245 сделай Ст3',event_id='none')
    assert result.intent=='clarify' and result.calls==1 and repo.load(1)==state


@pytest.mark.parametrize('damage',['overlap','unknown_unit','unsupported_length_field','new_number_without_evidence'])
def test_failed_group_batch_is_fully_atomic(tmp_path,damage):
    agent,repo,fake,_=seeded(tmp_path);state=repo.load(1)
    ops=[grade(sheets())]
    if damage=='overlap':ops += [grade(sheets())]
    elif damage=='unknown_unit':ops += [measured('add_quantity',dict(mode='all',cardinality='all',evidence='всем'))]
    elif damage=='unsupported_length_field':
        ops += [measured('set_length',sheets(),'6','м','grade')]
    else:
        bad=measured('set_quantity',sheets(),'50');bad['evidence']='7';ops += [bad]
    plan=edit(state,ops);fake.plans += [plan,deepcopy(plan)]
    result=agent.handle(1,'Лист сделай Ст3; всем 7 т и 6 м',event_id='bad')
    assert result.status=='failed' and result.calls==2 and repo.load(1)==state
    assert repo.version(1,state.id,2) is None


def test_quantity_addition_is_exact_even_for_different_mass_units_and_small_context(tmp_path):
    agent,repo,fake,_=seeded(tmp_path);state=repo.load(1)
    choose=dict(mode='where',predicates=[pred('label','Швеллер',match='token')],cardinality='all',evidence='Швеллеру')
    fake.plans.append(edit(state,[measured('add_quantity',choose,'7')]))
    agent.handle(1,'Швеллеру добавь 7 т',event_id='kg')
    assert repo.load(1).items[1].quantity==Decimal('7017.25')
    assert repo.load(1).items[1].unit=='кг'
    with localcontext() as ctx:
        ctx.prec=3
        assert exact_add(Decimal('12345678901234567890.1'),Decimal('0.000009'))==Decimal('12345678901234567890.100009')


@pytest.mark.parametrize('message,expected',[('Листу 50',False),('Листу 50т',True)])
def test_unit_is_not_inferred_from_letter_inside_product_word(tmp_path,message,expected):
    agent,repo,fake,_=seeded(tmp_path);state=repo.load(1)
    choose=sheets(evidence='Листу')
    p=edit(state,[measured('set_quantity',choose,'50')]);fake.plans += [p,deepcopy(p)]
    reply=agent.handle(1,message,event_id='unit')
    if expected:
        assert reply.status=='ok' and all(i.quantity==50 for i in repo.load(1).items if i.label=='Лист')
    else:assert reply.status=='failed' and repo.load(1)==state


def test_pending_confirmation_cannot_lend_its_unit_to_an_unrelated_new_edit(tmp_path):
    agent,repo,fake,_=seeded(tmp_path);state=repo.load(1)
    op=measured('set_quantity',sheets(one=True,evidence='одному листу'),'7')
    fake.plans.append(edit(state,[op]))
    agent.handle(1,'Поставь одному листу 7 т',event_id='amb')
    confirmed=deepcopy(op);confirmed['selection']=sheets([pred('characteristic','8 мм','thickness')],evidence='Лист 8')
    other=dict(mode='where',predicates=[pred('label','Швеллер',match='token')],
               cardinality='one',evidence='швеллеру')
    plan=edit(state,[confirmed,measured('set_quantity',other,'6')]);plan['clarification_event_id']='amb'
    fake.plans += [plan,deepcopy(plan)]
    reply=agent.handle(1,'Лист 8, швеллеру 6',event_id='confirm')
    assert reply.status=='failed' and reply.diagnostics==['clarification_scope_mismatch']
    assert repo.load(1)==state


def test_repair_cannot_switch_semantic_group_target_or_field(tmp_path):
    agent,repo,fake,_=seeded(tmp_path);state=repo.load(1)
    bad=measured('set_quantity',sheets(),'50');bad['evidence']='7'
    other=dict(mode='where',predicates=[pred('label','Швеллер',match='token')],
               cardinality='all',evidence='швеллер')
    fake.plans += [edit(state,[bad]),edit(state,[measured('set_quantity',other,'50')])]
    reply=agent.handle(1,'Лист 7 т, швеллер 50 т',event_id='repair')
    assert reply.status=='failed' and reply.diagnostics==['semantic_edit_scope_changed']
    assert repo.load(1)==state


def test_numbered_mass_conversion_repair_keeps_target_and_existing_guarantee(tmp_path):
    agent,repo,fake,_=seeded(tmp_path);state=repo.load(1)
    choose=dict(mode='display',positions=[3],cardinality='one',evidence='Позицию 3')
    bad=dict(kind='set',selection=choose,field='quantity',value='0.000625',evidence='0.000625')
    repaired=dict(kind='convert_mass',selection=choose,value='кг',evidence='килограммы')
    fake.plans += [edit(state,[bad]),edit(state,[repaired])]
    reply=agent.handle(1,'Позицию 3 пересчитай в килограммы',event_id='convert')
    new=repo.load(1)
    assert reply.status=='ok' and reply.calls==2
    assert new.items[5].quantity==Decimal('625') and new.items[5].unit=='кг'
    assert_untouched(state,new,{'i6'})


def test_real_sdk_serializes_selection_and_measures_with_strict_profile(tmp_path):
    agent,repo,fake,_=seeded(tmp_path);state=repo.load(1)
    plan=edit(state,[measured('set_quantity',sheets(),'50')])
    with sdk_transport(AgentPlan.model_validate(plan).model_dump(mode='json')) as (transport,sent):
        result=SteelAgent(transport,repo).handle(1,'Лист в итоге нужен 50 т',event_id='http')
    assert result.status=='ok' and len(sent)==1
    assert subset_issues(sent[0]['text']['format']['schema'])==[]
    assert sent[0]['model']=='gpt-6-luna' and sent[0]['reasoning']=={'effort':'high'}


def test_previous_pending_clarification_receipt_stays_byte_compatible():
    old=dict(text='question',status='ok',intent='clarify',request_id='request',revision=1,
             sources=[],diagnostics=[],calls=1,clarification=dict(
                 event_id='q',request_id='request',revision=1,proposal=dict(
                     target_ids=['i1'],field='characteristic',name='grade',value='C245',
                     evidence='C245',alternatives=['existing','new']),
                 source_message='C245',question='existing?'))
    raw=json.dumps(old,ensure_ascii=False,separators=(',',':'))
    assert Reply.model_validate_json(raw).model_dump_json()==raw


def test_same_item_ids_in_two_users_never_share_selection_or_request_revision(tmp_path):
    agent,repo,fake,source=seeded(tmp_path);one=repo.load(1)
    _,p,_=sample(ROWS,groups=GROUPS);fake.plans.append(p)
    agent.handle(2,source,event_id='create')
    two=repo.load(2)
    fake.plans.append(edit(two,[grade(sheets())]))
    agent.handle(2,'Лист сделай Ст3',event_id='edit')
    assert repo.load(1)==one and repo.load(2).revision==2
    wrong=edit(one,[grade(sheets())]);fake.plans += [wrong,deepcopy(wrong)]
    agent.handle(2,'Лист сделай Ст3',event_id='wrong-request')
    assert repo.load(2).revision==2 and repo.load(1)==one


def test_each_product_is_one_logical_line_even_with_multiline_characteristics():
    _,_,state=sample([('Лист\nг/к',[('grade','С245\nГОСТ 27772','С245\nГОСТ 27772')],'1','т'),
                      ('Швеллер',[],'2','т')])
    text=render(state)
    assert len(text.splitlines())==2 and len(display_order(state))==2
    assert 'Лист г/к' in text and 'С245 ГОСТ 27772' in text


@pytest.mark.parametrize('bad',[
 dict(mode='display',positions=[True],cardinality='one',evidence='3'),
 dict(mode='display',positions=[0],cardinality='one',evidence='0'),
 dict(mode='display',positions=[1,1],cardinality='all',evidence='1'),
 dict(mode='where',cardinality='all',evidence='Лист'),
 dict(mode='where',predicates=[dict(field='characteristic',name='size',value='8',match='token')],
      cardinality='all',evidence='Лист 8'),
])
def test_invalid_selection_contract_is_rejected(bad):
    with pytest.raises(ValueError):Selection.model_validate(bad)


def test_queue_keeps_display_receipt_atomic_and_isolates_users(rig):
    source,plan,_=sample(ROWS,groups=GROUPS);fake=Fake(plan)
    admit(rig,900,text=source)
    Executor(rig.queue,rig.access,lambda:rig.api,lambda:fake).execute(rig.queue.claim())
    repo=Repository(rig.queue.database);state=repo.load(1)
    assert repo.last_display(1,state).item_ids[2]=='i6' and repo.last_display(2,state) is None
    choose=dict(mode='display',positions=[3],cardinality='one',evidence='Позицию 3')
    fake.plans.append(edit(state,[measured('set_quantity',choose,'10')]))
    admit(rig,901,text='Позицию 3 сделай 10 т')
    Executor(rig.queue,rig.access,lambda:rig.api,lambda:fake).execute(rig.queue.claim())
    new=repo.load(1)
    assert new.items[5].quantity==Decimal(10) and new.items[2]==state.items[2]
    assert rig.queue.job(901)['state']=='delivered' and repo.load(2) is None
    assert repo.last_display(1,new).revision==2
    assert repo.version(1,state.id,2).items==new.items


def test_unknown_delivery_is_not_the_last_shown_normalized_request(rig):
    source,plan,_=sample(ROWS,groups=GROUPS);fake=Fake(plan)
    admit(rig,910,text=source)
    Executor(rig.queue,rig.access,lambda:rig.api,lambda:fake).execute(rig.queue.claim())
    repo=Repository(rig.queue.database);state=repo.load(1)
    choose=dict(mode='display',positions=[3],cardinality='one',evidence='Позицию 3')
    fake.plans.append(edit(state,[measured('set_quantity',choose,'10')]))
    rig.api.fail=True
    admit(rig,911,text='Позицию 3 сделай 10 т')
    Executor(rig.queue,rig.access,lambda:rig.api,lambda:fake).execute(rig.queue.claim())
    new=repo.load(1)
    assert rig.queue.job(911)['state']=='delivery_unknown'
    assert new.revision==2 and repo.last_display(1,new) is None
