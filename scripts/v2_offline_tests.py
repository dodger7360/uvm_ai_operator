"""Run pytest without real credentials/network or access to user SQLite files."""
import os
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
from urllib.parse import urlsplit
from urllib.request import url2pathname

ROOT=Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0,str(ROOT))
for key in list(os.environ):
    if key.startswith(('OPENAI_','TELEGRAM_','STEEL_AGENT_')) or key in {'OPERATOR_DB','ADMIN_TELEGRAM_ID','APP_VERSION'}:
        os.environ.pop(key,None)
os.environ.update(OPENAI_API_KEY='offline-test-not-a-key',PYTHON_DOTENV_DISABLED='1',
                  PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',PYTHONDONTWRITEBYTECODE='1')


def no_network(*args,**kwargs):
    raise RuntimeError('Network disabled by offline test launcher')


socket.socket.connect=no_network
socket.socket.connect_ex=no_network
connect=sqlite3.connect
allowed=(ROOT,Path(tempfile.gettempdir()).resolve())


def isolated_connect(database,*args,**kwargs):
    if str(database)!=':memory:':
        value=os.fsdecode(database)
        if kwargs.get('uri') and value.startswith('file:'):
            parsed=urlsplit(value)
            if parsed.netloc not in {'','localhost'}:
                raise RuntimeError('Remote SQLite access prohibited by offline launcher')
            value=url2pathname(parsed.path)
        path=Path(value).resolve()
        if not any(path.is_relative_to(root) for root in allowed):
            raise RuntimeError('User SQLite access prohibited by offline launcher')
    return connect(database,*args,**kwargs)


sqlite3.connect=isolated_connect
import pytest
raise SystemExit(pytest.main(['-q','-p','no:cacheprovider',*sys.argv[1:]]))
