"""Block 4.1: synthetic Telegram updates through queue, SDK HTTP and real SQLite.

Only external OpenAI/Telegram replies are supplied. These are not recorded live
model responses and do not establish Russian-language understanding or latency.
The diagnostic regression records the current unbound create/partial question gap.
"""
from copy import deepcopy
from decimal import Decimal
import json

import pytest
from pydantic import ValidationError

from database import Database
from diagnostics import http_attempt
from steel_agent_v2 import Repository
from steel_agent_v2.contracts import AgentPlan, Reply
from steel_agent_v2.queue import DurableQueue
from steel_agent_v2.wire import WireCoordinatorPlan
from test_steel_agent_v2 import edit, set_op
from test_steel_agent_v2_027_presentation import sample
from test_steel_agent_v2_queue import rig, admit, Executor
from test_steel_agent_v2_telegram_html import messages, stored, visible
from test_steel_agent_v2_wire_schema import sdk_transport, subset_issues


AMOUNTS=['1.125','2.250','0.375','4.0625','5.875']
QUESTION='Подтвердите единицу второй позиции: т?'


def five_pipes(*, repeated=False, missing=False, question=False):
    rows=[('Труба', [('size',size,size),('grade','Ст20','Ст20'),
                    ('standard','ГОСТ 10704-91','ГОСТ 10704-91')],amount,
           None if missing and n==1 else 'т т' if repeated and n==1 else 'т')
          for n,(size,amount) in enumerate(zip(
              ['42×3 мм','57×3,5 мм','89×4 мм','108×4 мм','159×5 мм'],AMOUNTS))]
    source,plan,_=sample(rows,questions=[QUESTION] if question else [])
    if repeated:
        # The model's supplied interpretation normalizes just the unit, while
        # provenance retains both literal tokens in the immutable source.
        plan['extraction']['items'][1]['unit']='т'
        plan['extraction']['items'][1]['unit_text']='т'
    return source,plan


def wire(plan):
    if plan['intent']=='create_request':
        allowed={'intent','extraction','pending_event_id','display_layout','mass_specs'}
        plan={key:value for key,value in plan.items() if key in allowed}
    else:
        plan=AgentPlan.model_validate(plan).model_dump(mode='json')
    return WireCoordinatorPlan.model_validate({'plan':plan}).model_dump(mode='json')


def execute(rig,number,message,responses):
    """Use existing SDK/queue fixtures; never replace a production algorithm."""
    responses=deepcopy(responses)
    def respond(_):
        assert responses, 'unexpected additional HTTP attempt'
        return responses.pop(0)
    def inspect(payload):
        assert payload['text']['format']['strict'] is True
        assert not subset_issues(payload['text']['format']['schema'])
        assert payload['store'] is False
    admit(rig,number,text=message)
    job=rig.queue.claim()
    assert job is not None and job['update_id']==number
    with sdk_transport(respond,inspect=inspect) as (transport,sent):
        Executor(rig.queue,rig.access,lambda:rig.api,lambda:transport).execute(job)
    assert not responses
    with rig.queue.database.connection() as c:
        row=c.execute('SELECT reply FROM steel_v2_turn WHERE event_id=?',
                      ('telegram:'+str(number),)).fetchone()
        assert row is not None, 'execution stopped before durable reply commit'
        reply=Reply.model_validate_json(row[0])
        attempts=list(c.execute('SELECT kind,state FROM steel_v2_attempt WHERE update_id=? ORDER BY number',(number,)))
    assert reply.calls==len(sent)==len(attempts)
    assert all(tuple(row)==('plan','result_received') for row in attempts)
    assert_delivery(rig,number,reply)
    return reply,sent


def assert_delivery(rig,number,reply):
    job=rig.queue.job(number)
    assert job['state']=='delivered' and job['committed']==1
    rows=stored(rig.queue,number)
    assert rows and all(row['state']=='delivered' and type(row['message_id']) is int for row in rows)
    actual=[]
    for row in rows:
        method,payload=rig.api.calls[row['message_id']-1]
        assert method=='sendMessage' and payload['chat_id']==1
        assert json.loads(row['payload']) in ({},payload)  # compact may discard delivered payload
        actual.append(payload['text'])
    assert visible(actual)==reply.text
    assert rig.queue.cursor()==number+1


def reopen(rig):
    # Fresh queue/repository objects over the same temporary file, no copied
    # in-memory history or clarification. recover performs production recovery.
    rig.queue=DurableQueue(Database(rig.queue.database.path))
    rig.queue.recover()
    return Repository(rig.queue.database)


def assert_snapshot(repo,source,plan,versions):
    state=repo.load(1)
    assert state.original_text==source and len(state.items)==len(state.originals)==5
    assert [item.id for item in state.items]==['i1','i2','i3','i4','i5']
    assert [item.quantity for item in state.items]==[Decimal(value) for value in AMOUNTS]
    assert len(state.records)==5
    for item,original,record,expected in zip(state.items,state.originals,state.records,plan['extraction']['items']):
        assert source[record.start:record.end]==source.splitlines()[int(item.id[1:])-1]
        assert item.characteristics==original.characteristics
        assert [(c.name,c.value) for c in item.characteristics]==[
            (c['name'],c['value']) for c in expected['characteristics']]
        assert original.quantity==item.quantity and original.source_id==item.source_id
    assert repo.load(2) is None and repo.pending(1) is None
    with repo.database.connection() as c:
        assert c.execute('SELECT count(*) FROM steel_v2_version').fetchone()[0]==versions
        assert c.execute('SELECT count(*) FROM steel_v2_inflight').fetchone()[0]==0
        assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    return state


def assert_replay_is_free(rig,number,message):
    before=(deepcopy(messages(rig)),stored(rig.queue,number))
    with rig.queue.database.connection() as c:
        counts=[c.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in
                ['steel_v2_version','steel_v2_turn','steel_v2_attempt']]
    assert admit(rig,number,text=message)==rig.queue.cursor() and rig.queue.claim() is None
    assert (messages(rig),stored(rig.queue,number))==before
    with rig.queue.database.connection() as c:
        assert counts==[c.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in
                       ['steel_v2_version','steel_v2_turn','steel_v2_attempt']]


@pytest.mark.parametrize('repeated',[False,True],ids=['plain-units','literal-t-t'])
def test_five_pipes_create_one_http_call_and_replay_after_restart(rig,repeated):
    source,plan=five_pipes(repeated=repeated)
    reply,sent=execute(rig,100,source,[wire(plan)])
    assert reply.status=='ok' and reply.calls==1
    assert json.loads(sent[0]['input'])['context']['message']==source
    repo=reopen(rig);state=assert_snapshot(repo,source,plan,1)
    assert all(item.unit=='т' for item in state.items) and state.items==state.originals
    assert state.revision==1 and not state.questions and repo.clarification(1,state) is None
    assert [m['content'] for m in repo.history(1,state.id) if m['role']=='user']==[source]
    assert_delivery(rig,100,reply)
    assert_replay_is_free(rig,100,source)


@pytest.mark.parametrize('repair',[False,True],ids=['valid-one-call','duplicate-characteristic-repair'])
def test_five_pipes_payload_sizes_and_existing_call_metrics(rig,caplog,repair):
    """Measure actual SDK bytes offline, without asserting a latency/size budget.

    Only the supplied duplicate characteristic requires repair. A correct first
    response must not cause a formatting/second coordinator call. Printed sizes
    and local timings are diagnostic measurements, not model quality or API SLA.
    """
    caplog.set_level('INFO',logger='operator_diagnostics')
    source,plan=five_pipes()
    good=wire(plan)
    bad=deepcopy(good)
    facts=bad['plan']['extraction']['items'][0]['characteristics']
    facts.append(deepcopy(facts[0]))
    responses=[bad,good] if repair else [good]
    sizes=[]
    def size(value):
        return len(json.dumps(value,ensure_ascii=False,separators=(',',':')).encode('utf-8'))
    def inspect_request(request):
        http_attempt(request)  # Same existing hook as from_environment, no new telemetry.
        body=json.loads(request.content)
        data=json.loads(body['input'])
        sizes.append(dict(body_bytes=len(request.content),
            instructions_json_bytes=size(body['instructions']),
            schema_json_bytes=size(body['text']['format']['schema']),
            input_json_bytes=size(body['input']),context_json_bytes=size(data['context']),
            previous_proposal_json_bytes=size(data['previous_proposal']),
            diagnostic_json_bytes=size(data['diagnostics']),
            timeout=request.extensions.get('timeout')))
    def respond(_):
        assert responses, 'unexpected extra API call'
        return responses.pop(0)
    admit(rig,100,text=source)
    with sdk_transport(respond,inspect_request=inspect_request) as (transport,sent):
        Executor(rig.queue,rig.access,lambda:rig.api,lambda:transport).execute(rig.queue.claim())
    assert not responses and len(sent)==len(sizes)==len(transport.metrics)==(2 if repair else 1)
    repo=Repository(rig.queue.database);state=assert_snapshot(repo,source,plan,1)
    assert state.revision==1 and state.items==state.originals and not state.questions
    with repo.database.connection() as c:
        reply=Reply.model_validate_json(c.execute('SELECT reply FROM steel_v2_turn').fetchone()[0])
    assert reply.status=='ok' and reply.calls==len(sent)
    assert_delivery(rig,100,reply)
    events=[json.loads(record.message) for record in caplog.records if record.name=='operator_diagnostics']
    complete=[event for event in events if event['event']=='operation_complete']
    assert len(complete)==1 and complete[0]['http_attempts']==len(sent) and complete[0]['sdk_retries']==0
    assert complete[0]['calls']==len(sent)
    validations=[event for event in events if event['event']=='steel_v2_validation']
    assert len(validations)==int(repair)
    if repair:
        assert validations[0]['reason']=='EngineError'
        assert validations[0]['diagnostic_codes']==['duplicate_characteristic']
    print(json.dumps(dict(scenario='repair' if repair else 'valid',wire=sizes,
        calls=[dict(stage=m['stage'],duration_ms=m['duration_ms']) for m in transport.metrics],
        operation={key:complete[0][key] for key in ['duration_ms','stage_ms','http_attempts','sdk_retries']}),
        ensure_ascii=True))


def test_typed_unit_question_restart_yes_applies_once_without_partial_corruption(rig):
    source,plan=five_pipes(missing=True,question=True)
    created,_=execute(rig,100,source,[wire(plan)])
    repo=Repository(rig.queue.database);old=assert_snapshot(repo,source,plan,1)
    assert created.status=='partial' and old.items[1].unit is None
    proposal=dict(target_ids=['i2'],field='unit',value='т',evidence='т',alternatives=['тонны'])
    asked,_=execute(rig,101,'Вторая позиция в т?',[
        wire(dict(intent='clarify',answer=QUESTION,clarification=proposal))])
    assert asked.intent=='clarify' and asked.calls==1 and repo.load(1)==old
    repo=reopen(rig);pending=repo.clarification(1,repo.load(1))
    assert pending.event_id=='telegram:101' and pending.request_id==old.id and pending.revision==1
    assert pending.proposal.target_ids==['i2'] and pending.proposal.value=='т'
    change=edit(old,[set_op('i2',field='unit',value='т',evidence='т')])
    change.update(clarification_event_id=pending.event_id,remaining_questions=[])
    reply,sent=execute(rig,102,'да',[wire(change)])
    assert reply.status=='ok' and reply.calls==1
    # Serialized SDK input proves persistence reached the model boundary, not
    # that a real model would choose the supplied confirmation plan.
    context=json.loads(sent[0]['input'])['context']
    assert context['pending_clarification']['event_id']==pending.event_id
    assert context['pending_clarification']['proposal']['target_ids']==['i2']
    state=assert_snapshot(repo,source,plan,2)
    assert state.id==old.id and state.revision==2 and not state.questions
    assert state.items[1].unit=='т' and all(a==b for a,b in zip(old.items,state.items) if a.id!='i2')
    assert state.items[1].model_dump(exclude={'unit'})==old.items[1].model_dump(exclude={'unit'})
    assert state.originals==old.originals and state.records==old.records and state.groups==old.groups
    assert len(state.changes)==1 and state.changes[0].evidence=='т' and state.changes[0].message=='да'
    assert repo.clarification(1,state) is None and repo.version(1,state.id,1)==old
    assert [m['content'] for m in repo.history(1,state.id) if m['role']=='user']==[source,'Вторая позиция в т?','да']
    rig.queue.compact()
    repo=reopen(rig);assert repo.load(1)==state
    assert_delivery(rig,102,reply)
    assert_replay_is_free(rig,102,'да')


def test_unbound_partial_question_then_invalid_yes_plans_fail_at_engine_and_wire(rig,caplog):
    caplog.set_level('INFO',logger='operator_diagnostics')
    source,plan=five_pipes(repeated=True,question=True)
    created,_=execute(rig,100,source,[wire(plan)])
    repo=reopen(rig);old=assert_snapshot(repo,source,plan,1)
    assert created.status=='partial' and old.questions==[QUESTION]
    assert all(item.unit=='т' for item in old.items) and repo.clarification(1,old) is None
    invalid=wire(edit(old,[set_op('i2',field='unit',value='т',evidence='т')]))
    empty=deepcopy(invalid);empty['plan']['operations']=[]
    reply,sent=execute(rig,101,'да',[invalid,empty])
    assert reply.status=='failed' and reply.calls==2
    assert repo.load(1)==old and repo.clarification(1,old) is None
    assert_snapshot(repo,source,plan,1)
    first,repair=[json.loads(payload['input']) for payload in sent]
    assert first['context']['pending_clarification'] is None
    assert any(QUESTION in row['content'] for row in first['context']['history'])
    assert any('edit_evidence_not_in_message' in code for code in repair['diagnostics'])
    assert any('value_error' in code for code in reply.diagnostics)
    events=[json.loads(record.message[record.message.index('{'):]) for record in caplog.records
            if '"event": "steel_v2_validation"' in record.message]
    assert [(event['reason'],event['validation_stage']) for event in events]==[
        ('EngineError','request_validation'),('ValidationError','structured_response')]
    assert [m['content'] for m in repo.history(1,old.id) if m['role']=='user']==[source,'да']
    assert_replay_is_free(rig,101,'да')


def test_create_with_explicit_unit_proposal_is_rejected_and_fallback_stays_unbound_after_restart(rig,caplog):
    """Diagnose today's contract/fallback, not a capability implemented by 4.2.

    Both mock replies intentionally exceed WireCreatePlan/AgentPlan. The offered
    unit is a question, not source evidence: the target's unit is still unknown.
    No typed binding is expected or invented from the other rows' unit tokens.
    """
    caplog.set_level('INFO',logger='operator_diagnostics')
    source,plan=five_pipes(missing=True,question=True)
    proposed=wire(plan)
    proposed['plan']['clarification']=dict(target_ids=['i2'],field='unit',value='т',
                                          evidence='т',alternatives=['тонны'])
    # An unsupported model reply must remain rejected by the actual contracts.
    with pytest.raises(ValidationError) as wire_error:
        WireCoordinatorPlan.model_validate(proposed)
    assert any(error['type']=='extra_forbidden' and 'clarification' in error['loc']
               for error in wire_error.value.errors())
    with pytest.raises(ValidationError):
        AgentPlan.model_validate(proposed['plan'])
    reply,sent=execute(rig,100,source,[proposed,proposed])
    assert reply.status=='partial' and reply.calls==2 and 'partial_recovered' in reply.diagnostics
    # The same two rejected responses reach the real SDK; no valid repair was substituted.
    for payload in sent:
        create_schema=payload['text']['format']['schema']['$defs']['WireCreatePlan']
        assert create_schema['additionalProperties'] is False
        assert 'clarification' not in create_schema['properties']
    events=[json.loads(record.message[record.message.index('{'):]) for record in caplog.records
            if '"event": "steel_v2_validation"' in record.message]
    assert [(event['reason'],event['validation_stage']) for event in events]==[
        ('ValidationError','structured_response'),('ValidationError','structured_response')]
    repo=reopen(rig);state=assert_snapshot(repo,source,plan,1)
    assert state.items==state.originals==AgentPlan.model_validate(plan).extraction.items
    # Recovery rebuilds its own safe questions, not the invalid proposal's question.
    assert state.questions and all(question in reply.text for question in state.questions)
    assert QUESTION not in state.questions
    assert state.items[1].unit is None and state.items[1].unit_text is None
    assert all(item.unit=='т' for item in state.items if item.id!='i2')
    assert state.revision==1 and not state.changes
    assert repo.clarification(1,state) is None and reply.clarification is None
    assert repo.version(1,state.id,1)==state
    assert [m['content'] for m in repo.history(1,state.id) if m['role']=='user']==[source]
    with repo.database.connection() as c:
        row=c.execute('SELECT reply FROM steel_v2_turn WHERE event_id=?',('telegram:100',)).fetchone()
        assert Reply.model_validate_json(row[0])==reply
        assert c.execute('SELECT count(*) FROM steel_v2_turn').fetchone()[0]==1
    # Reopening and redelivery cannot silently authorize the proposed unit.
    assert reopen(rig).load(1)==state
    assert_delivery(rig,100,reply)
    assert_replay_is_free(rig,100,source)
