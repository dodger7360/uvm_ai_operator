"""API failures: real SDK over mock HTTP, never a paid retry of user messages."""
import json
import logging
import pytest
from unittest.mock import Mock

import httpx2
from openai import OpenAI, DefaultHttpxClient

from database import Database
from diagnostics import operation, http_attempt
from steel_agent_v2.agent import SteelAgent
from steel_agent_v2.repository import Repository
from steel_agent_v2.transport import OpenAITransport
from steel_agent_v2.engine import EngineError
from steel_agent_v2.agent import validation_codes


def test_http_400_metadata_survives_without_response_body(tmp_path, caplog):
    # Illustrative mock reason, NOT the recovered cause of the real user's 400.
    private = 'PRIVATE_MESSAGE_AND_API_KEY_MUST_NOT_BE_LOGGED'
    attempts = []
    def respond(request):
        attempts.append(request)
        return httpx2.Response(400, headers={'x-request-id':'req_offline_diagnostics'},
            json={'error':{'message':private,'type':'invalid_request_error',
                           'code':'invalid_json_schema','param':'text.format.schema'}})
    repo = Repository(Database(tmp_path/'v2.sqlite3'))
    with DefaultHttpxClient(transport=httpx2.MockTransport(respond)) as http:
        service = SteelAgent(OpenAITransport(OpenAI(api_key='offline-test',http_client=http)),repo)
        service.engine.create = Mock(side_effect=AssertionError('No engine before successful API response'))
        service.engine.edit = Mock(side_effect=AssertionError('No engine before successful API response'))
        with caplog.at_level(logging.INFO,logger='operator_diagnostics'), operation() as trace:
            reply = service.handle(1,'Synthetic request',event_id='test-400')
    assert len(attempts)==1 and reply.status=='failed' and reply.diagnostics==['api_bad_request']
    assert repo.load(1) is None
    with repo.database.connection() as c:
        assert c.execute('SELECT count(*) FROM steel_v2_turn').fetchone()[0]==1
        assert c.execute('SELECT count(*) FROM steel_v2_inflight').fetchone()[0]==0
    events=[json.loads(r.getMessage()) for r in caplog.records if r.name=='operator_diagnostics']
    metric=next(e for e in events if e['event']=='steel_v2_model_call')
    assert metric['http_status']==400
    assert metric['api_error_code']=='invalid_json_schema'
    assert metric['api_error_param']=='text.format.schema'
    assert metric['api_request_id']=='req_offline_diagnostics'
    assert metric['operation']==trace.operation_id
    assert next(e for e in events if e['event']=='operation_complete')['result']=='api_bad_request'
    assert private not in caplog.text and 'offline-test' not in caplog.text


def test_unknown_error_strings_are_not_logged():
    from steel_agent_v2.transport import api_error_metadata
    error=type('Error',(),{'status_code':400,'code':'PRIVATE_CODE','param':'PRIVATE_PARAM',
                          'request_id':'PRIVATE_REQUEST_ID'})()
    assert api_error_metadata(error)=={'http_status':400,'api_error_code':'other','api_error_param':'other'}


@pytest.mark.parametrize('status',[400,401,403,404,429,500])
def test_all_api_statuses_keep_status_without_hidden_retry(status,caplog):
    attempts=[]
    def respond(request):
        attempts.append(request)
        return httpx2.Response(status,json={'error':{'message':'PRIVATE_BODY','type':'invalid_request_error'}})
    with DefaultHttpxClient(transport=httpx2.MockTransport(respond)) as http:
        transport=OpenAITransport(OpenAI(api_key='offline-test',http_client=http))
        with caplog.at_level(logging.INFO,logger='operator_diagnostics'),pytest.raises(Exception):
            transport.plan({'message':'synthetic'})
    assert len(attempts)==1 and transport.metrics[-1]['http_status']==status
    assert 'PRIVATE_BODY' not in caplog.text


@pytest.mark.parametrize('failure',[httpx2.ReadTimeout,httpx2.ConnectError])
def test_timeout_has_no_invented_http_status_or_retry(failure):
    attempts=[]
    def respond(request):
        attempts.append(request)
        raise failure('PRIVATE_CONNECTION_ERROR',request=request)
    with DefaultHttpxClient(transport=httpx2.MockTransport(respond)) as http:
        transport=OpenAITransport(OpenAI(api_key='offline-test',http_client=http))
        with pytest.raises(Exception):transport.plan({'message':'synthetic'})
    assert len(attempts)==1
    assert 'http_status' not in transport.metrics[-1]


def test_400_without_structured_metadata_does_not_invent_values():
    def respond(request):
        return httpx2.Response(400,json={'error':{'message':'PRIVATE_BODY'}})
    with DefaultHttpxClient(transport=httpx2.MockTransport(respond)) as http:
        transport=OpenAITransport(OpenAI(api_key='offline-test',http_client=http))
        with pytest.raises(Exception):transport.plan({'message':'synthetic'})
    metric=transport.metrics[-1]
    assert metric['http_status']==400
    assert not any(k in metric for k in ['api_error_code','api_error_param','api_request_id'])


def test_long_and_malformed_diagnostic_values_are_bounded():
    from steel_agent_v2.transport import api_error_metadata
    error=type('Error',(),{'status_code':400,'code':'x'*100000,'param':'text.'+'x'*100000,
                          'request_id':'req_'+'x'*100000})()
    assert api_error_metadata(error)=={'http_status':400,'api_error_code':'other','api_error_param':'other'}
    assert api_error_metadata(type('Error',(),{'status_code':True,'code':{},'param':[],
                                              'request_id':'req_bad\nPRIVATE'})())=={}


def test_api_failure_preserves_active_original_and_replay_calls_no_model(tmp_path,caplog):
    from test_steel_agent_v2 import Fake,proposal,SOURCE
    repo=Repository(Database(tmp_path/'v2.sqlite3'))
    service=SteelAgent(Fake(proposal()),repo)
    service.handle(1,SOURCE,event_id='create')
    before=repo.load(1).model_dump_json()
    attempts=[]
    def respond(request):
        attempts.append(request)
        return httpx2.Response(400,json={'error':{'message':'PRIVATE_BODY'}})
    with DefaultHttpxClient(transport=httpx2.MockTransport(respond)) as http:
        service.transport=OpenAITransport(OpenAI(api_key='offline-test',http_client=http))
        with caplog.at_level(logging.INFO,logger='operator_diagnostics'),operation():
            reply=service.handle(1,'Synthetic edit',event_id='edit')
        repeated=service.handle(1,'Synthetic edit',event_id='edit')
    assert reply==repeated and reply.status=='failed' and len(attempts)==1
    assert repo.load(1).model_dump_json()==before
    with repo.database.connection() as c:
        assert c.execute("SELECT message FROM steel_v2_turn WHERE event_id='edit'").fetchone()[0]=='Synthetic edit'
        assert c.execute('SELECT count(*) FROM steel_v2_version').fetchone()[0]==1
        assert c.execute('SELECT count(*) FROM steel_v2_inflight').fetchone()[0]==0
    assert 'PRIVATE_BODY' not in caplog.text and 'Synthetic edit' not in caplog.text


@pytest.mark.parametrize('message,expected',[
    ('stale_request_revision','stale_request_revision'),
    ('groups_must_partition_all_items_once','groups_must_partition_all_items_once'),
    ('duplicate_characteristic:PRIVATE_POSITION','request_validation'),
    ('stale_request_revision:PRIVATE_VALUE','request_validation'),
    ('stale_request_revision\nPRIVATE_SOURCE','request_validation'),
    ('PRIVATE_UNKNOWN_EXCEPTION','request_validation'),
])
def test_validation_code_is_exact_allowlist_not_exception_text(tmp_path,caplog,message,expected):
    from test_steel_agent_v2 import Fake
    error=EngineError(message)
    assert validation_codes(error)==[expected]
    repo=Repository(Database(tmp_path/'v2.sqlite3'))
    with caplog.at_level(logging.INFO,logger='operator_diagnostics'),operation():
        reply=SteelAgent(Fake(error,error),repo).handle(1,'PRIVATE_USER_INPUT',event_id='private-event')
    assert reply.status=='failed' and reply.calls==2
    events=[json.loads(record.message) for record in caplog.records if record.name=='operator_diagnostics']
    errors=[event for event in events if event['event']=='steel_v2_validation']
    assert len(errors)==2 and all(event['diagnostic_codes']==[expected] for event in errors)
    assert next(event for event in events if event['event']=='steel_v2_plan_result')['primary_failure']==expected
    assert 'PRIVATE_' not in caplog.text and 'private-event' not in caplog.text


@pytest.mark.parametrize('cached',[0,37,None,True,-1,'PRIVATE_CACHED_TOKENS'])
def test_sdk_cached_tokens_and_hook_bytes_are_numeric_and_safe(cached,caplog):
    from test_steel_agent_v2_ux_search import search_response
    payloads=[]
    response=search_response();response['output']=response['output'][1:]
    response['output'][0]['content'][0]['text']=json.dumps(dict(intent='free_chat',answer='PRIVATE_ANSWER'))
    response['usage']=dict(input_tokens=100,output_tokens=10,total_tokens=110,
                           input_tokens_details={'cached_tokens':cached})
    # Mock client keeps malformed usage values uncoerced for the numeric guard;
    # valid usage also runs through the actual SDK HTTP path below.
    if type(cached) is int and cached>=0:
        def respond(request):
            payloads.append(request.content)
            return httpx2.Response(200,json=response)
        with DefaultHttpxClient(transport=httpx2.MockTransport(respond),event_hooks={'request':[http_attempt]}) as http:
            transport=OpenAITransport(OpenAI(api_key='PRIVATE_API_KEY',http_client=http))
            with caplog.at_level(logging.INFO,logger='operator_diagnostics'),operation():
                result=transport.plan({'message':'PRIVATE_ТЕКСТ'})
        assert result.answer=='PRIVATE_ANSWER'
        metric=transport.metrics[-1]
        assert metric['cached_tokens']==cached
        assert metric['request_bytes']==len(payloads[0])>len(payloads[0].decode('utf-8'))
    else:
        client=Mock();client.with_options.return_value=client;client.responses.create.return_value=response
        transport=OpenAITransport(client)
        with caplog.at_level(logging.INFO,logger='operator_diagnostics'),operation():
            transport.plan({'message':'PRIVATE_ТЕКСТ'})
        metric=transport.metrics[-1]
        assert 'cached_tokens' not in metric and 'request_bytes' not in metric
    emitted=[json.loads(record.message) for record in caplog.records if record.name=='operator_diagnostics']
    assert next(event for event in emitted if event['event']=='steel_v2_model_call')=={'event':'steel_v2_model_call',**metric}
    assert 'PRIVATE_' not in caplog.text and 'https://' not in caplog.text and 'authorization' not in caplog.text.lower()


def test_hook_does_not_read_stream_or_reuse_previous_request_size(caplog):
    class Unread:
        headers={}
        @property
        def content(self):raise RuntimeError('PRIVATE_STREAM_ERROR')
    with operation() as trace:
        trace.http_request_bytes=123
        http_attempt(Unread())
        assert trace.http_request_bytes is None and trace.http_attempts==1
    client=Mock();client.with_options.return_value=client
    client.responses.create.side_effect=httpx2.ReadTimeout('PRIVATE_NETWORK_DETAIL')
    transport=OpenAITransport(client)
    with caplog.at_level(logging.INFO,logger='operator_diagnostics'),operation() as trace:
        trace.http_request_bytes=123
        with pytest.raises(httpx2.ReadTimeout):transport.plan({'message':'PRIVATE_INPUT'})
    assert client.responses.create.call_count==1 and 'request_bytes' not in transport.metrics[-1]
    assert 'PRIVATE_' not in caplog.text


def test_unknown_network_outcome_has_one_hook_call_and_no_repair(tmp_path,caplog):
    requests=[]
    def respond(request):
        requests.append(request.content)
        raise httpx2.ReadTimeout('PRIVATE_NETWORK_ERROR',request=request)
    repo=Repository(Database(tmp_path/'v2.sqlite3'))
    with DefaultHttpxClient(transport=httpx2.MockTransport(respond),event_hooks={'request':[http_attempt]}) as http:
        transport=OpenAITransport(OpenAI(api_key='PRIVATE_KEY',http_client=http))
        with caplog.at_level(logging.INFO,logger='operator_diagnostics'),operation():
            reply=SteelAgent(transport,repo).handle(1,'PRIVATE_INPUT',event_id='network')
    assert reply.status=='failed' and reply.calls==len(requests)==1 and repo.load(1) is None
    events=[json.loads(record.message) for record in caplog.records if record.name=='operator_diagnostics']
    result=next(event for event in events if event['event']=='steel_v2_plan_result')
    assert result['attempt']==1 and result['repair_attempted'] is False and result['outcome']=='failed'
    assert result['primary_failure']=='api_timeout'
    assert transport.metrics[-1]['request_bytes']==len(requests[0])
    assert 'PRIVATE_' not in caplog.text
