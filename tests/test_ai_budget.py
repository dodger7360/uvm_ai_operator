"""Budget admission uses real SQLite and offline transports only."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
from unittest.mock import Mock

import pytest

from ai import AIService
from ai_budget import BetaBudget, BudgetLimit, MAX_OUTPUT, budget_actor
from database import Database
from diagnostics import operation
from repositories.telegram_repository import TelegramRepository
from schemas import PurchaseRequest, RequestItem
from telegram_service import TelegramService
from work_assistant import WorkAssistantService


def reserve(budget, user=1, **kw):
    with budget_actor(user):
        return budget.reserve(stage='extraction', model=kw.pop('model', 'gpt-6-luna'), payload=kw)


def count(path):
    with sqlite3.connect(path) as c:
        return c.execute('SELECT COUNT(*), COALESCE(SUM(reserved_cents),0) FROM ai_reservation').fetchone()


def test_lifetime_survives_restart_and_does_not_reset_next_day(tmp_path):
    path = tmp_path/'budget.sqlite3'
    b = BetaBudget(path, total_calls=2, clock=lambda: 100000)
    reserve(b)
    reserve(b, 2)
    restarted = BetaBudget(path, total_calls=2, clock=lambda: 200000)
    with pytest.raises(BudgetLimit): reserve(restarted, 3)
    assert count(path) == (2, 10)


def test_atomic_last_reservation_across_connections(tmp_path):
    path = tmp_path/'budget.sqlite3'
    BetaBudget(path)
    def attempt(n):
        try: reserve(BetaBudget(path, total_calls=1), n); return True
        except BudgetLimit: return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(attempt, range(8))) == 1
    assert count(path) == (1, 5)


@pytest.mark.parametrize('payload', [{'model':'gpt-6-sol'}, {'tools':[{'type':'web_search'}]}, {'input':'Я'*24001}])
def test_model_tools_and_oversized_utf8_blocked_before_charge(tmp_path, payload):
    path = tmp_path/'budget.sqlite3'
    b = BetaBudget(path)
    with pytest.raises(BudgetLimit): reserve(b, **payload)
    assert count(path) == (0, 0)


def test_missing_actor_and_per_message_limit(tmp_path):
    b = BetaBudget(tmp_path/'budget.sqlite3')
    with pytest.raises(BudgetLimit): b.reserve(stage='extraction',model='gpt-6-luna',payload={})
    with budget_actor(1):
        for _ in range(3): b.reserve(stage='extraction',model='gpt-6-luna',payload={})
        with pytest.raises(BudgetLimit): b.reserve(stage='extraction',model='gpt-6-luna',payload={})


def test_user_hourly_daily_and_lifetime_limits(tmp_path):
    now = [100000]
    b = BetaBudget(tmp_path/'budget.sqlite3', clock=lambda:now[0])
    for _ in range(6): reserve(b)
    with pytest.raises(BudgetLimit): reserve(b)
    now[0] += 3601
    for _ in range(4): reserve(b)
    with pytest.raises(BudgetLimit): reserve(b)
    now[0] += 86401
    for _ in range(6): reserve(b)
    now[0] += 3601
    for _ in range(4): reserve(b)
    now[0] += 86401
    with pytest.raises(BudgetLimit): reserve(b)
    reserve(b, 2)


def test_global_hourly_limit(tmp_path):
    b=BetaBudget(tmp_path/'budget.sqlite3')
    for user in range(18): reserve(b,user)
    with pytest.raises(BudgetLimit): reserve(b,19)


def test_missing_or_corrupt_ledger_fails_closed(tmp_path):
    path=tmp_path/'budget.sqlite3'
    b=BetaBudget(path)
    path.unlink()
    with pytest.raises(BudgetLimit): reserve(b)
    path.write_bytes(b'not a database')
    with pytest.raises(BudgetLimit): reserve(b)


def test_transport_failure_consumes_reservation_and_caps_output(tmp_path):
    path=tmp_path/'budget.sqlite3'
    ai=object.__new__(AIService)
    ai.budget=BetaBudget(path, total_calls=1)
    ai._client=Mock()
    ai._client.beta.chat.completions.parse.side_effect=TimeoutError('do not log this secret')
    with budget_actor(1), operation(), pytest.raises(TimeoutError): ai.parse_request('Уголок 50х50х5 — 1 т')
    kwargs=ai._client.beta.chat.completions.parse.call_args.kwargs
    assert kwargs['max_completion_tokens']==MAX_OUTPUT
    assert kwargs['service_tier']=='default'
    with budget_actor(1), pytest.raises(BudgetLimit): ai.parse_request('Уголок 50х50х5 — 1 т')
    ai._client.beta.chat.completions.parse.assert_called_once()
    assert count(path)==(1,5)


def test_full_schema_is_part_of_payload_bound(tmp_path):
    ai=object.__new__(AIService); ai.budget=BetaBudget(tmp_path/'budget.sqlite3'); ai._client=Mock()
    class Huge:
        @staticmethod
        def model_json_schema(): return {'description':'x'*48001}
    with budget_actor(1), pytest.raises(BudgetLimit): ai._invoke('extraction',Huge,[])
    ai._client.beta.chat.completions.parse.assert_not_called()


def test_budget_fallback_preserves_previous_request_and_history(tmp_path):
    repo=TelegramRepository(Database(tmp_path/'requests.sqlite3'))
    ai=Mock()
    source='Уголок 50х50х5 Ст3 — 2 т'
    ai.parse_request.return_value=PurchaseRequest(items=[RequestItem(product='Уголок',product_group='angle',
        size_designation='50х50х5',grade='Ст3',quantity='2',unit='т',source_text=source)])
    service=TelegramService(ai,repo,reliable=True)
    service.process(1,source)
    before=repo.load(1).model_dump_json()
    with repo.database.connection() as c:
        dump_before=list(c.execute('SELECT * FROM telegram_history').fetchall())
    ai.parse_request.side_effect=BudgetLimit('persistent_allowance')
    with pytest.raises(BudgetLimit): service.process(1,'Швеллер 12П Ст3 — 7 т')
    assert repo.load(1).model_dump_json()==before
    with repo.database.connection() as c: assert list(c.execute('SELECT * FROM telegram_history').fetchall())==dump_before
    assert service.process(1,'/show')


def test_search_is_blocked_and_both_paths_share_allowance(tmp_path, caplog):
    path=tmp_path/'budget.sqlite3'
    b=BetaBudget(path,total_calls=1)
    client=Mock();client.with_options.return_value=client
    assistant=WorkAssistantService(client,budget=b)
    with budget_actor(99): answer=assistant.answer('Найди действующий ГОСТ',user_id=99)
    assert 'Интернет-поиск временно отключён' in answer.text
    client.responses.create.assert_not_called()
    assert count(path)==(0,0)
    reserve(b,99)
    with budget_actor(99): answer=assistant.answer('Что значит снабжение?',user_id=99)
    client.responses.create.assert_not_called()
    assert 'лимит' in answer.text


def test_guard_logs_only_metadata(tmp_path,caplog):
    caplog.set_level('INFO',logger='operator_diagnostics')
    b=BetaBudget(tmp_path/'budget.sqlite3')
    reserve(b,987654321,input='private request sk-secret')
    assert 'reserved_cents' in caplog.text
    assert '987654321' not in caplog.text
    assert 'private request' not in caplog.text and 'sk-secret' not in caplog.text


def test_custom_endpoint_rejected_before_reservation(tmp_path,monkeypatch):
    monkeypatch.setenv('OPENAI_BASE_URL','https://custom.invalid/v1')
    ai=object.__new__(AIService);ai.budget=BetaBudget(tmp_path/'budget.sqlite3');ai._client=Mock()
    with budget_actor(1),pytest.raises(BudgetLimit):ai.parse_request('Уголок 50х50х5 — 1 т')
    ai._client.beta.chat.completions.parse.assert_not_called()
    assert count(ai.budget.path)==(0,0)


def test_explicit_acceptance_extension_keeps_same_persistent_history(tmp_path):
    path=tmp_path/'budget.sqlite3'
    original=BetaBudget(path,total_calls=6,user_calls=6)
    for _ in range(6):reserve(original)
    with pytest.raises(BudgetLimit):reserve(original)
    extended=BetaBudget(path,total_calls=8,user_calls=8,user_hourly_calls=8)
    reserve(extended);reserve(extended)
    with pytest.raises(BudgetLimit):reserve(extended)
    assert count(path)==(8,40)
    # Production still uses the default six/hour; no automatic quota renewal.
    with pytest.raises(BudgetLimit):reserve(BetaBudget(path))


def test_100_row_timeout_fallback_keeps_every_source_and_exact_groups(tmp_path):
    from consolidation import consolidate_items
    from decimal import Decimal
    source='\n'.join(f'Уголок {50+i%15}х{50+i%15}х5 Ст3 — '
                     + ('1000 кг' if i%2 else '1 т') for i in range(100))
    repo=TelegramRepository(Database(tmp_path/'requests.sqlite3'))
    ai=Mock();ai.parse_request.side_effect=TimeoutError()
    service=TelegramService(ai,repo,reliable=True,commercial_view=True)
    messages=service.process(1,source)
    current=repo.load(1)
    assert current.fallback_status=='partial'  # Never call an API failure a verified extraction.
    assert current.source_text==source
    assert len(current.items)==100 and not current.unprocessed_source
    assert not current.is_valid
    groups=consolidate_items(current.items)
    assert len(groups)==15
    assert sorted(i for g in groups for i in g.source_indices)==list(range(1,101))
    assert sum(g.item.quantity for g in groups)==Decimal(100)
    assert messages
    ai.parse_request.assert_called_once()
