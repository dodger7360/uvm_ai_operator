"""Responses state matrix using the installed SDK, never a live API call."""
from copy import deepcopy
import json
import logging

import httpx2
from openai import OpenAI, DefaultHttpxClient
from openai.types.responses.response_function_web_search import ResponseFunctionWebSearch
import pytest

from database import Database
from steel_agent_v2 import Repository, SteelAgent
from steel_agent_v2.contracts import AgentPlan
from steel_agent_v2.transport import OpenAITransport, SearchUnavailable
from test_steel_agent_v2 import Fake, SOURCE, proposal
from test_steel_agent_v2_ux_search import search_response


def run_search(response,caplog):
    sent=[]
    def respond(request):
        sent.append(json.loads(request.content))
        return httpx2.Response(200,headers={'x-request-id':'req_runtime_offline'},json=response)
    with DefaultHttpxClient(transport=httpx2.MockTransport(respond)) as http:
        transport=OpenAITransport(OpenAI(api_key='offline-test',http_client=http))
        with caplog.at_level(logging.INFO,logger='operator_diagnostics'):
            try: result=transport.search('PRIVATE_QUERY_NOT_LOGGED')
            except SearchUnavailable as error: result=error
    assert len(sent)==1
    assert 'PRIVATE_QUERY_NOT_LOGGED' not in caplog.text
    return result,sent


@pytest.mark.parametrize('statuses,reason',[
    (['completed'],None),(['completed','completed'],None),
    (['completed','failed'],'tool_incomplete'),
    (['completed','incomplete'],'tool_incomplete'),
    (['completed','searching'],'tool_incomplete'),
    (['completed','in_progress'],'tool_incomplete'),
    (['completed',None],'tool_status_unknown'),
    ([None],'tool_status_unknown'),(['incomplete'],'tool_incomplete')])
def test_tool_status_matrix_is_not_inferred_from_links(statuses,reason,caplog):
    response=search_response()
    template=response['output'][0]
    calls=[]
    for index,status in enumerate(statuses):
        call=deepcopy(template);call['id']='ws'+str(index)
        if status is None:call.pop('status')
        else:
            call['status']=status
            # Known statuses/actions are validated by the installed SDK contract.
            ResponseFunctionWebSearch.model_validate(call)
        calls.append(call)
    response['output']=calls+[response['output'][-1]]
    result,sent=run_search(response,caplog)
    if reason is None:
        assert not isinstance(result,Exception) and result.sources
        assert 'max_tool_calls' not in sent[0]
    else:assert isinstance(result,SearchUnavailable) and result.reason==reason
    event=next(json.loads(record.message) for record in caplog.records
               if 'steel_v2_search_verification' in record.message)
    assert [call['status'] for call in event['calls']]==[s or 'missing' for s in statuses]
    assert event['response_status']=='completed'
    assert event['has_output_text'] and event['message_present']
    assert event['api_request_id']=='req_runtime_offline'
    assert event['search_performed']==('completed' in statuses)
    assert event['tools_completed']==all(s=='completed' for s in statuses)
    assert event['usable']==(reason is None)


@pytest.mark.parametrize('damage,reason',[
    ('incomplete_response','incomplete_response'),('no_message','no_final_message'),
    ('incomplete_message','no_final_message'),('message_before_tools','no_final_message'),
    ('empty_text','empty_text'),('no_sources','no_sources'),('unsafe','no_sources')])
def test_final_response_message_and_sources_are_independent(damage,reason,caplog):
    response=search_response()
    if damage=='incomplete_response':response['status']='incomplete'
    if damage=='no_message':response['output'].pop()
    if damage=='incomplete_message':response['output'][-1]['status']='incomplete'
    if damage=='message_before_tools':response['output'].reverse()
    if damage=='empty_text':response['output'][-1]['content'][0]['text']=''
    if damage=='no_sources':response['output'][0]['action']['sources']=[]
    if damage=='unsafe':response['output'][0]['action']['sources'][0]['url']='https://user:password@example.org/'
    result,_=run_search(response,caplog)
    assert isinstance(result,SearchUnavailable) and result.reason==reason
    event=next(json.loads(record.message) for record in caplog.records
               if 'steel_v2_search_verification' in record.message)
    assert event['response_status']==response['status']
    assert 'Synthetic current weather' not in caplog.text


@pytest.mark.parametrize('form',['annotation','structural','weather'])
def test_source_forms_require_completed_tool_and_final_message(form,caplog):
    response=search_response('feed' if form=='weather' else 'url')
    if form=='annotation':
        response['output'][0]['action']['sources']=[]
        response['output'][-1]['content'][0]['annotations']=[dict(type='url_citation',
            url='https://example.org/cited',title='Synthetic',start_index=0,end_index=5)]
    result,_=run_search(response,caplog)
    assert not isinstance(result,Exception)
    assert result.providers if form=='weather' else result.sources


def failed_search(tmp_path):
    repo=Repository(Database(tmp_path/'runtime.sqlite3'))
    fake=Fake(proposal(),dict(intent='web_search',search_query='Synthetic'),search=SearchUnavailable('tool_incomplete'))
    agent=SteelAgent(fake,repo)
    agent.handle(1,SOURCE,event_id='create')
    before=repo.load(1).model_dump_json()
    assert agent.handle(1,'Найди в интернете',event_id='failed').status=='failed'
    assert repo.load(1).model_dump_json()==before
    return repo,before


@pytest.mark.parametrize('after_other',[False,True])
def test_why_uses_immediate_operation_after_reopening_and_never_searches(tmp_path,after_other):
    repo,before=failed_search(tmp_path)
    if after_other:
        SteelAgent(Fake(dict(intent='free_chat',answer='Synthetic discussion')),repo).handle(1,'Другой разговор',event_id='chat')
    reopened=Repository(Database(tmp_path/'runtime.sqlite3'))
    requests=[]
    def respond(request):
        payload=json.loads(request.content);requests.append(payload)
        assert 'tools' not in payload
        context=json.loads(payload['input'])['context']
        assert context['last_search']=={'status':'failed','reason':'tool_incomplete'}
        assert context['previous_operation']['intent']==('free_chat' if after_other else 'web_search')
        plan=AgentPlan(intent='free_chat',answer='Обсуждаем предыдущий ответ') if after_other else AgentPlan(intent='answer_question',view='search_status')
        body=search_response();body['output']=[body['output'][-1]]
        body['output'][0]['content'][0]['text']=plan.model_dump_json()
        return httpx2.Response(200,json=body)
    with DefaultHttpxClient(transport=httpx2.MockTransport(respond)) as http:
        agent=SteelAgent(OpenAITransport(OpenAI(api_key='offline-test',http_client=http)),reopened)
        reply=agent.handle(1,'Почему?',event_id='why')
    assert len(requests)==1 and reply.status=='ok' and reply.calls==1
    assert 'Инструмент поиска' in reply.text if not after_other else 'Обсуждаем' in reply.text
    assert reopened.load(1).model_dump_json()==before
    assert reopened.last_operation(2) is None


def test_diagnostic_values_are_allowlisted_and_bounded(caplog):
    response=search_response()
    response['output'][0]['status']='PRIVATE_STATUS'*100
    response['output'][0]['action']['type']='PRIVATE_ACTION'
    result,_=run_search(response,caplog)
    assert isinstance(result,SearchUnavailable)
    assert 'PRIVATE_STATUS' not in caplog.text and 'PRIVATE_ACTION' not in caplog.text
    event=next(json.loads(record.message) for record in caplog.records
               if 'steel_v2_search_verification' in record.message)
    assert event['calls'][0]=={'status':'unknown','action':'unknown'}


def test_explanation_can_repeat_without_repeating_search(tmp_path):
    repo,before=failed_search(tmp_path)
    fake=Fake(dict(intent='answer_question',view='search_status'),dict(intent='answer_question',view='search_status'))
    agent=SteelAgent(fake,Repository(Database(tmp_path/'runtime.sqlite3')))
    for n in range(2):
        reply=agent.handle(1,'Почему?',event_id='why'+str(n))
        assert reply.status=='ok' and 'Инструмент поиска' in reply.text
    assert not fake.search_calls and len(fake.calls)==2
    assert repo.load(1).model_dump_json()==before


def test_explicit_user_retry_is_a_separate_readonly_search(tmp_path):
    repo,before=failed_search(tmp_path)
    sent=[]
    def respond(request):
        payload=json.loads(request.content);sent.append(payload)
        if 'tools' in payload:
            body=search_response()
        else:
            context=json.loads(payload['input'])['context']
            assert context['previous_operation']=={'intent':'web_search','status':'failed'}
            body=search_response();body['output']=[body['output'][-1]]
            body['output'][0]['content'][0]['text']=AgentPlan(intent='web_search',search_query='Synthetic retry').model_dump_json()
        return httpx2.Response(200,json=body)
    with DefaultHttpxClient(transport=httpx2.MockTransport(respond)) as http:
        agent=SteelAgent(OpenAITransport(OpenAI(api_key='offline-test',http_client=http)),repo)
        reply=agent.handle(1,'Повтори поиск в интернете',event_id='explicit-retry')
    assert reply.status=='ok' and reply.calls==2 and len(sent)==2
    assert repo.load(1).model_dump_json()==before
