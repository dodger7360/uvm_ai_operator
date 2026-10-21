"""Synthetic live-shaped plans; no production payload and no live model calls."""
from copy import deepcopy
from decimal import Decimal
import json

import pytest
from database import Database
from steel_agent_v2 import SteelAgent, Repository
from steel_agent_v2.contracts import AgentPlan
from test_steel_agent_v2 import Fake, edit, set_op
from test_steel_agent_v2_knowledge import CASES


def live_shape():
    # Published CASE data only. Reproduce the observed *structure*: null standard
    # with no semantic pointer and an explicit length omitted from dimensions.
    case=deepcopy(CASES[1]); plan=case['plan']
    for item in plan['extraction']['items']:
        item['characteristics']=[c for c in item['characteristics'] if c['name']!='standard']
        for c in item['characteristics']:
            if c['name']=='profile':c['name']='size'
        item['characteristics'].append(dict(name='standard',value=None,
            raw=item['characteristics'][0]['raw'],source_ids=[item['source_id']]))
    for spec in plan['mass_specs']:
        spec['standard_name']=None
        spec['profile_name']='size'
        spec['cutting']='confirmed_piece_length'
        spec['dimensions']=[d for d in spec['dimensions'] if d['role']!='piece_length']
        for d in spec['dimensions']:
            if d['name']=='profile':d['name']='size'
    for hint in plan['display_layout']['items']:
        for d in hint['dimensions']:
            if d['name']=='profile':d['name']='size'
    return case


def run(tmp_path,case):
    repo=Repository(Database(tmp_path/'simple.sqlite3')); fake=Fake(case['plan'])
    agent=SteelAgent(fake,repo);reply=agent.handle(1,case['source'],event_id='new')
    return agent,repo,fake,reply


def test_live_shaped_missing_pointers_use_only_approved_basis(tmp_path):
    case=live_shape();case['plan']['extraction']['questions']=['Уточните ГОСТ для всех позиций.']
    agent,repo,fake,reply=run(tmp_path,case);state=repo.load(1)
    assert len(state.items)==len(state.originals)==4
    assert state.original_text==case['source']
    assert state.items==AgentPlan.model_validate(case['plan']).extraction.items
    assert {m.spec.item_id:m.tonnes for m in state.theoretical_mass}=={k:Decimal(v) for k,v in case['expected_tonnes_by_id'].items()}
    assert reply.text==case['expected_text']
    assert reply.calls==len(fake.calls)==1 and not fake.search_calls
    reopened=Repository(Database(tmp_path/'simple.sqlite3'))
    assert reopened.load(1)==state
    assert reopened.load(2) is None
    assert state.questions==case['plan']['extraction']['questions']


@pytest.mark.parametrize('field,value',[('standard','ГОСТ 99999'),('execution','иное исполнение')])
def test_explicit_conditions_block_only_mass(tmp_path,field,value):
    case=live_shape();item=case['plan']['extraction']['items'][0]
    record=next(r for r in case['plan']['extraction']['records'] if r['id']==item['source_id'])
    # Synthetic explicit source facts, not facts copied from a real Telegram turn.
    insert=record['end'];extra=' '+value
    case['source']=case['source'][:insert]+extra+case['source'][insert:]
    for r in case['plan']['extraction']['records']:
        if r['start']>insert:r['start']+=len(extra)
        if r['end']>=insert:r['end']+=len(extra)
    item['characteristics']=[c for c in item['characteristics'] if c['name']!=field]
    item['characteristics'].append(dict(name=field,value=value,raw=value,source_ids=[item['source_id']]))
    _,repo,_,reply=run(tmp_path,case);state=repo.load(1)
    assert reply.status in {'ok','partial'},reply.diagnostics
    assert len(state.items)==4 and state.original_text==case['source']
    masses={m.spec.item_id:m for m in state.theoretical_mass}
    assert masses[item['id']].tonnes is None
    assert sum(m.status=='calculated' for m in masses.values())==3


def test_unknown_fields_have_human_names_and_duplicate_questions_once(tmp_path):
    case=live_shape();case['plan']['mass_specs']=[]
    case['plan']['extraction']['questions']=['Уточните количество.','Уточните количество.']
    _,repo,_,reply=run(tmp_path,case)
    assert 'standard' not in reply.text
    assert reply.text.count('Уточните количество.')==1
    assert len(repo.load(1).questions)==2  # Presentation never rewrites the audit.


def test_four_norm_only_questions_are_not_repeated_for_indicative_result(tmp_path):
    case=live_shape()
    questions=[f'Укажите ГОСТ для {i["id"]}.' for i in case['plan']['extraction']['items']]
    case['plan']['extraction']['questions']=questions
    _,repo,_,reply=run(tmp_path,case)
    assert reply.text==case['expected_text']
    assert repo.load(1).questions==questions


def test_standard_fact_may_be_entirely_absent(tmp_path):
    case=live_shape()
    for item in case['plan']['extraction']['items']:
        item['characteristics']=[c for c in item['characteristics'] if c['name']!='standard']
    _,repo,_,reply=run(tmp_path,case)
    assert reply.text==case['expected_text']
    assert all(m.status=='calculated' for m in repo.load(1).theoretical_mass)


@pytest.mark.parametrize('case',CASES,ids=lambda c:c['id'])
def test_compact_create_envelope_through_actual_sdk_and_repository(tmp_path,case):
    from test_steel_agent_v2_wire_schema import sdk_transport,subset_issues
    from steel_agent_v2.wire import WireCreatePlan
    raw=AgentPlan.model_validate(case['plan']).model_dump(mode='json')
    compact={k:raw[k] for k in WireCreatePlan.model_fields}
    with sdk_transport({'plan':compact}) as (transport,sent):
        repo=Repository(Database(tmp_path/'wire.sqlite3'))
        reply=SteelAgent(transport,repo).handle(1,case['source'],event_id='wire')
    assert reply.text==case['expected_text'] and len(sent)==reply.calls==1
    schema=sent[0]['text']['format']['schema']
    assert subset_issues(schema)==[]
    assert sent[0]['text']['format']['strict'] is True
    create_schema=schema['$defs']['WireCreatePlan']
    assert set(create_schema['required'])==set(create_schema['properties'])==set(WireCreatePlan.model_fields)
    assert 'operations' not in create_schema['properties']
    assert 'operations' in schema['$defs']['WireActionPlan']['properties']
    assert repo.load(1).original_text==case['source']


def test_create_action_and_repair_payload_scopes():
    from steel_agent_v2.transport import plan_request,CREATE_INSTRUCTIONS,ACTION_INSTRUCTIONS,INSTRUCTIONS
    small=plan_request({'message':'synthetic','active_request':None})
    large=plan_request({'message':'synthetic','active_request':{'id':'existing'}})
    assert small['instructions']==CREATE_INSTRUCTIONS
    assert large['instructions']==CREATE_INSTRUCTIONS+ACTION_INSTRUCTIONS
    repair=plan_request({'message':'synthetic'},diagnostics=['known_error'],previous='invalid proposal')
    assert repair['instructions']==INSTRUCTIONS
    assert small['instructions']!=large['instructions']!=repair['instructions']
    repair_input=json.loads(repair['input'])
    assert repair_input['diagnostics']==['known_error']
    assert repair_input['previous_proposal']=='invalid proposal'
    assert 'operations' not in small['text']['format']['schema']['$defs']['WireCreatePlan']['properties']


@pytest.mark.parametrize('questions',[
    ['Уточните ГОСТ и марку стали.'],['Нужен ли сертификат по ГОСТ?'],['Уточните исполнение.']])
def test_unrelated_or_mixed_questions_are_never_silenced(tmp_path,questions):
    case=live_shape();case['plan']['extraction']['questions']=questions
    _,repo,_,reply=run(tmp_path,case)
    assert questions[0] in reply.text
    assert repo.load(1).questions==questions


def test_restart_edit_undo_keeps_basis_and_originals(tmp_path):
    case=live_shape();agent,repo,fake,_=run(tmp_path,case);original=repo.load(1)
    fake.plans.append(edit(original,[set_op(item='i1',field='quantity',name=None,value='10',evidence='10')]))
    changed=agent.handle(1,'Позицию 1 сделай 10 шт',event_id='edit')
    assert changed.status in {'ok','partial'}
    state=repo.load(1)
    assert state.items[0].quantity==10 and state.theoretical_mass[0].tonnes==Decimal('0.2016')
    assert state.originals==original.originals and state.original_text==original.original_text
    reopened=Repository(Database(tmp_path/'simple.sqlite3'))
    undo=SteelAgent(Fake(edit(state,[dict(kind='undo')])),reopened).handle(1,'Отмени',event_id='undo')
    restored=reopened.load(1)
    assert undo.status in {'ok','partial'}
    assert restored.items==original.items and restored.theoretical_mass==original.theoretical_mass
    assert restored.revision==3


@pytest.mark.parametrize('damage',['length','grade','wall','total_length','ambiguous_length'])
def test_approved_scope_and_length_semantics_cannot_be_widened(tmp_path,damage):
    from steel_agent_v2.theoretical_mass import refresh,validate_annotations
    from steel_agent_v2.contracts import DisplayDimension,Characteristic
    case=live_shape();_,repo,_,_=run(tmp_path,case);state=repo.load(1)
    item=state.items[0];spec=state.theoretical_mass[0].spec
    if damage in {'length','grade','wall'}:
        name,value={'length':('length','8 м'),'grade':('grade','09Г2С'),'wall':('size','40×40×5')}[damage]
        item=item.model_copy(update={'characteristics':[c.model_copy(update={'value':value}) if c.name==name else c for c in item.characteristics]})
    elif damage=='total_length':
        spec=spec.model_copy(update={'dimensions':[*spec.dimensions,DisplayDimension(role='total_length',name='length',unit='м')]})
    else:
        item=item.model_copy(update={'characteristics':[*item.characteristics,Characteristic(name='cut_length',value='8 м',raw='synthetic')]})
    modified=state.model_copy(update={'items':[item,*state.items[1:]]})
    fixed=refresh(modified,[spec],SteelAgent(Fake(),repo).engine.mass_catalog)
    assert fixed.theoretical_mass[0].tonnes is None
    validate_annotations(fixed)
    assert len(fixed.items)==4 and fixed.items[0]==item


def test_compact_invalid_response_repairs_once_without_losing_source(tmp_path):
    from test_steel_agent_v2_wire_schema import sdk_transport
    from steel_agent_v2.wire import WireCreatePlan
    case=live_shape();raw=AgentPlan.model_validate(case['plan']).model_dump(mode='json')
    compact={k:raw[k] for k in WireCreatePlan.model_fields}
    damaged=deepcopy(compact);damaged['extraction']['items'][0]['quantity']='-1'
    calls=[]
    def respond(payload):
        calls.append(payload)
        return {'plan':damaged if len(calls)==1 else compact}
    with sdk_transport(respond) as (transport,sent):
        repo=Repository(Database(tmp_path/'repair.sqlite3'))
        reply=SteelAgent(transport,repo).handle(1,case['source'],event_id='repair')
    assert reply.calls==len(sent)==2 and reply.text==case['expected_text']
    assert repo.load(1).original_text==case['source']
    repair=json.loads(sent[1]['input'])
    assert repair['diagnostics'] and json.loads(repair['previous_proposal'])['intent']=='create_request'
