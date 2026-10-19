"""BR-007 on the agent/repository/outbox path. Synthetic data, no live services."""
from copy import deepcopy
import json
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from database import Database
from steel_agent_v2 import Repository, SteelAgent
from steel_agent_v2.contracts import AgentPlan, Reply, SourceLink, SourceCitation
from steel_agent_v2.queue import DurableQueue
from steel_agent_v2.search_sources import compact, merge_sources
from steel_agent_v2.telegram_format import payloads, html_chunks
from steel_agent_v2.transport import OpenAITransport, SearchAnswer, SearchUnavailable, INSTRUCTIONS
from test_steel_agent_v2 import Fake, SOURCE, proposal, edit, set_op
from test_steel_agent_v2_ux_search import search_response, sdk_plan
from test_steel_agent_v2_wire_schema import subset_issues
from test_steel_agent_v2_search_runtime import run_search
from test_steel_agent_v2_telegram_html import InspectHTML, visible, messages, stored
from test_steel_agent_v2_queue import rig, admit, Executor


def sites(count):
    return [SourceLink(url=f'https://example.org/source/{n}',title=f'Справочник {n}') for n in range(count)]


def links(reply):
    return [url for part in payloads(reply,1) for url in InspectHTML(part['text']).links]


def search(agent,fake,event='search',sources=None,text='Масса — 22,64 кг/м.',user=1):
    fake.plans.append(dict(intent='web_search',search_query='synthetic '+event))
    fake.search_result=SearchAnswer(text,tuple(sources if sources is not None else sites(10)))
    return agent.handle(user,'Проверь технические сведения',event_id=event)


def full(agent,fake,event='full',user=1,message='Покажи все источники'):
    fake.plans.append(dict(intent='answer_question',view='sources'))
    before=len(fake.search_calls)
    result=agent.handle(user,message,event_id=event)
    assert len(fake.search_calls)==before and result.calls==1
    return result


@pytest.mark.parametrize('count',[1,2,10])
def test_compact_and_full_lists_use_saved_reply_and_keep_all_metadata(tmp_path,count):
    repo=Repository(Database(tmp_path/'sources.sqlite3'));fake=Fake();agent=SteelAgent(fake,repo)
    before_schema=None
    with repo.database.connection() as c:before_schema=list(c.execute('SELECT name,sql FROM sqlite_master ORDER BY name'))
    result=search(agent,fake,sources=sites(count))
    assert result.status=='ok' and links(result)==[s.url for s in sites(count)[:2]]
    assert len(result.sources)==count and result.search_text=='Масса — 22,64 кг/м.'
    assert 'https://' not in result.text and visible([p['text'] for p in payloads(result,1)])==result.text
    assert not any(s.title in result.text for s in sites(count)[2:])
    saved=repo.source_search(1,None)
    assert saved['reply']==result and saved['event_id']=='search'
    all_sources=full(agent,fake)
    assert links(all_sources)==[s.url for s in sites(count)] and all_sources.sources==result.sources
    assert all_sources.source_event_id=='search' and repo.load(1) is None
    # Event receipts/reopen retain the exact presentation, with no second call.
    reopen=SteelAgent(Fake(),Repository(Database(tmp_path/'sources.sqlite3')))
    assert reopen.handle(1,'Покажи все источники',event_id='full')==all_sources
    with repo.database.connection() as c:
        assert list(c.execute('SELECT name,sql FROM sqlite_master ORDER BY name'))==before_schema
        assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'


def test_citations_rank_above_first_results_and_duplicates_keep_title_provenance(tmp_path):
    sources=sites(10)
    sources[8]=sources[8].model_copy(update={'origins':['url_citation'],'citations':[SourceCitation(start=0,end=4,text='Масса')]})
    sources[6]=sources[6].model_copy(update={'origins':['open_page']})
    duplicate=SourceLink(url=sources[8].url,title='Таблица массы',origins=['url_citation'],titles=['Название страницы'])
    repo=Repository(Database(tmp_path/'rank.sqlite3'));fake=Fake();agent=SteelAgent(fake,repo)
    result=search(agent,fake,sources=[*sources,duplicate])
    assert links(result)==[sources[8].url,sources[6].url] and len(result.sources)==10
    merged=result.sources[8]
    assert set(merged.titles)=={'Справочник 8','Таблица массы','Название страницы'}
    assert merged.citations==sources[8].citations
    assert result.text.startswith('Масса — 22,64 кг/м.')  # annotation spans are not blindly erased


@pytest.mark.parametrize('style',['markdown','html','bare'])
def test_inline_facts_and_separate_source_lists_have_at_most_two_links(style):
    sources=sites(10)
    def ref(s,label=None):
        name=label or s.title
        if style=='markdown':return f'[{name}]({s.url})'
        if style=='html':return f'<a href="{s.url}">{name}</a>'
        return name+' — '+s.url
    text='Толщина '+ref(sources[7],'8 мм')+'; количество 1,125 т.\n\nИсточники:\n'+ '\n'.join(ref(s) for s in sources)
    all_sources,display,runs=compact(text,sources)
    actual=visible(html_chunks(runs))
    assert '8 мм' in actual and '1,125 т' in actual and len(all_sources)==10
    assert len([u for part in html_chunks(runs) for u in InspectHTML(part).links])==2
    assert 'https://' not in display and display.count('Источники:')==1
    # Only selected names survive the explicit bibliography; the factual anchor remains.
    assert 'Справочник 9' not in display and 'Справочник 0' in display


def test_unknown_inline_urls_never_become_links_and_factual_anchor_is_not_discarded():
    source=SourceLink(url='https://example.org/table',title='Масса 22,64 кг/м')
    text=f'[{source.title}]({source.url})\n[Чужое утверждение](https://unverified.example/info)\n'+source.url
    _,display,runs=compact(text,[source])
    assert source.title in display and 'Чужое утверждение' in display
    assert [u for p in html_chunks(runs) for u in InspectHTML(p).links]==[source.url]
    assert 'unverified.example' not in display


def test_nonnumeric_claim_anchor_is_preserved_even_when_equal_to_source_title():
    source=SourceLink(url='https://example.org/table',title='Характеристики требуют проверки')
    _,display,_=compact(f'[{source.title}]({source.url})',[source])
    assert display.startswith(source.title+'\n\nИсточники:')


def test_api_markers_and_only_validated_reference_spans_are_removed():
    marker='\ue200cite\ue202turn0search0\ue201'
    text='Масса — 22,64 кг/м.[1]'+marker+'\nОбозначение [7] сохранено.'
    start=text.index('[1]');source=sites(1)[0].model_copy(update={
        'origins':['url_citation'],'citations':[SourceCitation(start=start,end=start+3,text='[1]')]})
    _,display,_=compact(text,[source])
    assert '[1]' not in display and marker not in display and '[7]' in display and '22,64 кг/м' in display
    # Inconsistent offsets cannot delete the claim or unverified bracket data.
    source=source.model_copy(update={'citations':[SourceCitation(start=0,end=3,text='[7]')]})
    assert '[7]' in compact(text,[source])[1]


def test_literal_product_data_and_markup_are_protected_on_real_agent_path(tmp_path):
    repo=Repository(Database(tmp_path/'literal.sqlite3'));fake=Fake(proposal());agent=SteelAgent(fake,repo)
    agent.handle(1,SOURCE,event_id='create');before=repo.load(1).model_dump_json()
    raw=SOURCE.splitlines()[0]
    result=search(agent,fake,text='**Проверка:** '+raw+'\nОбозначение **A** <X> & 89*4',sources=sites(3))
    assert raw in result.text and '89*4' in result.text and '<X>' in result.text
    assert repo.load(1).model_dump_json()==before
    readonly=full(agent,fake)
    assert repo.load(1).model_dump_json()==before and len(readonly.sources)==3
    assert repo.version(1,repo.load(1).id,2) is None


@pytest.mark.parametrize('message',[
    'Покажи все источники','Дай полный список ссылок',
    'Откуда ты взял эту информацию? Покажи сайты','Покажи все источники последнего поиска'])
def test_natural_source_requests_use_existing_coordinator_and_no_search(tmp_path,message):
    repo=Repository(Database(tmp_path/'natural.sqlite3'));fake=Fake();agent=SteelAgent(fake,repo)
    search(agent,fake);count=len(fake.calls)
    result=full(agent,fake,message=message)
    assert len(result.sources)==10 and len(fake.calls)==count+1 and len(fake.search_calls)==1
    assert fake.calls[-1][0]['saved_search_sources']=={'available':True,'count':10}
    assert 'view=sources' in INSTRUCTIONS


def test_sources_view_is_in_strict_sdk_schema_and_cannot_mutate_or_search():
    plan=AgentPlan(intent='answer_question',view='sources')
    parsed,sent=sdk_plan(plan.model_dump(mode='json'))
    assert parsed.view=='sources' and not subset_issues(sent[0]['text']['format']['schema'])
    for fields in [dict(intent='web_search',search_query='new'),dict(intent='edit_request'),dict(intent='create_request')]:
        with pytest.raises(ValidationError):AgentPlan(view='sources',**fields)


def test_absent_and_failed_latest_search_never_substitute_older_sources(tmp_path):
    repo=Repository(Database(tmp_path/'failed.sqlite3'));fake=Fake();agent=SteelAgent(fake,repo)
    assert not full(agent,fake).sources
    search(agent,fake,event='first',sources=sites(2))
    second=search(agent,fake,event='second',sources=sites(4))
    assert full(agent,fake,event='full2').sources==second.sources
    fake.plans.append(dict(intent='web_search',search_query='broken'))
    fake.search_result=SearchUnavailable('no_sources')
    assert agent.handle(1,'Новый поиск',event_id='bad').status=='failed'
    assert not full(agent,fake,event='after-bad').sources
    assert 'Нет сохранённого списка' in full(agent,fake,event='after-bad2').text


def test_provider_only_latest_search_has_no_fake_sites_or_old_source_fallback(tmp_path):
    repo=Repository(Database(tmp_path/'feed.sqlite3'));fake=Fake();agent=SteelAgent(fake,repo)
    search(agent,fake)
    fake.plans.append(dict(intent='web_search',search_query='weather'))
    fake.search_result=SearchAnswer('Сведения получены.\n\nИсточники:\nСервис web_search: oai-weather',(),('oai-weather',))
    result=agent.handle(1,'Погода',event_id='weather')
    assert result.search_providers==['oai-weather'] and 'oai-weather' not in result.text
    assert 'Сведения получены.' in result.text and 'Источники:' not in result.text
    assert not full(agent,fake).sources


def test_sources_are_scoped_to_user_request_and_revision(tmp_path):
    repo=Repository(Database(tmp_path/'scope.sqlite3'));fake=Fake();agent=SteelAgent(fake,repo)
    search(agent,fake,event='global')
    assert not full(agent,fake,event='other',user=2).sources
    fake.plans.append(proposal());agent.handle(1,SOURCE,event_id='create')
    assert not full(agent,fake,event='new-request').sources
    searched=search(agent,fake,event='current')
    old=repo.load(1);fake.plans.append(edit(old,[set_op(value='Ст3',evidence='Ст3')]))
    agent.handle(1,'Поставь Ст3',event_id='edit')
    assert repo.load(1).revision==old.revision+1 and not full(agent,fake,event='changed').sources
    assert searched.sources and repo.load(1).original_text==SOURCE
    search(agent,fake,event='updated')
    fake.plans.append(proposal());agent.handle(1,SOURCE,event_id='replacement')
    assert not full(agent,fake,event='replacement-view').sources


def test_transport_preserves_annotation_offsets_titles_actions_and_all_urls():
    response=search_response();action=response['output'][0]['action']
    action['sources']=[dict(type='url',url=s.url,title=s.title) for s in sites(10)]
    cited=sites(10)[8].url
    text='😀 Масса — 22,64 кг/м.[1]';start=text.index('[1]')
    part=response['output'][-1]['content'][0];part['text']=text
    part['annotations']=[dict(type='url_citation',url=cited,title='Таблица массы',start_index=start,end_index=start+3),
                         dict(type='url_citation',url=sites(10)[7].url,title='Другое название',start_index=-1,end_index=999)]
    response['output'].insert(1,dict(type='web_search_call',status='completed',action=dict(type='open_page',url=cited)))
    client=Mock();client.with_options.return_value=client;client.responses.create.return_value=response
    result=OpenAITransport(client).search('synthetic')
    assert len(result.sources)==10
    saved=next(s for s in result.sources if s.url==cited)
    assert saved.title=='Таблица массы' and set(saved.origins)=={'search','open_page','url_citation'}
    assert set(saved.titles)=={'Справочник 8','Таблица массы'}
    assert saved.citations==[SourceCitation(start=start,end=start+3,text='[1]')]
    assert not result.sources[7].citations
    all_sources,display,runs=compact(result.text,result.sources)
    assert '[1]' not in display and '22,64 кг/м' in display
    assert [u for p in html_chunks(runs) for u in InspectHTML(p).links]==[cited,sites(10)[7].url]
    assert len(all_sources)==10 and client.responses.create.call_count==1


def test_old_receipts_serialize_identically_and_merge_preserves_metadata():
    old=Reply(text='Прежний ответ',status='ok',intent='web_search',sources=sites(1))
    encoded=old.model_dump_json();payload=json.loads(encoded)
    assert payload['sources']==[dict(url=sites(1)[0].url,title=sites(1)[0].title)]
    assert not {'search_text','search_query','search_providers','source_event_id'} & payload.keys()
    assert Reply.model_validate_json(encoded).model_dump_json()==encoded
    newer=sites(1)[0].model_copy(update={'title':'Название из цитирования','origins':['url_citation']})
    merged=merge_sources([old.sources[0],newer])
    assert len(merged)==1 and merged[0].title==newer.title and old.sources[0].title in merged[0].titles


def test_real_sdk_annotations_and_legacy_saved_search_are_read_without_migration(tmp_path,caplog):
    response=search_response();part=response['output'][-1]['content'][0]
    part['text']='Подтвердите актуальность.[1]';start=part['text'].index('[1]')
    part['annotations']=[dict(type='url_citation',url=sites(1)[0].url,title='Таблица & нормы',start_index=start,end_index=start+3)]
    result,_=run_search(response,caplog)
    assert not isinstance(result,Exception) and result.sources[-1].citations[0].text=='[1]'
    repo=Repository(Database(tmp_path/'old.sqlite3'))
    old=Reply(text='Исторический поиск',status='ok',intent='web_search',sources=sites(4))
    _,current,sequence=repo.begin(1,'old','Тестовый запрос');repo.finish(1,'old',sequence,'Тестовый запрос',old)
    assert current is None and repo.source_search(1,None)['reply']==old
    fake=Fake();agent=SteelAgent(fake,repo)
    assert len(full(agent,fake).sources)==4 and not fake.search_calls


@pytest.mark.parametrize('title',['','x'*130,'https://example.org/title','Название < & " страницы'])
def test_short_label_is_saved_title_or_actual_hostname_and_html_is_escaped(title):
    source=SourceLink(url='https://example.org/reference',title=title)
    _,text,runs=compact('Количество — 1,125 т.',[source])
    expected=title if title and len(title)<=64 and not title.startswith('https://') else 'example.org'
    assert text.endswith('Источники: '+expected)
    assert visible(html_chunks(runs))==text
    assert [u for part in html_chunks(runs) for u in InspectHTML(part).links]==[source.url]


def test_full_sources_survive_bounded_chat_history_and_do_not_update_display_map(tmp_path):
    repo=Repository(Database(tmp_path/'bounded.sqlite3'));fake=Fake(proposal());agent=SteelAgent(fake,repo)
    agent.handle(1,SOURCE,event_id='create');before=repo.load(1);mapping=repo.last_display(1,before)
    search(agent,fake)
    for n in range(22):
        fake.plans.append(dict(intent='free_chat',answer='Короткий ответ'))
        agent.handle(1,'Свободный вопрос',event_id='chat'+str(n))
    result=full(agent,fake)
    assert len(result.sources)==10 and result.source_event_id=='search'
    assert repo.load(1)==before and repo.last_display(1,before)==mapping


def test_unsafe_custom_search_results_fail_closed_without_exposing_targets(tmp_path):
    repo=Repository(Database(tmp_path/'unsafe.sqlite3'));fake=Fake();agent=SteelAgent(fake,repo)
    for n,url in enumerate(['javascript:alert(1)','https://user:password@example.org/x','https://example.org/?token=synthetic']):
        result=search(agent,fake,event='bad'+str(n),sources=[SourceLink(url=url,title='Unsafe')])
        assert result.status=='failed' and not result.sources and not links(result) and url not in result.text
    assert not full(agent,fake).sources


def test_long_full_sources_commit_restart_delivery_and_event_dedup(rig,monkeypatch):
    sources=[s.model_copy(update={'title':('Сайт < & " 🧱 '+str(n)+' ')*3}) for n,s in enumerate(sites(110))]
    fake=Fake(dict(intent='web_search',search_query='synthetic'),dict(intent='answer_question',view='sources'))
    fake.search_result=SearchAnswer('Достоверная часть: количество 1,125 т.',sources)
    executor=Executor(rig.queue,rig.access,lambda:rig.api,lambda:fake)
    admit(rig,100,text='Проверь');executor.execute(rig.queue.claim())
    repo=Repository(rig.queue.database);assert len(repo.source_search(1,None)['reply'].sources)==110
    monkeypatch.setattr(rig.queue,'deliver',lambda *args:None)
    admit(rig,101,text='Покажи все источники');executor.execute(rig.queue.claim())
    rows=stored(rig.queue,101);saved=[json.loads(row['payload']) for row in rows]
    assert len(saved)>1 and len(fake.search_calls)==1 and len(fake.calls)==2
    assert [u for part in saved for u in InspectHTML(part['text']).links]==[s.url for s in sources]
    assert 'Все источники' in visible([p['text'] for p in saved])
    before=deepcopy(messages(rig));reopen=DurableQueue(rig.queue.database);reopen.recover()
    reopen.deliver(101,rig.api,rig.access)
    assert messages(rig)==before+saved and reopen.job(101)['state']=='delivered'
    assert admit(rig,101,text='Покажи все источники')==reopen.cursor() and reopen.claim() is None
    assert len(fake.search_calls)==1 and messages(rig)==before+saved


@pytest.mark.parametrize('state',['delivery_pending','delivery_unknown','denied'])
def test_search_sources_require_confirmed_delivery_and_unknown_is_not_replayed(rig,monkeypatch,state):
    fake=Fake(dict(intent='web_search',search_query='synthetic'))
    fake.search_result=SearchAnswer('Сведения',sites(4))
    monkeypatch.setattr(rig.queue,'deliver',lambda *args:None)
    admit(rig,100,text='Проверь');Executor(rig.queue,rig.access,lambda:rig.api,lambda:fake).execute(rig.queue.claim())
    if state=='delivery_unknown':
        rig.api.fail=True
        with pytest.raises(TimeoutError):DurableQueue(rig.queue.database).deliver(100,rig.api,rig.access)
    elif state=='denied':
        with rig.queue.database.connection() as c:c.execute("UPDATE steel_v2_inbox SET state='denied' WHERE update_id=100")
    assert Repository(rig.queue.database).source_search(1,None) is None
    if state=='delivery_unknown':
        before=deepcopy(messages(rig));reopen=DurableQueue(rig.queue.database);reopen.recover()
        reopen.deliver(100,rig.api,rig.access)
        assert messages(rig)==before and len(fake.search_calls)==1 and reopen.claim() is None


def test_full_multipart_unknown_does_not_repeat_search_send_or_lose_sources(rig,monkeypatch):
    sources=[s.model_copy(update={'title':('Справочник '+str(n)+' ')*4}) for n,s in enumerate(sites(110))]
    fake=Fake(dict(intent='web_search',search_query='synthetic'),dict(intent='answer_question',view='sources'))
    fake.search_result=SearchAnswer('Фактический ответ',sources)
    executor=Executor(rig.queue,rig.access,lambda:rig.api,lambda:fake)
    admit(rig,100,text='Проверь');executor.execute(rig.queue.claim())
    original_call=rig.api.call;start=len(messages(rig))
    def uncertain(method,**payload):
        result=original_call(method,**payload)
        if method=='sendMessage' and len(messages(rig))==start+2:raise TimeoutError('fake lost receipt')
        return result
    monkeypatch.setattr(rig.api,'call',uncertain)
    admit(rig,101,text='Покажи все источники');executor.execute(rig.queue.claim())
    rows=stored(rig.queue,101)
    assert rows[0]['state']=='delivered' and rows[1]['state']=='delivery_unknown'
    assert rig.queue.job(101)['state']=='delivery_unknown'
    with rig.queue.database.connection() as c:
        reply=Reply.model_validate_json(c.execute("SELECT reply FROM steel_v2_turn WHERE event_id='telegram:101'").fetchone()[0])
    assert len(reply.sources)==110 and reply.source_event_id=='telegram:100'
    before=deepcopy(messages(rig));reopen=DurableQueue(rig.queue.database);reopen.recover()
    reopen.deliver(101,rig.api,rig.access)
    assert before==messages(rig) and len(fake.search_calls)==1 and len(fake.calls)==2 and reopen.claim() is None
