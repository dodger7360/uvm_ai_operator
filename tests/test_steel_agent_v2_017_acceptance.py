"""Independent acceptance: real child crashes and explicit unresolved rollback gate.

Only synthetic data; formerly red launch tests now exercise the installed external
boundary, including a checkout of the actual previous published SHA. No skip/xfail.
"""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import ModuleType
from threading import Event, Thread
import time

import pytest
from database import Database
from steel_agent_v2 import Repository
from steel_agent_v2.queue import DurableQueue, QueueConfig, Backpressure, QueueConflict
from windows_runtime import ProcessLock, AlreadyRunning
from test_steel_agent_v2_queue import rig, admit, update, API, Fake, proposal, Executor, Scheduler, drain
from test_steel_agent_v2_guard import guard_install, assert_denied

ROOT = Path(__file__).resolve().parents[1]
OLD = 'aa11380dfe1ed646fec2355b22880fdb03b0d706'


@pytest.mark.parametrize('published', [False, True], ids=['candidate', 'previous-release'])
@pytest.mark.parametrize('config', [None, {'handler': 'legacy'}], ids=['missing-config', 'legacy'])
def test_pending_queue_must_prevent_legacy_selector_bypass(rig, tmp_path, published, config, guard_install):
    """Same pending/offset/handler requirement, at the permanent launch boundary.

    Old selector remains untouched: only a guard surviving the checkout can stop it.
    A real guard process must reject before importing any checkout/Telegram code.
    """
    installed=guard_install
    from steel_agent_v2 import runtime
    if published:
        source = subprocess.run(['git', 'show', OLD+':steel_agent_v2/runtime.py'],
                                capture_output=True, check=True, encoding='utf-8').stdout
        runtime = ModuleType('published_runtime')
        exec(compile(source, 'published_runtime.py', 'exec'), runtime.__dict__)
        installed.published()
    admit(rig, 41)
    rig.legacy.set_offset(42)
    assert not rig.queue.rollback_ready()
    installed.config(config)
    assert runtime.selection(installed.root)==('legacy',None)
    assert_denied(installed)


CRASH = r'''
import json, os, socket, sys
from contextlib import contextmanager
from pathlib import Path
sys.path[:0] = [sys.argv[1], str(Path(sys.argv[1])/'tests')]
def offline(*a, **kw): raise RuntimeError('No network in crash harness')
socket.socket.connect = offline
socket.socket.connect_ex = offline
from database import Database
from repositories.telegram_repository import TelegramRepository
from repositories.access_repository import AccessRepository
from telegram_access import TelegramAccess
from steel_agent_v2 import Repository
from steel_agent_v2.agent import SteelAgent
from steel_agent_v2.queue import DurableQueue
from steel_agent_v2.queue_runtime import AttemptTransport
from test_steel_agent_v2 import SOURCE, proposal
root, stage = Path(sys.argv[2]), sys.argv[3]
legacy = TelegramRepository(Database(root/'legacy.sqlite3'))
permissions = AccessRepository(legacy.database)
permissions.bootstrap_configured_admin(1)
access = TelegramAccess(permissions)
db = Database(root/'v2.sqlite3'); Repository(db); q = DurableQueue(db)
def die(): os._exit(73)
def log(name):
    with (root/'external-counts.txt').open('a') as f:
        f.write(name+'\n'); f.flush(); os.fsync(f.fileno())
if stage == 'before_inbox': die()
q.admit(dict(update_id=41,message={'from':{'id':1},'chat':{'id':1,'type':'private'},'text':SOURCE}),allowed=True)
if stage == 'after_inbox': die()
legacy.set_offset(q.cursor())
if stage == 'after_offset': die()
q.claim()
if stage == 'claimed': die()
class Model:
    def plan(self, *a, **kw):
        log('plan')
        if stage == 'api_started': die()
        return dict(intent='free_chat',answer='X'*9000) if stage == 'between_parts' else proposal()
class API:
    def call(self, method, **kw):
        if stage == 'delivery_started': die()
        log('send')
        if stage == 'send_without_receipt': die()
        return {'message_id': 777}
hook = q.finish_hook(41)
def finish(c, *a):
    if stage == 'result_received': die()
    hook(c, *a)
    if stage == 'during_commit': die()
repo = Repository(db, finish_hook=finish)
SteelAgent(AttemptTransport(Model(), q, 41), repo).handle(1, SOURCE, event_id='telegram:41')
if stage == 'after_commit': die()
original = q.write
@contextmanager
def fault_write():
    with original() as c:
        yield c
        receipts = c.execute("SELECT count(*) FROM steel_v2_outbox WHERE state='delivered'").fetchone()[0]
        if stage == 'receipt_before_commit' and receipts: die()
    if stage == 'between_parts' and receipts == 1: die()
q.write = fault_write
q.deliver(41, API(), access)
raise RuntimeError('unreached crash boundary')
'''


@pytest.mark.parametrize('stage,expected', [
    ('before_inbox', None), ('after_inbox', 'queued'), ('after_offset', 'queued'),
    ('claimed', 'queued'), ('api_started', 'execution_unknown'),
    ('result_received', 'execution_unknown'), ('during_commit', 'execution_unknown'),
    ('after_commit', 'committed'), ('delivery_started', 'delivery_unknown'),
    ('send_without_receipt', 'delivery_unknown'), ('receipt_before_commit', 'delivery_unknown'),
    ('between_parts', 'delivery_pending'),
])
def test_real_process_crash_boundaries(tmp_path, stage, expected):
    child = subprocess.run([sys.executable, '-B', '-c', CRASH, str(ROOT), str(tmp_path), stage],
                           capture_output=True, timeout=20)
    assert child.returncode == 73, child.stderr.decode(errors='replace')
    from repositories.telegram_repository import TelegramRepository
    from repositories.access_repository import AccessRepository
    from telegram_access import TelegramAccess
    legacy = TelegramRepository(Database(tmp_path/'legacy.sqlite3'))
    db = Database(tmp_path/'v2.sqlite3'); q = DurableQueue(db); q.recover()
    job = q.job(41)
    assert (job['state'] if job else None) == expected
    offset = legacy.get_offset()
    assert offset == (0 if stage in {'before_inbox', 'after_inbox'} else 42)
    if job:
        # The mirrored offset may lag, never lead the durable cursor.
        assert q.cursor() == 42
        legacy.set_offset(max(offset, q.cursor()))
        assert q.admit(update(41), allowed=True) == 42
    with db.connection() as c:
        assert c.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        committed = stage in {'after_commit', 'delivery_started', 'send_without_receipt',
                              'receipt_before_commit', 'between_parts'}
        assert c.execute('SELECT count(*) FROM steel_v2_turn').fetchone()[0] == int(committed)
        assert c.execute('SELECT count(*) FROM steel_v2_version').fetchone()[0] == int(committed and stage != 'between_parts')
        if not committed:
            assert c.execute('SELECT count(*) FROM steel_v2_outbox').fetchone()[0] == 0
    counts = (tmp_path/'external-counts.txt').read_text().splitlines() if (tmp_path/'external-counts.txt').exists() else []
    if expected in {'execution_unknown', 'delivery_unknown'}:
        q.admit(update(42), allowed=True)
        assert q.claim() is None
        q.admit(update(43, user=2), allowed=True)
        assert q.claim()['user_id'] == 2
        assert not q.delivery_candidates()
    if expected in {'committed', 'delivery_pending'}:
        api = API()
        q.deliver(41, api, TelegramAccess(AccessRepository(legacy.database)))
        assert q.job(41)['state'] == 'delivered'
        if stage == 'between_parts':
            assert counts.count('send') == 1
            assert len(api.calls) == 2  # Continue parts 2/3; never resend part 1.
        else:
            assert len(api.calls) == 1
    if stage == 'receipt_before_commit':
        assert counts.count('send') == 1
        with db.connection() as c:
            assert c.execute('SELECT message_id,state FROM steel_v2_outbox').fetchone()[:] == (None, 'delivery_unknown')


def test_exact_default_limits_and_capacity_resume(rig):
    assert vars(QueueConfig()) == dict(workers=3, max_pending=100, max_pending_per_user=20, max_pending_bytes=8388608)
    for n in range(1, 21): admit(rig, n, user=2, text='small')
    with pytest.raises(Backpressure): admit(rig, 21, user=2, text='small')
    assert rig.queue.cursor() == 21 and rig.queue.job(21) is None
    Executor(rig.queue, rig.access, lambda:rig.api, lambda:Fake(dict(intent='free_chat',answer='ok'))).execute(rig.queue.claim())
    assert admit(rig, 21, user=2, text='small') == 22
    # Reinitialize a separate DB for the global 100-job boundary.
    db = Database(rig.queue.database.path.with_name('global.sqlite3')); Repository(db)
    q = DurableQueue(db)
    for n in range(100): q.admit(update(n, user=1+n//20, text='small'), allowed=True)
    with pytest.raises(Backpressure): q.admit(update(100, user=6), allowed=True)
    assert q.cursor() == 100 and q.job(100) is None
    q.deny(q.claim()['update_id'])
    assert q.admit(update(100, user=6), allowed=True) == 101


def test_exact_default_bytes_limit_and_oversized_event(rig):
    q = rig.queue
    with pytest.raises(Backpressure): q.admit(update(1, text='x'*8388608), allowed=True)
    assert q.cursor() == 0
    first = 'x'*4194304
    q.admit(update(1, text=first), allowed=True)
    with pytest.raises(Backpressure): q.admit(update(2, user=2, text=first), allowed=True)
    assert q.cursor() == 2 and q.job(2) is None
    q.deny(q.claim()['update_id'])
    assert q.admit(update(2, user=2, text=first), allowed=True) == 3


@pytest.mark.parametrize('lock', ['telegram.lock', 'telegram-supervisor.lock'])
def test_operator_unknown_close_refuses_live_process_locks(rig, tmp_path, lock):
    from scripts.v2_queue_status import inspect, main
    admit(rig, 1); rig.queue.claim(); rig.queue.start_api(1, 'plan'); rig.queue.recover()
    (tmp_path/'telegram-handler.json').write_text(json.dumps({'handler':'v2','database':'v2.sqlite3'}))
    (tmp_path/'telegram.maintenance').touch()
    confirmation = inspect(rig.queue.database.path)['unknown'][0]['confirmation']
    args = ['--database',str(rig.queue.database.path),'--root',str(tmp_path),'--abandon','1','--confirm',confirmation]
    with ProcessLock(tmp_path/lock):
        with pytest.raises(AlreadyRunning): main(args)
    assert rig.queue.job(1)['state'] == 'execution_unknown'
    assert main(args) == 0
    assert rig.queue.job(1)['state'] == 'rejected'
    assert not rig.api.calls
    with rig.queue.database.connection() as c:
        assert c.execute('SELECT count(*) FROM steel_v2_turn').fetchone()[0] == 1


def test_dpapi_survives_independent_process_of_same_account(tmp_path):
    from steel_agent_v2.windows_secrets import protect
    ciphertext = tmp_path/'synthetic.dpapi'
    ciphertext.write_text(protect('synthetic-stage17-binding'))
    code = ('import sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); '
            'from steel_agent_v2.windows_secrets import unprotect; '
            "assert unprotect(Path(sys.argv[2]).read_text())=='synthetic-stage17-binding'; print('roundtrip-ok')")
    child = subprocess.run([sys.executable,'-B','-c',code,str(ROOT),str(ciphertext)],capture_output=True,timeout=10)
    assert child.returncode == 0, child.stderr.decode(errors='replace')
    assert child.stdout.strip() == b'roundtrip-ok'
    assert b'synthetic-stage17-binding' not in ciphertext.read_bytes()


def test_overlap_and_both_databases_writable_during_slow_gpt(rig):
    entered, release, second = Event(), Event(), Event()
    times = {}
    class Model:
        def plan(self, context, **kw):
            if context['message'] == 'slow':
                times['a_start'] = time.monotonic(); entered.set()
                assert release.wait(5)
                times['a_end'] = time.monotonic()
            else:
                times['b_start'] = time.monotonic(); second.set()
            return dict(intent='free_chat', answer=context['message'])
    admit(rig, 1, 2, 'slow')
    scheduler = Scheduler(rig.queue, Executor(rig.queue, rig.access, lambda:rig.api, Model))
    try:
        scheduler.tick(); assert entered.wait(3)
        # Real independent write connections, not a timing assertion about Python threads.
        for database in [rig.legacy.database, rig.queue.database]:
            with database.connection() as c:
                c.execute('PRAGMA busy_timeout=1'); c.execute('BEGIN IMMEDIATE')
        admit(rig, 2, 3, 'fast'); scheduler.tick()
        assert second.wait(3) and not release.is_set()
        release.set(); drain(scheduler, rig.queue.rollback_ready)
    finally:
        release.set(); scheduler.close()
    assert times['a_start'] < times['b_start'] < times['a_end']


def test_revocation_after_commit_before_delivery_preserves_version_without_send(rig, monkeypatch):
    admit(rig, 1, user=2)
    deliver = rig.queue.deliver
    monkeypatch.setattr(rig.queue, 'deliver', lambda *a:None)
    Executor(rig.queue, rig.access, lambda:rig.api, lambda:Fake(proposal())).execute(rig.queue.claim())
    assert rig.queue.job(1)['state'] == 'committed'
    rig.permissions.transition(1, 2, 'revoke', rig.permissions.get(2)['revision'])
    deliver(1, rig.api, rig.access)
    assert rig.queue.job(1)['state'] == 'denied' and not rig.api.calls
    assert Repository(rig.queue.database).load(2).revision == 1
    with rig.queue.database.connection() as c:
        assert c.execute('SELECT count(*) FROM steel_v2_turn').fetchone()[0] == 1


def test_revoke_after_send_marker_cannot_cancel_already_started_external_send(rig):
    admit(rig, 1, user=2)
    class RevokeAtNetwork:
        calls = 0
        def call(self, method, **kw):
            self.calls += 1
            assert rig.queue.job(1)['state'] == 'delivery_started'
            rig.permissions.transition(1, 2, 'revoke', rig.permissions.get(2)['revision'])
            return {'message_id':123}
    api = RevokeAtNetwork()
    Executor(rig.queue, rig.access, lambda:api, lambda:Fake(proposal())).execute(rig.queue.claim())
    assert api.calls == 1 and rig.queue.job(1)['state'] == 'delivered'
    assert not rig.access.allowed(2)


def test_full_user_queue_blocks_later_poll_update_until_capacity_released(rig, tmp_path, monkeypatch):
    from steel_agent_v2.queue_runtime import run_queue
    from steel_agent_v2.transport import OpenAITransport
    entered, release, blocked = Event(), Event(), Event()
    errors = []; offsets = []
    class Model:
        def plan(self, context, **kw):
            if context['message'] == 'first':
                entered.set(); assert release.wait(5)
            return dict(intent='free_chat', answer='ok')
    monkeypatch.setattr(OpenAITransport, 'from_environment', classmethod(lambda cls,**kw:Model()))
    for number in range(1, 21): admit(rig, number, 2, 'first' if number == 1 else 'queued')
    class Poll:
        def call(self, method, **kw):
            assert method == 'getUpdates'; offsets.append(kw['offset'])
            if kw['offset'] == 21:
                assert entered.wait(3)
                if offsets.count(21) > 1: blocked.set()
                return [update(21, 2, 'overflow')]
            if kw['offset'] == 22: return [update(22, 3, 'other')]
            (tmp_path/'telegram.maintenance').touch(); return []
    def run():
        try: run_queue(tmp_path, Poll(), rig.access, rig.legacy, rig.queue.database.path, api_factory=lambda:rig.api)
        except BaseException as error: errors.append(error)
    thread = Thread(target=run); thread.start()
    try:
        assert blocked.wait(3)
        assert rig.queue.job(21) is None and rig.queue.job(22) is None
        assert rig.legacy.get_offset() == 21
        release.set(); thread.join(8)
    finally:
        release.set(); (tmp_path/'telegram.maintenance').touch(); thread.join(8)
    assert not thread.is_alive() and not errors
    assert rig.queue.job(21) is not None and rig.queue.job(22) is not None
    assert rig.legacy.get_offset() == 23


def test_stop_script_timeout_keeps_maintenance_and_never_kills_process(tmp_path):
    # Exercise the real PowerShell timeout path at 1 s, with synthetic CIM metadata.
    # No production script is invoked; no real process is selected by this harness.
    stop = tmp_path/'stop-telegram.ps1'
    stop.write_bytes((ROOT/'stop-telegram.ps1').read_bytes())
    harness = tmp_path/'stop-harness.ps1'
    harness.write_text('''param([string]$ScriptPath)
function Get-CimInstance { [pscustomobject]@{Name='python.exe'; CommandLine=$ScriptPath.Replace('stop-telegram.ps1','main.py')} }
function Stop-Process { throw 'FORBIDDEN_KILL' }
try { & $ScriptPath -TimeoutSeconds 1; exit 2 }
catch { if ($_.Exception.Message -like 'Bot still finishing work.*') { Write-Output 'timeout-safe'; exit 0 }; throw }
''')
    child = subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass',
                            '-File',str(harness),'-ScriptPath',str(stop)],capture_output=True,timeout=15)
    assert child.returncode == 0, child.stderr.decode(errors='replace')
    assert b'timeout-safe' in child.stdout and (tmp_path/'telegram.maintenance').exists()


def test_durable_admin_revoke_overlaps_employee_gpt_and_prevents_commit(rig):
    entered, release = Event(), Event()
    class Paused:
        def plan(self, *a, **kw):
            entered.set(); assert release.wait(5); return proposal()
    admit(rig, 1, user=2)
    callback = dict(update_id=2, callback_query={'id':'stage17-callback','from':{'id':1},
        'data':'access:revoke:2:2','message':{'chat':{'id':1,'type':'private'},'message_id':7}})
    scheduler = Scheduler(rig.queue, Executor(rig.queue, rig.access, lambda:rig.api, Paused))
    try:
        scheduler.tick(); assert entered.wait(3)
        rig.queue.admit(callback, allowed=True, revision=1, is_admin=True)
        scheduler.tick()
        drain(scheduler, lambda:not rig.access.allowed(2))
        assert not release.is_set()
        release.set(); drain(scheduler, rig.queue.rollback_ready)
    finally:
        release.set(); scheduler.close()
    assert rig.queue.job(1)['state'] == 'denied'
    assert rig.queue.job(2)['state'] == 'delivered'
    assert Repository(rig.queue.database).load(2) is None
    # Dedup callback cannot change permission generation twice.
    revision = rig.permissions.get(2)['revision']
    rig.queue.admit(callback, allowed=True, revision=1, is_admin=True)
    assert rig.queue.claim() is None and rig.permissions.get(2)['revision'] == revision


def test_rollback_must_not_drop_inbox_without_checking_legacy_offset(rig, tmp_path):
    from scripts.v2_queue_status import prepare_rollback, inspect
    admit(rig, 41)
    Executor(rig.queue, rig.access, lambda:rig.api, lambda:Fake(proposal())).execute(rig.queue.claim())
    assert rig.queue.rollback_ready() and rig.legacy.get_offset() == 0 and rig.queue.cursor() == 42
    archive = tmp_path/'must-not-archive-unchecked.sqlite3'
    with pytest.raises(ValueError):
        prepare_rollback(rig.queue.database.path, archive,
                         inspect(rig.queue.database.path)['rollback_confirmation'])
    assert not archive.exists()
    assert rig.queue.job(41)['state'] == 'delivered'


@pytest.mark.parametrize('offset', [0, 43], ids=['mirror-behind', 'mirror-ahead'])
def test_rollback_refuses_mismatched_mirror_without_creating_archive(rig, tmp_path, offset):
    from scripts.v2_queue_status import prepare_rollback, inspect, main
    admit(rig, 41)
    Executor(rig.queue, rig.access, lambda:rig.api, lambda:Fake(proposal())).execute(rig.queue.claim())
    rig.legacy.set_offset(offset)
    state = inspect(rig.queue.database.path, rig.legacy.database.path)
    assert state['queue_drained'] and not state['offset_verified'] and not state['rollback_ready']
    assert main(['--database',str(rig.queue.database.path),'--legacy-database',str(rig.legacy.database.path),
                 '--guard-rollback']) == 2
    archive = tmp_path/'offset-mismatch.sqlite3'
    with pytest.raises(ValueError, match='queue_not_safe'):
        prepare_rollback(rig.queue.database.path, archive, state['rollback_confirmation'],
                         legacy_database=rig.legacy.database.path)
    assert not archive.exists() and rig.legacy.get_offset() == offset


def test_rollback_rechecks_offset_after_backup_and_leaves_queue_intact(rig, tmp_path, monkeypatch):
    from scripts.v2_queue_status import prepare_rollback, inspect
    from scripts import v2_sqlite_snapshot
    admit(rig, 41)
    Executor(rig.queue, rig.access, lambda:rig.api, lambda:Fake(proposal())).execute(rig.queue.claim())
    rig.legacy.set_offset(42)
    original = v2_sqlite_snapshot.snapshot
    def moved(*args):
        result = original(*args); rig.legacy.set_offset(43); return result
    monkeypatch.setattr(v2_sqlite_snapshot, 'snapshot', moved)
    archive = tmp_path/'archive-before-concurrent-change.sqlite3'
    with pytest.raises(ValueError, match='legacy_offset_changed'):
        prepare_rollback(rig.queue.database.path, archive,
            inspect(rig.queue.database.path)['rollback_confirmation'], legacy_database=rig.legacy.database.path)
    assert archive.exists() and rig.queue.job(41)['state'] == 'delivered'


def test_rollback_readonly_check_never_creates_missing_legacy_database(rig, tmp_path):
    from scripts.v2_queue_status import inspect
    absent = tmp_path/'absent-legacy.sqlite3'
    with pytest.raises(sqlite3.OperationalError): inspect(rig.queue.database.path, absent)
    assert not absent.exists()
