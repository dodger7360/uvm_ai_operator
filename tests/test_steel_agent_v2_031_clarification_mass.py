"""Synthetic scripted plans through the real agent/repository, not live GPT quality."""
from copy import deepcopy
from decimal import Decimal, localcontext
import json

import pytest
from database import Database
from steel_agent_v2 import SteelAgent, Repository
from steel_agent_v2.contracts import AgentPlan, Reply
from steel_agent_v2.engine import EngineError, convert_mass
from steel_agent_v2.transport import INSTRUCTIONS
from test_steel_agent_v2 import Fake, edit, set_op
from test_steel_agent_v2_027_presentation import sample
from test_steel_agent_v2_wire_schema import sdk_transport, subset_issues

ROWS=[
 ('Уголок',[('size','40×40×4','40×40×4')],'0.125','т'),
 ('Труба',[('size','89×4','89×4')],'112','м'),
 ('Лист г/к',[('size','8×1500×6000','8×1500×6000'),('grade','С245','С245')],'1375','кг'),
 ('Швеллер',[('size','10П','10П')],'2.375','т'),
 ('Полоса',[('size','40×4','40×4')],'87','кг')]


def setup(tmp_path):
    source,plan,_=sample(ROWS)
    repo=Repository(Database(tmp_path/'v2.sqlite3'));fake=Fake(plan)
    agent=SteelAgent(fake,repo);agent.handle(1,source,event_id='create')
    return agent,repo,fake,source


def proposal(targets=None):
    return dict(target_ids=targets or ['i3'],field='characteristic',name='cut',
                value='2х6',evidence='2х6',alternatives=['существующая позиция 3','новый товар'])


def conversion(state, target='т', evidence='тонны', item='i3'):
    return edit(state,[dict(kind='convert_mass',target_id=item,value=target,evidence=evidence)])


def test_clarification_short_replies_reopen_then_apply_saved_value_and_undo(tmp_path):
    agent,repo,fake,source=setup(tmp_path);original=repo.load(1)
    fake.plans.append(dict(intent='clarify',answer='Это уточнение позиции 3 или новый товар?',
                           clarification=proposal()))
    assert agent.handle(1,'Лист раскрой 2х6',event_id='cut').intent=='clarify'
    # This scripted coordinator leaves the first short answer ambiguous.
    fake.plans.append(dict(intent='clarify',answer='Применить к позиции 3?'))
    agent.handle(1,'Уточнение',event_id='short1')
    context=fake.calls[-1][0]['pending_clarification']
    assert context['proposal']['value']=='2х6' and context['event_id']=='cut'
    # Reopen the actual SQLite, without copying chat history.
    repo=Repository(Database(tmp_path/'v2.sqlite3'));agent=SteelAgent(fake,repo)
    fake.plans.append(dict(intent='clarify',answer='Раскрой 2х6 указан в метрах?',
                           clarification=proposal(['i3'])))
    agent.handle(1,'Да',event_id='short2')
    pending=repo.clarification(1,repo.load(1))
    assert pending.source_message=='Лист раскрой 2х6' and pending.question=='Раскрой 2х6 указан в метрах?'
    assert repo.load(1)==original  # No unconfirmed unit or value is applied.
    change=edit(original,[set_op('i3',name='cut',value='2х6',evidence='2х6'),
                          set_op('i3',name='cut_unit',value='м',evidence='м')])
    change['clarification_event_id']='cut';fake.plans.append(change)
    reply=agent.handle(1,'Да, относится к позиции 3, в м',event_id='confirm')
    state=repo.load(1)
    assert reply.status=='ok' and reply.calls==1
    assert state.revision==original.revision+1 and len(state.items)==5
    assert state.originals==original.originals and state.original_text==source
    assert [i.quantity for i in state.items]==[i.quantity for i in original.items]
    assert state.groups==original.groups
    assert state.items[2].characteristics[-2].value=='2х6'
    assert state.items[2].characteristics[-1].value=='м'
    assert state.changes[-2].evidence=='2х6' and state.changes[-2].message=='Да, относится к позиции 3, в м'
    assert repo.clarification(1,state) is None
    assert repo.clarification(2,state) is None
    calls=len(fake.calls)
    assert agent.handle(1,'Да, относится к позиции 3, в м',event_id='confirm')==reply
    assert len(fake.calls)==calls
    fake.plans.append(edit(state,[dict(kind='undo')]))
    agent.handle(1,'Отмени',event_id='undo')
    restored=repo.load(1)
    assert restored.items==original.items and restored.originals==original.originals
    assert len(repo.history(1,state.id))==12


@pytest.mark.parametrize('answer',['Уточнение','Да','Относится'])
def test_short_answer_can_finish_without_reasking_value(tmp_path,answer):
    agent,repo,fake,_=setup(tmp_path);state=repo.load(1)
    fake.plans.append(dict(intent='clarify',answer='Это раскрой позиции 3?',clarification=proposal()))
    agent.handle(1,'Раскрой 2х6',event_id='q')
    plan=edit(state,[set_op('i3',name='cut',value='2х6',evidence='2х6')])
    plan['clarification_event_id']='q';fake.plans.append(plan)
    reply=agent.handle(1,answer,event_id='a')
    assert reply.status=='ok' and reply.calls==1
    assert fake.calls[-1][0]['pending_clarification']['proposal']['value']=='2х6'
    assert repo.load(1).items[2].characteristics[-1].value=='2х6'


@pytest.mark.parametrize('damage',['target','value','reference','name'])
def test_pending_evidence_does_not_authorize_unrelated_changes(tmp_path,damage):
    agent,repo,fake,_=setup(tmp_path);state=repo.load(1)
    fake.plans.append(dict(intent='clarify',answer='Позиция 3?',clarification=proposal()))
    agent.handle(1,'Раскрой 2х6',event_id='q')
    plan=edit(state,[set_op('i3',name='cut',value='2х6',evidence='2х6')])
    plan['clarification_event_id']='q'
    if damage=='target':plan['operations'][0]['target_id']='i2'
    if damage=='value':plan['operations'][0]['value']='9х9'
    if damage=='reference':plan['clarification_event_id']='other'
    if damage=='name':plan['operations'][0]['name']='grade'
    fake.plans += [deepcopy(plan),plan]
    reply=agent.handle(1,'Да',event_id='confirm')
    assert reply.status=='failed' and reply.calls==2 and repo.load(1)==state
    assert repo.clarification(1,state).proposal.value=='2х6'


def test_topic_change_and_request_revision_expire_clarification(tmp_path):
    agent,repo,fake,_=setup(tmp_path);state=repo.load(1)
    fake.plans.append(dict(intent='clarify',answer='Позиция 3?',clarification=proposal()))
    agent.handle(1,'Раскрой 2х6',event_id='q')
    assert repo.clarification(1,state.model_copy(update={'revision':state.revision+1})) is None
    fake.plans.append(dict(intent='free_chat',answer='Хорошо'))
    agent.handle(1,'Сменим тему',event_id='other')
    assert repo.clarification(1,state) is None


def test_old_current_message_only_evidence_failure_repairs_with_saved_reference(tmp_path):
    agent,repo,fake,_=setup(tmp_path);state=repo.load(1)
    fake.plans.append(dict(intent='clarify',answer='Это позиция 3?',clarification=proposal()))
    agent.handle(1,'Раскрой 2х6',event_id='q')
    old_plan=edit(state,[set_op('i3',name='cut',value='2х6',evidence='2х6')])
    repaired=deepcopy(old_plan);repaired['clarification_event_id']='q'
    fake.plans += [old_plan,repaired]
    result=agent.handle(1,'Относится',event_id='answer')
    assert result.status=='ok' and result.calls==2
    assert fake.calls[-1][1]['diagnostics']==['edit_evidence_not_in_message']
    assert fake.calls[-1][0]['pending_clarification']['proposal']['value']=='2х6'
    assert repo.load(1).items[2].characteristics[-1].value=='2х6'


def test_actual_sdk_clarification_survives_turns_and_applies_original_evidence(tmp_path):
    agent,repo,fake,_=setup(tmp_path);state=repo.load(1)
    q=AgentPlan(intent='clarify',answer='Это уточнение позиции 3?',clarification=proposal())
    p=edit(state,[set_op('i3',name='cut',value='2х6',evidence='2х6')])
    p['clarification_event_id']='q'
    answers=iter([q.model_dump(mode='json'),AgentPlan.model_validate(p).model_dump(mode='json')])
    with sdk_transport(lambda _:next(answers)) as (transport,sent):
        agent=SteelAgent(transport,repo)
        agent.handle(1,'Раскрой 2х6',event_id='q')
        result=agent.handle(1,'Уточнение',event_id='a')
    assert result.status=='ok' and len(sent)==2
    assert repo.load(1).items[2].characteristics[-1].value=='2х6'


def test_unknown_api_outcome_keeps_pending_without_automatic_repeat(tmp_path):
    agent,repo,fake,_=setup(tmp_path);state=repo.load(1)
    fake.plans.append(dict(intent='clarify',answer='Позиция 3?',clarification=proposal()))
    agent.handle(1,'Раскрой 2х6',event_id='q')
    fake.plans.append(TimeoutError('synthetic'))
    result=agent.handle(1,'Да',event_id='timeout')
    assert result.status=='failed' and result.calls==1
    assert repo.load(1)==state and repo.clarification(1,state).event_id=='q'


def test_mass_conversion_is_one_atomic_version_no_model_number_and_undo(tmp_path):
    agent,repo,fake,source=setup(tmp_path);old=repo.load(1)
    fake.plans.append(conversion(old))
    reply=agent.handle(1,'Поз 3 пересчитай в тонны',event_id='mass')
    state=repo.load(1)
    assert reply.status=='ok' and reply.calls==1
    assert state.items[2].quantity==Decimal('1.375') and state.items[2].unit=='т'
    assert '1,375 т' in reply.text
    assert state.items[:2]==old.items[:2] and state.items[3:]==old.items[3:]
    assert state.original_text==source and state.originals==old.originals
    assert state.originals[2].quantity==Decimal('1375') and state.originals[2].unit=='кг'
    assert state.groups==old.groups and state.revision==old.revision+1
    assert [c.field for c in state.changes[-2:]]==['quantity','unit']
    assert repo.load(2) is None
    n=len(fake.calls)
    assert agent.handle(1,'Поз 3 пересчитай в тонны',event_id='mass')==reply and len(fake.calls)==n
    repo=Repository(Database(tmp_path/'v2.sqlite3'));agent=SteelAgent(fake,repo)
    fake.plans.append(edit(state,[dict(kind='undo')]))
    agent.handle(1,'Отмени пересчёт',event_id='undo')
    assert repo.load(1).items==old.items
    assert len(repo.history(1,state.id))==6


def test_old_two_set_plan_reproduces_evidence_failure_then_narrow_repair(tmp_path,caplog):
    agent,repo,fake,_=setup(tmp_path);old=repo.load(1)
    broken=edit(old,[set_op('i3','quantity','1.375',evidence='1.375'),
                     set_op('i3','unit','т',evidence='тонны')])
    fake.plans += [broken,conversion(old)]
    with caplog.at_level('INFO'):
        reply=agent.handle(1,'Поз 3 пересчитай в тонны',event_id='mass')
    assert reply.status=='ok' and reply.calls==2
    assert fake.calls[-1][1]['diagnostics']==['edit_evidence_not_in_message']
    assert fake.calls[-1][0]['validation_repair'][0]['msg']=='edit_evidence_not_in_message'
    assert 'edit_evidence_not_in_message' in caplog.text
    assert repo.load(1).items[2].quantity==Decimal('1.375')


def test_two_invalid_edits_preserve_all_five_items_and_no_third_api(tmp_path):
    agent,repo,fake,_=setup(tmp_path);old=repo.load(1)
    broken=edit(old,[set_op('i3','quantity','1.375',evidence='1.375')])
    fake.plans += [broken,deepcopy(broken)]
    result=agent.handle(1,'Пересчитай позицию 3 в тонны',event_id='bad')
    assert result.status=='failed' and result.calls==2 and repo.load(1)==old
    assert len(fake.calls)==3 and 'evidence' not in result.text


def test_mass_repair_cannot_switch_target_or_invent_unrelated_edit(tmp_path):
    agent,repo,fake,_=setup(tmp_path);state=repo.load(1)
    broken=edit(state,[set_op('i3','quantity','1.375',evidence='1.375')])
    fake.plans += [broken,conversion(state,item='i5')]
    result=agent.handle(1,'Поз 3 пересчитай в тонны',event_id='badrepair')
    assert result.status=='failed' and result.diagnostics==['mass_conversion_repair_scope_mismatch']
    assert repo.load(1)==state


@pytest.mark.parametrize('unit,amount,target,expected',[
 ('кг','1250','т','1.250'),('т','1.250','кг','1250'),
 ('г','123456789012345678901234567890.125','т','123456789012345678901234.567890125'),
 ('килограммов','0','тонны','0'),('т','0.000001','г','1'),
])
def test_standard_mass_is_exact_independent_of_decimal_context(unit,amount,target,expected):
    with localcontext() as ctx:
        ctx.prec=3
        value,_=convert_mass(Decimal(amount),unit,target)
    assert value==Decimal(expected)


@pytest.mark.parametrize('unit,quantity',[('м',Decimal(120)),('шт',Decimal(5)),(None,Decimal(1)),('кг',None)])
def test_no_length_or_piece_to_mass_or_unknown_source(unit,quantity):
    with pytest.raises(EngineError):convert_mass(quantity,unit,'т')


@pytest.mark.parametrize('damage',['evidence','mixed','bad_unit','unknown_quantity','stale'])
def test_mass_failure_rolls_back_whole_batch(tmp_path,damage):
    agent,repo,fake,_=setup(tmp_path);old=repo.load(1)
    plan=conversion(old)
    if damage=='evidence':plan['operations'][0]['evidence']='килограммы'
    if damage=='mixed':plan['operations'].append(set_op('i3','quantity','3',evidence='3'))
    if damage=='bad_unit':plan['operations'][0]['value']='м'
    if damage=='unknown_quantity':plan['operations'][0]['target_id']='i2'
    if damage=='stale':plan['expected_revision']=0
    fake.plans += [deepcopy(plan),plan]
    result=agent.handle(1,'Поз 3 пересчитай в тонны',event_id='fail')
    assert result.status=='failed' and repo.load(1)==old


def test_actual_sdk_payload_contains_nullable_clarification_and_mass_operation(tmp_path):
    agent,repo,fake,_=setup(tmp_path);state=repo.load(1)
    with sdk_transport(AgentPlan.model_validate(conversion(state)).model_dump(mode='json')) as (transport,sent):
        reply=SteelAgent(transport,repo).handle(1,'Поз 3 пересчитай в тонны',event_id='sdk')
    assert reply.status=='ok' and len(sent)==1
    schema=sent[0]['text']['format']['schema']
    assert subset_issues(schema)==[]
    assert 'convert_mass' in schema['$defs']['Operation']['properties']['kind']['enum']
    action=schema['$defs']['WireActionPlan']['properties']
    assert 'clarification' in action and {'type':'null'} in action['clarification']['anyOf']
    assert 'PendingClarification' not in schema['$defs']
    assert sent[0]['model']=='gpt-6-luna' and sent[0]['reasoning']=={'effort':'high'}
    assert 'pending_clarification' in INSTRUCTIONS and 'convert_mass' in INSTRUCTIONS


def test_old_reply_serialization_remains_byte_compatible():
    data=dict(text='ok',status='ok',intent='clarify',request_id=None,revision=None,
              sources=[],diagnostics=[],calls=1)
    assert json.loads(Reply(**data).model_dump_json())==data
