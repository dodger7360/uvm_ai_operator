"""Synthetic UX/search cases; no live model, user database or polling."""
from copy import deepcopy
import json
from unittest.mock import Mock

import httpx2
from openai import OpenAI, DefaultHttpxClient
import pytest

from database import Database
from steel_agent_v2 import SteelAgent, Repository
from steel_agent_v2.contracts import AgentPlan
from steel_agent_v2.presentation import render
from steel_agent_v2.transport import OpenAITransport, SearchUnavailable
from test_steel_agent_v2 import Fake, SOURCE, proposal, edit, set_op
from test_steel_agent_v2_wire_schema import sdk_plan, subset_issues


def state(tmp_path):
    repo=Repository(Database(tmp_path/'ux.sqlite3'))
    fake=Fake(proposal())
    agent=SteelAgent(fake,repo)
    agent.handle(1,SOURCE,event_id='create')
    return agent,repo,fake


def test_normal_view_hides_internal_structure_retains_facts(tmp_path):
    _,repo,_=state(tmp_path)
    request=repo.load(1)
    text=render(request)
    assert request.id not in text
    for value in ('[g1]','[i1]','[r1]','Основание предложения GPT','источник:'):
        assert value not in text
    for value in ('С245','Ст3','1,25','750','кг'):
        assert value in text
    assert 'версия' not in text.lower()
    assert 'взаимозаменяемость' not in text
    assert 'взаимозаменяемость' in render(request,'explanation')


@pytest.mark.parametrize('message', ['Дай заявку текстом','Подготовь для отправки поставщику',
    'Сделай список для копирования','Напиши заявку для менеджера','Сформируй короткий текст для пересылки'])
def test_forward_is_readonly_and_one_coordinator_call(tmp_path,message):
    agent,repo,fake=state(tmp_path)
    before=repo.load(1).model_dump_json()
    fake.plans=[dict(intent='show_request',view='forward')]
    count=len(fake.calls)
    reply=agent.handle(1,message,event_id='forward')
    assert reply.status=='ok' and reply.calls==1 and len(fake.calls)==count+1
    assert 'С245' in reply.text and 'Ст3' in reply.text and '0,75 т' in reply.text
    assert 'версия' not in reply.text.lower() and '[i1]' not in reply.text
    assert repo.load(1).model_dump_json()==before


@pytest.mark.parametrize('view', ['normalized','forward','explanation','custom'])
def test_new_views_actual_sdk_schema_and_internal_contract(view):
    raw=AgentPlan(intent='show_request',view=view,answer='| Позиция | Количество |').model_dump(mode='json')
    plan,sent=sdk_plan(raw)
    assert plan.view==view
    assert subset_issues(sent[0]['text']['format']['schema'])==[]


def test_custom_table_uses_single_plan_without_mutation(tmp_path):
    agent,repo,fake=state(tmp_path)
    before=repo.load(1).model_dump_json()
    fake.plans=[dict(intent='show_request',view='custom',answer='| Лист С245 | 1,25 т |')]
    result=agent.handle(1,'Сделай таблицей',event_id='table')
    assert result.text=='| Лист С245 | 1,25 т |' and result.calls==1
    assert repo.load(1).model_dump_json()==before


def search_response(kind='url'):
    sources=[{'type':'url','url':'https://example.org/weather'}]
    if kind=='feed':sources=[{'type':'api','name':'oai-weather','url':None}]
    return {'id':'resp_search','object':'response','created_at':0,'model':'gpt-6-luna','status':'completed',
        'output':[{'id':'ws1','type':'web_search_call','status':'completed',
                   'action':{'type':'search','query':'synthetic weather','sources':sources}},
                  {'id':'msg1','type':'message','role':'assistant','status':'completed',
                   'content':[{'type':'output_text','text':'Synthetic current weather','annotations':[]}]}]}


@pytest.mark.parametrize('kind',['url','feed'])
def test_real_sdk_reads_structural_search_sources(kind):
    sent=[]
    def respond(request):
        sent.append(json.loads(request.content))
        return httpx2.Response(200,json=search_response(kind))
    with DefaultHttpxClient(transport=httpx2.MockTransport(respond)) as http:
        transport=OpenAITransport(OpenAI(api_key='offline-test',http_client=http))
        result=transport.search('synthetic')
    assert result.text=='Synthetic current weather'
    if kind=='url':assert result.sources[0].url=='https://example.org/weather'
    else:assert result.providers==('oai-weather',) and not result.sources
    assert sent[0]['include']==['web_search_call.action.sources']
    assert sent[0]['reasoning']=={'effort':'high'}


def test_failed_search_diagnostic_persists_after_reopen(tmp_path):
    agent,repo,fake=state(tmp_path)
    before=repo.load(1).model_dump_json()
    fake.plans=[dict(intent='web_search',search_query='synthetic')]
    fake.search_result=SearchUnavailable('no_sources')
    failed=agent.handle(1,'Проверь в интернете',event_id='search')
    assert failed.status=='failed' and repo.load(1).model_dump_json()==before
    later=Fake(dict(intent='answer_question',view='search_status',answer='Invented network cause'))
    reopened=Repository(Database(tmp_path/'ux.sqlite3'))
    result=SteelAgent(later,reopened).handle(1,'Почему поиск не сработал?',event_id='why')
    assert 'Invented' not in result.text and 'источник' in result.text.lower()
    assert reopened.load(1).model_dump_json()==before
    assert later.calls[0][0]['last_search']


@pytest.mark.parametrize('damage', ['missing_tool','failed_tool','missing_sources','unsafe_url','empty_text','incomplete'])
def test_search_rejects_unverified_response_with_safe_reason(damage,caplog):
    response=search_response()
    if damage=='missing_tool':response['output'].pop(0)
    if damage=='failed_tool':response['output'][0]['status']='failed'
    if damage=='missing_sources':response['output'][0]['action']['sources']=[]
    if damage=='unsafe_url':response['output'][0]['action']['sources'][0]['url']='https://example.org/?api_key=secret'
    if damage=='empty_text':response['output'][1]['content'][0]['text']=''
    if damage=='incomplete':response['status']='incomplete'
    with DefaultHttpxClient(transport=httpx2.MockTransport(lambda _:httpx2.Response(200,json=response))) as http:
        transport=OpenAITransport(OpenAI(api_key='offline-test',http_client=http))
        with pytest.raises(SearchUnavailable):transport.search('private-query-not-for-logs')
    assert 'private-query-not-for-logs' not in caplog.text
    assert 'api_key=secret' not in caplog.text


@pytest.mark.parametrize('amount', ['9.8','5.000','0.0572','123456789.123456789'])
def test_presentation_decimal_and_city_without_service_notes(tmp_path,amount):
    from decimal import Decimal
    from steel_agent_v2.engine import RequestEngine
    source=f'Арматура А500С 12 мм ГОСТ 34028-2016 ТУ 1-2 — {amount} т, Екатеринбург\nСлужебная заметка'
    end=source.index('\n')
    plan=AgentPlan(intent='create_request',extraction=dict(
        records=[dict(id='s1',kind='product',start=0,end=end),dict(id='note1',kind='note',start=end+1,end=len(source))],
        items=[dict(id='i1',source_id='s1',label='Арматура А500С',quantity=amount,unit='т',quantity_text=amount,unit_text='т',
            characteristics=[dict(name=name,value=value,raw=value,source_ids=['s1']) for name,value in
                [('grade','А500С'),('size','12 мм'),('standard','ГОСТ 34028-2016'),('spec','ТУ 1-2'),('city','Екатеринбург')]])],
        groups=[dict(id='g1',label='Арматура',item_ids=['i1'],rationale='internal reason')]))
    request=RequestEngine().create(plan.extraction,source,'synthetic')
    before=request.model_dump_json()
    for view in ('normalized','forward','explanation'):
        text=render(request,view)
        assert 'Город: Екатеринбург' in text
        assert 'ГОСТ 34028-2016' in text and 'ТУ 1-2' in text and '12 мм' in text
        assert text.count('А500С')==1
        expected=format(Decimal(amount),'f').rstrip('0').rstrip('.') if '.' in amount else amount
        assert expected.replace('.',',')+' т' in text
        assert 'Служебная заметка' in text if view=='explanation' else 'Служебная заметка' not in text
    assert request.model_dump_json()==before


def test_display_after_mass_edit_undo_reopen_and_other_user(tmp_path):
    agent,repo,fake=state(tmp_path)
    first=repo.load(1)
    fake.plans=[edit(first,[set_op('i1',field='quantity',value='7.5',evidence='7.5'),
                            set_op('i2',field='quantity',value='7.5',evidence='7.5')])]
    changed=agent.handle(1,'7.5',event_id='edit')
    assert changed.status=='ok' and '7,5' in changed.text
    assert repo.load(1).revision==2
    assert repo.load(1).original_text==SOURCE
    fake.plans=[edit(repo.load(1),[dict(kind='undo')])]
    undone=agent.handle(1,'Отмени изменение',event_id='undo')
    assert undone.status=='ok' and '1,25' in undone.text and '0,75 т' in undone.text
    assert repo.load(1).revision==3
    reopened=Repository(Database(tmp_path/'ux.sqlite3'))
    assert reopened.version(1,first.id,2).changes
    before=reopened.load(1).model_dump_json()
    readonly=Fake(dict(intent='show_request',view='forward'),dict(intent='free_chat',answer='Привет'))
    other=SteelAgent(readonly,reopened)
    assert other.handle(2,'Дай заявку',event_id='other').text=='Активной заявки нет.'
    assert other.handle(1,'Привет',event_id='hello').text=='Привет'
    assert reopened.load(1).model_dump_json()==before


def test_unknown_units_questions_partial_and_split_do_not_lose_positions(tmp_path):
    from steel_agent_v2.engine import RequestEngine
    from telegram_operator import safe_chunks
    raw=proposal()
    raw['extraction']['items'][0]['quantity']=None
    raw['extraction']['items'][0]['unit']=None
    raw['extraction']['questions']=['Уточните количество и единицу первой позиции']
    raw['extraction']['groups']=[dict(id='g'+str(n),label=item['label'],item_ids=[item['id']],rationale='Synthetic')
                                  for n,item in enumerate(raw['extraction']['items'],1)]
    request=RequestEngine().create(AgentPlan.model_validate(raw).extraction,SOURCE,'synthetic')
    for view in ('forward','normalized','totals','explanation'):
        text=render(request,view)
        assert 'i1' not in text and 'g0' not in text
        if view!='totals':assert 'Уточните' in text and 'не уточнено' in text
        else:assert 'Известная часть' in text
    large='\n'.join(render(request) for _ in range(100))
    chunks=safe_chunks(large)
    assert len(chunks)>1 and all(len(chunk)<=3200 for chunk in chunks)
    assert sum(chunk.count('С245') for chunk in chunks)==large.count('С245')


@pytest.mark.parametrize('status',[400,401,403,404,429,500])
def test_search_api_metadata_is_safe_and_single_attempt(status,caplog):
    import logging
    attempts=[]
    def respond(request):
        attempts.append(request)
        return httpx2.Response(status,headers={'x-request-id':'req_safe_search'},json={'error':{
            'message':'PRIVATE_BODY_MUST_NOT_LEAK','type':'invalid_request_error',
            'code':'unsupported_parameter','param':'tools'}})
    with DefaultHttpxClient(transport=httpx2.MockTransport(respond)) as http:
        transport=OpenAITransport(OpenAI(api_key='offline-test',http_client=http))
        with caplog.at_level(logging.INFO,logger='operator_diagnostics'),pytest.raises(SearchUnavailable):
            transport.search('PRIVATE_QUERY_MUST_NOT_LEAK')
    assert len(attempts)==1
    assert transport.metrics[-1]['http_status']==status
    assert 'req_safe_search' in caplog.text and 'unsupported_parameter' in caplog.text
    assert 'PRIVATE_BODY_MUST_NOT_LEAK' not in caplog.text and 'PRIVATE_QUERY_MUST_NOT_LEAK' not in caplog.text


def test_feeds_readonly_search_and_explanation_isolated_by_user(tmp_path):
    from steel_agent_v2.transport import SearchAnswer
    agent,repo,fake=state(tmp_path)
    before=repo.load(1).model_dump_json()
    fake.plans=[dict(intent='web_search',search_query='synthetic')]
    fake.search_result=SearchAnswer('Synthetic weather',(),('oai-weather',))
    result=agent.handle(1,'Какая погода?',event_id='weather')
    assert result.status=='ok' and 'oai-weather' not in result.text and not result.sources
    assert result.search_providers==['oai-weather'] and 'ссылки не предоставлены' in result.text
    assert repo.load(1).model_dump_json()==before
    assert repo.last_search(2) is None


@pytest.mark.parametrize('damage',['invalid_output','unknown_feed','unsafe_feed','malformed_content'])
def test_search_malformed_and_unknown_providers_fail_closed(damage):
    response=search_response('feed')
    if damage=='invalid_output':response['output']='bad'
    if damage=='unknown_feed':response['output'][0]['action']['sources'][0]['name']='unverified-provider'
    if damage=='unsafe_feed':response['output'][0]['action']['sources'][0]['type']='untrusted'
    if damage=='malformed_content':response['output'][1]['content']=42
    client=Mock();client.with_options.return_value=client
    client.responses.create.return_value=response
    with pytest.raises(SearchUnavailable):OpenAITransport(client).search('synthetic')


@pytest.mark.parametrize('action',['open_page','find_in_page'])
def test_completed_page_action_url_is_verified_source(action):
    response=search_response()
    response['output'][0]['action']={'type':action,'url':'https://example.org/consulted','pattern':'synthetic'}
    with DefaultHttpxClient(transport=httpx2.MockTransport(lambda _:httpx2.Response(200,json=response))) as http:
        result=OpenAITransport(OpenAI(api_key='offline-test',http_client=http)).search('synthetic')
    assert result.sources[0].url=='https://example.org/consulted'


def test_hidden_display_numbers_match_actual_rows_when_groups_reordered(tmp_path):
    agent,repo,fake=state(tmp_path)
    request=repo.load(1)
    groups=[dict(id='b',label='Second',item_ids=['i2'],rationale='Synthetic'),
            dict(id='a',label='First',item_ids=['i1'],rationale='Synthetic')]
    fake.plans=[edit(request,[dict(kind='regroup',groups=groups)])]
    reply=agent.handle(1,'Раздели группы',event_id='regroup')
    assert '2. Лист Ст3' not in reply.text and '1. Лист С245' not in reply.text
    assert reply.text.index('Лист Ст3') < reply.text.index('Лист С245')
    fake.plans=[dict(intent='show_request')]
    agent.handle(1,'Покажи коротко',event_id='show')
    assert fake.calls[-1][0]['display_numbers']=={'1':'i2','2':'i1'}


def test_partial_forward_retains_partial_status(tmp_path):
    repo=Repository(Database(tmp_path/'partial.sqlite3'))
    raw=proposal();raw['extraction']['items'][0]['quantity']=None
    raw['extraction']['questions']=['Уточните количество i1']
    fake=Fake(raw,dict(intent='show_request',view='forward'))
    agent=SteelAgent(fake,repo)
    agent.handle(1,SOURCE,event_id='create')
    before=repo.load(1).model_dump_json()
    reply=agent.handle(1,'Дай текст',event_id='forward')
    assert reply.status=='partial' and 'требует уточнения' in reply.text
    assert 'i1' not in reply.text
    assert repo.load(1).model_dump_json()==before
