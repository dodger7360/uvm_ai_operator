"""Real launcher regressions: collection boundaries, inherited IO guards, no Git."""
from pathlib import Path
import os
import subprocess
import sys
import uuid
import pytest
from test_suite_inventory import CURRENT, LEGACY, RESEARCH

ROOT=Path(__file__).resolve().parents[1]
LAUNCHER=ROOT/'scripts'/'v2_offline_tests.py'


def launch(arguments, **kwargs):
    return subprocess.run([sys.executable,'-B',str(LAUNCHER),*map(str,arguments)],cwd=ROOT,
                          capture_output=True,text=True,timeout=90,**kwargs)


def test_every_test_module_has_one_explicit_suite():
    actual={path.relative_to(ROOT).as_posix() for path in (ROOT/'tests').rglob('test_*.py')}
    assert not CURRENT.intersection(LEGACY|RESEARCH) and not LEGACY.intersection(RESEARCH)
    assert actual==CURRENT|LEGACY|RESEARCH


def test_real_launcher_confines_sqlite_and_network_in_parent_and_children(tmp_path):
    # Existing ordinary files outside the run must be rejected before SQLite
    # can inspect them. Both are created solely for this test and removed below.
    repository_file=ROOT/('.tmp-guard-'+uuid.uuid4().hex)
    system_file=Path(os.environ['V2_OFFLINE_RUN_ROOT']).parent/('guard-'+uuid.uuid4().hex)
    for path in (repository_file,system_file):path.write_bytes(b'synthetic do not open')
    probe=tmp_path/'test_guard_probe.py'
    probe.write_text('''
from pathlib import Path
import os,socket,sqlite3,subprocess,sys
import sqlite3.dbapi2 as dbapi2
import pytest
OUTSIDE=OUTSIDE_VALUE

def test_normal_and_uri(tmp_path):
    path=tmp_path/'space and юникод.sqlite3'
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE sample(value)')
        connection.execute('INSERT INTO sample VALUES(42)')
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as connection:
        assert connection.execute('SELECT value FROM sample').fetchone()[0]==42

def test_external_files_and_aliases():
    for value in OUTSIDE:
        for path in [value,Path(value).as_uri()+'?mode=ro']:
            for connect in [sqlite3.connect,dbapi2.connect]:
                with pytest.raises(RuntimeError,match='User SQLite access prohibited'):connect(path,uri=True)
    with pytest.raises(RuntimeError,match='Remote SQLite access prohibited'):
        sqlite3.connect('file://remote-host/share/data.sqlite3?mode=ro',uri=True)
    with sqlite3.connect(':memory:') as connection:
        with pytest.raises(sqlite3.DatabaseError):connection.execute('ATTACH DATABASE ? AS forbidden',(OUTSIDE[0],))

def test_parent_network():
    with pytest.raises(RuntimeError,match='Network disabled'):socket.create_connection(('127.0.0.1',9))
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as udp:
        with pytest.raises(RuntimeError,match='Network disabled'):udp.sendto(b'offline',('127.0.0.1',9))

def test_child_inherits_guards(tmp_path):
    code="""import os,socket,sqlite3,sys
assert os.environ['OPENAI_API_KEY']=='offline-test-not-a-key'
assert os.environ['PYTHON_DOTENV_DISABLED']=='1'
assert sys.dont_write_bytecode
for value in sys.argv[1:-1]:
    try:sqlite3.connect(value)
    except RuntimeError:pass
    else:raise AssertionError('child SQLite escaped')
try:socket.create_connection(('127.0.0.1',9))
except RuntimeError:pass
else:raise AssertionError('child network escaped')
with sqlite3.connect(sys.argv[-1]) as connection:connection.execute('CREATE TABLE allowed(value)')
print('child-guarded')
"""
    env=os.environ.copy();env['OPENAI_API_KEY']='synthetic-parent-key'
    child=subprocess.run([sys.executable,'-B','-c',code,*OUTSIDE,str(tmp_path/'child.sqlite3')],env=env,capture_output=True,text=True,timeout=15)
    assert child.returncode==0,child.stdout+child.stderr
    assert child.stdout.strip()=='child-guarded'
'''.replace('OUTSIDE_VALUE',repr([str(repository_file),str(system_file)])),encoding='utf-8')
    try:
        result=launch([probe,'--basetemp='+str(tmp_path/'guard-tmp')])
        assert result.returncode==0,result.stdout+result.stderr
        assert '4 passed' in result.stdout
        assert all(path.read_bytes()==b'synthetic do not open' for path in (repository_file,system_file))
    finally:
        for path in (repository_file,system_file):path.unlink(missing_ok=True)


@pytest.mark.parametrize('module',['tests/test_ai_service.py','tests/legacy/test_reasoning_high.py',
                                   'tests/research/test_historical_git_contracts.py'])
def test_explicit_historical_path_requires_opt_in(module):
    result=launch([module,'--collect-only'])
    assert result.returncode==4 and 'outside --suite=current' in result.stderr


def test_collection_does_not_import_historical_modules(tmp_path):
    code='''import sys,pytest
from pathlib import Path
root=Path.cwd()
assert pytest.main(['--collect-only','-q','-p','no:cacheprovider',str(root/'tests')])==0
assert 'test_ai_service' not in sys.modules
assert 'test_historical_git_contracts' not in sys.modules
assert 'test_armatura_price_ingestion' not in sys.modules
assert 'tests.legacy.test_reasoning_high' not in sys.modules
print('collection-current-only')
'''
    child=subprocess.run([sys.executable,'-B','-c',code],cwd=ROOT,capture_output=True,text=True,timeout=60)
    assert child.returncode==0,child.stdout+child.stderr
    assert 'collection-current-only' in child.stdout


def test_launcher_rejects_destructive_basetemp_and_ignores_external_pytest_options(tmp_path):
    marker=tmp_path/'keep.txt';marker.write_text('keep')
    result=launch(['--basetemp='+str(tmp_path)])
    assert result.returncode!=0 and marker.read_text()=='keep'
    env=os.environ.copy()
    env.update(PYTEST_ADDOPTS='--suite=research',PYTEST_PLUGINS='nonexistent_external_plugin')
    result=launch(['tests/test_shared_primitives.py','--collect-only'],env=env)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'test_quantity_precision' in result.stdout and 'test_four_b6' not in result.stdout
