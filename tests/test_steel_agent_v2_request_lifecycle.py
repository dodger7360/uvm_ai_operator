"""Supplied coordinator plans, approved CASE002 and synthetic data, no live APIs."""
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
import json

import pytest

from database import Database
from steel_agent_v2 import SteelAgent, Repository
from steel_agent_v2.contracts import Reply
from test_steel_agent_v2 import Fake, proposal, SOURCE, edit, set_op
from test_steel_agent_v2_queue import rig, admit, Executor


CASE=json.loads((Path(__file__).parent/'fixtures/steel_v2_stage7_cases.json').read_text(encoding='utf-8'))['cases'][1]


def seed(tmp_path):
    repo=Repository(Database(tmp_path/'lifecycle.sqlite3'));fake=Fake(proposal());agent=SteelAgent(fake,repo)
    assert agent.handle(1,SOURCE,event_id='old').status=='ok'
    return agent,repo,fake


def pending(repo):
    _,current,sequence=repo.begin(1,'pending','Синтетический старый неразобранный исходник')
    repo.finish(1,'pending',sequence,'Синтетический старый неразобранный исходник',
        Reply(text='Ошибка разбора',intent='clarify',status='failed',diagnostics=['pending_source'],
              request_id=current.id,revision=current.revision))


def append_plan(current):
    source='Добавь швеллер 20П Ст3 — 5 т'
    return source,dict(intent='append_request',expected_request_id=current.id,expected_revision=current.revision,
        extraction=dict(records=[dict(id='r1',kind='product',start=0,end=len(source))],items=[
            dict(id='i1',source_id='r1',label='Швеллер',quantity='5',unit='т',quantity_text='5',unit_text='т',
                characteristics=[dict(name='profile',value='20П',raw='20П',source_ids=['r1']),
                                 dict(name='grade',value='Ст3',raw='Ст3',source_ids=['r1'])])],
            groups=[dict(id='g1',label='Швеллер',item_ids=['i1'],rationale='Новая позиция')]))


@pytest.mark.parametrize('old_pending',[False,True])
def test_independent_case002_replaces_active_without_losing_old_versions(tmp_path,old_pending):
    agent,repo,fake=seed(tmp_path);old=repo.load(1)
    if old_pending:pending(repo)
    fake.plans.append(deepcopy(CASE['plan']))
    reply=agent.handle(1,CASE['source'],event_id='new')
    current=repo.load(1)
    assert reply.status in {'ok','partial'} and current.id!=old.id and len(current.items)==4
    assert current.original_text==CASE['source'] and repo.version(1,old.id,1)==old
    assert repo.pending(1) is None and repo.load(2) is None
    assert fake.calls[-1][0]['source_lines'] and len(fake.calls)==2
    assert reply.text==CASE['expected_text']


def test_new_request_confirmation_pins_source_across_restart(tmp_path):
    agent,repo,fake=seed(tmp_path);pending(repo);old=repo.load(1)
    fake.plans.append(dict(intent='clarify',answer='Начать новую заявку?',awaiting_new_request=True))
    assert agent.handle(1,CASE['source'],event_id='list').intent=='clarify'
    assert repo.pending(1)['event_id']=='list' and repo.load(1)==old
    repo=Repository(Database(tmp_path/'lifecycle.sqlite3'))
    plan=deepcopy(CASE['plan']);plan['pending_event_id']='list'
    def confirmed(context,kwargs):
        assert context['pending_request']['source_text']==CASE['source']
        return plan
    fake=Fake(confirmed);reply=SteelAgent(fake,repo).handle(1,'Новая заявка',event_id='confirm')
    assert reply.status in {'ok','partial'} and len(repo.load(1).items)==4
    assert repo.load(1).original_text==CASE['source'] and repo.pending(1) is None
    assert len(fake.calls)==1 and repo.version(1,old.id,1)==old


def test_append_fifth_item_edit_semantically_restart_and_undo(tmp_path):
    repo=Repository(Database(tmp_path/'lifecycle.sqlite3'));fake=Fake(deepcopy(CASE['plan']));agent=SteelAgent(fake,repo)
    assert agent.handle(1,CASE['source'],event_id='case').status in {'ok','partial'};before=repo.load(1)
    message,plan=append_plan(before);fake.plans.append(plan)
    appended_reply=agent.handle(1,message,event_id='append');added=repo.load(1)
    assert appended_reply.status in {'ok','partial'},appended_reply.diagnostics
    assert added.id==before.id and added.revision==2 and len(added.items)==5
    assert added.items[:4]==before.items and added.originals[:4]==before.originals
    assert added.records[:len(before.records)]==before.records and added.groups[:len(before.groups)]==before.groups
    assert added.theoretical_mass==before.theoretical_mass
    item=added.items[-1];assert item.id not in {i.id for i in before.items}
    assert item.quantity==Decimal(5) and item.unit=='т'
    record=next(r for r in added.records if r.id==item.source_id)
    assert added.original_text[record.start:record.end]==message
    assert all(c.raw in message and c.source_ids==[record.id] for c in item.characteristics)
    assert agent.handle(1,message,event_id='append')==appended_reply and len(fake.calls)==2
    repo=Repository(Database(tmp_path/'lifecycle.sqlite3'));fake=Fake();agent=SteelAgent(fake,repo)
    op=set_op(field='quantity',name=None,value='7',evidence='7');op.pop('target_id')
    op['selection']=dict(mode='where',predicates=[dict(field='label',value='Швеллер',match='token')],
        cardinality='one',evidence='швеллера')
    fake.plans.append(edit(added,[op]))
    assert agent.handle(1,'Измени количество швеллера на 7',event_id='edit').status in {'ok','partial'}
    changed=repo.load(1);assert changed.items[:4]==before.items and changed.items[-1].quantity==7
    fake.plans.append(edit(changed,[dict(kind='undo')]))
    assert agent.handle(1,'Отмени',event_id='undo-edit').status in {'ok','partial'}
    reverted=repo.load(1);assert reverted.items==added.items
    fake.plans.append(edit(reverted,[dict(kind='undo')]))
    assert agent.handle(1,'Отмени',event_id='undo-append').status in {'ok','partial'}
    restored=repo.load(1)
    assert restored.items==before.items and restored.original_text==before.original_text
    assert restored.records==before.records and restored.theoretical_mass==before.theoretical_mass
    assert repo.version(1,before.id,2)==added and repo.load(2) is None


def test_invalid_append_is_atomic_and_keeps_source_in_history(tmp_path):
    agent,repo,fake=seed(tmp_path);before=repo.load(1);source,bad=append_plan(before)
    bad['extraction']['items'][0]['quantity']='7'
    fake.plans.extend([bad,deepcopy(bad)])
    reply=agent.handle(1,source,event_id='bad-append')
    assert reply.status=='failed' and repo.load(1)==before
    assert repo.version(1,before.id,2) is None and len(fake.calls)==3
    assert any(t['content']==source for t in repo.history(1,before.id))


@pytest.mark.parametrize('command',['/close','Закрой заявку','Заверши заявку','Сбрось текущую'])
def test_close_preserves_versions_clears_working_context_and_allows_free_chat(tmp_path,command):
    agent,repo,fake=seed(tmp_path);pending(repo);old=repo.load(1)
    if command!='/close':fake.plans.append(dict(intent='close_request',expected_request_id=old.id,expected_revision=old.revision))
    closed=agent.handle(1,command,event_id='close')
    assert closed.status=='ok' and closed.intent=='close_request' and repo.load(1) is None
    assert closed.calls==(0 if command=='/close' else 1)
    assert repo.version(1,old.id,1)==old and repo.pending(1) is None
    assert agent.handle(1,command,event_id='close')==closed
    repo=Repository(Database(tmp_path/'lifecycle.sqlite3'))
    def chat(context,kwargs):
        assert context['active_request'] is None and context['pending_request'] is None
        assert context['pending_clarification'] is None and context['history']==[]
        return dict(intent='free_chat',answer='Здравствуйте!')
    next_fake=Fake(chat);reply=SteelAgent(next_fake,repo).handle(1,'Привет, как дела?',event_id='chat')
    assert reply.intent=='free_chat' and len(next_fake.calls)==1 and repo.load(1) is None
    with repo.database.connection() as c:assert c.execute('SELECT count(*) FROM steel_v2_turn').fetchone()[0]==4


def test_close_durable_outbox_and_repeat_never_call_gpt(rig):
    fake=Fake()
    admit(rig,1201,text='/close')
    Executor(rig.queue,rig.access,lambda:rig.api,lambda:fake).execute(rig.queue.claim())
    assert rig.queue.job(1201)['state']=='delivered' and not fake.calls
    repo=Repository(rig.queue.database)
    assert repo.load(1) is None
    admit(rig,1201,text='/close');assert rig.queue.claim() is None
    with rig.queue.database.connection() as c:
        assert c.execute('SELECT count(*) FROM steel_v2_attempt').fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM steel_v2_outbox').fetchone()[0]==1


def test_append_new_display_hints_are_remapped_to_durable_ids(tmp_path):
    agent,repo,fake=seed(tmp_path);before=repo.load(1)
    source,plan=append_plan(before)
    plan['display_layout']=dict(items=[dict(item_id='i1',kind='channel',grade_name='grade',dimensions=[])])
    fake.plans.append(plan)
    reply=agent.handle(1,source,event_id='append-layout');current=repo.load(1)
    assert reply.status in {'ok','partial'}
    target=current.items[-1].id
    hints=repo.last_display(1,current).layout.items
    assert any(h.item_id==target and h.kind=='channel' for h in hints)
    position=repo.last_display(1,current).item_ids.index(target)+1
    op=set_op(field='quantity',value='9',evidence='9');op.pop('target_id')
    op['selection']=dict(mode='display',positions=[position],cardinality='one',evidence=str(position))
    fake.plans.append(edit(current,[op]))
    assert agent.handle(1,f'Позиция {position}: количество 9',event_id='display-edit').status=='ok'
    assert repo.load(1).items[-1].quantity==9 and repo.load(1).items[:-1]==before.items


@pytest.mark.parametrize('intent',['append','close'])
def test_finish_failure_rolls_back_whole_mutation_and_history(tmp_path,intent):
    agent,repo,fake=seed(tmp_path);before=repo.load(1)
    def fail(*args):raise RuntimeError('synthetic commit failure')
    repo.finish_hook=fail
    if intent=='append':
        source,plan=append_plan(before);fake.plans.append(plan)
    else:source='/close'
    reply=agent.handle(1,source,event_id='atomic-fail')
    assert reply.diagnostics==['storage_unconfirmed'] and repo.load(1)==before
    with repo.database.connection() as c:
        assert c.execute('SELECT count(*) FROM steel_v2_version').fetchone()[0]==1
        assert c.execute("SELECT count(*) FROM steel_v2_turn WHERE event_id='atomic-fail'").fetchone()[0]==0


def test_close_is_authorized_and_does_not_close_another_user(tmp_path):
    agent,repo,fake=seed(tmp_path)
    fake.plans.append(proposal());assert agent.handle(2,SOURCE,event_id='other').status=='ok'
    other=repo.load(2);own=repo.load(1)
    repo.authorize=lambda user:user!=1
    with pytest.raises(PermissionError):agent.handle(1,'/close',event_id='denied')
    repo.authorize=lambda _:True
    assert repo.load(1)==own and repo.load(2)==other
    assert agent.handle(1,'/close',event_id='allowed').status=='ok'
    assert repo.load(1) is None and repo.load(2)==other


def test_lifecycle_contract_reaches_real_sdk_mock_http():
    import httpx2
    from openai import OpenAI,DefaultHttpxClient
    from steel_agent_v2.contracts import AgentPlan
    from steel_agent_v2.transport import OpenAITransport
    captured=[]
    def respond(request):
        payload=json.loads(request.content);captured.append(payload)
        return httpx2.Response(200,json={'id':'resp_fake','object':'response','created_at':0,
            'model':'gpt-6-luna','status':'completed','output':[{'id':'message_fake','type':'message',
            'role':'assistant','status':'completed','content':[{'type':'output_text',
            'text':AgentPlan(intent='close_request').model_dump_json(),'annotations':[]}]}]})
    with DefaultHttpxClient(transport=httpx2.MockTransport(respond)) as http:
        model=OpenAITransport(OpenAI(api_key='offline-test',http_client=http))
        assert model.plan({'message':'Закрой заявку'}).intent=='close_request'
    schema=captured[0]['text']['format']['schema']
    assert {'append_request','close_request'}<=set(schema['$defs']['WireActionPlan']['properties']['intent']['enum'])
    assert schema['$defs']['WireCreatePlan']['properties']['intent']['const']=='create_request'
    assert captured[0]['reasoning']=={'effort':'high'} and captured[0]['model']=='gpt-6-luna'
    assert 'awaiting_new_request' in captured[0]['instructions']


def test_legacy_ai_functions_not_used_by_lifecycle(monkeypatch,tmp_path):
    import normalizer
    def forbidden(*a,**k):raise AssertionError('legacy normalization called')
    monkeypatch.setattr(normalizer,'normalize_request',forbidden)
    agent,repo,fake=seed(tmp_path);source,plan=append_plan(repo.load(1));fake.plans.append(plan)
    assert agent.handle(1,source,event_id='append').status=='ok'
    assert agent.handle(1,'/close',event_id='close').calls==0


def test_append_commits_with_outbox_and_unknown_delivery_does_not_replay(rig):
    fake=Fake(deepcopy(CASE['plan']))
    executor=Executor(rig.queue,rig.access,lambda:rig.api,lambda:fake)
    admit(rig,1301,text=CASE['source']);executor.execute(rig.queue.claim())
    repo=Repository(rig.queue.database);before=repo.load(1)
    message,plan=append_plan(before);fake.plans.append(plan)
    rig.api.fail=True
    admit(rig,1302,text=message);executor.execute(rig.queue.claim())
    assert rig.queue.job(1302)['state']=='delivery_unknown'
    changed=repo.load(1)
    assert len(changed.items)==5 and changed.items[:4]==before.items and changed.revision==2
    assert repo.version(1,before.id,2)==changed and len(fake.calls)==2
    with rig.queue.database.connection() as c:
        assert c.execute('SELECT count(*) FROM steel_v2_outbox WHERE update_id=1302').fetchone()[0]>=1
        assert c.execute("SELECT count(*) FROM steel_v2_turn WHERE event_id='telegram:1302'").fetchone()[0]==1
    rig.api.fail=False;calls=list(rig.api.calls)
    rig.queue.recover();rig.queue.deliver(1302,rig.api,rig.access)
    assert rig.api.calls==calls and len(fake.calls)==2 and repo.load(1)==changed


def test_append_preserves_approved_mass_provenance_after_id_remapping(tmp_path):
    agent,repo,fake=seed(tmp_path);before=repo.load(1)
    plan=deepcopy(CASE['plan']);plan.update(intent='append_request',
        expected_request_id=before.id,expected_revision=before.revision)
    prefix='Добавь\n';source=prefix+CASE['source']
    for record in plan['extraction']['records']:
        record['start']+=len(prefix);record['end']+=len(prefix)
    plan['extraction']['records'].insert(0,dict(id='append-note',kind='note',start=0,end=len(prefix)))
    fake.plans.append(plan)
    reply=agent.handle(1,source,event_id='append-masses');current=repo.load(1)
    assert reply.status in {'ok','partial'} and current.items[:2]==before.items
    remap={old['id']:new.id for old,new in zip(CASE['plan']['extraction']['items'],current.items[2:])}
    for old,value in CASE['expected_tonnes_by_id'].items():
        mass=next(m for m in current.theoretical_mass if m.spec.item_id==remap[old])
        assert mass.tonnes==Decimal(value) and mass.reference.id.startswith(CASE['id']+':')
        assert mass.reference.revision.startswith(agent.knowledge.version)
        assert mass.reference.sources and mass.reference.status=='owner_indicative'
