"""Offline operations tests: isolated processes/locks; no production or network."""
import json
import logging
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
from unittest.mock import Mock

import pytest
import telegram_bot as bot
import windows_supervisor as supervisor
from windows_runtime import AlreadyRunning, ProcessLock, maintenance_requested, wait_or_maintenance

REPO = str(Path(__file__).resolve().parents[1])
NATIVE_PYTHON = sys._base_executable


def child_command(code):
    return [NATIVE_PYTHON, '-c', 'import sys; sys.path.insert(0, '+repr(REPO)+'); '+code]


def wait_for(path, timeout=5):
    deadline = time.monotonic()+timeout
    while time.monotonic()<deadline:
        if path.exists():
            return
        time.sleep(0.01)
    raise AssertionError('Isolated child did not become ready')


def test_lock_excludes_independent_process_and_releases_after_crash(tmp_path):
    lock = tmp_path/'telegram.lock'
    signal = tmp_path/'signal'
    code = ('from pathlib import Path; import time; from windows_runtime import ProcessLock; '
            'guard=ProcessLock('+repr(str(lock))+'); guard.__enter__(); '
            'Path('+repr(str(signal))+').touch(); time.sleep(20)')
    child = subprocess.Popen(child_command(code))
    try:
        wait_for(signal)
        with pytest.raises(AlreadyRunning):
            with ProcessLock(lock):
                pass
        child.terminate()  # Dedicated native test process, never the bot/redirector.
        child.wait(timeout=5)
        with ProcessLock(lock):
            pass
    finally:
        if child.poll() is None:
            child.kill(); child.wait(timeout=5)


def test_duplicate_supervisor_does_not_launch_child(tmp_path, monkeypatch):
    launcher = Mock()
    monkeypatch.setattr(supervisor.subprocess, 'Popen', launcher)
    with ProcessLock(tmp_path/'telegram-supervisor.lock'):
        assert supervisor.supervise(tmp_path, ['not-a-real-program']) == 0
    launcher.assert_not_called()


def test_existing_worker_is_not_replaced_or_killed(tmp_path, monkeypatch):
    launcher = Mock()
    monkeypatch.setattr(supervisor.subprocess, 'Popen', launcher)
    def maintenance(root, delay):
        assert delay == 30
        (root/'telegram.maintenance').touch()
        return True
    monkeypatch.setattr(supervisor, 'wait_or_maintenance', maintenance)
    with ProcessLock(tmp_path/'telegram.lock'):
        assert supervisor.supervise(tmp_path, ['not-a-real-program']) == 0
    launcher.assert_not_called()


def test_duplicate_bot_exits_before_network_or_sqlite(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, '__file__', str(tmp_path/'telegram_bot.py'))
    monkeypatch.setattr(bot, 'load_dotenv', Mock())
    monkeypatch.setenv('TELEGRAM_TOKEN', 'offline-test')
    network = Mock(side_effect=AssertionError('network forbidden'))
    monkeypatch.setattr(bot, 'run_bot', network)
    with ProcessLock(tmp_path/'telegram.lock'):
        assert bot.main() == 3
    network.assert_not_called()


def test_maintenance_marker_prevents_any_child(tmp_path, monkeypatch):
    (tmp_path/'telegram.maintenance').touch()
    launcher = Mock()
    monkeypatch.setattr(supervisor.subprocess, 'Popen', launcher)
    assert supervisor.supervise(tmp_path, ['not-a-real-program']) == 0
    launcher.assert_not_called()
    assert wait_or_maintenance(tmp_path, 30)


def test_real_fake_child_crash_recovers_with_delay_and_stops_for_maintenance(tmp_path, caplog):
    counter, starts = tmp_path/'counter', tmp_path/'starts'
    code = """
from pathlib import Path
import os,time,json
from windows_runtime import ProcessLock
root=Path(ROOT)
with ProcessLock(root/'telegram.lock'):
    count=int((root/'counter').read_text())+1 if (root/'counter').exists() else 1
    (root/'counter').write_text(str(count))
    values=json.loads((root/'starts').read_text()) if (root/'starts').exists() else []
    values.append(time.monotonic())
    (root/'starts').write_text(json.dumps(values))
    (root/'telegram.ready').touch()
    time.sleep(0.15)
    if count==1:
        os._exit(23)
    (root/'telegram.maintenance').touch()
""".replace('ROOT', repr(str(tmp_path)))
    policy = supervisor.RestartPolicy(base_delay=0.08, max_delay=0.08)
    with caplog.at_level(logging.INFO, logger='telegram_supervisor'):
        assert supervisor.supervise(tmp_path, child_command(code), policy, poll_interval=0.01) == 0
    assert counter.read_text() == '2'
    times = json.loads(starts.read_text())
    assert times[1]-times[0] >= 0.08
    assert 'exit_code=23' in caplog.text
    assert 'restart_scheduled' in caplog.text
    assert 'maintenance_stop no_restart=true' in caplog.text
    with ProcessLock(tmp_path/'telegram.lock'), ProcessLock(tmp_path/'telegram-supervisor.lock'):
        pass


def test_maintenance_interrupts_recovery_delay_without_relaunch(tmp_path, monkeypatch):
    code = 'raise SystemExit(6)'
    original = supervisor.subprocess.Popen
    launched=[]
    def spawn(*args, **kwargs):
        child=original(*args, **kwargs)
        launched.append(child)
        return child
    monkeypatch.setattr(supervisor.subprocess, 'Popen', spawn)
    def pause(root, seconds):
        assert seconds==30
        (root/'telegram.maintenance').touch()
        return True
    monkeypatch.setattr(supervisor, 'wait_or_maintenance', pause)
    assert supervisor.supervise(tmp_path, child_command(code), poll_interval=0.01)==0
    assert len(launched)==1


@pytest.mark.parametrize('code', [0,2,3,4,5,7])
def test_permanent_exits_do_not_restart(code):
    assert supervisor.RestartPolicy().next_delay(code, False, 0) is None


def test_manual_initial_failure_budget_is_exactly_two():
    policy=supervisor.RestartPolicy(initial_attempts=2)
    assert policy.next_delay(6,False,0)==30
    assert policy.next_delay(6,False,40) is None


def test_crash_rate_is_bounded_with_cooldown_and_ages_out():
    policy=supervisor.RestartPolicy()
    assert [policy.next_delay(1,True,t) for t in range(5)] == [30,60,120,240,900]
    assert policy.next_delay(6,True,6)==900
    assert policy.next_delay(1,True,1000)==30


@pytest.mark.parametrize('error,category,stage', [
    (urllib.error.URLError(socket.gaierror(-2,'PRIVATE_URL')), 'dns','dns'),
    (urllib.error.URLError(ssl.SSLCertVerificationError(1,'PRIVATE_URL')), 'tls','tls'),
    (urllib.error.URLError(TimeoutError('PRIVATE_URL')), 'timeout','request'),
    (urllib.error.HTTPError('https://PRIVATE_URL',407,'PRIVATE_TOKEN',None,None), 'proxy_authentication','response'),
    (urllib.error.URLError('unknown proxy socks PRIVATE_TOKEN'), 'proxy','connection'),
    (ConnectionResetError('PRIVATE_TOKEN'), 'network','request'),
    (ValueError('PRIVATE_TOKEN'), 'configuration','configuration'),
])
def test_network_diagnostics_are_safe_and_classified(monkeypatch, caplog, error, category, stage):
    def fail(*args,**kwargs): raise error
    monkeypatch.setattr(bot.urllib.request,'urlopen',fail)
    with caplog.at_level(logging.WARNING,logger='telegram_operator'):
        with pytest.raises(bot.TelegramError) as failure:
            bot.TelegramAPI('PRIVATE_TOKEN').call('getMe',_attempt=2)
    assert failure.value.category==category
    assert failure.value.stage==stage
    assert 'attempt=2' in caplog.text
    assert 'PRIVATE_TOKEN' not in caplog.text
    assert 'PRIVATE_URL' not in caplog.text
    assert 'https://' not in caplog.text


def wire_service(monkeypatch, api):
    repository=Mock()
    repository.get_offset.return_value=0
    access_repository=Mock()
    access_repository.admin_id.return_value=1
    monkeypatch.setattr(bot,'TelegramAPI',lambda token:api)
    monkeypatch.setattr(bot,'Database',Mock())
    monkeypatch.setattr(bot,'TelegramRepository',lambda db:repository)
    monkeypatch.setattr(bot,'AccessRepository',lambda db:access_repository)
    monkeypatch.setattr(bot,'AIService',Mock())
    monkeypatch.setattr(bot,'TelegramAccess',Mock())
    monkeypatch.setattr(bot,'AuthorizedRequestRepository',Mock())
    monkeypatch.setattr(bot,'TelegramService',Mock())
    monkeypatch.delenv('ADMIN_TELEGRAM_ID',raising=False)
    return repository


def test_maintenance_finishes_current_update_and_preserves_unprocessed_offset(tmp_path,monkeypatch):
    api=Mock()
    def call(method,**kwargs):
        return {'username':'offline'} if method=='getMe' else {} if method=='getWebhookInfo' else [{'update_id':1},{'update_id':2}]
    api.call.side_effect=call
    repo=wire_service(monkeypatch,api)
    handled=[]
    def handle(update,*args):
        handled.append(update['update_id'])
        assert (tmp_path/'telegram.ready').exists()
        (tmp_path/'telegram.maintenance').touch()
    monkeypatch.setattr(bot,'handle_update',handle)
    assert bot.run_bot(tmp_path,'offline')==0
    assert handled==[1]
    repo.set_offset.assert_called_once_with(2)
    assert not (tmp_path/'telegram.ready').exists()


def test_temporary_polling_error_recovers_without_restarting_worker(tmp_path,monkeypatch,caplog):
    api=Mock()
    polls=[]
    def call(method,**kwargs):
        if method=='getMe': return {'username':'offline'}
        if method=='getWebhookInfo': return {}
        polls.append(kwargs['_attempt'])
        if len(polls)==1: raise bot.TelegramError(0,'timeout',exception_type='TimeoutError')
        (tmp_path/'telegram.maintenance').touch()
        return []
    api.call.side_effect=call
    wire_service(monkeypatch,api)
    delays=[]
    monkeypatch.setattr(bot,'wait_or_maintenance',lambda root,delay:delays.append(delay) or False)
    with caplog.at_level(logging.INFO,logger='telegram_operator'):
        assert bot.run_bot(tmp_path,'offline')==0
    assert polls==[1,2] and delays==[5]
    assert 'polling_connected recovery=True' in caplog.text


@pytest.mark.parametrize('code',[401,403,407,409])
def test_permanent_polling_failure_requires_operator(tmp_path,monkeypatch,code):
    api=Mock()
    def call(method,**kwargs):
        if method=='getMe': return {'username':'offline'}
        if method=='getWebhookInfo': return {}
        raise bot.TelegramError(code)
    api.call.side_effect=call
    wire_service(monkeypatch,api)
    wait=Mock(side_effect=AssertionError('no retry'))
    monkeypatch.setattr(bot,'wait_or_maintenance',wait)
    assert bot.run_bot(tmp_path,'offline')==5
    wait.assert_not_called()


def test_startup_network_failure_is_safe_and_eligible_for_supervisor(tmp_path,monkeypatch,caplog):
    api=Mock()
    api.call.side_effect=bot.TelegramError(0,'dns','dns','gaierror')
    monkeypatch.setattr(bot,'TelegramAPI',lambda token:api)
    with caplog.at_level(logging.ERROR,logger='telegram_operator'):
        assert bot.run_bot(tmp_path,'PRIVATE_TOKEN')==6
    assert 'category=dns stage=dns exception=gaierror' in caplog.text
    assert 'PRIVATE_TOKEN' not in caplog.text
    assert not (tmp_path/'telegram.ready').exists()


def test_maintenance_blocks_startup_before_api(tmp_path,monkeypatch):
    (tmp_path/'telegram.maintenance').touch()
    api=Mock(side_effect=AssertionError('no API'))
    monkeypatch.setattr(bot,'TelegramAPI',api)
    assert bot.run_bot(tmp_path,'offline')==0
    api.assert_not_called()
