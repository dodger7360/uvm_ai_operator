"""Persistent Windows launch boundary. Standard library only; no checkout imports.

Install this file and frozen supervisor/locks outside the mutable checkout.
All database connections are read-only. No migration, replay, offset update or API.
"""
import argparse
from contextlib import closing, contextmanager
import hashlib
import importlib.util
import json
import logging
import os
from pathlib import Path
import re
import runpy
import sqlite3
import subprocess
import sys
import tempfile

CORE = {'steel_v2_active','steel_v2_version','steel_v2_turn','steel_v2_inflight'}
QUEUE = {'steel_v2_inbox','steel_v2_attempt','steel_v2_outbox','steel_v2_queue_meta'}
STATES = {'queued','claimed','api_started','result_received','committed','delivery_pending',
          'delivery_started','delivered','denied','rejected','execution_unknown','delivery_unknown'}
SEND_STATES = {'delivery_pending','delivery_started','delivery_unknown','delivered','rejected'}
DESCRIPTOR = {'queue_protocol':1,'schema_versions':[1],'admission':'write-before-ack-v1',
              'commit':'snapshot-history-outbox-v1','recovery':'quarantine-unknown-v1',
              'epoch_key':'guard_epoch'}
BUNDLE = {'guard.py','windows_runtime.py','windows_supervisor.py','Start-UVM.ps1','Test-GuardInstall.ps1'}
FIELDS = {'version','root','legacy_database','v2_database','guard_python','worker_python','git',
          'bundle','phase','epoch','offset_floor','approvals','rollback'}


class Denied(RuntimeError):
    """Only constant diagnostic reasons are exposed by the command line."""


def require(ok, reason):
    if not ok: raise Denied(reason)


def digest(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def persistent_manifest(path, bundle):
    require(Path(path).resolve()==Path(bundle).resolve()/'runtime.json', 'persistent_manifest_path_required')


def parse_json(data):
    def pairs(values):
        result = {}
        for key,value in values:
            require(key not in result, 'duplicate_json_key'); result[key] = value
        return result
    return json.loads(data, object_pairs_hook=pairs)


def read_json(path):return parse_json(Path(path).read_text(encoding='utf-8-sig'))


def canonical(value):
    require(isinstance(value,str) and Path(value).is_absolute(), 'absolute_path_required')
    require(not value.startswith(('\\\\','//')), 'remote_runtime_path')
    path = Path(value).resolve(strict=True)
    require(str(path) == value, 'noncanonical_manifest_path')
    return path


def model(value):
    require(isinstance(value,dict) and set(value)==FIELDS, 'invalid_manifest')
    require(type(value['version']) is int and value['version']==1, 'unknown_guard_version')
    for name in ['root','legacy_database','v2_database','guard_python','worker_python','git']:
        canonical(value[name])
    require(not os.path.samefile(value['legacy_database'],value['v2_database']), 'database_alias_collision')
    require(value['phase'] in {'baseline','armed','active','retired'}, 'unknown_phase')
    for name in ['epoch','offset_floor']:
        require(type(value[name]) is int and value[name]>=0, 'invalid_epoch_or_floor')
    require((value['phase']=='baseline' and value['epoch']==0) or
            (value['phase']!='baseline' and value['epoch']>0), 'invalid_epoch')
    require(isinstance(value['bundle'],dict) and set(value['bundle'])==BUNDLE, 'invalid_bundle')
    for name,sha in value['bundle'].items():
        require(isinstance(sha,str) and re.fullmatch('[0-9a-f]{64}',sha), 'invalid_bundle_hash')
    require(isinstance(value['approvals'],list) and value['approvals'], 'missing_approvals')
    seen=set()
    for entry in value['approvals']:
        require(isinstance(entry,dict) and set(entry)=={'sha','queue_protocol','handlers'}, 'invalid_approval')
        require(isinstance(entry['sha'],str) and re.fullmatch('[0-9a-f]{40}',entry['sha']), 'invalid_release_sha')
        require(entry['sha'] not in seen, 'duplicate_approval'); seen.add(entry['sha'])
        require(type(entry['queue_protocol']) is int and entry['queue_protocol'] in {0,1}, 'unknown_queue_protocol')
        require(isinstance(entry['handlers'],list) and entry['handlers'] and
                all(x in {'legacy','v2'} for x in entry['handlers']), 'invalid_handler_approval')
    require(value['rollback'] is None or value['phase']=='retired', 'unexpected_rollback_receipt')
    return value


def bundle_check(m, bundle):
    bundle=Path(bundle).resolve(strict=True)
    require(not bundle.is_relative_to(Path(m['root'])), 'guard_inside_checkout')
    for name,sha in m['bundle'].items():
        require(digest(bundle/name)==sha, 'guard_bundle_mismatch')


@contextmanager
def read_db(path):
    with closing(sqlite3.connect(Path(path).resolve(strict=True).as_uri()+'?mode=ro',uri=True)) as c:
        c.execute('BEGIN')
        require(c.execute('PRAGMA integrity_check').fetchall()==[('ok',)], 'database_integrity')
        yield c


def tables(c): return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def offset(c):
    row=c.execute("SELECT value FROM telegram_state WHERE key='offset'").fetchone()
    value=int(row[0]) if row else 0
    require(value>=0, 'invalid_offset')
    return value


def queue_state(c):
    require(tables(c)==CORE|QUEUE, 'unknown_database_schema')
    expected={
        'steel_v2_queue_meta':({'key','value'},['key']),
        'steel_v2_inbox':({'update_id','user_id','input_hash','payload','bytes','state','admission_allowed',
                           'served','committed','uncertain','admission_revision','access_revision','admission_admin'},['update_id']),
        'steel_v2_attempt':({'update_id','number','kind','state'},['update_id','number']),
        'steel_v2_outbox':({'update_id','part','method','payload','state','message_id'},['update_id','part'])}
    for table,(columns,primary) in expected.items():
        info=c.execute('PRAGMA table_info('+table+')').fetchall()
        require({r[1] for r in info}==columns and
                [r[1] for r in sorted(info,key=lambda r:r[5]) if r[5]]==primary, 'unknown_queue_layout')
    meta=dict(c.execute('SELECT key,value FROM steel_v2_queue_meta'))
    require(type(meta.get('schema')) is int and meta['schema']==1, 'unknown_queue_schema')
    require(type(meta.get('cursor')) is int and meta['cursor']>=0, 'invalid_cursor')
    require(type(meta.get('guard_epoch')) is int and meta['guard_epoch']>0, 'missing_queue_epoch')
    states=dict(c.execute('SELECT state,count(*) FROM steel_v2_inbox GROUP BY state'))
    sends=dict(c.execute('SELECT state,count(*) FROM steel_v2_outbox GROUP BY state'))
    attempts={r[0] for r in c.execute('SELECT DISTINCT state FROM steel_v2_attempt')}
    require(set(states)<=STATES and set(sends)<=SEND_STATES and
            attempts<={'api_started','result_received','execution_unknown'}, 'unknown_queue_state')
    inflight=c.execute('SELECT count(*) FROM steel_v2_inflight').fetchone()[0]
    drained=not inflight and set(states)<={'delivered','denied','rejected'} and set(sends)<={'delivered','rejected'}
    return dict(cursor=meta['cursor'],epoch=meta['guard_epoch'],drained=drained)


def core_hash(c):
    result={}
    for table in sorted(CORE):
        schema=c.execute('SELECT sql FROM sqlite_master WHERE name=?',(table,)).fetchone()[0]
        rows=[hashlib.sha256(json.dumps(list(r),ensure_ascii=True,separators=(',',':')).encode()).digest()
              for r in c.execute('SELECT * FROM '+table)]
        result[table]=hashlib.sha256(schema.encode()+b''.join(sorted(rows))).hexdigest()
    return result


def archive_proof(m, *, compare_live=False):
    receipt=m['rollback']
    require(isinstance(receipt,dict) and set(receipt)=={'epoch','cursor','archive','sha256','core'}, 'missing_rollback_receipt')
    require(receipt['epoch']==m['epoch'] and receipt['cursor']==m['offset_floor'], 'rollback_epoch_mismatch')
    archive=canonical(receipt['archive'])
    require(not archive.is_relative_to(Path(m['root'])), 'archive_inside_checkout')
    require(digest(archive)==receipt['sha256'], 'rollback_archive_changed')
    with read_db(archive) as saved:
        q=queue_state(saved)
        require(q==dict(cursor=receipt['cursor'],epoch=m['epoch'],drained=True), 'archive_not_drained')
        require(core_hash(saved)==receipt['core'], 'archive_core_mismatch')
    if compare_live:
        with read_db(m['legacy_database']) as old:
            require(offset(old)==receipt['cursor'], 'rollback_offset_mismatch')
        with read_db(m['v2_database']) as live:
            require(tables(live)==CORE and core_hash(live)==receipt['core'], 'rollback_core_changed')


def git_read(m, *args):
    env=dict(os.environ,GIT_OPTIONAL_LOCKS='0')
    return subprocess.check_output([m['git'],'--no-optional-locks','-C',m['root'],*args],
                                   env=env,stderr=subprocess.DEVNULL).decode('utf-8').strip()


def handler(m):
    root=Path(m['root']); path=root/'telegram-handler.json'
    if not path.exists():return 'legacy'
    value=read_json(path)
    require(isinstance(value,dict) and set(value)<= {'handler','database'}, 'invalid_handler')
    if value=={'handler':'legacy'}:return 'legacy'
    require(value.get('handler')=='v2' and isinstance(value.get('database'),str), 'invalid_handler')
    actual=(root/value['database']).resolve(strict=True)
    require(os.path.samefile(actual,m['v2_database']), 'v2_database_changed')
    return 'v2'


def check(manifest, *, bundle=None, allow_maintenance=False):
    m=model(read_json(manifest))
    bundle_check(m,bundle or Path(__file__).parent)
    require(m['phase']!='armed', 'installation_armed')
    root=Path(m['root'])
    require(allow_maintenance or not (root/'telegram.maintenance').exists(), 'maintenance')
    require(not git_read(m,'status','--porcelain','--untracked-files=no'), 'dirty_checkout')
    for name in ['MERGE_HEAD','CHERRY_PICK_HEAD','REVERT_HEAD','rebase-merge','rebase-apply']:
        path=git_read(m,'rev-parse','--git-path',name)
        require(not (root/path).exists(), 'git_operation_in_progress')
    sha=git_read(m,'rev-parse','HEAD')
    approved=[r for r in m['approvals'] if r['sha']==sha]
    require(len(approved)==1, 'release_not_approved')
    release=approved[0]; selected=handler(m)
    require(selected in release['handlers'], 'handler_not_approved')
    try: published=json.loads(git_read(m,'show','HEAD:ops/windows_guard/queue-protocol.json'))
    except subprocess.CalledProcessError: published=None
    with read_db(m['legacy_database']) as old, read_db(m['v2_database']) as new:
        mirror=offset(old)
        require(mirror>=m['offset_floor'], 'offset_below_epoch_floor')
        if m['phase']=='active':
            q=queue_state(new)
            require(q['epoch']==m['epoch'], 'queue_epoch_mismatch')
            require(selected=='v2' and release['queue_protocol']==1, 'queue_protocol_incompatible')
            descriptor=read_json(root/'ops/windows_guard/queue-protocol.json')
            require(descriptor==published==DESCRIPTOR, 'queue_protocol_incompatible')
            require(mirror<=q['cursor'], 'offset_ahead_of_durable_cursor')
        else:
            require(release['queue_protocol']==0 and published is None, 'queue_activation_required')
            require(tables(new)==CORE, 'queue_not_retired')
            require(new.execute('SELECT count(*) FROM steel_v2_inflight').fetchone()[0]==0, 'old_inflight')
            if m['phase']=='retired':archive_proof(m)
            else:require(m['rollback'] is None, 'unexpected_rollback_receipt')
    return m


def frozen(bundle):
    """Import only the protected, installed copies, never checkout modules."""
    bundle=str(Path(bundle).resolve())
    names=['windows_runtime','windows_supervisor']; previous={n:sys.modules.get(n) for n in names}
    loaded=[]
    try:
        for name in names:
            spec=importlib.util.spec_from_file_location(name,Path(bundle)/(name+'.py'))
            module=importlib.util.module_from_spec(spec);sys.modules[name]=module;spec.loader.exec_module(module)
            loaded.append(module)
        return tuple(loaded)
    finally:
        for name in names:
            if previous[name] is None:sys.modules.pop(name,None)
            else:sys.modules[name]=previous[name]


def atomic_write(path, value):
    path=Path(path)
    fd,temp=tempfile.mkstemp(prefix='manifest-',suffix='.tmp',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            json.dump(value,f,ensure_ascii=True,indent=2);f.write('\n');f.flush();os.fsync(f.fileno())
        administrative_owner(temp)
        os.replace(temp,path)
    finally:
        if Path(temp).exists():Path(temp).unlink()


def administrative_owner(path):
    """Do not leave the unelevated runtime user as owner of a new protected manifest."""
    if os.name!='nt':raise Denied('windows_installation_required')
    import ctypes
    from ctypes import wintypes
    advapi=ctypes.WinDLL('advapi32',use_last_error=True)
    sid=ctypes.c_void_p()
    convert=advapi.ConvertStringSidToSidW
    convert.argtypes=[wintypes.LPCWSTR,ctypes.POINTER(ctypes.c_void_p)];convert.restype=wintypes.BOOL
    require(convert('S-1-5-32-544',ctypes.byref(sid)), 'owner_sid_failed')
    setter=advapi.SetNamedSecurityInfoW
    setter.argtypes=[wintypes.LPWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p]
    setter.restype=wintypes.DWORD
    free=ctypes.WinDLL('kernel32').LocalFree;free.argtypes=[ctypes.c_void_p];free.restype=ctypes.c_void_p
    try:require(setter(str(path),1,1,sid,None,None,None)==0, 'elevated_manifest_update_required')
    finally:free(sid)


def update(manifest, proposal, confirmation, *, bundle=None):
    """Elevated offline operation; filesystem ACL is the authority, not a CLI password."""
    bundle=Path(bundle or Path(__file__).parent)
    persistent_manifest(manifest,bundle)
    data=Path(proposal).read_bytes()
    desired=model(parse_json(data.decode('utf-8-sig')));bundle_check(desired,bundle)
    require(confirmation==hashlib.sha256(data).hexdigest(), 'proposal_confirmation_required')
    runtime,_=frozen(bundle);root=Path(desired['root'])
    with runtime.ProcessLock(root/'telegram-supervisor.lock'),runtime.ProcessLock(root/'telegram.lock'):
        require((root/'telegram.maintenance').exists(), 'maintenance_required')
        previous=model(read_json(manifest)) if Path(manifest).exists() else None
        if previous is None:
            require(desired['phase']=='baseline', 'initial_phase_must_be_baseline')
        else:
            for name in FIELDS-{'phase','epoch','offset_floor','approvals','rollback'}:
                require(previous[name]==desired[name], 'immutable_installation_changed')
            a,b=previous['phase'],desired['phase']
            require((a,b) in {('baseline','baseline'),('baseline','armed'),('active','active'),('active','armed'),
                              ('armed','active'),('armed','retired'),('retired','retired'),('retired','armed')}, 'invalid_phase_transition')
            if a in {'baseline','retired'} and b=='armed':
                require(desired['epoch']==previous['epoch']+1, 'new_epoch_required')
            else:require(desired['epoch']==previous['epoch'], 'epoch_changed')
            require(desired['offset_floor']>=previous['offset_floor'], 'floor_rewind')
            if a==b:
                require(desired['offset_floor']==previous['offset_floor'] and desired['rollback']==previous['rollback'], 'epoch_receipt_changed')
            if b=='armed':
                with read_db(previous['legacy_database']) as old,read_db(previous['v2_database']) as new:
                    cursor=queue_state(new)['cursor'] if a=='active' else offset(old)
                    require(offset(old)==cursor==desired['offset_floor'], 'arming_cursor_not_aligned')
            if b=='retired':require(a=='retired' or previous['epoch']>0, 'no_previous_epoch')
        # Retirement records verified data BEFORE the checkout of protocol-0 code.
        # The still-installed queue release must remain denied in retired phase;
        # its launch incompatibility must not prevent publishing the verified receipt.
        if desired['phase']=='retired':
            archive_proof(desired,compare_live=not previous or previous['phase']!='retired')
            with read_db(desired['v2_database']) as new,read_db(desired['legacy_database']) as old:
                require(tables(new)==CORE and new.execute('SELECT count(*) FROM steel_v2_inflight').fetchone()[0]==0, 'queue_not_retired')
                require(offset(old)>=desired['offset_floor'], 'offset_below_epoch_floor')
        elif desired['phase']!='armed':
            check(proposal,bundle=bundle,allow_maintenance=True)
        atomic_write(manifest,desired)


def launch(manifest, *, mode, supervisor_script=None, worker_script=None, bundle=None, initial_attempts=None):
    bundle=Path(bundle or Path(__file__).parent)
    persistent_manifest(manifest,bundle)
    m=check(manifest,bundle=bundle)
    runtime,supervisor=frozen(bundle)
    root=Path(m['root'])
    if mode=='supervise':
        require(supervisor_script is not None and Path(supervisor_script).resolve()==root/'windows_supervisor.py', 'invalid_supervisor_label')
        # Old stop-telegram.ps1 recognizes these validated path labels in command lines.
        command=[m['worker_python'],'-I','-B',str(bundle/'guard.py'),'worker','--manifest',str(Path(manifest).resolve()),
                 '--worker-script',str(root/'main.py')]
        logging.basicConfig(filename=root/'telegram-supervisor.log',level=logging.INFO,
                            format='%(asctime)s %(levelname)s %(message)s',encoding='utf-8')
        return supervisor.supervise(root,command,supervisor.RestartPolicy(initial_attempts=initial_attempts))
    require(worker_script is not None and Path(worker_script).resolve()==root/'main.py', 'invalid_worker_label')
    try:
        with runtime.ProcessLock(root/'telegram-supervisor.lock'):
            raise Denied('supervisor_required')
    except runtime.AlreadyRunning:pass
    # Check again in the fresh process before importing *any* checkout code.
    check(manifest,bundle=bundle)
    os.chdir(root);os.environ['OPERATOR_DB']=m['legacy_database']
    sys.path.insert(0,str(root));sys.argv=[str(root/'main.py')]
    runpy.run_path(str(root/'main.py'),run_name='__main__')
    return 0


def start(manifest, initial_attempts=2):
    """Explicit manual launch; scheduler uses supervise and respects maintenance."""
    persistent_manifest(manifest,Path(__file__).parent)
    m=check(manifest,allow_maintenance=True)
    runtime,_=frozen(Path(__file__).parent);root=Path(m['root'])
    with runtime.ProcessLock(root/'telegram-supervisor.lock'),runtime.ProcessLock(root/'telegram.lock'):
        check(manifest,allow_maintenance=True)
        (root/'telegram.maintenance').unlink(missing_ok=True)
    # The supervisor lock arbitrates concurrent launches; every child rechecks policy.
    subprocess.Popen([m['guard_python'],'-I','-S','-B',str(Path(__file__).resolve()),'supervise',
                      '--manifest',str(Path(manifest).resolve()),'--supervisor-script',str(root/'windows_supervisor.py'),
                      '--initial-attempts',str(initial_attempts)],
                     cwd=root,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                     creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    return 0


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode',choices=['check','start','supervise','worker','update'])
    p.add_argument('--manifest',type=Path,required=True)
    p.add_argument('--proposal',type=Path);p.add_argument('--confirm')
    p.add_argument('--supervisor-script',type=Path);p.add_argument('--worker-script',type=Path)
    p.add_argument('--initial-attempts',type=int,choices=range(1,11))
    args=p.parse_args(argv)
    try:
        persistent_manifest(args.manifest,Path(__file__).parent)
        if args.mode=='update':update(args.manifest,args.proposal,args.confirm)
        elif args.mode=='check':check(args.manifest,allow_maintenance=True)
        elif args.mode=='start':return start(args.manifest,initial_attempts=args.initial_attempts or 2)
        else:return launch(args.manifest,mode=args.mode,supervisor_script=args.supervisor_script,
                           worker_script=args.worker_script,initial_attempts=args.initial_attempts)
        print('guard_ok');return 0
    except Exception as error:
        reason=str(error) if isinstance(error,Denied) else 'guard_state_unavailable'
        print('guard_denied:'+reason,file=sys.stderr);return 7


if __name__=='__main__':raise SystemExit(main())
