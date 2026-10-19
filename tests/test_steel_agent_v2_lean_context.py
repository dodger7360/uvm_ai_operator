"""Literal retrieval and safe metadata only; approved cases, fake SDK, no live API."""
import json
import logging

import pytest

from steel_agent_v2.knowledge import installed
from steel_agent_v2.theoretical_mass import MassCatalog
from steel_agent_v2.transport import network_error_metadata, OpenAITransport
from test_steel_agent_v2_knowledge import CASES, create


def records(text,current=None,pending=None):
    package=installed();context=package.context(text,current,pending_text=pending)
    catalog=[*MassCatalog().records,*package.mass_records()]
    return context,package.context_records(catalog,text,current,pending_text=pending,
                                          selected_cases={c['id'] for c in context['cases']})


@pytest.mark.parametrize('case',CASES,ids=lambda c:c['id'])
def test_each_approved_source_selects_its_own_example_without_generic_neighbors(case):
    context,selected=records(case['source'])
    assert [c['id'] for c in context['cases']]==[case['id']]
    assert len(context['rules'])==11
    if case['id'].endswith(('002','006')):
        assert len(selected)==(4 if case['id'].endswith('002') else 5)
        assert all(r['id'].startswith(case['id']+':') for r in selected)


@pytest.mark.parametrize('text',['труба уголок Ст3','лист','Привет, как дела?','Товар Z9 — 2 шт.'])
def test_generic_words_are_not_enough_to_select_case_collections(text):
    context,_=records(text)
    assert context['cases']==[] and len(context['rules'])==11


@pytest.mark.parametrize('separator',['×','x','х',' * '])
def test_case002_literal_separator_variants_keep_four_approved_coefficients(separator):
    text=CASES[1]['source'].replace('х',separator).replace('×',separator)
    context,selected=records(text)
    assert [c['id'] for c in context['cases']]==[CASES[1]['id']]
    assert {r['profile'] for r in selected}=={'40×40×3','50×50×5','60×40×3','63×40×5'}


def test_new_case002_does_not_retrieve_old_active_or_pending_examples(tmp_path):
    _,repo,_,_=create(tmp_path,CASES[4]);current=repo.load(1)
    context,selected=records(CASES[1]['source'],current,CASES[2]['source'])
    assert [c['id'] for c in context['cases']]==[CASES[1]['id']] and len(selected)==4


def test_short_continuation_can_retrieve_pending_or_active_without_whitelist(tmp_path):
    context,_=records('Разбери сохранённый исходник',pending=CASES[2]['source'])
    assert [c['id'] for c in context['cases']]==[CASES[2]['id']]
    _,repo,_,_=create(tmp_path,CASES[1])
    context,selected=records('Объясни расчёт',repo.load(1))
    assert [c['id'] for c in context['cases']]==[CASES[1]['id']] and len(selected)==4


def test_mixed_concrete_sources_keep_two_relevant_cases_and_all_nine_records():
    context,selected=records(CASES[1]['source']+'\n'+CASES[5]['source'])
    assert {c['id'] for c in context['cases']}=={CASES[1]['id'],CASES[5]['id']}
    assert len(selected)==9 and len(installed().mass_records())==9


@pytest.mark.parametrize('cause_name,stage',[('ConnectError','connect'),('ReadError','response_read'),
                                           ('WriteTimeout','request_write'),('ProxyError','connect')])
def test_network_metadata_never_exposes_exception_messages_or_private_request(cause_name,stage):
    import httpx2
    from openai import APIConnectionError
    cause=getattr(httpx2,cause_name)('PRIVATE_PROXY_TOKEN',request=httpx2.Request('POST','https://private.invalid/secret'))
    cause.__cause__=OSError('PRIVATE_USER_DATA')
    error=APIConnectionError(request=httpx2.Request('POST','https://private.invalid/secret'))
    error.__cause__=cause
    data=network_error_metadata(error)
    assert data=={'exception_types':['APIConnectionError',cause_name,'OSError'],'http_stage':stage}
    assert not any(x in json.dumps(data) for x in ['PRIVATE','private.invalid','secret'])


def test_unknown_exception_class_is_redacted_and_phase_not_invented():
    error=type('PRIVATE_SECRET_CLASS',(Exception,),{})('PRIVATE_MESSAGE')
    assert network_error_metadata(error)=={'exception_types':['OtherError'],'http_stage':'unknown'}


def test_actual_sdk_connection_failure_logs_safe_chain_phase_duration_once(caplog):
    import httpx2
    from openai import OpenAI,DefaultHttpxClient,APIConnectionError
    attempts=[]
    def fail(request):
        attempts.append(1)
        raise httpx2.ReadError('PRIVATE_PROXY_TOKEN',request=request)
    caplog.set_level(logging.INFO,logger='operator_diagnostics')
    with DefaultHttpxClient(transport=httpx2.MockTransport(fail)) as http:
        transport=OpenAITransport(OpenAI(api_key='offline-test',http_client=http))
        with pytest.raises(APIConnectionError):transport.plan({'message':'Synthetic'})
    assert len(attempts)==1
    metric=transport.metrics[-1]
    assert metric['reason']=='api_connection' and metric['stage']=='coordinate'
    assert metric['http_stage']=='response_read' and metric['exception_types']==['APIConnectionError','ReadError']
    assert metric['duration_ms']>=0 and 'http_status' not in metric
    assert 'PRIVATE_PROXY_TOKEN' not in caplog.text


def test_case002_actual_sdk_payload_has_one_case_four_records_and_one_call(tmp_path):
    from database import Database
    from steel_agent_v2 import SteelAgent,Repository
    from steel_agent_v2.contracts import AgentPlan
    from test_steel_agent_v2_wire_schema import sdk_transport
    case=CASES[1]
    with sdk_transport(AgentPlan.model_validate(case['plan']).model_dump(mode='json')) as (transport,sent):
        agent=SteelAgent(transport,Repository(Database(tmp_path/'wire.sqlite3')))
        reply=agent.handle(1,case['source'],event_id='wire')
    assert reply.text==case['expected_text'] and reply.calls==1 and len(sent)==1
    context=json.loads(sent[0]['input'])['context']
    assert [c['id'] for c in context['expert_knowledge']['cases']]==[case['id']]
    assert len(context['mass_reference_records'])==4
    assert len(agent.engine.mass_catalog.records)==12
