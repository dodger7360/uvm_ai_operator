"""Actual external guard processes, isolated git checkouts and synthetic SQLite."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
from test_steel_agent_v2_queue import rig, admit, update, Fake, proposal, Executor

ROOT=Path(__file__).resolve().parents[1]
OLD='aa11380dfe1ed646fec2355b22880fdb03b0d706'
spec=importlib.util.spec_from_file_location('persistent_guard',ROOT/'ops/windows_guard/guard.py')
guard=importlib.util.module_from_spec(spec);spec.loader.exec_module(guard)


@pytest.fixture
def guard_install(rig,tmp_path):
    root=tmp_path/'checkout';root.mkdir();bundle=tmp_path/'protected-guard';bundle.mkdir()
    for name in ['windows_runtime.py','windows_supervisor.py']:
        shutil.copyfile(ROOT/name,bundle/name)
        shutil.copyfile(ROOT/name,root/name)
    for name in ['guard.py','Start-UVM.ps1','Test-GuardInstall.ps1']:
        shutil.copyfile(ROOT/'ops/windows_guard'/name,bundle/name)
    main='''from pathlib import Path
import time
from windows_runtime import ProcessLock
root=Path(__file__).resolve().parent
with ProcessLock(root/'telegram.lock'):
    with (root/'started.marker').open('a') as f:f.write('one\\n')
    while (root/'hold.marker').exists() and not (root/'release.marker').exists():time.sleep(.01)
'''
    (root/'main.py').write_text(main)
    (root/'.gitignore').write_text('/telegram*\n/*.marker\n')
    git=Path(shutil.which('git')).resolve()
    def command(*args):
        return subprocess.check_output([str(git),'-C',str(root),*args],stderr=subprocess.DEVNULL).decode().strip()
    command('init','-q');command('config','user.name','Offline Test');command('config','user.email','offline@example.invalid')
    command('add','.');command('commit','-qm','old sequential fixture');old=command('rev-parse','HEAD')
    (root/'ops/windows_guard').mkdir(parents=True)
    (root/'ops/windows_guard/queue-protocol.json').write_text(json.dumps(guard.DESCRIPTOR))
    command('add','.');command('commit','-qm','queue protocol fixture');current=command('rev-parse','HEAD')
    with rig.queue.write() as c:c.execute("INSERT INTO steel_v2_queue_meta VALUES('guard_epoch',1)")
    manifest=bundle/'runtime.json'
    m=dict(version=1,root=str(root.resolve()),legacy_database=str(rig.legacy.database.path.resolve()),
           v2_database=str(rig.queue.database.path.resolve()),guard_python=str(Path(sys.executable).resolve()),
           worker_python=str(Path(sys.executable).resolve()),git=str(git),
           bundle={name:guard.digest(bundle/name) for name in guard.BUNDLE},
           phase='active',epoch=1,offset_floor=0,rollback=None,
           approvals=[dict(sha=old,queue_protocol=0,handlers=['v2','legacy']),
                      dict(sha=current,queue_protocol=1,handlers=['v2','legacy'])])
    state=SimpleNamespace(root=root,bundle=bundle,manifest=manifest,m=m,git=command,old=old,current=current,rig=rig)
    def save():manifest.write_text(json.dumps(m))
    def config(value):
        path=Path(m['root'])/'telegram-handler.json'
        if value is None:path.unlink(missing_ok=True)
        else:path.write_text(json.dumps(value))
    def invoke(mode='supervise'):
        root=Path(m['root'])
        args=['--worker-script',str(root/'main.py')] if mode=='worker' else ['--supervisor-script',str(root/'windows_supervisor.py')] if mode=='supervise' else []
        return subprocess.run([sys.executable,'-I','-B',str(bundle/'guard.py'),mode,'--manifest',str(manifest),*args],
                              capture_output=True,timeout=10)
    def published():
        clone=tmp_path/'published-checkout'
        subprocess.run([str(git),'clone','-q','--shared','--no-checkout',str(ROOT),str(clone)],check=True,capture_output=True)
        subprocess.run([str(git),'-C',str(clone),'checkout','-q','--detach',OLD],check=True,capture_output=True)
        m['root']=str(clone.resolve());m['approvals'].append(dict(sha=OLD,queue_protocol=0,handlers=['v2','legacy']))
        save();state.root=clone
    state.save=save;state.config=config;state.invoke=invoke;state.published=published
    config({'handler':'v2','database':m['v2_database']});save()
    return state


def assert_denied(s, reason=None, mode='supervise'):
    result=s.invoke(mode)
    assert result.returncode==7, (result.stdout,result.stderr)
    assert b'guard_denied:' in result.stderr
    if reason:assert reason.encode() in result.stderr
    assert not (Path(s.m['root'])/'started.marker').exists()


def test_compatible_v2_launches_through_installed_guard(guard_install):
    s=guard_install
    result=s.invoke('supervise')
    assert result.returncode==0,result.stderr
    assert (s.root/'started.marker').read_text().splitlines()==['one']


@pytest.mark.parametrize('config',[None,{'handler':'legacy'},'v2'])
def test_old_sha_refused_independent_of_handler(guard_install,config):
    s=guard_install;s.git('checkout','-q','--detach',s.old)
    s.config({'handler':'v2','database':s.m['v2_database']} if config=='v2' else config)
    assert_denied(s)


@pytest.mark.parametrize('state',['queued','execution_unknown','delivery_unknown','committed','delivered'])
def test_old_release_never_runs_even_when_queue_fully_drained(guard_install,state):
    s=guard_install;admit(s.rig,41)
    with s.rig.queue.write() as c:c.execute('UPDATE steel_v2_inbox SET state=?',(state,))
    s.rig.legacy.set_offset(42)
    s.git('checkout','-q','--detach',s.old)
    assert_denied(s)


@pytest.mark.parametrize('damage',['missing-manifest','broken-manifest','unknown-schema','missing-db','path-alias',
                                   'epoch','ahead-offset','changed-db','bundle','bad-protocol'])
def test_fail_closed_before_any_worker_or_polling(guard_install,damage):
    s=guard_install
    if damage=='missing-manifest':s.manifest.unlink()
    elif damage=='broken-manifest':s.manifest.write_text('{')
    elif damage=='missing-db':Path(s.m['v2_database']).rename(s.bundle/'unavailable.sqlite3')
    elif damage=='path-alias':s.m['legacy_database']=s.m['v2_database'];s.save()
    elif damage=='epoch':s.m['epoch']=2;s.save()
    elif damage=='ahead-offset':s.rig.legacy.set_offset(1)
    elif damage=='changed-db':s.config({'handler':'v2','database':s.m['legacy_database']})
    elif damage=='bundle':(s.bundle/'windows_supervisor.py').write_text('raise RuntimeError()')
    elif damage=='bad-protocol':s.m['approvals'][1]['queue_protocol']=0;s.save()
    else:
        with s.rig.queue.write() as c:c.execute("UPDATE steel_v2_queue_meta SET value=2 WHERE key='schema'")
    assert_denied(s)


def test_safe_lagging_mirror_allowed_only_for_compatible_recovery(guard_install):
    s=guard_install;admit(s.rig,41)
    assert s.rig.legacy.get_offset()==0
    assert s.invoke().returncode==0


def test_alias_to_same_v2_file_is_canonicalized_but_not_legacy_alias(guard_install):
    s=guard_install
    alias=s.root/'v2-alias.sqlite3';os.link(s.m['v2_database'],alias)
    s.config({'handler':'v2','database':str(alias)})
    assert s.invoke().returncode==0
    # Different spellings/hardlinks must not let legacy and v2 be one file.
    s.m['legacy_database']=str(alias.resolve());s.save()
    result=s.invoke();assert result.returncode==7 and b'database_alias_collision' in result.stderr


def test_checkout_after_guard_install_does_not_replace_external_barrier(guard_install):
    s=guard_install;before=guard.digest(s.bundle/'guard.py')
    s.git('checkout','-q','--detach',s.old);s.config(None)
    assert_denied(s)
    assert guard.digest(s.bundle/'guard.py')==before


def test_atomic_manifest_failure_leaves_previous_document(guard_install,monkeypatch,tmp_path):
    s=guard_install;before=s.manifest.read_bytes()
    def fail(*a):raise OSError('synthetic replace failure')
    monkeypatch.setattr(guard.os,'replace',fail)
    monkeypatch.setattr(guard,'administrative_owner',lambda path:None)  # Mock elevated install environment only.
    with pytest.raises(OSError):guard.atomic_write(s.manifest,{'invalid':'proposal'})
    assert s.manifest.read_bytes()==before and not list(s.bundle.glob('manifest-*.tmp'))


def test_duplicate_supervisor_never_starts_second_worker(guard_install):
    s=guard_install;(s.root/'hold.marker').touch()
    args=[sys.executable,'-I','-B',str(s.bundle/'guard.py'),'supervise','--manifest',str(s.manifest),
          '--supervisor-script',str(s.root/'windows_supervisor.py')]
    child=subprocess.Popen(args,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    try:
        deadline=time.monotonic()+5
        while not (s.root/'started.marker').exists() and time.monotonic()<deadline:time.sleep(.01)
        assert (s.root/'started.marker').exists()
        assert s.invoke('supervise').returncode==0
        assert (s.root/'started.marker').read_text().splitlines()==['one']
    finally:
        (s.root/'release.marker').touch();child.wait(timeout=10)
        child.stderr.close()


def retired(s,tmp_path):
    from scripts.v2_queue_status import prepare_rollback,inspect
    archive=tmp_path/'archived-epoch.sqlite3'
    cursor=s.rig.queue.cursor()
    s.m.update(phase='armed',offset_floor=cursor);s.save()
    prepare_rollback(s.m['v2_database'],archive,inspect(s.m['v2_database'])['rollback_confirmation'],
                     legacy_database=s.m['legacy_database'],startup_manifest=s.manifest,startup_guard=s.bundle/'guard.py')
    with guard.read_db(archive) as c:core=guard.core_hash(c)
    s.m.update(phase='retired',offset_floor=cursor,
               rollback=dict(epoch=1,cursor=cursor,archive=str(archive.resolve()),sha256=guard.digest(archive),core=core))
    s.git('checkout','-q','--detach',s.old);s.save()
    return archive


def test_retired_requires_completed_archive_and_allows_previous_handler(guard_install,tmp_path):
    s=guard_install;admit(s.rig,41)
    Executor(s.rig.queue,s.rig.access,lambda:s.rig.api,lambda:Fake(proposal())).execute(s.rig.queue.claim())
    s.rig.legacy.set_offset(s.rig.queue.cursor())
    archive=retired(s,tmp_path)
    s.config(None)
    assert s.invoke().returncode==0
    (s.root/'started.marker').unlink()
    archive.unlink();assert_denied(s)


@pytest.mark.parametrize('kind',['pending','execution_unknown','delivery_unknown','outbox','inflight'])
def test_retirement_refuses_unfinished_archives_even_after_manual_drop(guard_install,tmp_path,kind):
    from scripts.v2_sqlite_snapshot import snapshot
    from steel_agent_v2 import Repository
    s=guard_install;admit(s.rig,41);s.rig.legacy.set_offset(42)
    with s.rig.queue.write() as c:
        if kind in {'execution_unknown','delivery_unknown'}:
            c.execute('UPDATE steel_v2_inbox SET state=?',(kind,))
        if kind=='outbox':
            c.execute("UPDATE steel_v2_inbox SET state='delivered'")
            c.execute("INSERT INTO steel_v2_outbox VALUES(41,0,'sendMessage','{}','delivery_pending',NULL)")
    if kind=='inflight':Repository(s.rig.queue.database).begin(1,'telegram:41','synthetic')
    archive=tmp_path/'unfinished-archive.sqlite3'
    snapshot(s.m['v2_database'],archive,tmp_path/'unfinished.manifest.json')
    with guard.read_db(archive) as c:core=guard.core_hash(c)
    with s.rig.queue.write() as c:
        for table in guard.QUEUE:c.execute('DROP TABLE '+table)
    s.m.update(phase='retired',offset_floor=42,
               rollback=dict(epoch=1,cursor=42,archive=str(archive.resolve()),sha256=guard.digest(archive),core=core))
    s.git('checkout','-q','--detach',s.old);s.save()
    assert_denied(s)


def test_offline_update_requires_maintenance_locks_confirmation_and_atomic_owner(guard_install,tmp_path,monkeypatch):
    from windows_runtime import ProcessLock
    s=guard_install;before=s.manifest.read_bytes();proposal=tmp_path/'proposal.json'
    s.m['phase']='armed';proposal.write_text(json.dumps(s.m))
    with pytest.raises(guard.Denied,match='maintenance_required'):
        guard.update(s.manifest,proposal,guard.digest(proposal),bundle=s.bundle)
    (s.root/'telegram.maintenance').touch()
    with ProcessLock(s.root/'telegram.lock'):
        with pytest.raises(RuntimeError):guard.update(s.manifest,proposal,guard.digest(proposal),bundle=s.bundle)
    with pytest.raises(guard.Denied,match='confirmation'):
        guard.update(s.manifest,proposal,'wrong',bundle=s.bundle)
    # An owner/ACL failure occurs before replacing the protected old manifest.
    def owner_fail(path):raise guard.Denied('elevated_manifest_update_required')
    monkeypatch.setattr(guard,'administrative_owner',owner_fail)
    with pytest.raises(guard.Denied,match='elevated_manifest'):
        guard.update(s.manifest,proposal,guard.digest(proposal),bundle=s.bundle)
    assert s.manifest.read_bytes()==before
    monkeypatch.setattr(guard,'administrative_owner',lambda path:None)
    guard.update(s.manifest,proposal,guard.digest(proposal),bundle=s.bundle)
    assert guard.read_json(s.manifest)['phase']=='armed'
    assert s.invoke('supervise').returncode==7


def test_epoch_binding_is_explicit_offline_and_preserves_core(guard_install):
    from scripts.v2_queue_epoch import bind
    s=guard_install
    with s.rig.queue.write() as c:c.execute("DELETE FROM steel_v2_queue_meta WHERE key='guard_epoch'")
    s.m['phase']='armed';s.save();(s.root/'telegram.maintenance').touch()
    with guard.read_db(s.m['v2_database']) as c:before=guard.core_hash(c)
    with pytest.raises(RuntimeError):bind(s.manifest,s.bundle/'guard.py','wrong')
    bind(s.manifest,s.bundle/'guard.py','activate:1')
    bind(s.manifest,s.bundle/'guard.py','activate:1')
    with guard.read_db(s.m['v2_database']) as c:
        assert guard.queue_state(c)['epoch']==1 and guard.core_hash(c)==before
    assert s.rig.legacy.get_offset()==0


def test_protocol_marker_does_not_allow_queue_activation_in_baseline(guard_install):
    s=guard_install
    with s.rig.queue.write() as c:
        for table in guard.QUEUE:c.execute('DROP TABLE '+table)
    s.m.update(phase='baseline',epoch=0);s.save()
    assert_denied(s,'queue_activation_required')


def test_bound_epoch_cannot_be_dropped_before_manifest_is_armed(guard_install,tmp_path):
    from scripts.v2_queue_status import prepare_rollback,inspect
    s=guard_install;archive=tmp_path/'refused.sqlite3'
    confirmation=inspect(s.m['v2_database'])['rollback_confirmation']
    with pytest.raises(ValueError,match='persistent_guard_required'):
        prepare_rollback(s.m['v2_database'],archive,confirmation,legacy_database=s.m['legacy_database'])
    with pytest.raises(ValueError,match='not_armed'):
        prepare_rollback(s.m['v2_database'],archive,confirmation,legacy_database=s.m['legacy_database'],
                         startup_manifest=s.manifest,startup_guard=s.bundle/'guard.py')
    assert not archive.exists() and s.rig.queue.cursor()==0


def test_stale_closed_archive_cannot_authorize_loss_of_new_pending_jobs(guard_install,tmp_path,monkeypatch):
    from scripts.v2_sqlite_snapshot import snapshot
    s=guard_install;archive=tmp_path/'old-closed-archive.sqlite3'
    snapshot(s.m['v2_database'],archive,tmp_path/'old.manifest.json')
    with guard.read_db(archive) as c:core=guard.core_hash(c)
    admit(s.rig,41);s.rig.legacy.set_offset(42)
    (s.root/'telegram.maintenance').touch()
    m=dict(s.m,phase='armed',offset_floor=42);proposal=tmp_path/'arming.json';proposal.write_text(json.dumps(m))
    monkeypatch.setattr(guard,'administrative_owner',lambda path:None)
    guard.update(s.manifest,proposal,guard.digest(proposal),bundle=s.bundle)
    # Even an operator's manual DROP cannot make an earlier archive current.
    with s.rig.queue.write() as c:
        for table in guard.QUEUE:c.execute('DROP TABLE '+table)
    m.update(phase='retired',rollback=dict(epoch=1,cursor=0,archive=str(archive),sha256=guard.digest(archive),core=core))
    proposal.write_text(json.dumps(m))
    with pytest.raises(guard.Denied,match='rollback_epoch_mismatch'):
        guard.update(s.manifest,proposal,guard.digest(proposal),bundle=s.bundle)
    assert guard.read_json(s.manifest)['phase']=='armed'


def test_guard_check_is_readonly_for_both_databases(guard_install):
    s=guard_install;before={n:guard.digest(s.m[n]) for n in ['legacy_database','v2_database']}
    assert s.invoke('check').returncode==0
    assert before=={n:guard.digest(s.m[n]) for n in before}


def test_failed_manual_start_retains_maintenance_without_fallback(guard_install):
    s=guard_install;(s.root/'telegram.maintenance').touch();s.manifest.unlink()
    assert_denied(s,mode='start')
    assert (s.root/'telegram.maintenance').exists()


def test_worker_entry_requires_a_live_supervisor_lock(guard_install):
    assert_denied(guard_install,'supervisor_required',mode='worker')


def test_manifest_diagnostics_do_not_echo_arbitrary_content(guard_install):
    s=guard_install;s.manifest.write_text('{"PRIVATE_MESSAGE_NOT_TO_LOG":')
    result=s.invoke()
    assert result.returncode==7 and b'PRIVATE_MESSAGE' not in result.stderr


def test_powershell_installers_parse_without_running_them(tmp_path):
    script=tmp_path/'parse.ps1'
    script.write_text('''param([string]$Folder)
foreach ($file in Get-ChildItem -LiteralPath $Folder -Filter '*.ps1') {
    $tokens=$null; $errors=$null
    [System.Management.Automation.Language.Parser]::ParseFile($file.FullName,[ref]$tokens,[ref]$errors) | Out-Null
    if ($errors.Count) { throw 'Invalid PowerShell installer syntax' }
}
Write-Output 'parse-ok'
''')
    result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',
                           str(script),'-Folder',str(ROOT/'ops/windows_guard')],capture_output=True,timeout=15)
    assert result.returncode==0,result.stderr


def test_proposal_builder_and_update_complete_activation_without_gap(guard_install,tmp_path,monkeypatch):
    from test_steel_agent_v2 import proposal as proposal_plan
    from scripts.v2_guard_proposal import generate
    from scripts.v2_queue_epoch import bind
    from steel_agent_v2.queue import DurableQueue
    s=guard_install
    with s.rig.queue.write() as c:
        for table in guard.QUEUE:c.execute('DROP TABLE '+table)
    s.git('checkout','-q','--detach',s.old)
    s.manifest.unlink();(s.root/'telegram.maintenance').touch()
    monkeypatch.setattr(guard,'administrative_owner',lambda path:None)
    def proposal(action,**kw):
        out=tmp_path/(action+'-'+str(time.monotonic_ns())+'.json')
        args=SimpleNamespace(action=action,guard=s.bundle/'guard.py',manifest=s.manifest,out=out,archive=None,
            root=s.root,legacy_database=Path(s.m['legacy_database']),v2_database=Path(s.m['v2_database']),
            guard_python=Path(s.m['guard_python']),worker_python=Path(s.m['worker_python']),git=Path(s.m['git']),
            sha=None,protocol=None,handlers=None)
        for k,v in kw.items():setattr(args,k,v)
        generate(args);guard.update(s.manifest,out,guard.digest(out),bundle=s.bundle)
    proposal('baseline',sha=s.old,protocol=0,handlers=['v2'])
    assert guard.check(s.manifest,bundle=s.bundle,allow_maintenance=True)['phase']=='baseline'
    proposal('arm',sha=s.current,protocol=1,handlers=['v2'])
    s.git('checkout','-q','--detach',s.current)
    assert s.invoke().returncode==7  # No launch between code switch and queue activation.
    DurableQueue(s.rig.queue.database).align_offset(s.rig.legacy.get_offset())
    bind(s.manifest,s.bundle/'guard.py','activate:1')
    assert s.invoke().returncode==7  # Epoch alone is not authorization to launch.
    proposal('activate')
    assert guard.check(s.manifest,bundle=s.bundle,allow_maintenance=True)['phase']=='active'
    # Retirement is published before checkout of old code. The currently installed
    # queue release must remain refused once retired, not prevent recording retirement.
    (s.root/'telegram.maintenance').unlink()
    admit(s.rig,41)
    Executor(s.rig.queue,s.rig.access,lambda:s.rig.api,lambda:Fake(proposal_plan())).execute(s.rig.queue.claim())
    s.rig.legacy.set_offset(42);(s.root/'telegram.maintenance').touch()
    proposal('arm')
    from scripts.v2_queue_status import prepare_rollback,inspect
    archive=tmp_path/'complete-lifecycle.sqlite3'
    prepare_rollback(s.m['v2_database'],archive,inspect(s.m['v2_database'])['rollback_confirmation'],
                     legacy_database=s.m['legacy_database'],startup_manifest=s.manifest,startup_guard=s.bundle/'guard.py')
    proposal('retire',archive=archive)
    with pytest.raises(guard.Denied,match='queue_activation_required'):
        guard.check(s.manifest,bundle=s.bundle,allow_maintenance=True)
    s.git('checkout','-q','--detach',s.old)
    assert guard.check(s.manifest,bundle=s.bundle,allow_maintenance=True)['phase']=='retired'


def test_checkout_start_delegates_without_removing_maintenance_or_launching_directly(tmp_path):
    # Replace the Windows known-folder lookup only in this temporary launch environment.
    data=tmp_path/'programdata';bundle=data/'UVM_Steel-Guard-v1';bundle.mkdir(parents=True)
    (bundle/'Start-UVM.ps1').write_text('param([int]$StartupAttempts)\n[IO.File]::WriteAllText((Join-Path $PSScriptRoot "called.marker"),[string]$StartupAttempts)\nexit 0\n')
    source=(ROOT/'start-telegram.ps1').read_text()
    source=source.replace("([Environment]::GetFolderPath('CommonApplicationData'))", "'"+str(data)+"'")
    script=tmp_path/'start-telegram.ps1';script.write_text(source)
    maintenance=tmp_path/'telegram.maintenance';maintenance.touch()
    result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass',
                           '-File',str(script),'-StartupAttempts','3'],capture_output=True,timeout=15)
    assert result.returncode==0,result.stderr
    assert (bundle/'called.marker').read_text()=='3' and maintenance.exists()
    (bundle/'Start-UVM.ps1').unlink();(bundle/'called.marker').unlink()
    denied=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass',
                           '-File',str(script)],capture_output=True,timeout=15)
    assert denied.returncode!=0 and maintenance.exists() and not (bundle/'called.marker').exists()


def test_unknown_v1_table_layout_refused_before_launch(guard_install):
    s=guard_install
    with s.rig.queue.write() as c:c.execute('ALTER TABLE steel_v2_inbox ADD COLUMN unapproved TEXT')
    assert_denied(s,'unknown_queue_layout')


def test_cli_cannot_substitute_another_manifest_for_the_installed_one(guard_install,tmp_path):
    s=guard_install;shadow=tmp_path/'shadow.json';shadow.write_bytes(s.manifest.read_bytes())
    result=subprocess.run([sys.executable,'-I','-B',str(s.bundle/'guard.py'),'supervise','--manifest',str(shadow),
                           '--supervisor-script',str(s.root/'windows_supervisor.py')],capture_output=True,timeout=10)
    assert result.returncode==7 and b'persistent_manifest_path_required' in result.stderr
    assert not (s.root/'started.marker').exists()


def test_manual_drop_does_not_authorize_old_sha(guard_install):
    s=guard_install
    with s.rig.queue.write() as c:
        for table in guard.QUEUE:c.execute('DROP TABLE '+table)
    s.git('checkout','-q','--detach',s.old);s.config(None)
    assert_denied(s)


def test_forged_protocol_descriptor_in_old_untracked_tree_not_sufficient(guard_install):
    s=guard_install;s.git('checkout','-q','--detach',s.old)
    (s.root/'ops/windows_guard').mkdir(parents=True,exist_ok=True)
    (s.root/'ops/windows_guard/queue-protocol.json').write_text(json.dumps(guard.DESCRIPTOR))
    s.m['approvals'][0]['queue_protocol']=1;s.save()
    assert_denied(s,'queue_protocol_incompatible')
