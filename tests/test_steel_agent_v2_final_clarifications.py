"""Final PR22: synthetic dimensions and supplied coordinator plans, never live GPT."""
from copy import deepcopy
from decimal import Decimal

import pytest

from database import Database
from steel_agent_v2 import SteelAgent, Repository
from steel_agent_v2.theoretical_mass import dimension_consistency
from test_steel_agent_v2 import Fake, edit, set_op
from test_steel_agent_v2_dimension_consistency import pipe, run


NAMES=['толщина стенки трубы','фактическая толщина','wall (actual)',
       'wall_or_thickness','specified pipe wall thickness (mm)',
       'ширина трубы фактическая','actual height of pipe']


@pytest.mark.parametrize('name',NAMES)
@pytest.mark.parametrize('conflict',[True,False])
def test_structured_dimension_modifiers_cannot_bypass_consistency(tmp_path,name,conflict):
    gauge='wall' in name or 'толщин' in name
    value=('5' if gauge else '41') if conflict else ('3' if gauge else '40')
    source,plan=pipe(value+' мм',name)
    _,repo,_,_=run(tmp_path,source,plan)
    state=repo.load(1)
    assert state.theoretical_mass[0].tonnes==(None if conflict else Decimal('8.6688'))
    assert state.items==state.originals and state.items[0].quantity==430


@pytest.mark.parametrize('name',['width_or_height','неизвестная толщина','diameter reference unknown'])
def test_explicit_but_unresolved_dimension_role_blocks_only_mass(tmp_path,name):
    source,plan=pipe('3 мм',name);_,repo,_,_=run(tmp_path,source,plan)
    assert repo.load(1).theoretical_mass[0].tonnes is None
    assert repo.load(1).items==repo.load(1).originals


@pytest.mark.parametrize('name',['colour','comment','purpose','surface'])
def test_nondimensional_metadata_does_not_block_mass(tmp_path,name):
    source,plan=pipe('синтетический',name);_,repo,_,_=run(tmp_path,source,plan)
    assert repo.load(1).theoretical_mass[0].tonnes==Decimal('8.6688')


@pytest.mark.parametrize('resubmit_spec',[False,True])
@pytest.mark.parametrize('wall_name',['wall','толщина стенки трубы','wall (actual)','wall_or_thickness'])
def test_explicit_wall_correction_uses_existing_atomic_replacement_restart_undo(tmp_path,resubmit_spec,wall_name):
    source,plan=pipe(name=wall_name);agent,repo,fake,_=run(tmp_path,source,plan);before=repo.load(1)
    # GPT resolves the same position; Python only validates/applies its operations.
    ops=[dict(**set_op(name='profile',value='5',evidence='5'),replace_from='3'),
         set_op(name=wall_name,value='5 мм',evidence='5 мм')]
    for op in ops:
        op.pop('target_id')
        op['selection']=dict(mode='where',cardinality='one',evidence='Стенка',
            predicates=[dict(field='label',value='Труба',match='token')])
    proposal=edit(before,ops)
    if resubmit_spec:proposal['mass_specs']=deepcopy(plan['mass_specs'])
    fake.plans.append(proposal)
    answer=agent.handle(1,'Стенка 5 мм',event_id='correct')
    assert answer.status=='ok',answer.diagnostics
    changed=repo.load(1);item=changed.items[0]
    assert next(c.value for c in item.characteristics if c.name=='profile')=='40×40×5'
    assert next(c.value for c in item.characteristics if c.name==wall_name)=='5 мм'
    assert dimension_consistency(item,changed.theoretical_mass[0].spec) is None
    assert item.quantity==430 and item.unit=='шт'
    assert next(c.value for c in item.characteristics if c.name=='grade')=='Ст3'
    assert next(c.value for c in item.characteristics if c.name=='length')=='6м'
    assert changed.originals==before.originals and changed.original_text==source
    assert changed.theoretical_mass[0].tonnes is None
    assert '40×40×3' not in answer.text and '≈8,669' not in answer.text
    assert repo.version(1,before.id,1)==before and changed.revision==2
    assert repo.load(2) is None
    assert agent.handle(1,'Стенка 5 мм',event_id='correct')==answer and len(fake.calls)==2
    reopened=Repository(Database(tmp_path/'dimensions.sqlite3'))
    next_fake=Fake(dict(intent='show_request'),edit(changed,[dict(kind='undo')]))
    next_agent=SteelAgent(next_fake,reopened)
    shown=next_agent.handle(1,'Покажи',event_id='show')
    assert '40×40×5' in shown.text and '≈8,669' not in shown.text
    assert next_agent.handle(1,'Отмени',event_id='undo').status=='ok'
    restored=reopened.load(1)
    assert restored.items==before.items and restored.originals==before.originals
    assert restored.theoretical_mass==before.theoretical_mass and restored.revision==3
    assert reopened.version(1,before.id,2)==changed


def test_failed_second_correction_does_not_partially_change_profile(tmp_path):
    source,plan=pipe();agent,repo,fake,_=run(tmp_path,source,plan);before=repo.load(1)
    bad=edit(before,[dict(**set_op(name='profile',value='5',evidence='5'),replace_from='3'),
                     set_op(name='wall',value='7 мм',evidence='7 мм')])
    fake.plans.extend([bad,deepcopy(bad)])
    reply=agent.handle(1,'Стенка 5 мм',event_id='bad')
    assert reply.status=='failed' and repo.load(1)==before and len(fake.calls)==3
    assert repo.version(1,before.id,2) is None


def test_actual_sdk_preserves_wall_message_and_parses_one_supplied_reply_without_network():
    import json
    import httpx2
    from openai import OpenAI, DefaultHttpxClient
    from steel_agent_v2.contracts import AgentPlan
    from steel_agent_v2.transport import OpenAITransport
    from test_steel_agent_v2_wire_schema import subset_issues
    sent=[]
    def respond(request):
        sent.append(json.loads(request.content))
        plan=AgentPlan(intent='free_chat',answer='Офлайн')
        return httpx2.Response(200,json={'id':'resp_test','object':'response','created_at':0,
            'status':'completed','model':'gpt-6-luna','output':[{'id':'msg_test','type':'message',
            'role':'assistant','status':'completed','content':[{'type':'output_text',
            'text':plan.model_dump_json(),'annotations':[]}]}]})
    with DefaultHttpxClient(transport=httpx2.MockTransport(respond)) as http:
        transport=OpenAITransport(OpenAI(api_key='offline-test',http_client=http))
        # This checks transport, not whether GPT understands a wall correction.
        plan=transport.plan({'message':'Стенка 5 мм'})
    assert len(sent)==1
    assert json.loads(sent[0]['input'])['context']['message']=='Стенка 5 мм'
    assert sent[0]['text']['format']['strict'] is True
    assert subset_issues(sent[0]['text']['format']['schema'])==[]
    assert plan==AgentPlan(intent='free_chat',answer='Офлайн')


def test_sequential_correction_in_one_input_keeps_original_evidence(tmp_path):
    source,plan=pipe();source+='; Уточнение той же трубы: стенка 5 мм'
    plan['extraction']['records'][0]['end']=len(source)
    facts=plan['extraction']['items'][0]['characteristics']
    # Supplied coordinator output: current facts are corrected, raw evidence stays.
    next(c for c in facts if c['name']=='profile')['value']='40×40×5'
    next(c for c in facts if c['name']=='wall').update(value='5 мм',raw='5 мм')
    _,repo,_,answer=run(tmp_path,source,plan);state=repo.load(1)
    assert state.items[0].characteristics[0].raw=='40×40×3'
    assert state.items[0].characteristics[0].value=='40×40×5'
    assert state.original_text==source and state.items[0].quantity==430
    assert state.theoretical_mass[0].tonnes is None and '≈8,669' not in answer.text
