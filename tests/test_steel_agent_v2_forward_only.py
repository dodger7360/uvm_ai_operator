"""Forward-only replaces stage-18 infrastructure tests; all data checks stay live."""
from pathlib import Path
from types import SimpleNamespace
import json
import sqlite3
import subprocess
import sys
from unittest.mock import Mock

import pytest
from database import Database
from steel_agent_v2 import Repository, SteelAgent
from steel_agent_v2.queue import DurableQueue, QueueConflict, TABLES
from steel_agent_v2.queue_runtime import Executor
from scripts.v2_queue_status import inspect, prepare_rollback
from scripts.v2_sqlite_snapshot import snapshot, metadata
from test_steel_agent_v2_queue import rig, admit, Fake, proposal

ROOT=Path(__file__).resolve().parents[1]
from prequeue_fixture import seed_prequeue




def digest(db):
    with db.connection() as c:return metadata(c)




@pytest.mark.parametrize('maintenance',[False,True])
def test_real_powershell_manual_start_without_external_files_or_elevation(tmp_path,maintenance):
    import shutil
    shutil.copyfile(ROOT/'start-telegram.ps1',tmp_path/'start-telegram.ps1')
    if maintenance:(tmp_path/'telegram.maintenance').touch()
    # Execute the actual script, intercept only OS process creation. Nothing starts.
    code=r'''
param($Folder)
$ErrorActionPreference='Stop'
function Start-Process {
 param($FilePath,$ArgumentList,$WorkingDirectory,$WindowStyle)
 if ($WindowStyle -ne 'Hidden') { throw 'visible process' }
 if ($WorkingDirectory -ne $Folder) { throw 'wrong root' }
 if ($FilePath -ne (Join-Path $Folder '.venv\Scripts\pythonw.exe')) { throw 'wrong interpreter' }
 if ($ArgumentList[0] -ne ('"'+(Join-Path $Folder 'windows_supervisor.py')+'"')) { throw 'wrong supervisor' }
 if ($ArgumentList[1] -ne '--initial-attempts' -or $ArgumentList[2] -ne 2) { throw 'wrong policy' }
 Write-Output 'ordinary-supervisor-launch'
}
& (Join-Path $Folder 'start-telegram.ps1')
'''
    script=tmp_path/'harness.ps1';script.write_text(code)
    result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass',
                           '-File',str(script),'-Folder',str(tmp_path)],capture_output=True,timeout=15)
    assert result.returncode==0,result.stderr
    assert b'ordinary-supervisor-launch' in result.stdout
    assert not (tmp_path/'telegram.maintenance').exists()


@pytest.mark.parametrize('entry',['telegram_bot.py','windows_supervisor.py','windows_runtime.py',
    'start-telegram.ps1','stop-telegram.ps1','steel_agent_v2/queue.py','steel_agent_v2/queue_runtime.py',
    'steel_agent_v2/repository.py','steel_agent_v2/runtime.py','scripts/v2_queue_status.py'])
def test_supported_runtime_has_no_external_guard_dependency(entry):
    source=(ROOT/entry).read_text(encoding='utf-8').lower()
    for forbidden in ['programdata','commonapplicationdata','windows_guard','startup_manifest',
                      'startup_guard','runas','icacls','register-scheduledtask','guard_epoch']:
        assert forbidden not in source


@pytest.mark.parametrize('state',['queued','claimed','api_started','result_received','committed',
                                  'delivery_started','execution_unknown','delivery_unknown','delivered'])
def test_forward_reopen_and_backup_keep_every_queue_stage_and_core_history(rig,tmp_path,state):
    admit(rig,41)
    job=rig.queue.claim()
    if state in {'committed','delivery_started','delivery_unknown','delivered'}:
        Executor(rig.queue,rig.access,lambda:rig.api,lambda:Fake(proposal())).execute(job)
    with rig.queue.write() as c:
        c.execute('UPDATE steel_v2_inbox SET state=? WHERE update_id=41',(state,))
        if state=='delivery_started':
            c.execute("UPDATE steel_v2_outbox SET state='delivery_started',message_id=NULL WHERE update_id=41")
    rig.legacy.set_offset(42)
    before=digest(rig.queue.database)
    backup=tmp_path/'backup.sqlite3'
    snapshot(rig.queue.database.path,backup,tmp_path/'manifest.json')
    Repository(rig.queue.database);q=DurableQueue(rig.queue.database)
    assert digest(q.database)==before
    assert q.align_offset(42)==42
    q.recover()
    expected=('execution_unknown' if state in {'api_started','result_received'} else
              'delivery_unknown' if state=='delivery_started' else 'queued' if state=='claimed' else state)
    assert q.job(41)['state']==expected
    after=digest(q.database)
    for table in ['steel_v2_active','steel_v2_version','steel_v2_turn']:
        assert before['tables'][table]==after['tables'][table]
    with sqlite3.connect(backup) as c:assert metadata(c)==before
    assert rig.legacy.get_offset()==42
    if expected.endswith('unknown'):
        q.recover();assert not q.delivery_candidates()
        assert q.claim() is None


@pytest.mark.parametrize('damage', ['future','missing-schema','missing-cursor','negative-cursor',
    'string-cursor','negative-served','bad-initialized','cursor-behind-inbox',
    'missing-table','missing-key','orphan-attempt','orphan-outbox'])
def test_unknown_or_damaged_data_refused_without_migration_or_cleanup(rig,damage):
    admit(rig,41)
    with rig.queue.write() as c:
        if damage=='future':c.execute("UPDATE steel_v2_queue_meta SET value=2 WHERE key='schema'")
        elif damage.startswith('missing-') and damage.split('-')[1] in {'schema','cursor'}:
            c.execute('DELETE FROM steel_v2_queue_meta WHERE key=?',(damage[8:],))
        elif damage in {'negative-cursor','string-cursor','cursor-behind-inbox'}:
            value={'negative-cursor':-1,'string-cursor':'not-integer','cursor-behind-inbox':41}[damage]
            c.execute("UPDATE steel_v2_queue_meta SET value=? WHERE key='cursor'",(value,))
        elif damage=='negative-served':c.execute("UPDATE steel_v2_queue_meta SET value=-1 WHERE key='served'")
        elif damage=='bad-initialized':c.execute("INSERT INTO steel_v2_queue_meta VALUES('initialized',2)")
        elif damage=='missing-table':c.execute('DROP TABLE steel_v2_outbox')
        elif damage=='missing-key':
            c.execute('ALTER TABLE steel_v2_queue_meta RENAME TO old_meta')
            c.execute('CREATE TABLE steel_v2_queue_meta(key TEXT,value INTEGER NOT NULL)')
            c.execute('INSERT INTO steel_v2_queue_meta SELECT * FROM old_meta');c.execute('DROP TABLE old_meta')
        elif damage=='orphan-attempt':c.execute("INSERT INTO steel_v2_attempt VALUES(99,1,'plan','api_started')")
        elif damage=='orphan-outbox':c.execute("INSERT INTO steel_v2_outbox VALUES(99,1,'sendMessage','{}','delivery_pending',NULL)")
    before=digest(rig.queue.database)
    for factory in [Repository,DurableQueue]:
        with pytest.raises((ValueError,sqlite3.DatabaseError)):factory(rig.queue.database)
        assert digest(rig.queue.database)==before
    with pytest.raises((ValueError,sqlite3.DatabaseError)):inspect(rig.queue.database.path,rig.legacy.database.path)


@pytest.mark.parametrize('offset',[-1,True,1.5,'42'])
def test_invalid_mirror_is_not_silently_adopted(rig,offset):
    before=digest(rig.queue.database)
    with pytest.raises(QueueConflict):rig.queue.align_offset(offset)
    assert digest(rig.queue.database)==before


def test_real_old_core_migrates_additively_and_reopens_without_external_manifest(tmp_path):
    db=Database(tmp_path/'old-production-shape.sqlite3')
    seed_prequeue(db)
    before=digest(db)
    assert len(before['tables'])==4
    Repository(db);q=DurableQueue(db)
    q.align_offset(1234)
    Repository(db);DurableQueue(db)
    after=digest(db)
    assert set(after['tables'])==set(before['tables'])|set(TABLES)
    for table in before['tables']:assert after['tables'][table]==before['tables'][table]
    assert q.cursor()==1234 and Repository(db).load(1).revision==1
    shown=SteelAgent(Fake({'intent':'show_request'}),Repository(db)).handle(1,'Покажи',event_id='new-code')
    assert shown.status=='ok' and Repository(db).load(1).revision==1


@pytest.mark.parametrize('source',['corrupt','missing'])
def test_readonly_diagnostics_never_recreates_unreadable_database(tmp_path,source):
    path=tmp_path/'v2.sqlite3'
    if source=='corrupt':path.write_bytes(b'not sqlite')
    before=path.read_bytes() if path.exists() else None
    with pytest.raises(sqlite3.DatabaseError):inspect(path)
    assert (path.read_bytes() if path.exists() else None)==before


@pytest.mark.parametrize('offset',[0,42,43])
def test_forward_cursor_check_keeps_safe_lag_and_refuses_ahead(rig,offset):
    admit(rig,41);rig.legacy.set_offset(offset)
    before=digest(rig.queue.database)
    state=inspect(rig.queue.database.path,rig.legacy.database.path)
    assert state['forward_compatible']==(offset<=42)
    assert state['mirror_recovery_required']==(offset<42)
    assert digest(rig.queue.database)==before


def test_stale_guard_metadata_does_not_require_retirement_or_delete_data(rig):
    with rig.queue.write() as c:c.execute("INSERT INTO steel_v2_queue_meta VALUES('guard_epoch',1)")
    before=digest(rig.queue.database)
    q=DurableQueue(rig.queue.database);Repository(rig.queue.database)
    assert digest(q.database)==before
    assert q.cursor()==0


def test_rollback_entrypoint_cannot_remove_even_fully_drained_queue(rig,tmp_path):
    before=digest(rig.queue.database)
    with pytest.raises(ValueError,match='forward_only'):
        prepare_rollback(rig.queue.database.path,tmp_path/'never.sqlite3','rollback:0')
    assert digest(rig.queue.database)==before and not (tmp_path/'never.sqlite3').exists()


def test_normal_supervisor_uses_existing_lock_without_guard(tmp_path,monkeypatch):
    import windows_supervisor as supervisor
    launches=[]
    def launch(command,**kwargs):
        launches.append(command)
        (tmp_path/'telegram.ready').touch()
        (tmp_path/'telegram.maintenance').touch()
        return SimpleNamespace(pid=7,returncode=0,poll=lambda:0)
    monkeypatch.setattr(supervisor.subprocess,'Popen',launch)
    assert supervisor.supervise(tmp_path,['offline-fake-worker'],poll_interval=0)==0
    assert launches==[['offline-fake-worker']]


def test_validation_uses_single_snapshot_during_concurrent_admission(rig,monkeypatch):
    from threading import Event,Thread
    admit(rig,1)
    with rig.queue.database.connection() as c:c.execute('PRAGMA journal_mode=WAL')
    ready,done=Event(),Event();errors=[]
    def writer():
        try:
            assert ready.wait(3)
            admit(rig,2,user=2)
        except BaseException as e:errors.append(e)
        finally:done.set()
    real=DurableQueue._validate_schema
    class ReadBarrier:
        def __init__(self,connection):self.connection=connection
        def __getattr__(self,name):return getattr(self.connection,name)
        def execute(self,sql,*args):
            cursor=self.connection.execute(sql,*args)
            if sql=='SELECT key,value FROM steel_v2_queue_meta':
                rows=cursor.fetchall();ready.set()
                assert done.wait(3)
                return rows
            return cursor
    monkeypatch.setattr(DurableQueue,'_validate_schema',staticmethod(lambda c:real(ReadBarrier(c))))
    thread=Thread(target=writer);thread.start()
    try:Repository(rig.queue.database)
    finally:ready.set();thread.join(4)
    assert not thread.is_alive() and not errors
    assert rig.queue.cursor()==3 and rig.queue.job(2)['user_id']==2


def test_additive_migration_fault_rolls_back_all_ddl_without_touching_history(tmp_path,monkeypatch):
    db=Database(tmp_path/'pre-queue.sqlite3')
    seed_prequeue(db)
    before=digest(db)
    def fail(_):raise RuntimeError('injected before migration commit')
    monkeypatch.setattr(DurableQueue,'_validate_schema',staticmethod(fail))
    with pytest.raises(RuntimeError,match='injected'):DurableQueue(db)
    assert digest(db)==before


def test_active_entrypoint_has_only_v2_brain_and_no_legacy_handler_exports():
    import ast
    import telegram_bot as bot
    tree=ast.parse((ROOT/'telegram_bot.py').read_text(encoding='utf-8'))
    imported={node.module for node in ast.walk(tree) if isinstance(node,ast.ImportFrom)}
    assert not imported.intersection({'ai','telegram_service','work_assistant','legacy_telegram_adapter',
                                     'repositories.normalization_policy_repository'})
    assert not hasattr(bot,'handle_update') and not hasattr(bot,'AIService')
    # The current selector has no mode allowing any caller to re-enable legacy.
    import inspect as python_inspect
    from steel_agent_v2.runtime import selection
    assert list(python_inspect.signature(selection).parameters)==['root']


@pytest.mark.parametrize('config',[None,{'handler':'legacy'}])
def test_selector_always_rejects_old_ai_even_without_queue(tmp_path,config):
    from steel_agent_v2.runtime import selection
    if config is not None:(tmp_path/'telegram-handler.json').write_text(json.dumps(config))
    with pytest.raises(ValueError):selection(tmp_path)
