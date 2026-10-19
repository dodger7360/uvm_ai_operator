"""BR-006: inspect real durable payloads with fake Telegram and temporary SQLite."""
from copy import deepcopy
from html.parser import HTMLParser
from io import BytesIO
import json
import re
from unittest.mock import patch

import pytest

from database import Database
from steel_agent_v2 import Repository, RequestEngine, SteelAgent
from steel_agent_v2.contracts import Reply, PresentationRun, SourceLink
from steel_agent_v2.queue import DurableQueue
from steel_agent_v2.telegram_format import prose, literal, html_chunks, payloads, safe_link
from steel_agent_v2.transport import SearchAnswer
from telegram_bot import TelegramAPI
from test_steel_agent_v2 import Fake, proposal, SOURCE
from test_steel_agent_v2_027_presentation import sample
from test_steel_agent_v2_display_sorting import APPROVED, APPROVED_ORDER, approved_plan
from test_steel_agent_v2_queue import rig, admit, Executor


class InspectHTML(HTMLParser):
    def __init__(self,value):
        super().__init__(convert_charrefs=True);self.stack=[];self.text=[];self.tags=[];self.links=[]
        self.feed(value);self.close();assert self.stack==[]
    def handle_starttag(self,tag,attrs):
        assert tag in {'b','i','code','pre','a'}
        if tag=='a':
            assert len(attrs)==1 and attrs[0][0]=='href' and safe_link(attrs[0][1])
            self.links.append(attrs[0][1])
        else:assert attrs==[]
        self.stack.append(tag);self.tags.append(tag)
    def handle_endtag(self,tag):assert self.stack.pop()==tag
    def handle_data(self,data):self.text.append(data)
    def handle_comment(self,data):raise AssertionError('raw comment')


def visible(parts,limit=3200):
    result=[]
    for part in parts:
        parsed=InspectHTML(part);text=''.join(parsed.text)
        assert 0<len(text.encode('utf-16-le'))//2<=limit
        result.append(text)
    return ''.join(result)


def messages(rig):return [payload for method,payload in rig.api.calls if method=='sendMessage']


def stored(queue,number):
    with queue.database.connection() as c:
        return [dict(row) for row in c.execute('SELECT * FROM steel_v2_outbox WHERE update_id=? ORDER BY part',(number,))]


def test_approved_seven_sheets_keep_text_ids_quantities_and_native_payload(rig):
    fake=Fake(approved_plan());admit(rig,100,text=APPROVED)
    Executor(rig.queue,rig.access,lambda:rig.api,lambda:fake).execute(rig.queue.claim())
    repo=Repository(rig.queue.database);state=repo.load(1);sent=messages(rig)
    assert sent and all(part['parse_mode']=='HTML' for part in sent)
    text=visible([part['text'] for part in sent])
    assert len(text.splitlines())==7 and text.count(' — 40 т')==7
    assert all(value not in text for value in ('ГОСТ','Группа ','Количество:','**','###'))
    assert not any(re.match(r'^\d+\.',line) for line in text.splitlines())
    assert repo.last_display(1,state).item_ids==APPROVED_ORDER
    assert all(i.quantity==40 and i.unit=='т' for i in state.items) and state.original_text==APPROVED
    before=(len(fake.calls),len(sent));assert admit(rig,100,text=APPROVED)==rig.queue.cursor()
    assert rig.queue.claim() is None and (len(fake.calls),len(messages(rig)))==before
    assert all(json.loads(row['payload'])==sent[n] for n,row in enumerate(stored(rig.queue,100)))


def test_mixed_items_and_literal_markup_characters_are_never_cleaned(tmp_path):
    rows=[('Лист * < > & " спец',[('grade','Ст3','Ст3'),('note','<b>данные</b> **literal**','<b>данные</b> **literal**')],'1.125','т'),
          ('Труба',[('size','89*4','89*4'),('note',r'C:\Temp\spec\* &amp;',r'C:\Temp\spec\* &amp;')],'240','м'),
          ('Уголок',[('size','50×50×5','50×50×5')],'2.500','т')]
    source,plan,_=sample(rows);repo=Repository(Database(tmp_path/'literal.sqlite3'))
    reply=SteelAgent(Fake(plan),repo).handle(1,source,event_id='literal')
    html=[p['text'] for p in payloads(reply,1)]
    assert visible(html)==reply.text
    assert all(InspectHTML(part).tags==[] for part in html)
    assert '&lt;b&gt;данные&lt;/b&gt;' in ''.join(html) and '&amp;amp;' in ''.join(html)
    assert '89*4' in reply.text and r'C:\Temp\spec\*' in reply.text
    assert repo.load(1).original_text==source and repo.load(1).items==repo.load(1).originals


@pytest.mark.parametrize('answer,expected',[
    ('### Проверка\n**Готово**: без выдуманных данных.','Проверка\nГотово: без выдуманных данных.'),
    (r'### Размер\*\n'.replace('\\n','\n')+'**Толщина \\> 8 мм**','Размер*\nТолщина > 8 мм'),
    ('<b>Готово &amp; проверено</b>','Готово & проверено'),
    ('<h3>Проверка</h3>\nГотово','Проверка\nГотово'),
    ('<strong onclick="bad()">Готово</strong> <em>текст</em>','Готово текст'),
    ('[Сайт](https://example.com/price?q=1&city=2)','Сайт'),
    ('<a href="https://example.com/?q=1&amp;city=2" onclick="bad()">Сайт</a>','Сайт'),
    ('`89*4 & <10`','89*4 & <10'),
    ('```text\n89*4 & <10\n```','89*4 & <10\n'),
])
def test_known_model_markup_is_rebuilt_as_safe_native_html(answer,expected):
    parts=html_chunks(prose(answer))
    assert visible(parts)==expected
    assert any(InspectHTML(part).tags for part in parts)
    assert '**' not in ''.join(parts) and 'onclick' not in ''.join(parts)


@pytest.mark.parametrize('text',[
    '2*3 и 4**2, _индекс_, **незакрытая разметка',
    '<spec dimension="8*1500">данные</spec>',
    '<script>alert("x")</script>',
    '<b>сломано<i>вложение</b>',
    '<?xml version="1.0"?> данные',
    '<!-- комментарий --> текст',
    r'C:\Temp\spec\* и \\server\share',
])
def test_unsupported_or_ambiguous_syntax_is_safe_literal_without_data_loss(text):
    parts=html_chunks(prose(text));assert visible(parts)==text
    if '<' in text:assert all(InspectHTML(part).tags==[] for part in parts)


@pytest.mark.parametrize('url',[
    'javascript:alert(1)','data:text/html,test','file:///C:/secret','tg://user?id=1',
    'https://user:password@example.com/x','https://example.com/?token=not-a-secret',
    'https://example.com/\x00bad','https://example.com/\\bad','https://example.com/"bad',
])
def test_untrusted_targets_never_become_clickable_links(url):
    assert not safe_link(url)
    md=html_chunks(prose('[Источник]('+url+')'))
    html=html_chunks(prose('<a href="'+url.replace('"','&quot;')+'">Источник</a>'))
    for parts in (md,html):
        assert visible(parts)=='Источник'
        assert all(not InspectHTML(part).links for part in parts)


def test_source_protection_survives_wrappers_entities_and_links():
    raw='Сталь **А** <b>буквально</b> &amp; 2*3'
    answer='**Проверено:** '+raw
    result=html_chunks(prose(answer,[raw]))
    assert visible(result)=='Проверено: '+raw
    assert InspectHTML(result[0]).tags==['b']
    url='https://example.com/spec?a=1&b=2'
    result=html_chunks(prose('[Источник]('+url+')',[url]))
    assert visible(result)=='Источник' and InspectHTML(result[0]).links==[url]
    literal_data='### Маркировка **A** \\* <b> &amp;'
    assert visible(html_chunks(prose(literal_data,[literal_data])))==literal_data


def test_decoded_entities_are_data_not_a_second_markup_language():
    text='<b>A&#38;B</b> &#42;&#42;буквально&#42;&#42; &lt;spec&gt;'
    parts=html_chunks(prose(text))
    assert visible(parts)=='A&B **буквально** <spec>'
    assert InspectHTML(parts[0]).tags==['b']
    # A second pass through serialization does not unescape or parse stored runs.
    runs=prose('<b>&amp;lt;</b>')
    restored=[PresentationRun.model_validate(row) for row in json.loads(json.dumps([r.model_dump() for r in runs]))]
    assert visible(html_chunks(restored))=='&lt;' and html_chunks(restored)==['<b>&amp;lt;</b>']


@pytest.mark.parametrize('limit',[7,16,3200,4096])
def test_long_links_entities_and_lines_have_independent_valid_chunks(limit):
    text=('A<&> " 🧱 строка\n'*80)+('длиннаястрока&😀'*700)
    runs=[PresentationRun(text=text,marks=['b','i'],url='https://example.com/?a=1&b=2')]
    parts=html_chunks(runs,limit)
    assert len(parts)>1 and visible(parts,limit)==text
    assert all(InspectHTML(part).links==['https://example.com/?a=1&b=2'] for part in parts)
    assert all(part.endswith('</a>') and part.startswith('<a href=') for part in parts)


def test_unicode_emoji_and_combining_sequences_are_not_split():
    clusters=['👩🏽‍🔧','🇷🇺','е\u0301','😀','👨‍👩‍👧‍👦']
    text=''.join(clusters)*60
    parts=html_chunks(literal(text),limit=16)
    assert visible(parts,16)==text
    for part in parts:
        text_part=''.join(InspectHTML(part).text)
        assert not text_part.startswith(('\u0301','\u200d','🏽')) and not text_part.endswith('\u200d')
        # A cut is at a known complete cluster boundary, not within a family/flag.
        while text_part:
            found=next((c for c in clusters if text_part.startswith(c)),None)
            assert found is not None;text_part=text_part[len(found):]


def test_pathological_combining_sequence_retains_all_code_points():
    text='а'+'\u0301'*7000
    assert visible(html_chunks(literal(text)))==text


def test_line_preference_does_not_overflow_on_a_large_following_emoji_cluster():
    text='\n'+'a'*10+'👨‍👩‍👧‍👦'
    parts=html_chunks(literal(text),limit=16)
    assert visible(parts,16)==text and parts==['\n','a'*10,'👨‍👩‍👧‍👦']


@pytest.mark.parametrize('view',['explanation','custom'])
def test_explanations_and_custom_prose_keep_literal_rows_and_display_map(tmp_path,view):
    source,plan,_=sample([('Лист',[('size','8*1500*6000','8*1500*6000')],'1.125','т'),
                          ('Труба',[('size','89*4','89*4')],'240','м')])
    plan['extraction']['groups'][0]['rationale']='**Различия сохранены**'
    repo=Repository(Database(tmp_path/'views.sqlite3'));fake=Fake(plan);agent=SteelAgent(fake,repo)
    first=agent.handle(1,source,event_id='create');old=repo.load(1);mapping=repo.last_display(1,old)
    answer=dict(intent='show_request',view=view)
    if view=='custom':answer['answer']='### Пояснение\n**Различия сохранены**\n'+first.text
    fake.plans.append(answer);reply=agent.handle(1,'Поясни',event_id='view')
    text=visible([p['text'] for p in payloads(reply,1)])
    assert 'Различия сохранены' in text and '**Различия сохранены**' not in text
    assert '8*1500*6000' in text and '89*4' in text and '1,125 т' in text
    assert repo.load(1)==old and repo.last_display(1,old)==mapping


def test_errors_clarification_original_and_reopen_keep_literals(tmp_path):
    source,plan,_=sample([('Труба',[('size','89*4 <8 & "','89*4 <8 & "')],'240','м')])
    repo=Repository(Database(tmp_path/'reopen.sqlite3'));fake=Fake(plan);agent=SteelAgent(fake,repo)
    agent.handle(1,source,event_id='create');old=repo.load(1)
    fake.plans.append(dict(intent='clarify',answer='### Уточните\n**Марка:** '+source))
    clarified=agent.handle(1,'Как уточнить?',event_id='clarify')
    assert visible([p['text'] for p in payloads(clarified,1)])=='Уточните\nМарка: '+source
    fake.plans.append(dict(intent='show_request',view='original'))
    original=agent.handle(1,'Оригинал',event_id='original')
    assert visible([p['text'] for p in payloads(original,1)])==original.text
    repo=Repository(Database(tmp_path/'reopen.sqlite3'));agent=SteelAgent(Fake(),repo)
    cached=agent.handle(1,'Как уточнить?',event_id='clarify')
    assert cached==clarified and payloads(cached,1)==payloads(clarified,1) and repo.load(1)==old
    # Known model structure errors: private diagnostics do not become HTML or model data.
    fake=Fake({'intent':'create_request'},{'intent':'create_request'})
    failed=SteelAgent(fake,repo).handle(1,'<draft> * & \\*',event_id='bad')
    assert failed.status=='failed' and visible([p['text'] for p in payloads(failed,1)])==failed.text
    assert repo.load(1)==old


def test_search_keeps_all_sources_but_renders_short_checked_html_links(tmp_path):
    urls=['https://example.com/one','https://example.org/two','https://example.net/three']
    sources=[SourceLink(url=url,title='Сайт '+str(n)) for n,url in enumerate(urls,1)]
    fake=Fake(dict(intent='web_search',search_query='synthetic'))
    fake.search_result=SearchAnswer('### Проверка\n**Уточните актуальность**',sources)
    repo=Repository(Database(tmp_path/'search.sqlite3'))
    reply=SteelAgent(fake,repo).handle(1,'Проверь сведения',event_id='search')
    parts=[p['text'] for p in payloads(reply,1)];text=visible(parts)
    assert len(fake.calls)==1 and len(fake.search_calls)==1 and reply.sources==sources
    assert all(url not in text for url in urls) and 'Сайт 3' in text
    assert [link for part in parts for link in InspectHTML(part).links]==urls
    assert 'Уточните актуальность' in text and '**' not in text and '###' not in text


def test_ready_native_multipart_outbox_survives_restart_without_reformatting(rig,monkeypatch):
    answer='### Сведения\n'+'**Строка & <8**\n'*600
    fake=Fake(dict(intent='free_chat',answer=answer));admit(rig,200,text='Расскажи')
    monkeypatch.setattr(rig.queue,'deliver',lambda *args:None)
    Executor(rig.queue,rig.access,lambda:rig.api,lambda:fake).execute(rig.queue.claim())
    rows=stored(rig.queue,200);saved=[json.loads(row['payload']) for row in rows]
    assert len(saved)>1 and not messages(rig)
    assert all(p['parse_mode']=='HTML' and p['chat_id']==1 for p in saved)
    expected='Сведения\n'+'Строка & <8\n'*600
    assert visible([p['text'] for p in saved])==expected
    reopened=DurableQueue(rig.queue.database);reopened.recover()
    reopened.deliver(200,rig.api,rig.access)
    assert messages(rig)==saved and reopened.job(200)['state']=='delivered'
    assert reopened.delivery_candidates()==[] and admit(rig,200,text='Расскажи')==reopened.cursor()
    assert reopened.claim() is None and messages(rig)==saved and len(fake.calls)==1


def test_unknown_second_part_preserves_receipts_and_never_replays(rig,monkeypatch):
    fake=Fake(dict(intent='free_chat',answer='**Длинный ответ**\n'+'текст & '*1600))
    original_call=rig.api.call
    def uncertain(method,**payload):
        result=original_call(method,**payload)
        if method=='sendMessage' and len(messages(rig))==2:raise TimeoutError('fake lost receipt')
        return result
    monkeypatch.setattr(rig.api,'call',uncertain);admit(rig,201,text='Расскажи')
    Executor(rig.queue,rig.access,lambda:rig.api,lambda:fake).execute(rig.queue.claim())
    rows=stored(rig.queue,201)
    assert [r['state'] for r in rows][:3]==['delivered','delivery_unknown','delivery_pending']
    assert rows[0]['message_id'] is not None and rig.queue.job(201)['state']=='delivery_unknown'
    before=deepcopy(messages(rig));reopened=DurableQueue(rig.queue.database);reopened.recover()
    reopened.deliver(201,rig.api,rig.access)
    assert messages(rig)==before and len(fake.calls)==1 and reopened.claim() is None


def test_old_outbox_payload_without_parse_mode_is_delivered_unchanged(rig,monkeypatch):
    fake=Fake(dict(intent='free_chat',answer='old text'));admit(rig,202,text='Расскажи')
    monkeypatch.setattr(rig.queue,'deliver',lambda *args:None)
    Executor(rig.queue,rig.access,lambda:rig.api,lambda:fake).execute(rig.queue.claim())
    old=dict(chat_id=1,text='<b>historical literal</b> **unchanged** &')
    with rig.queue.write() as c:c.execute('UPDATE steel_v2_outbox SET payload=? WHERE update_id=202',(json.dumps(old),))
    reopened=DurableQueue(rig.queue.database);reopened.recover();reopened.deliver(202,rig.api,rig.access)
    assert messages(rig)==[old] and 'parse_mode' not in messages(rig)[0] and len(fake.calls)==1


def test_snapshot_history_and_native_outbox_commit_remain_atomic(rig):
    admit(rig,203,text=APPROVED);job=rig.queue.claim();base=rig.queue.finish_hook(203)
    def fail(c,*args):base(c,*args);raise RuntimeError('fake transaction fault')
    repo=Repository(rig.queue.database,finish_hook=fail)
    _,_,sequence=repo.begin(1,'telegram:203',APPROVED)
    state=RequestEngine().create(approved_plan_to_extraction(),APPROVED,'atomic')
    reply=Reply(text='safe & text',status='ok',intent='create_request',request_id=state.id,revision=1)
    with pytest.raises(RuntimeError):repo.finish(1,'telegram:203',sequence,APPROVED,reply,state)
    assert repo.load(1) is None and repo.version(1,state.id,1) is None and not stored(rig.queue,203)
    assert rig.queue.job(203)['state']=='claimed'


def approved_plan_to_extraction():
    from steel_agent_v2.contracts import AgentPlan
    return AgentPlan.model_validate(approved_plan()).extraction


def test_no_global_html_mode_in_telegram_api():
    response=BytesIO(json.dumps(dict(ok=True,result={'message_id':1})).encode())
    with patch('urllib.request.urlopen',return_value=response) as opened:
        TelegramAPI('offline-only').call('sendMessage',chat_id=1,text='<b>literal</b>')
    sent=json.loads(opened.call_args.args[0].data)
    assert sent==dict(chat_id=1,text='<b>literal</b>')


def test_old_reply_serialization_remains_byte_compatible():
    raw='{"text":"old","status":"ok","intent":"free_chat","request_id":null,"revision":null,"sources":[],"diagnostics":[],"calls":0}'
    assert Reply.model_validate_json(raw).model_dump_json()==raw
