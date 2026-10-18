"""Queue typing and failure UX; fake APIs, events and temporary SQLite only."""
from copy import deepcopy
from pathlib import Path
from threading import Event,Thread
import json
import logging
import time

import pytest
import telegram_bot
import steel_agent_v2.queue_runtime as runtime
from steel_agent_v2 import Repository,SteelAgent
from steel_agent_v2.queue import DurableQueue
from diagnostics import operation
from test_steel_agent_v2 import Fake,SOURCE,proposal
from test_steel_agent_v2_queue import rig,admit,Executor


@pytest.fixture
def indicators(monkeypatch):
    result=[]
    def create(api,user,**kwargs):
        value=telegram_bot.TypingIndicator(api,user,interval=.01,**kwargs)
        result.append(value)
        return value
    monkeypatch.setattr(runtime,'TypingIndicator',create)
    return result


def wait_until(predicate,timeout=3):
    end=time.monotonic()+timeout
    while not predicate() and time.monotonic()<end:Event().wait(.005)
    assert predicate()


def test_typing_only_active_job_repeats_and_stops_before_delivery(rig,indicators):
    started,release=Event(),Event()
    def plan(*_):
        started.set();assert release.wait(3)
        return proposal()
    fake=Fake(plan);admit(rig,1)
    assert not rig.api.typing and not indicators
    executor=Executor(rig.queue,rig.access,lambda:rig.api,lambda:fake)
    thread=Thread(target=lambda:executor.execute(rig.queue.claim()));thread.start()
    try:
        assert started.wait(2)
        wait_until(lambda:len(rig.api.typing)>=2)
        # Both SQLite databases remain writable during the GPT wait.
        for db in [rig.queue.database,rig.legacy.database]:
            with db.connection() as c:c.execute('BEGIN IMMEDIATE')
        assert all(p['chat_id']==1 and p['action']=='typing' for p in rig.api.typing)
    finally:release.set();thread.join(4)
    assert not thread.is_alive() and not indicators[0].thread.is_alive()
    assert rig.queue.job(1)['state']=='delivered'
    before=len(rig.api.typing);Event().wait(.03)
    assert len(rig.api.typing)==before
    DurableQueue(rig.queue.database).recover()
    assert len(indicators)==1 and rig.queue.claim() is None


def test_typing_error_does_not_change_success_or_outbox(rig,indicators):
    call=rig.api.call;typing_attempted=Event()
    def api(method,**payload):
        if method=='sendChatAction':
            typing_attempted.set();raise RuntimeError('PRIVATE_EXCEPTION')
        return call(method,**payload)
    rig.api.call=api
    def plan(*_):assert typing_attempted.wait(2);return proposal()
    admit(rig,1)
    Executor(rig.queue,rig.access,lambda:rig.api,lambda:Fake(plan)).execute(rig.queue.claim())
    assert rig.queue.job(1)['state']=='delivered'
    assert Repository(rig.queue.database).load(1).revision==1
    assert len(rig.api.calls)==1 and not indicators[0].thread.is_alive()


def test_typing_stops_on_unknown_model_result_without_replay(rig,indicators):
    admit(rig,1);fake=Fake(TimeoutError('model timeout'))
    Executor(rig.queue,rig.access,lambda:rig.api,lambda:fake).execute(rig.queue.claim())
    assert rig.queue.job(1)['state']=='execution_unknown'
    assert not indicators[0].thread.is_alive()
    DurableQueue(rig.queue.database).recover()
    assert rig.queue.claim() is None and len(fake.calls)==1 and len(indicators)==1


def test_shutdown_stops_ephemeral_indicator_while_gpt_drains(rig,indicators):
    started,release,stop=Event(),Event(),Event()
    def plan(*_):started.set();assert release.wait(3);return proposal()
    admit(rig,1)
    executor=Executor(rig.queue,rig.access,lambda:rig.api,lambda:Fake(plan),stopping=stop.is_set)
    thread=Thread(target=lambda:executor.execute(rig.queue.claim()));thread.start()
    try:
        assert started.wait(2);wait_until(lambda:bool(rig.api.typing))
        stop.set();wait_until(lambda:not indicators[0].thread.is_alive())
        assert thread.is_alive() and rig.queue.job(1)['state']=='api_started'
    finally:release.set();thread.join(4)
    assert not thread.is_alive()


def test_indicators_of_parallel_users_are_independent(rig,indicators):
    starts=[Event(),Event()];releases=[Event(),Event()];index=iter(range(2))
    def factory():
        n=next(index)
        def plan(*_):starts[n].set();assert releases[n].wait(4);return proposal()
        return Fake(plan)
    executor=Executor(rig.queue,rig.access,lambda:rig.api,factory)
    admit(rig,1,user=1);admit(rig,2,user=2)
    a=Thread(target=lambda:executor.execute(rig.queue.claim()));a.start();assert starts[0].wait(2)
    b=Thread(target=lambda:executor.execute(rig.queue.claim()));b.start()
    try:
        assert starts[1].wait(2)
        wait_until(lambda:{p['chat_id'] for p in rig.api.typing}=={1,2})
        releases[0].set();a.join(2)
        assert not a.is_alive() and not indicators[0].thread.is_alive()
        before=sum(p['chat_id']==2 for p in rig.api.typing)
        wait_until(lambda:sum(p['chat_id']==2 for p in rig.api.typing)>before)
        assert b.is_alive()
    finally:
        for e in releases:e.set()
        a.join(3);b.join(3)
    assert all(not i.thread.is_alive() for i in indicators)


def test_rejected_creation_has_no_append_claim_and_retains_active(rig,caplog):
    repo=Repository(rig.queue.database);agent=SteelAgent(Fake(proposal()),repo)
    agent.handle(1,SOURCE,event_id='initial');before=repo.load(1).model_dump_json()
    bad=proposal();bad['extraction']['items'][0]['characteristics']*=2
    with caplog.at_level(logging.INFO,logger='operator_diagnostics'),operation():
        reply=SteelAgent(Fake(bad,bad),repo).handle(1,SOURCE,event_id='invalid-new-request')
    assert reply.status=='failed' and reply.diagnostics==['duplicate_characteristic:i1','pending_source']
    assert 'Добавление' not in reply.text and 'append' not in reply.text and 'schema:' not in reply.text
    assert repo.load(1).model_dump_json()==before
    events=[json.loads(r.message) for r in caplog.records if r.message.startswith('{')]
    validation=[e for e in events if e.get('event')=='steel_v2_validation']
    assert [e['intent'] for e in validation]==['create_request','create_request']
    assert all(e['extracted_count']==2 and e['diagnostic_codes']==['duplicate_characteristic'] for e in validation)
    assert all('i1' not in json.dumps(e) for e in validation)


def test_actual_unsupported_append_still_uses_typed_clarification(rig):
    repo=Repository(rig.queue.database);SteelAgent(Fake(proposal()),repo).handle(1,SOURCE,event_id='initial')
    before=repo.load(1).model_dump_json()
    reply=SteelAgent(Fake(dict(intent='clarify',answer='Добавление новых позиций пока не поддерживается.')),repo).handle(1,'Добавь позиции',event_id='append')
    assert 'Добавление' in reply.text and reply.intent=='clarify'
    assert repo.load(1).model_dump_json()==before


def test_diagnostic_explanation_view_currently_repeats_snapshot(rig):
    repo=Repository(rig.queue.database);SteelAgent(Fake(proposal()),repo).handle(1,SOURCE,event_id='initial')
    before=repo.load(1).model_dump_json()
    reply=SteelAgent(Fake(dict(intent='answer_question',view='explanation',answer='Краткое объяснение.')),repo).handle(1,'Почему сгруппировал?',event_id='explain')
    assert 'Краткое объяснение.' not in reply.text
    assert 'С245' in reply.text and '750' in reply.text and 'Обоснование группировки' in reply.text
    assert repo.load(1).model_dump_json()==before


def six_synthetic_plan():
    """Invented fixture and controlled GPT extraction; no production data or parser."""
    source=(Path(__file__).parent/'fixtures'/'steel_v2_021_synthetic_angles_channels.txt').read_text(encoding='utf-8')
    records=[];items=[];groups=[];offset=0
    for n,line in enumerate(source.splitlines(keepends=True),1):
        text=line.rstrip('\r\n');facts,tail=text.split(' — ');amount,unit=tail.split();product,size,grade,gost,standard=facts.split()
        sid,iid=f'r{n}',f'i{n}'
        records.append(dict(id=sid,kind='product',start=offset,end=offset+len(text)))
        items.append(dict(id=iid,source_id=sid,label=product,quantity=amount.replace(',','.'),unit=unit,quantity_text=amount,unit_text=unit,characteristics=[dict(name=name,value=value,raw=value,source_ids=[sid]) for name,value in [('size',size),('grade',grade),('standard',gost+' '+standard)]]))
        groups.append(dict(id=f'g{n}',label='Исходная позиция',item_ids=[iid],rationale='Отдельная позиция'))
        offset+=len(line)
    return source,dict(intent='create_request',extraction=dict(records=records,items=items,groups=groups))


def test_synthetic_six_full_route_keeps_positions_quantities_and_evidence(rig):
    from decimal import Decimal
    source,plan=six_synthetic_plan()
    admit(rig,1,text=source)
    Executor(rig.queue,rig.access,lambda:rig.api,lambda:Fake(plan)).execute(rig.queue.claim())
    request=Repository(rig.queue.database).load(1)
    assert request.original_text==source and len(request.items)==6
    assert sum(i.quantity for i in request.items)/Decimal(1000)==Decimal('0.240726')
    assert [i.model_dump() for i in request.items]==[i.model_dump() for i in request.originals]
    for item,line in zip(request.items,source.splitlines()):
        for fact in item.characteristics:
            assert fact.raw in line and fact.value==fact.raw
        assert item.quantity_text in line and item.unit_text in line
    assert rig.queue.job(1)['state']=='delivered'


@pytest.mark.parametrize('prior_schema_error',[False,True])
def test_injected_duplicate_characteristic_on_synthetic_six(rig,caplog,prior_schema_error):
    from pydantic import ValidationError
    from steel_agent_v2.contracts import AgentPlan
    source,bad=six_synthetic_plan()
    bad['extraction']['items'][0]['characteristics'].append(deepcopy(bad['extraction']['items'][0]['characteristics'][1]))
    try:AgentPlan.model_validate({'intent':'create_request'})
    except ValidationError as error:initial=error
    fake=Fake(initial if prior_schema_error else deepcopy(bad),bad)
    admit(rig,1,text=source)
    with caplog.at_level(logging.INFO,logger='operator_diagnostics'):
        Executor(rig.queue,rig.access,lambda:rig.api,lambda:fake).execute(rig.queue.claim())
    recovered=Repository(rig.queue.database).load(1)
    assert len(recovered.items)==5 and all(i.id!='i1' for i in recovered.items)
    assert recovered.original_text==source and any(r.kind=='unresolved' for r in recovered.records)
    from steel_agent_v2.engine import RequestEngine
    RequestEngine().validate(recovered)
    with rig.queue.database.connection() as c:
        message,raw=c.execute("SELECT message,reply FROM steel_v2_turn WHERE event_id='telegram:1'").fetchone()
    reply=json.loads(raw)
    assert message==source and reply['status']=='partial'
    assert reply['diagnostics']==['duplicate_characteristic:i1','partial_recovered']
    assert 'Добавление' not in reply['text']
    events=[json.loads(r.message) for r in caplog.records if r.message.startswith('{')]
    validation=[e for e in events if e.get('event')=='steel_v2_validation']
    assert [e['reason'] for e in validation]==['ValidationError' if prior_schema_error else 'EngineError','EngineError']
    if prior_schema_error:
        assert validation[0]['intent'] is None and validation[0]['extracted_count'] is None
    else:
        assert validation[0]['intent']=='create_request' and validation[0]['extracted_count']==6
    assert validation[1]['intent']=='create_request' and validation[1]['extracted_count']==6
    assert all(e['operation'] for e in validation)
