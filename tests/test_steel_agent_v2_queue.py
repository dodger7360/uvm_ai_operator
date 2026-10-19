"""Durability and concurrency with real repositories, fake external effects and events."""
from contextlib import contextmanager
from threading import Event, Lock, Thread
from types import ModuleType
import subprocess
import json
import sqlite3
import time
from types import SimpleNamespace

import pytest
from database import Database
from repositories.telegram_repository import TelegramRepository
from repositories.access_repository import AccessRepository
from telegram_access import TelegramAccess
from steel_agent_v2 import Repository, RequestEngine
from steel_agent_v2.contracts import AgentPlan, Reply
from steel_agent_v2.queue import DurableQueue, QueueConfig, Backpressure, QueueConflict
from steel_agent_v2.queue_runtime import Executor, Scheduler
from test_steel_agent_v2 import Fake, SOURCE, proposal
from test_steel_agent_v2_015_reproduction import BEAMS, beams_plan


def update(number,user=1,text=SOURCE):
    return dict(update_id=number,message={'from':{'id':user},'chat':{'id':user,'type':'private'},'text':text})


class API:
    def __init__(self):self.calls=[];self.typing=[];self.lock=Lock();self.fail=False
    def call(self,method,**payload):
        with self.lock:
            if method=='sendChatAction':
                self.typing.append(payload)
                if self.fail:raise TimeoutError('test typing unavailable')
                return True
            self.calls.append((method,payload))
            number=len(self.calls)
        if self.fail:raise TimeoutError('test network uncertainty')
        return {'message_id':number}


@pytest.fixture
def rig(tmp_path):
    legacy=TelegramRepository(Database(tmp_path/'legacy.sqlite3'))
    permissions=AccessRepository(legacy.database)
    permissions.bootstrap_configured_admin(1)
    for user in range(2,6):
        permissions.request_access({'id':user})
        permissions.transition(1,user,'approve',1,notify=lambda _:True)
    access=TelegramAccess(permissions)
    database=Database(tmp_path/'v2.sqlite3')
    Repository(database)
    queue=DurableQueue(database)
    api=API()
    return SimpleNamespace(queue=queue,access=access,api=api,legacy=legacy,permissions=permissions)


def admit(rig,number,user=1,text=SOURCE):
    grant=rig.permissions.get(user)
    return rig.queue.admit(update(number,user,text),allowed=rig.access.allowed(user),revision=grant['revision'] if grant else None,
        is_admin=bool(grant and grant['role']=='admin'))


def drain(scheduler,condition,timeout=5):
    deadline=time.monotonic()+timeout
    while not condition() and time.monotonic()<deadline:
        scheduler.tick();time.sleep(.005)
    assert condition()


def test_three_overlap_fourth_waits_and_same_user_fifo(rig):
    started={label:Event() for label in ['A1','A2','B','C','D']}
    release=Event();finished_b=Event();contexts=[];lock=Lock()
    class Controlled:
        def plan(self,context,**kwargs):
            label=context['message']
            with lock:contexts.append(context)
            started[label].set()
            if label in {'A1','B','C'}:assert release.wait(5)
            if label=='B':finished_b.set()
            return dict(intent='free_chat',answer=label)
    for number,user,text in [(1,1,'A1'),(2,1,'A2'),(3,2,'B'),(4,3,'C'),(5,4,'D')]:admit(rig,number,user,text)
    scheduler=Scheduler(rig.queue,Executor(rig.queue,rig.access,lambda:rig.api,Controlled))
    try:
        scheduler.tick()
        assert all(started[k].wait(3) for k in ['A1','B','C'])
        assert not started['A2'].is_set() and not started['D'].is_set()
        release.set()
        drain(scheduler,lambda:rig.queue.drained())
    finally:release.set();scheduler.close()
    assert finished_b.is_set() and started['A2'].is_set() and started['D'].is_set()
    sends=[p for m,p in rig.api.calls if m=='sendMessage']
    assert {(p['chat_id'],p['text']) for p in sends}=={(1,'A1'),(1,'A2'),(2,'B'),(3,'C'),(4,'D')}
    assert [p['text'] for p in sends if p['chat_id']==1]==['A1','A2']
    assert any(c['message']=='A2' and any(h['content']=='A1' for h in c['history']) for c in contexts)
    assert all(not any(h['content']=='A1' for h in c['history']) for c in contexts if c['message'] in {'B','C','D'})


def test_slow_a_does_not_block_b(rig):
    entered,release,done=Event(),Event(),Event()
    class Slow:
        def plan(self,context,**kwargs):
            if context['message']=='A':entered.set();assert release.wait(5)
            else:done.set()
            return dict(intent='free_chat',answer=context['message'])
    admit(rig,1,1,'A');admit(rig,2,2,'B')
    scheduler=Scheduler(rig.queue,Executor(rig.queue,rig.access,lambda:rig.api,Slow))
    try:
        scheduler.tick()
        assert entered.wait(3) and done.wait(3) and not release.is_set()
        release.set();drain(scheduler,lambda:rig.queue.drained())
    finally:release.set();scheduler.close()


def test_inbox_before_offset_crash_duplicate_and_noncontiguous_ids(rig):
    assert rig.legacy.get_offset()==0
    assert admit(rig,41)==42
    # Crash before the separate legacy offset write: redelivery is durable dedup.
    reopened=DurableQueue(rig.queue.database)
    assert reopened.admit(update(41),allowed=True)==42
    rig.legacy.set_offset(reopened.cursor())
    assert rig.legacy.get_offset()==42
    assert admit(rig,900)==901
    with pytest.raises(QueueConflict):rig.queue.admit(update(41,text='different'),allowed=True)
    with rig.queue.database.connection() as c:assert c.execute('SELECT count(*) FROM steel_v2_inbox').fetchone()[0]==2


def test_repeated_update_executes_model_and_mutation_once(rig):
    fake=Fake(beams_plan());admit(rig,1,text=BEAMS)
    executor=Executor(rig.queue,rig.access,lambda:rig.api,lambda:fake)
    executor.execute(rig.queue.claim())
    admit(rig,1,text=BEAMS)
    assert rig.queue.claim() is None and len(fake.calls)==1
    current=Repository(rig.queue.database).load(1)
    assert len(current.items)==4 and current.original_text==BEAMS
    with rig.queue.database.connection() as c:
        assert c.execute('SELECT count(*) FROM steel_v2_version').fetchone()[0]==1
        assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'


@pytest.mark.parametrize('stage', ['queued','claimed','api_started','result_received'])
def test_restart_stages_never_replay_unknown_paid_attempt(rig,stage):
    admit(rig,1)
    if stage!='queued':rig.queue.claim()
    if stage in {'api_started','result_received'}:
        attempt=rig.queue.start_api(1,'plan')
        if stage=='result_received':rig.queue.end_api(1,attempt,known=True)
    reopened=DurableQueue(rig.queue.database);reopened.recover()
    expected='queued' if stage in {'queued','claimed'} else 'execution_unknown'
    assert reopened.job(1)['state']==expected
    if expected=='execution_unknown':
        admit(rig,2)
        assert reopened.claim() is None


def test_unknown_network_result_no_second_gpt_or_following_message(rig):
    fake=Fake(TimeoutError('lost response'));admit(rig,1);admit(rig,2,text='next')
    Executor(rig.queue,rig.access,lambda:rig.api,lambda:fake).execute(rig.queue.claim())
    assert len(fake.calls)==1 and rig.queue.job(1)['state']=='execution_unknown'
    assert rig.queue.claim() is None and not rig.queue.drained()
    assert Repository(rig.queue.database).load(1) is None
    with pytest.raises(QueueConflict):rig.queue.resolve(1,'incorrect confirmation')
    job=rig.queue.job(1)
    rig.queue.resolve(1,f"1:execution_unknown:{job['input_hash']}")
    assert rig.queue.claim()['update_id']==2


def test_crash_after_atomic_commit_resumes_only_delivery(rig):
    admit(rig,1)
    fake=Fake(proposal());job=rig.queue.claim()
    from steel_agent_v2.agent import SteelAgent
    from steel_agent_v2.queue_runtime import AttemptTransport
    repo=Repository(rig.queue.database,authorize=rig.access.allowed,commit_guard=rig.permissions.commit_permission,
        finish_hook=rig.queue.finish_hook(1))
    SteelAgent(AttemptTransport(fake,rig.queue,1),repo).handle(1,SOURCE,event_id='telegram:1')
    assert rig.queue.job(1)['state']=='committed'
    reopened=DurableQueue(rig.queue.database);reopened.recover()
    reopened.deliver(1,rig.api,rig.access)
    assert len(fake.calls)==1 and reopened.job(1)['state']=='delivered'


def test_atomic_finish_rolls_back_all_tables_and_outbox_on_fault(rig):
    admit(rig,1);rig.queue.claim()
    repo=Repository(rig.queue.database)
    _,_,sequence=repo.begin(1,'telegram:1',SOURCE)
    current=RequestEngine().create(AgentPlan.model_validate(proposal()).extraction,SOURCE,'1')
    hook=rig.queue.finish_hook(1)
    def failed(c,*args):hook(c,*args);raise sqlite3.OperationalError('injected commit failure')
    repo.finish_hook=failed
    with pytest.raises(sqlite3.OperationalError):
        repo.finish(1,'telegram:1',sequence,SOURCE,Reply(text='ok',status='ok',intent='create_request'),current)
    with rig.queue.database.connection() as c:
        for table in ['steel_v2_active','steel_v2_version','steel_v2_turn','steel_v2_outbox']:
            assert c.execute('SELECT count(*) FROM '+table).fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM steel_v2_inflight').fetchone()[0]==1


def test_unknown_delivery_never_resends_or_repeats_mutation(rig):
    fake=Fake(proposal());admit(rig,1);rig.api.fail=True
    Executor(rig.queue,rig.access,lambda:rig.api,lambda:fake).execute(rig.queue.claim())
    assert rig.queue.job(1)['state']=='delivery_unknown' and len(fake.calls)==1
    calls=len(rig.api.calls)
    reopened=DurableQueue(rig.queue.database);reopened.recover()
    assert reopened.delivery_candidates()==[] and not reopened.drained()
    assert len(rig.api.calls)==calls and Repository(rig.queue.database).load(1).revision==1


def test_multipart_order_receipts_and_recovery_after_send_started(rig):
    fake=Fake(dict(intent='free_chat',answer='A'*9000));admit(rig,1,text='long reply')
    Executor(rig.queue,rig.access,lambda:rig.api,lambda:fake).execute(rig.queue.claim())
    assert len(rig.api.calls)>=3
    assert ''.join(p['text'] for m,p in rig.api.calls)=='A'*9000
    with rig.queue.database.connection() as c:
        rows=c.execute('SELECT state,message_id FROM steel_v2_outbox ORDER BY part').fetchall()
        assert all(r[0]=='delivered' and type(r[1]) is int for r in rows)
    admit(rig,2,text='queued');rig.queue.claim()
    with rig.queue.write() as c:
        c.execute("UPDATE steel_v2_inbox SET state='delivery_started',committed=1 WHERE update_id=2")
        c.execute("INSERT INTO steel_v2_outbox VALUES(2,0,'sendMessage','{}','delivery_started',NULL)")
    DurableQueue(rig.queue.database).recover()
    assert rig.queue.job(2)['state']=='delivery_unknown'


@pytest.mark.parametrize('config', [QueueConfig(max_pending=1,max_pending_per_user=1),
    QueueConfig(max_pending=3,max_pending_per_user=1),QueueConfig(max_pending_bytes=170)])
def test_backpressure_does_not_ack_unadmitted_update(rig,config):
    queue=DurableQueue(rig.queue.database,config)
    queue.admit(update(1,text='first'),allowed=True)
    with pytest.raises(Backpressure):queue.admit(update(2,text='second'),allowed=True)
    assert queue.cursor()==2 and queue.job(2) is None


def test_revocation_before_commit_prevents_mutation(rig):
    user=2;admit(rig,1,user)
    entered,release=Event(),Event()
    class Paused:
        def plan(self,*args,**kwargs):entered.set();assert release.wait(5);return proposal()
    executor=Executor(rig.queue,rig.access,lambda:rig.api,Paused)
    thread=Thread(target=executor.execute,args=(rig.queue.claim(),));thread.start()
    try:
        assert entered.wait(3)
        rig.permissions.transition(1,user,'revoke',rig.permissions.get(user)['revision'])
        release.set();thread.join(5)
    finally:release.set();thread.join(5)
    assert not thread.is_alive() and rig.queue.job(1)['state']=='denied'
    assert Repository(rig.queue.database).load(user) is None and not rig.api.calls


def test_commit_guard_linearizes_against_other_access_writer(rig):
    entered,release,revoke_started,revoke_done=Event(),Event(),Event(),Event()
    errors=[]
    def commit():
        try:
            with rig.permissions.commit_permission(2):
                entered.set();assert release.wait(5)
                with rig.queue.write() as c:c.execute("INSERT INTO steel_v2_queue_meta VALUES('guard_test',1)")
        except BaseException as error:errors.append(error)
    def revoke():
        try:
            revoke_started.set()
            AccessRepository(rig.legacy.database).transition(1,2,'revoke',2)
            revoke_done.set()
        except BaseException as error:errors.append(error)
    a=Thread(target=commit);b=Thread(target=revoke)
    a.start()
    try:
        assert entered.wait(3);b.start();assert revoke_started.wait(3)
        assert not revoke_done.wait(.1)
        release.set();a.join(5);b.join(5)
    finally:release.set();a.join(5);b.join(5)
    assert revoke_done.is_set() and not errors and not rig.access.allowed(2)
    with pytest.raises(PermissionError):
        with rig.permissions.commit_permission(2):pytest.fail('revoked user entered commit')


def test_maintenance_drain_does_not_start_waiting_job(rig):
    rig.queue.config=QueueConfig(workers=1)
    entered,release=Event(),Event()
    class Slow:
        def plan(self,*a,**kw):entered.set();assert release.wait(5);return dict(intent='free_chat',answer='ok')
    admit(rig,1);admit(rig,2,2)
    scheduler=Scheduler(rig.queue,Executor(rig.queue,rig.access,lambda:rig.api,Slow))
    scheduler.tick();assert entered.wait(3)
    closer=Thread(target=scheduler.close);closer.start()
    try:
        assert closer.is_alive() and rig.queue.job(2)['state']=='queued'
        release.set();closer.join(5)
    finally:release.set();closer.join(5)
    assert not closer.is_alive() and rig.queue.job(2)['state']=='queued'


def test_sqlite_busy_before_admission_does_not_advance_cursor(rig):
    with rig.queue.database.connection() as blocker:
        blocker.execute('BEGIN IMMEDIATE')
        with pytest.raises(sqlite3.OperationalError):admit(rig,1)
    assert rig.queue.cursor()==0 and rig.queue.job(1) is None
    assert admit(rig,1)==2


def test_administrative_callback_durable_once_and_notification_not_faked(rig):
    rig.permissions.request_access({'id':9})
    callback=dict(update_id=10,callback_query={'id':'cb','from':{'id':1},'data':'access:approve:9:1',
        'message':{'chat':{'type':'private','id':1},'message_id':7}})
    rig.queue.admit(callback,allowed=True,is_admin=True)
    rig.api.fail=True
    Executor(rig.queue,rig.access,lambda:rig.api).execute(rig.queue.claim())
    assert not rig.access.allowed(9)  # Enqueued/failed notification is NOT a grant.
    assert rig.queue.job(10)['state']=='delivery_unknown'
    calls=len(rig.api.calls)
    rig.queue.admit(callback,allowed=True,is_admin=True)
    DurableQueue(rig.queue.database).recover()
    assert rig.queue.claim() is None and len(rig.api.calls)==calls


def test_readonly_diagnostics_and_rollback_gate_hide_source(rig):
    from scripts.v2_queue_status import inspect,main
    admit(rig,1,text='PRIVATE_TEST_SOURCE')
    data=inspect(rig.queue.database.path)
    assert 'PRIVATE_TEST_SOURCE' not in json.dumps(data)
    assert not data['queue_drained']
    assert main(['--database',str(rig.queue.database.path),'--legacy-database',str(rig.legacy.database.path),'--check-forward'])==0


def test_completed_queue_payloads_compact_but_replay_still_deduplicated(rig):
    fake=Fake(proposal());admit(rig,1)
    Executor(rig.queue,rig.access,lambda:rig.api,lambda:fake).execute(rig.queue.claim())
    rig.queue.compact()
    assert rig.queue.job(1)['payload'] is None
    admit(rig,1)
    assert rig.queue.claim() is None and len(fake.calls)==1


def old_repository_class():
    source=subprocess.run(['git','show','aa11380dfe1ed646fec2355b22880fdb03b0d706:steel_agent_v2/repository.py'],
        capture_output=True,check=True,encoding='utf-8').stdout
    module=ModuleType('steel_agent_v2._published_repository');module.__package__='steel_agent_v2'
    exec(compile(source,'published_repository.py','exec'),module.__dict__)
    return module.Repository


def test_old_release_is_unsupported_and_forward_reopen_preserves_every_table(rig,tmp_path):
    from scripts.v2_queue_status import prepare_rollback
    from scripts.v2_sqlite_snapshot import metadata,snapshot
    old=old_repository_class()
    admit(rig,1)
    with pytest.raises(ValueError):old(rig.queue.database)
    with pytest.raises(ValueError,match='forward_only'):
        prepare_rollback(rig.queue.database.path,tmp_path/'refused.sqlite3','rollback:2')
    assert not (tmp_path/'refused.sqlite3').exists()
    Executor(rig.queue,rig.access,lambda:rig.api,lambda:Fake(proposal())).execute(rig.queue.claim())
    rig.legacy.set_offset(rig.queue.cursor())
    with rig.queue.database.connection() as c:before=metadata(c)
    archive=tmp_path/'verified-queue-archive.sqlite3'
    snapshot(rig.queue.database.path,archive,tmp_path/'manifest.json')
    Repository(rig.queue.database);DurableQueue(rig.queue.database).recover()
    with rig.queue.database.connection() as c:assert metadata(c)==before
    with sqlite3.connect(archive) as c:assert metadata(c)==before
    assert Repository(rig.queue.database).load(1).revision==1
    assert rig.legacy.get_offset()==2


def test_crash_after_access_commit_never_repeats_approval(rig,monkeypatch):
    rig.permissions.request_access({'id':9})
    callback=dict(update_id=10,callback_query={'id':'cb','from':{'id':1},'data':'access:approve:9:1',
        'message':{'chat':{'type':'private','id':1},'message_id':7}})
    rig.queue.admit(callback,allowed=True,is_admin=True)
    def crash(*args,**kwargs):raise RuntimeError('injected after access transaction commit')
    monkeypatch.setattr(rig.queue,'end_api',crash)
    Executor(rig.queue,rig.access,lambda:rig.api).execute(rig.queue.claim())
    assert rig.access.allowed(9) and rig.permissions.get(9)['revision']==2
    calls=len(rig.api.calls)
    DurableQueue(rig.queue.database).recover()
    assert rig.queue.job(10)['state']=='execution_unknown' and rig.queue.claim() is None
    assert rig.permissions.get(9)['revision']==2 and len(rig.api.calls)==calls


def test_deny_or_explicit_unknown_close_preserves_unfinished_original(rig):
    admit(rig,1,text='PRIVATE_ORIGINAL')
    rig.queue.claim();rig.queue.start_api(1,'plan')
    rig.queue.recover()
    row=rig.queue.job(1)
    rig.queue.resolve(1,f"1:execution_unknown:{row['input_hash']}")
    rig.queue.compact()
    assert rig.queue.job(1)['payload'] is None
    with rig.queue.database.connection() as c:
        assert c.execute('SELECT message FROM steel_v2_turn').fetchone()[0]=='PRIVATE_ORIGINAL'
        assert c.execute('SELECT count(*) FROM steel_v2_version').fetchone()[0]==0


def test_stopping_prevents_new_claim_and_rescheduled_worker_execution(rig):
    stopping=Event();fake=Fake(proposal())
    admit(rig,1)
    executor=Executor(rig.queue,rig.access,lambda:rig.api,lambda:fake,stopping=stopping.is_set)
    scheduler=Scheduler(rig.queue,executor,stopping=stopping.is_set)
    try:
        stopping.set();scheduler.tick()
        assert rig.queue.job(1)['state']=='queued' and not fake.calls
        job=rig.queue.claim();executor.execute(job)
        assert rig.queue.job(1)['state']=='queued' and not fake.calls
    finally:scheduler.close()


def test_future_schema_is_refused_without_modification(rig):
    with rig.queue.write() as c:c.execute("UPDATE steel_v2_queue_meta SET value=999 WHERE key='schema'")
    before=rig.queue.database.path.read_bytes()
    with pytest.raises(ValueError):DurableQueue(rig.queue.database)
    assert rig.queue.database.path.read_bytes()==before


@pytest.mark.parametrize('kind', ['product','unresolved','referenced_note','duplicate_id','overlap','out_of_bounds'])
def test_whitespace_cleanup_never_hides_referenced_or_structural_damage(rig,kind):
    from test_steel_agent_v2_015_reproduction import whitespace_plan
    plan=whitespace_plan();records=plan['extraction']['records'];record=records[1]
    if kind in {'product','unresolved'}:record['kind']=kind
    if kind=='referenced_note':plan['extraction']['items'][0]['characteristics'][0]['source_ids'].append('s2')
    if kind=='duplicate_id':record['id']='r1'
    if kind=='overlap':records[0]['end']+=1
    if kind=='out_of_bounds':record['end']=len(BEAMS)+1
    from steel_agent_v2.engine import EngineError
    with pytest.raises(EngineError):
        RequestEngine().create(AgentPlan.model_validate(plan).extraction,BEAMS,'strict-original')
    fake=Fake(plan,plan);admit(rig,1,text=BEAMS)
    Executor(rig.queue,rig.access,lambda:rig.api,lambda:fake).execute(rig.queue.claim())
    recovered=Repository(rig.queue.database).load(1)
    assert recovered is not None and len(fake.calls)==2
    RequestEngine().validate(recovered)
    assert recovered.original_text==BEAMS
    assert len({i.source_id for i in recovered.items})==len(recovered.items)
    with rig.queue.database.connection() as c:
        reply=Reply.model_validate_json(c.execute("SELECT reply FROM steel_v2_turn WHERE event_id='telegram:1'").fetchone()[0])
    assert reply.status=='partial' and 'partial_recovered' in reply.diagnostics
    assert any(c!='partial_recovered' for c in reply.diagnostics)
    assert not any('source_range' in p.get('text','') or 'schema:' in p.get('text','') for m,p in rig.api.calls)


def test_queued_beams_cleanup_and_followup_unsupported_add_keep_request(rig):
    from test_steel_agent_v2_015_reproduction import whitespace_plan
    fake=Fake(whitespace_plan(),dict(intent='clarify',answer='Добавление новых позиций пока не поддерживается. Сохраните исходный список.'))
    admit(rig,1,text=BEAMS)
    executor=Executor(rig.queue,rig.access,lambda:rig.api,lambda:fake)
    executor.execute(rig.queue.claim())
    before=Repository(rig.queue.database).load(1).model_dump_json()
    admit(rig,2,text='Добавь ещё балки из предыдущего списка')
    executor.execute(rig.queue.claim())
    assert Repository(rig.queue.database).load(1).model_dump_json()==before
    assert fake.calls[1][0]['append_supported'] is False
    assert fake.calls[1][0]['supported_edit_operations']==['set','regroup','undo','resolve','convert_mass',
                                                          'set_quantity','add_quantity','set_length','calculate_mass']


def test_cached_pre_queue_turn_does_not_mutate_call_gpt_or_resend_unknown_delivery(rig):
    from steel_agent_v2.agent import SteelAgent
    fake=Fake(proposal())
    SteelAgent(fake,Repository(rig.queue.database)).handle(1,SOURCE,event_id='telegram:1')
    before=Repository(rig.queue.database).load(1).model_dump_json()
    admit(rig,1)
    no_calls=Fake()
    Executor(rig.queue,rig.access,lambda:rig.api,lambda:no_calls).execute(rig.queue.claim())
    assert not no_calls.calls and rig.queue.job(1)['state']=='delivery_unknown'
    assert not rig.api.calls
    assert Repository(rig.queue.database).load(1).model_dump_json()==before


def test_crash_inside_admission_rolls_back_input_and_watermark(rig,monkeypatch):
    original_write=rig.queue.write
    @contextmanager
    def crash():
        with original_write() as c:
            yield c
            raise RuntimeError('injected before admission commit')
    with monkeypatch.context() as patch:
        patch.setattr(rig.queue,'write',crash)
        with pytest.raises(RuntimeError):admit(rig,1)
    assert rig.queue.cursor()==0 and rig.queue.job(1) is None
    assert rig.legacy.get_offset()==0
    admit(rig,1)
    assert rig.queue.job(1)['state']=='queued'


def test_partially_delivered_outbox_resumes_only_unstarted_parts(rig):
    admit(rig,1);rig.queue.claim()
    with rig.queue.write() as c:
        c.execute("UPDATE steel_v2_inbox SET state='delivery_pending',committed=1 WHERE update_id=1")
        for part,state in [(0,'delivered'),(1,'delivery_pending')]:
            c.execute("INSERT INTO steel_v2_outbox VALUES(?,?,'sendMessage',?,?,?)",
                (1,part,json.dumps({'chat_id':1,'text':str(part)}),state,101 if part==0 else None))
    DurableQueue(rig.queue.database).recover()
    rig.queue.deliver(1,rig.api,rig.access)
    assert [p['text'] for m,p in rig.api.calls]==['1']
    assert rig.queue.job(1)['state']=='delivered'


def test_claimed_before_api_recovery_only_clears_matching_claim(rig):
    admit(rig,1);rig.queue.claim()
    Repository(rig.queue.database).begin(1,'telegram:1',SOURCE)
    rig.queue.recover()
    assert rig.queue.job(1)['state']=='queued'
    with rig.queue.database.connection() as c:assert c.execute('SELECT count(*) FROM steel_v2_inflight').fetchone()[0]==0
    Repository(rig.queue.database).begin(2,'unmanaged',SOURCE)
    with pytest.raises(QueueConflict):rig.queue.recover()
    with rig.queue.database.connection() as c:assert c.execute('SELECT event_id FROM steel_v2_inflight').fetchone()[0]=='unmanaged'


def test_unicode_crlf_long_header_notes_and_commas_survive_cleanup(rig):
    from copy import deepcopy
    header='Заявка № 12, объект «Ёж». '+'Длинная шапка '*30
    prefix=header+'\r\n \t\r\n'
    note='Примечание: сохранить исходные обозначения, без замен.'
    source=prefix+BEAMS+note
    plan=deepcopy(beams_plan())
    for record in plan['extraction']['records']:
        record['start']+=len(prefix);record['end']+=len(prefix)
    plan['extraction']['records'][:0]=[dict(id='h',kind='header',start=0,end=len(header)),
        dict(id='blank',kind='note',start=len(header),end=len(prefix))]
    plan['extraction']['records'].append(dict(id='note',kind='note',start=len(prefix)+len(BEAMS),end=len(source)))
    admit(rig,1,text=source)
    Executor(rig.queue,rig.access,lambda:rig.api,lambda:Fake(plan)).execute(rig.queue.claim())
    current=Repository(rig.queue.database).load(1)
    assert current.original_text==source and len(current.items)==4
    assert {r.id for r in current.records}=={'h','note','r1','r2','r3','r4'}
    from steel_agent_v2.engine import totals
    assert totals(current.items)==({'т':'381.164'},[])


def test_scheduler_progresses_queued_users_while_poller_is_waiting(rig):
    a_started,release_a,d_started=Event(),Event(),Event()
    rig.queue.config=QueueConfig(workers=1)
    class Controlled:
        def plan(self,context,**kwargs):
            if context['message']=='A':a_started.set();assert release_a.wait(5)
            else:d_started.set()
            return dict(intent='free_chat',answer='ok')
    admit(rig,1,text='A');admit(rig,2,2,text='D')
    scheduler=Scheduler(rig.queue,Executor(rig.queue,rig.access,lambda:rig.api,Controlled))
    scheduler.start()
    try:
        assert a_started.wait(3)
        release_a.set()
        # No poller tick or new getUpdates; the persistent scheduler must wake on its own.
        assert d_started.wait(3)
    finally:release_a.set();scheduler.close()


@pytest.mark.parametrize('config',[{'workers':True},{'workers':0},{'workers':33},
    {'max_pending':2,'max_pending_per_user':3},{'unknown':1}])
def test_configuration_fails_closed(config):
    with pytest.raises((ValueError,TypeError)):QueueConfig(**config)


def test_revoke_then_reapprove_during_gpt_does_not_revive_old_operation(rig):
    entered,release=Event(),Event()
    class Paused:
        def plan(self,*args,**kwargs):entered.set();assert release.wait(5);return proposal()
    admit(rig,1,2)
    executor=Executor(rig.queue,rig.access,lambda:rig.api,Paused)
    thread=Thread(target=executor.execute,args=(rig.queue.claim(),));thread.start()
    try:
        assert entered.wait(3)
        rig.permissions.transition(1,2,'revoke',2)
        rig.permissions.transition(1,2,'approve',3,notify=lambda _:True)
        release.set();thread.join(5)
    finally:release.set();thread.join(5)
    assert rig.access.allowed(2) and rig.queue.job(1)['state']=='denied'
    assert Repository(rig.queue.database).load(2) is None and not rig.api.calls


def test_admission_generation_change_denies_queued_job_without_gpt(rig):
    admit(rig,1,2)
    rig.permissions.transition(1,2,'revoke',2)
    rig.permissions.transition(1,2,'approve',3,notify=lambda _:True)
    fake=Fake()
    Executor(rig.queue,rig.access,lambda:rig.api,lambda:fake).execute(rig.queue.claim())
    assert not fake.calls and rig.queue.job(1)['state']=='denied'


def test_search_unknown_network_outcome_is_not_masked_by_value_error(rig):
    from steel_agent_v2.transport import SearchUnavailable
    fake=Fake(dict(intent='web_search',search_query='offline query'),search=SearchUnavailable('api_error',outcome_known=False))
    admit(rig,1,text='Найди сведения')
    Executor(rig.queue,rig.access,lambda:rig.api,lambda:fake).execute(rig.queue.claim())
    assert len(fake.calls)==len(fake.search_calls)==1
    assert rig.queue.job(1)['state']=='execution_unknown'
    with rig.queue.database.connection() as c:
        assert [tuple(r) for r in c.execute('SELECT kind,state FROM steel_v2_attempt ORDER BY number')]==[
            ('plan','result_received'),('search','execution_unknown')]


def test_known_http_error_is_not_retried_but_does_not_freeze_next_user_turn(rig):
    import httpx2 as httpx
    from openai import APIStatusError
    error=APIStatusError('offline error',response=httpx.Response(400,request=httpx.Request('POST','https://offline.invalid')),body=None)
    fake=Fake(error,dict(intent='free_chat',answer='следующий ответ'))
    admit(rig,1);admit(rig,2,text='next')
    executor=Executor(rig.queue,rig.access,lambda:rig.api,lambda:fake)
    executor.execute(rig.queue.claim());executor.execute(rig.queue.claim())
    assert len(fake.calls)==2 and rig.queue.drained()
    with rig.queue.database.connection() as c:
        assert c.execute('SELECT count(*) FROM steel_v2_attempt WHERE update_id=1').fetchone()[0]==1


def test_binding_command_dpapi_roundtrip_durable_dedup_and_no_plaintext(tmp_path):
    import os
    if os.name!='nt':pytest.skip('Windows DPAPI test')
    token='SYNTHETIC_BINDING_CREDENTIAL_FOR_OFFLINE_TEST'
    legacy=TelegramRepository(Database(tmp_path/'legacy.sqlite3'))
    permissions=AccessRepository(legacy.database)
    permissions.issue_binding(token)
    access=TelegramAccess(permissions)
    database=Database(tmp_path/'v2.sqlite3');Repository(database)
    queue=DurableQueue(database);api=API()
    event=update(1,8,'/claim '+token)
    queue.admit(event,allowed=False)
    original=queue.job(1)
    assert token not in original['payload'] and token.encode() not in database.path.read_bytes()
    queue.admit(event,allowed=False)  # Fresh randomized ciphertext must not change the stable input hash.
    assert queue.job(1)['payload']==original['payload']
    Executor(queue,access,lambda:api,lambda:pytest.fail('binding must not call GPT')).execute(queue.claim())
    assert permissions.is_admin(8) and queue.job(1)['state']=='delivered'
    queue.compact()
    assert token.encode() not in database.path.read_bytes()
    with database.connection() as c:assert c.execute('SELECT count(*) FROM steel_v2_turn').fetchone()[0]==0


def test_default_execution_factory_explicit_high_and_independent_clients(rig,monkeypatch):
    from steel_agent_v2.transport import OpenAITransport
    clients=[];options=[]
    def factory(cls,**kwargs):
        options.append(kwargs);client=Fake(dict(intent='free_chat',answer='ok'));clients.append(client);return client
    monkeypatch.setattr(OpenAITransport,'from_environment',classmethod(factory))
    monkeypatch.setenv('OPENAI_REASONING_EFFORT','low')
    executor=Executor(rig.queue,rig.access,lambda:rig.api)
    for number,user in [(1,1),(2,2),(3,3)]:
        admit(rig,number,user);executor.execute(rig.queue.claim())
    assert len({id(client) for client in clients})==3
    assert options==[{'model':'gpt-6-luna','reasoning':'high'}]*3
    import os
    assert os.environ['OPENAI_REASONING_EFFORT']=='low'


@pytest.mark.parametrize('status',[None,400])
def test_real_sdk_search_exception_retains_known_vs_unknown_outcome(rig,status):
    import httpx2
    from openai import OpenAI,DefaultHttpxClient
    from steel_agent_v2.transport import OpenAITransport,SearchUnavailable
    from steel_agent_v2.queue_runtime import AttemptTransport
    requests=[]
    def respond(request):
        requests.append(request)
        if status is None:raise httpx2.ReadTimeout('offline unknown',request=request)
        return httpx2.Response(status,json={'error':{'message':'offline failure'}})
    admit(rig,1);rig.queue.claim()
    with DefaultHttpxClient(transport=httpx2.MockTransport(respond)) as http:
        native=OpenAITransport(OpenAI(api_key='offline-test',http_client=http))
        with pytest.raises(SearchUnavailable) as caught:
            AttemptTransport(native,rig.queue,1).search('offline query')
    assert len(requests)==1 and caught.value.outcome_known==(status is not None)
    assert rig.queue.job(1)['state']==('execution_unknown' if status is None else 'result_received')


def test_new_arrivals_join_fair_round_robin_after_waiting_user(rig):
    fake=lambda:Fake(dict(intent='free_chat',answer='ok'))
    executor=Executor(rig.queue,rig.access,lambda:rig.api,fake)
    admit(rig,1,1,'A1');admit(rig,2,1,'A2');admit(rig,3,2,'B')
    first=rig.queue.claim();assert first['update_id']==1;executor.execute(first)
    second=rig.queue.claim();assert second['update_id']==3;executor.execute(second)
    admit(rig,4,3,'new C')
    assert rig.queue.claim()['update_id']==2


def test_compare_and_set_rejects_invalid_or_duplicate_state_transition(rig):
    admit(rig,1)
    with pytest.raises(QueueConflict):rig.queue.change(1,'queued','delivered')
    rig.queue.claim();rig.queue.change(1,'claimed','queued')
    with pytest.raises(QueueConflict):rig.queue.change(1,'claimed','queued')


def test_busy_before_delivery_start_retries_only_provably_unstarted_send(rig):
    from steel_agent_v2 import SteelAgent
    from steel_agent_v2.queue_runtime import AttemptTransport
    admit(rig,1);rig.queue.claim()
    fake=Fake(proposal())
    repo=Repository(rig.queue.database,finish_hook=rig.queue.finish_hook(1))
    SteelAgent(AttemptTransport(fake,rig.queue,1),repo).handle(1,SOURCE,event_id='telegram:1')
    scheduler=Scheduler(rig.queue,Executor(rig.queue,rig.access,lambda:rig.api,lambda:fake))
    try:
        with rig.queue.database.connection() as blocker:
            blocker.execute('BEGIN IMMEDIATE')
            scheduler._resume_delivery(1)
            assert rig.queue.job(1)['state']=='committed' and not rig.api.calls
        scheduler._resume_delivery(1)
        assert rig.queue.job(1)['state']=='delivered' and len(fake.calls)==1
    finally:scheduler.close()


def test_actual_runtime_has_one_poller_separate_delivery_clients_and_durable_offset(rig,tmp_path,monkeypatch):
    from steel_agent_v2.queue_runtime import run_queue
    from steel_agent_v2.transport import OpenAITransport
    clients=[];transport_options=[];done=Event();receipts=[];lock=Lock();polls=[]
    monkeypatch.setattr(OpenAITransport,'from_environment',classmethod(
        lambda cls,**kwargs:(transport_options.append(kwargs) or Fake(proposal()))))
    class WorkerAPI:
        def call(self,method,**payload):
            assert method!='getUpdates'
            if method=='sendChatAction':return True  # Ephemeral action is not a delivery receipt.
            assert method=='sendMessage'
            with lock:
                receipts.append(payload['chat_id'])
                number=len(receipts)
                if number==2:(tmp_path/'telegram.maintenance').touch();done.set()
            return {'message_id':number}
    def factory():
        client=WorkerAPI();clients.append(client);return client
    class PollAPI:
        def call(self,method,**payload):
            assert method=='getUpdates';polls.append(payload['offset'])
            if payload['offset']==0:return [update(1,2)]
            if payload['offset']==2:return [update(2,3)]
            assert done.wait(5)
            return []
    assert run_queue(tmp_path,PollAPI(),rig.access,rig.legacy,rig.queue.database.path,api_factory=factory)==0
    assert polls[:2]==[0,2] and rig.legacy.get_offset()==3
    assert sorted(receipts)==[2,3] and len({id(c) for c in clients})==2
    assert transport_options==[{'model':'gpt-6-luna','reasoning':'high'}]*2
    assert Repository(rig.queue.database).load(2).id!=Repository(rig.queue.database).load(3).id


def test_runtime_recovers_offset_mirror_without_replaying_unknown_job(rig,tmp_path):
    from steel_agent_v2.queue_runtime import run_queue
    admit(rig,41);rig.queue.claim();rig.queue.start_api(41,'plan')
    assert rig.legacy.get_offset()==0
    class Poll:
        def call(self,method,**payload):
            assert method=='getUpdates' and payload['offset']==42
            (tmp_path/'telegram.maintenance').touch()
            return []
    assert run_queue(tmp_path,Poll(),rig.access,rig.legacy,rig.queue.database.path,api_factory=lambda:pytest.fail('no delivery'))==0
    assert rig.legacy.get_offset()==42 and rig.queue.job(41)['state']=='execution_unknown'


def test_parallel_diagnostic_contexts_are_distinct_and_do_not_log_input(rig,caplog):
    from diagnostics import CURRENT
    import logging
    contexts=[];entered=Event();release=Event();lock=Lock()
    class Controlled:
        def plan(self,*args,**kwargs):
            with lock:
                contexts.append(CURRENT.get().operation_id)
                if len(contexts)==2:entered.set()
            assert release.wait(5)
            return dict(intent='free_chat',answer='ok')
    admit(rig,1,1,'PRIVATE_CONTEXT_A');admit(rig,2,2,'PRIVATE_CONTEXT_B')
    scheduler=Scheduler(rig.queue,Executor(rig.queue,rig.access,lambda:rig.api,Controlled))
    with caplog.at_level(logging.INFO,logger='operator_diagnostics'):
        try:
            scheduler.tick();assert entered.wait(3);release.set()
            drain(scheduler,lambda:rig.queue.drained())
        finally:release.set();scheduler.close()
    assert len(set(contexts))==2
    assert 'PRIVATE_CONTEXT_A' not in caplog.text and 'PRIVATE_CONTEXT_B' not in caplog.text
    assert 'steel_v2_queue_execution' in caplog.text


def test_no_retroactive_gpt_or_admin_privilege_for_preapproval_admission(rig):
    event=update(1,2,'пока не было доступа')
    rig.queue.admit(event,allowed=False)
    fake=Fake()
    Executor(rig.queue,rig.access,lambda:rig.api,lambda:fake).execute(rig.queue.claim())
    assert not fake.calls and Repository(rig.queue.database).load(2) is None
    rig.permissions.request_access({'id':9})
    callback=dict(update_id=2,callback_query={'id':'cb','from':{'id':1},'data':'access:approve:9:1',
        'message':{'chat':{'type':'private','id':1},'message_id':7}})
    rig.queue.admit(callback,allowed=False,is_admin=False)
    Executor(rig.queue,rig.access,lambda:rig.api).execute(rig.queue.claim())
    assert not rig.access.allowed(9)


@pytest.mark.parametrize('damage',['missing_table','wrong_columns'])
def test_partial_or_malformed_v1_schema_refused_without_repair(rig,damage):
    with rig.queue.write() as c:
        if damage=='missing_table':c.execute('DROP TABLE steel_v2_attempt')
        else:c.execute('ALTER TABLE steel_v2_inbox ADD COLUMN unexpected TEXT')
    before=rig.queue.database.path.read_bytes()
    with pytest.raises(ValueError):DurableQueue(rig.queue.database)
    assert rig.queue.database.path.read_bytes()==before


def test_first_install_adopts_legacy_baseline_only_once(rig):
    assert rig.queue.align_offset(80)==80
    assert rig.queue.admit(update(80),allowed=True)==81
    before=rig.queue.database.path.read_bytes()
    with pytest.raises(QueueConflict):rig.queue.align_offset(90)
    assert rig.queue.database.path.read_bytes()==before and rig.queue.cursor()==81


def test_offset_ahead_of_existing_inbox_is_blocked_even_before_first_runtime(rig):
    admit(rig,1)
    with pytest.raises(QueueConflict):rig.queue.align_offset(10)
    assert rig.queue.cursor()==2 and rig.queue.job(1)['state']=='queued'
