"""Inspect real SDK HTTP payloads offline; not a simulation of API acceptance."""
from copy import deepcopy
from contextlib import contextmanager
from decimal import Decimal
import json

import httpx2
from openai import DefaultHttpxClient, OpenAI
import pytest
from pydantic import ValidationError

from steel_agent_v2.contracts import AgentPlan
from steel_agent_v2.transport import OpenAITransport


def schema_nodes(node, path='$'):
    yield path, node
    for key in ('$defs', 'properties'):
        for name, child in node.get(key, {}).items():
            yield from schema_nodes(child, f'{path}/{key}/{name}')
    for i, child in enumerate(node.get('anyOf', [])):
        yield from schema_nodes(child, f'{path}/anyOf/{i}')
    if 'items' in node:
        yield from schema_nodes(node['items'], path+'/items')


def subset_issues(schema):
    """Project's documented supported profile, not a complete API validator."""
    issues=[]
    if schema.get('type') != 'object' or 'anyOf' in schema:
        issues.append('$:root')
    for path, node in schema_nodes(schema):
        supported={'$defs','$ref','type','title','description','properties','required',
                   'additionalProperties','items','anyOf','enum','const','pattern','format',
                   'minimum','maximum','exclusiveMinimum','exclusiveMaximum','multipleOf',
                   'minLength','maxLength','minItems','maxItems'}
        issues.extend(path+'/'+key for key in node if key not in supported)
        for key in ('default', 'allOf', 'not', 'dependentRequired', 'dependentSchemas', 'if', 'then', 'else'):
            if key in node:
                if path+'/'+key not in issues:
                    issues.append(path+'/'+key)
        if node.get('type') == 'object':
            if node.get('additionalProperties') is not False:
                issues.append(path+'/additionalProperties')
            if set(node.get('required', [])) != set(node.get('properties', {})):
                issues.append(path+'/required')
        if '$ref' in node:
            assert node['$ref'].startswith('#/$defs/')
            assert node['$ref'].split('/')[-1] in schema['$defs']
    return issues


@contextmanager
def sdk_transport(reply, *, inspect=None, inspect_request=None):
    """Actual Responses.parse -> HTTP serializer -> mock -> actual SDK parser."""
    sent=[]
    def respond(request):
        if inspect_request:
            inspect_request(request)
        payload=json.loads(request.content)
        sent.append(payload)
        if inspect:
            inspect(payload)
        return httpx2.Response(200, json={
            'id':'resp_offline', 'object':'response', 'created_at':0, 'status':'completed',
            'model':'gpt-6-luna', 'output':[{'id':'msg_offline', 'type':'message',
            'role':'assistant', 'status':'completed', 'content':[{'type':'output_text',
            'text':json.dumps(reply(payload) if callable(reply) else reply), 'annotations':[]}]}]})
    with DefaultHttpxClient(transport=httpx2.MockTransport(respond)) as http:
        transport=OpenAITransport(OpenAI(api_key='offline-test', http_client=http))
        yield transport, sent


def sdk_plan(reply, *, inspect=None):
    with sdk_transport(reply, inspect=inspect) as (transport, sent):
        plan=transport.plan({'message':'synthetic'})
    return plan, sent


def test_final_http_schema_supported_profile():
    def inspect(payload):
        fmt=payload['text']['format']
        assert fmt['type']=='json_schema' and fmt['strict'] is True
        assert subset_issues(fmt['schema']) == []
        assert payload['model']=='gpt-6-luna'
        assert payload['reasoning']=={'effort':'high'}
        assert payload['store'] is False
    sdk_plan(AgentPlan(intent='free_chat', answer='Hello').model_dump(mode='json'), inspect=inspect)


def test_final_quantity_is_exact_nullable_string():
    _, sent=sdk_plan(AgentPlan(intent='free_chat', answer='Hello').model_dump(mode='json'))
    quantity=sent[0]['text']['format']['schema']['$defs']['Item']['properties']['quantity']
    assert quantity['anyOf']==[{'type':'string'}, {'type':'null'}]


@pytest.mark.parametrize('intent', ['show_request','answer_question','free_chat','clarify','web_search'])
def test_readonly_intents_survive_sdk_and_internal_validation(intent):
    raw=AgentPlan(intent=intent, answer='Synthetic answer',
                  search_query='Synthetic query' if intent=='web_search' else None).model_dump(mode='json')
    plan, _=sdk_plan(raw)
    assert type(plan) is AgentPlan
    assert plan.model_dump(mode='json')==raw


def create_plan(quantity='0.12345678901234567890123456789'):
    return AgentPlan(intent='create_request', extraction={
        'records':[{'id':'r1','kind':'product','start':0,'end':10}],
        'items':[{'id':'i1','source_id':'r1','label':'Synthetic', 'quantity':quantity,
                  'characteristics':[{'name':'grade','value':'X','raw':'X','source_ids':['r1']}]}],
        'groups':[{'id':'g1','label':'Group','item_ids':['i1'],'rationale':'Synthetic grouping'}]
    }).model_dump(mode='json')


def test_create_decimal_characteristics_groups_preserved():
    raw=create_plan()
    plan, _=sdk_plan(raw)
    assert type(plan) is AgentPlan
    assert plan.extraction.items[0].quantity==Decimal(raw['extraction']['items'][0]['quantity'])
    assert plan.model_dump(mode='json')==raw


@pytest.mark.parametrize('kind', ['set', 'regroup', 'undo'])
def test_edit_and_undo_survive_wire(kind):
    raw=AgentPlan(intent='edit_request', expected_request_id='request1', expected_revision=2,
                  operations=[{'kind':kind,'target_id':'i1','field':'quantity','value':'7.5'}]).model_dump(mode='json')
    plan, _=sdk_plan(raw)
    assert plan.model_dump(mode='json')==raw


@pytest.mark.parametrize('damage', ['float','negative','nan','empty','extra','bad_intent','bad_operation'])
def test_internal_invariants_still_reject_damaged_wire(damage):
    raw=deepcopy(create_plan())
    item=raw['extraction']['items'][0]
    if damage=='float': item['quantity']=0.1
    if damage=='negative': item['quantity']='-1'
    if damage=='nan': item['quantity']='NaN'
    if damage=='empty': item['label']=' '
    if damage=='extra': item['unapproved']='x'
    if damage=='bad_intent': raw['intent']='free_chat'
    if damage=='bad_operation': raw['operations']=[{'kind':'undo'}]
    with pytest.raises(ValidationError):
        sdk_plan(raw)


def test_sdk_engine_sqlite_history_edit_undo_and_failed_response(tmp_path):
    from database import Database
    from steel_agent_v2 import Repository, SteelAgent
    source='Synthetic X — 1.25 t'
    raw=create_plan('1.25')
    raw['extraction']['records'][0]['end']=len(source)
    raw['extraction']['items'][0].update(quantity_text='1.25', unit='t', unit_text='t')
    responses=[raw]
    repo=Repository(Database(tmp_path/'wire.sqlite3'))
    def respond(payload):
        assert subset_issues(payload['text']['format']['schema'])==[]
        return responses.pop(0)
    with sdk_transport(respond) as (transport, sent):
        agent=SteelAgent(transport, repo)
        assert agent.handle(1,source,event_id='create').status=='ok'
        original=repo.load(1)
        assert original.original_text==source
        assert original.items[0].quantity==Decimal('1.25')
        responses.append(AgentPlan(intent='edit_request', expected_request_id=original.id,
            expected_revision=1, operations=[{'kind':'set','target_id':'i1','field':'quantity',
            'value':'7.5','evidence':'7.5'}]).model_dump(mode='json'))
        assert agent.handle(1,'Set 7.5',event_id='edit').status=='ok'
        assert repo.load(1).items[0].quantity==Decimal('7.5')
        responses.append(AgentPlan(intent='edit_request', expected_request_id=original.id,
            expected_revision=2, operations=[{'kind':'undo'}]).model_dump(mode='json'))
        assert agent.handle(1,'Undo',event_id='undo').status=='ok'
        before=repo.load(1).model_dump_json()
        assert repo.load(1).items[0].quantity==Decimal('1.25')
        # Two bounded invalid proposals cannot overwrite the active request.
        bad=deepcopy(raw)
        bad['extraction']['items'][0]['quantity']=0.1
        responses.extend([bad,bad])
        failed=agent.handle(1,'Malformed synthetic input',event_id='bad')
        assert failed.status=='failed' and failed.calls==2
        assert repo.load(1).model_dump_json()==before
        calls=len(sent)
        assert agent.handle(1,'Malformed synthetic input',event_id='bad')==failed
        assert len(sent)==calls
    reopened=Repository(Database(tmp_path/'wire.sqlite3'))
    assert reopened.load(1).model_dump_json()==before
    assert reopened.version(1,original.id,1).original_text==source
    with reopened.database.connection() as connection:
        assert connection.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert connection.execute('SELECT COUNT(*) FROM steel_v2_inflight').fetchone()[0]==0
        assert connection.execute('SELECT COUNT(*) FROM steel_v2_turn').fetchone()[0]==4
