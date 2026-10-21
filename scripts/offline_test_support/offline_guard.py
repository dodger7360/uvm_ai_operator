"""SQLite/network test guard inherited by ordinary Python children.

This is test isolation, not an OS security sandbox.
"""
import os
from pathlib import Path
import socket
import sqlite3
import sqlite3.dbapi2
import sys
import tempfile
from urllib.parse import urlsplit
from urllib.request import url2pathname

_installed_roots = set()


def scrub_environment():
    for key in list(os.environ):
        if key.startswith(('OPENAI_', 'TELEGRAM_', 'STEEL_AGENT_')) or key in {
            'OPERATOR_DB', 'ADMIN_TELEGRAM_ID', 'APP_VERSION', 'PYTEST_ADDOPTS', 'PYTEST_PLUGINS',
        }:
            os.environ.pop(key, None)
    os.environ.update(OPENAI_API_KEY='offline-test-not-a-key', PYTHON_DOTENV_DISABLED='1',
                      PYTEST_DISABLE_PLUGIN_AUTOLOAD='1', PYTHONDONTWRITEBYTECODE='1')
    sys.dont_write_bytecode = True


def database_path(value):
    value = os.fsdecode(value)
    if value == ':memory:':
        return None
    if value.lower().startswith('file:'):
        parsed = urlsplit(value)
        if parsed.netloc.lower() not in {'', 'localhost'}:
            raise RuntimeError('Remote SQLite access prohibited by offline launcher')
        value = url2pathname(parsed.path)
    if value.startswith(('\\\\', '//')):
        raise RuntimeError('Remote SQLite access prohibited by offline launcher')
    return Path(value).resolve()


def install():
    root = Path(os.environ['V2_OFFLINE_RUN_ROOT']).resolve(strict=True)
    if root in _installed_roots:
        return
    scrub_environment()
    for name in ('TMP', 'TEMP', 'TMPDIR'):
        os.environ[name] = str(root)
    tempfile.tempdir = str(root)

    def check_database(value):
        path = database_path(value)
        if path is not None and not path.is_relative_to(root):
            raise RuntimeError('User SQLite access prohibited by offline launcher')

    def authorize(action, arg1, arg2, database, source):
        if action == sqlite3.SQLITE_ATTACH:
            check_database(arg1)
        return sqlite3.SQLITE_OK

    def audit(event, args):
        if event in {'socket.connect', 'socket.getaddrinfo', 'socket.sendto', 'socket.sendmsg'}:
            raise RuntimeError('Network disabled by offline test launcher')
        if event == 'sqlite3.connect':
            check_database(args[0])

    def no_network(*args, **kwargs):
        raise RuntimeError('Network disabled by offline test launcher')

    socket.socket.connect = no_network
    socket.socket.connect_ex = no_network
    socket.create_connection = no_network
    sys.addaudithook(audit)
    real_connect = sqlite3.connect

    def isolated_connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        connection.set_authorizer(authorize)
        return connection

    sqlite3.connect = sqlite3.dbapi2.connect = isolated_connect
    _installed_roots.add(root)
