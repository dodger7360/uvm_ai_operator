"""Offline launcher's SQLite URI paths stay confined on Windows and Mac."""
from pathlib import Path
import os
import subprocess
import sys
import json
from types import SimpleNamespace
from unittest.mock import Mock


def test_offline_launcher_accepts_local_uri_and_rejects_outside_uri(tmp_path):
    root = Path(__file__).resolve().parents[1]
    outside = root.parent / 'forbidden-user-database.sqlite3'
    # The outside path need not exist: the guard must reject before SQLite opens it.
    probe = tmp_path / 'test_uri_probe.py'
    probe.write_text('''from pathlib import Path
import sqlite3
import pytest

def test_uri(tmp_path):
    source = tmp_path / 'space and юникод.sqlite3'
    with sqlite3.connect(source) as c:
        c.execute('CREATE TABLE sample(value)')
        c.execute('INSERT INTO sample VALUES(42)')
    with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as c:
        assert c.execute('SELECT value FROM sample').fetchone()[0] == 42
    with pytest.raises(RuntimeError, match='User SQLite access prohibited'):
        sqlite3.connect(OUTSIDE, uri=True)
    with pytest.raises(RuntimeError, match='Remote SQLite access prohibited'):
        sqlite3.connect('file://remote-host/share/user.sqlite3?mode=ro', uri=True)
'''.replace('OUTSIDE', repr(outside.as_uri() + '?mode=ro')), encoding='utf-8')
    env = os.environ.copy()
    env['PYTHONUTF8'] = '1'
    result = subprocess.run([sys.executable, '-B', str(root / 'scripts/v2_offline_tests.py'),
                             str(probe), '--basetemp=' + str(tmp_path / 'probe-tmp')],
                            cwd=root, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert '1 passed' in result.stdout


def test_runtime_high_reaches_responses_without_changing_legacy_environment(tmp_path, monkeypatch):
    import httpx2
    import openai
    from database import Database
    from steel_agent_v2.contracts import AgentPlan
    from steel_agent_v2.runtime import connect
    sent = []
    def respond(request):
        sent.append(json.loads(request.content))
        return httpx2.Response(200, json={'id':'resp_offline','object':'response','created_at':0,
            'status':'completed','model':'gpt-6-luna','output':[{'id':'msg_offline','type':'message',
            'role':'assistant','status':'completed','content':[{'type':'output_text',
            'text':AgentPlan(intent='free_chat',answer='Ответ').model_dump_json(),'annotations':[]}]}]})
    monkeypatch.setenv('OPENAI_MODEL','legacy-model')
    monkeypatch.setenv('OPENAI_REASONING_EFFORT','low')
    monkeypatch.setenv('OPENAI_FALLBACK_REASONING_EFFORT','medium')
    before = {k:os.environ[k] for k in ('OPENAI_MODEL','OPENAI_REASONING_EFFORT','OPENAI_FALLBACK_REASONING_EFFORT')}
    real_client = openai.OpenAI
    with openai.DefaultHttpxClient(transport=httpx2.MockTransport(respond)) as http:
        monkeypatch.setattr(openai,'DefaultHttpxClient',lambda **kwargs:http)
        monkeypatch.setattr(openai,'OpenAI',lambda **kwargs:real_client(api_key='offline-test',**kwargs))
        access = SimpleNamespace(repository=SimpleNamespace(database=Database(tmp_path/'legacy.sqlite3')),
                                 allowed=lambda user:True)
        service, _ = connect(tmp_path/'v2.sqlite3',access)
        service.handle(1,'Привет',event_id='offline-high')
    assert sent[0]['model'] == 'gpt-6-luna'
    assert sent[0]['reasoning'] == {'effort':'high'}
    assert len(sent) == 1 and 'max_output_tokens' not in sent[0]
    assert {k:os.environ[k] for k in before} == before
    # Legacy still resolves its own setting; no v2 override leaks into its call.
    from ai import AIService
    from schemas import PurchaseRequestExtraction
    legacy = AIService.__new__(AIService)
    legacy._client = Mock()
    legacy._invoke('extraction',PurchaseRequestExtraction,[])
    assert legacy._client.beta.chat.completions.parse.call_args.kwargs['reasoning_effort'] == 'low'
