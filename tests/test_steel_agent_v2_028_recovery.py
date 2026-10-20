"""P0 recovery through real agent/repository/SDK, synthetic data and fake HTTP only."""
from copy import deepcopy
from decimal import Decimal
import json
import logging
from pathlib import Path

import httpx2
import pytest
from openai import OpenAI, DefaultHttpxClient
from pydantic import ValidationError

from database import Database
from steel_agent_v2 import SteelAgent, Repository, RequestEngine
from steel_agent_v2.contracts import AgentPlan
from steel_agent_v2.engine import totals, EngineError
from steel_agent_v2.presentation import render
from steel_agent_v2.transport import OpenAITransport
from test_steel_agent_v2 import Fake, edit, set_op
from test_steel_agent_v2_027_presentation import sample
from test_steel_agent_v2_wire_schema import subset_issues
from test_steel_agent_v2_queue import rig, admit, Executor


ROWS=[('Труба',[('Размер','159×6 мм','159×6 мм')], '240','м'),
      ('Уголок',[('Размер','50×50×5 мм','50×50×5 мм'),('Марка',None,'марка неизвестна')], '2.125','т'),
      ('Лист',[('Толщина','8 мм','8 мм'),('Марка','С245','С245')], '17.625','кг')]


def run(tmp_path, *plans, source=None):
    text, plan, _=sample(ROWS)
    repo=Repository(Database(tmp_path/'recovery.sqlite3'))
    fake=Fake(*(plans or [plan]))
    result=SteelAgent(fake,repo).handle(1,source or text,event_id='create')
    return result,repo,fake


def damaged(kind):
    source,plan,_=sample(ROWS)
    ex=plan['extraction']
    if kind=='duplicate_characteristic':ex['items'][1]['characteristics']*=2
    elif kind=='quantity_mismatch':ex['items'][1]['quantity']='100'
    elif kind=='wrong_source':ex['items'][1]['source_id']='missing'
    elif kind=='missing_item':ex['items'].pop(1)
    elif kind=='duplicate_item':ex['items'].append(deepcopy(ex['items'][1]))
    elif kind=='range':ex['records'][1]['end']=len(source)+1
    elif kind=='whitespace':
        ex['records'][1]['start']=source.index('\n');ex['records'][1]['end']=source.index('\n')+1
    elif kind=='overlap':ex['records'][1]['start']=ex['records'][0]['end']-1
    elif kind=='uncovered':ex['records'][1]['start']+=1
    elif kind=='group':ex['groups'][0]['item_ids']+=['i1']
    elif kind=='bad_contract':ex['items'][1]['quantity']=True
    return source,plan


def test_partial_input_retains_all_known_facts_and_pipe_metres(tmp_path):
    reply,repo,fake=run(tmp_path)
    state=repo.load(1)
    assert reply.status=='partial' and len(fake.calls)==1 and len(state.items)==3
    assert state.items[0].quantity==Decimal(240) and state.items[0].unit=='м'
    assert 'Труба 159×6 мм — 240 м' in reply.text
    assert state.items[1].characteristics[1].value is None
    assert 'Марка: не уточнено' in reply.text
    assert 'Не требуется уточнений' not in reply.text
    for item in state.items:
        for c in item.characteristics:
            if c.value is not None:assert c.value in reply.text
    assert totals(state.items)==({'м':'240','т':'2.142625'},[])


@pytest.mark.parametrize('kind',['duplicate_characteristic','quantity_mismatch','wrong_source','missing_item',
    'duplicate_item','range','whitespace','overlap','uncovered','group','bad_contract'])
def test_two_invalid_plans_recover_only_independently_validated_items(tmp_path,kind):
    source,bad=damaged(kind)
    reply,repo,fake=run(tmp_path,deepcopy(bad),bad,source=source)
    state=repo.load(1)
    assert reply.status=='partial' and reply.calls==2 and len(fake.calls)==2
    assert state is not None and state.original_text==source
    RequestEngine().validate(state)
    assert len({i.id for i in state.items})==len(state.items)
    assert len(state.items)==len(state.originals)
    assert 'i3' in {i.id for i in state.items}  # Unrelated third item always survives.
    if kind not in {'group','uncovered'}:
        assert 'i2' not in {i.id for i in state.items}
        assert any(r.kind=='unresolved' for r in state.records)
    assert fake.calls[1][1]['diagnostics']
    assert all(word not in reply.text for word in ['duplicate_characteristic','Pydantic','UUID','schema:'])


def test_duplicate_on_first_plan_then_correct_repair_preserves_full_request(tmp_path):
    source,bad=damaged('duplicate_characteristic');_,good,_=sample(ROWS)
    reply,repo,fake=run(tmp_path,bad,good,source=source)
    assert reply.status=='partial' and len(repo.load(1).items)==3
    assert fake.calls[1][1]['diagnostics']==['duplicate_characteristic:i2']
    assert fake.calls[1][1]['previous']['extraction']['items'][1]['characteristics']


def test_all_invalid_pending_survives_reopen_history_truncation_and_explicit_resume(tmp_path):
    source='Неясный синтетический исходник ' * 500
    bad={'intent':'create_request','extraction':{'records':[{'id':'u','kind':'unresolved','start':0,'end':len(source)}],
                                               'items':[],'groups':[]}}
    reply,repo,fake=run(tmp_path,bad,bad,source=source)
    assert reply.status=='failed' and repo.load(1) is None
    assert source in reply.text and 'pending_source' in reply.diagnostics
    for n in range(12):
        SteelAgent(Fake({'intent':'free_chat','answer':'Привет'}),repo).handle(1,'Разговор',event_id=f'chat-{n}')
    reopened=Repository(Database(repo.database.path))
    pending=reopened.pending(1)
    assert pending['source_text']==source and pending['event_id']=='create'
    # No invented normalized request: user can show the saved pending source in context.
    def explain(context,kw):
        assert context['pending_request']['source_text']==source
        return {'intent':'clarify','answer':'Уточните обозначение товара в сохранённом исходнике.'}
    SteelAgent(Fake(explain),reopened).handle(1,'Продолжи разбор',event_id='resume-question')
    assert reopened.load(1) is None


def test_saved_original_resumes_without_resending_it_and_consumes_pending(tmp_path):
    source,good,_=sample(ROWS)
    reply,repo,_=run(tmp_path,'{broken','{broken',source=source)
    assert reply.status=='failed' and repo.load(1) is None
    reopened=Repository(Database(repo.database.path))
    good['pending_event_id']='create'
    fake=Fake(good)
    result=SteelAgent(fake,reopened).handle(1,'Продолжи разбор сохранённого исходника',event_id='resume')
    assert result.status=='partial' and reopened.load(1).original_text==source
    assert reopened.pending(1) is None and fake.calls[0][0]['pending_request']['event_id']=='create'
    cached=SteelAgent(Fake(),reopened).handle(1,'Продолжи разбор сохранённого исходника',event_id='resume')
    assert cached==result and reopened.load(1).revision==1


def test_failed_new_request_preserves_active_and_persists_verified_pending(tmp_path):
    _,repo,_=run(tmp_path)
    before=repo.load(1).model_dump_json()
    source,bad=damaged('duplicate_characteristic')
    result=SteelAgent(Fake(bad,bad),repo).handle(1,source,event_id='new-failed')
    assert result.status=='failed' and repo.load(1).model_dump_json()==before
    pending=Repository(Database(repo.database.path)).pending(1)
    assert pending['source_text']==source and len(pending['verified_partial']['items'])==2
    assert 'Проверенная часть новой заявки (ещё не активна)' in result.text
    assert result.pending_snapshot.original_text==source


def test_numbered_and_group_edits_are_atomic_and_keep_source_history_and_undo(tmp_path):
    _,repo,_=run(tmp_path);original=repo.load(1);before=original.model_dump_json()
    fake=Fake(edit(original,[set_op('i3','quantity','3',evidence='3'),
                             set_op('i2','characteristic','09Г2С',name='Марка',evidence='09Г2С')]))
    reply=SteelAgent(fake,repo).handle(1,'В третьей строке 3 кг; у всех уголков марка 09Г2С',event_id='edits')
    current=repo.load(1)
    assert current.items[0]==original.items[0] and current.items[2].quantity==Decimal(3)
    assert current.items[1].characteristics[1].value=='09Г2С' and current.revision==2
    assert fake.calls[0][0]['display_numbers']=={'1':'i1','2':'i2','3':'i3'}
    assert reply.status=='ok' and current.original_text==original.original_text
    bad=edit(current,[set_op('i1','quantity','2',evidence='2'),set_op('missing','quantity','2',evidence='2')])
    failed=SteelAgent(Fake(bad,bad),repo).handle(1,'Поставь 2',event_id='bad-batch')
    assert failed.status=='failed' and repo.load(1).model_dump_json()==current.model_dump_json()
    undo=edit(current,[{'kind':'undo'}])
    SteelAgent(Fake(undo),repo).handle(1,'Отмени изменение',event_id='undo')
    final=repo.load(1)
    assert final.items==original.items and final.groups==original.groups and final.revision==3
    assert repo.version(1,original.id,1).model_dump_json()==before


def test_ambiguous_target_and_formatting_do_not_mutate_technical_state(tmp_path):
    _,repo,_=run(tmp_path);before=repo.load(1).model_dump_json()
    fake=Fake({'intent':'clarify','answer':'Какую из позиций нужно изменить?'},
              {'intent':'show_request','view':'custom','answer':'Лист: 8 мм, С245 — 17,625 кг'})
    assert SteelAgent(fake,repo).handle(1,'Поменяй марку',event_id='ambiguous').text.count('?')==1
    SteelAgent(fake,repo).handle(1,'Во всех листах порядок: толщина, марка, раскрой',event_id='format')
    assert repo.load(1).model_dump_json()==before


def response(plan,status='completed'):
    return {'id':'resp_test','object':'response','created_at':0,'model':'gpt-6-luna','status':status,
            'output':[{'id':'msg_test','type':'message','role':'assistant','status':'completed',
                       'content':[{'type':'output_text','text':json.dumps(plan) if not isinstance(plan,str) else plan,'annotations':[]}]}]}


def test_actual_sdk_wire_error_preserves_private_proposal_and_repairs_once(tmp_path,caplog):
    source,good,_=sample(ROWS);bad=deepcopy(good);bad['extraction']['items'][1]['quantity']=True
    plans=iter([bad,good]);sent=[]
    def respond(request):
        sent.append(json.loads(request.content));return httpx2.Response(200,json=response(next(plans)))
    repo=Repository(Database(tmp_path/'sdk.sqlite3'))
    with DefaultHttpxClient(transport=httpx2.MockTransport(respond)) as http:
        with caplog.at_level(logging.INFO,logger='operator_diagnostics'):
            result=SteelAgent(OpenAITransport(OpenAI(api_key='offline-test',http_client=http)),repo).handle(1,source,event_id='sdk')
    assert result.status=='partial' and len(sent)==2 and len(repo.load(1).items)==3
    repair=json.loads(sent[1]['input'])
    assert repair['diagnostics'] and repair['previous_proposal']
    details=repair['context']['validation_repair']
    assert details and details[0]['loc'][:2]==['extraction','items'] and details[0]['msg']
    assert subset_issues(sent[0]['text']['format']['schema'])==[]
    assert sent[0]['model']=='gpt-6-luna' and sent[0]['reasoning']=={'effort':'high'}
    assert source not in caplog.text and '159×6' not in caplog.text


@pytest.mark.parametrize('failure',['timeout','incomplete','refusal','http400'])
def test_transport_unknown_or_refusal_never_runs_repair_or_local_salvage(rig,failure):
    source,plan,_=sample(ROWS);calls=[]
    def respond(request):
        calls.append(request)
        if failure=='timeout':raise httpx2.ReadTimeout('synthetic unknown')
        if failure=='http400':return httpx2.Response(400,json={'error':{'message':'safe synthetic','type':'invalid_request_error'}})
        value=response(plan,'incomplete' if failure=='incomplete' else 'completed')
        if failure=='refusal':value['output'][0]['content']=[{'type':'refusal','refusal':'Synthetic refusal'}]
        return httpx2.Response(200,json=value)
    with DefaultHttpxClient(transport=httpx2.MockTransport(respond)) as http:
        api=OpenAITransport(OpenAI(api_key='offline-test',http_client=http,max_retries=0))
        admit(rig,201,text=source)
        Executor(rig.queue,rig.access,lambda:rig.api,lambda:api).execute(rig.queue.claim())
    assert len(calls)==1 and Repository(rig.queue.database).load(1) is None
    if failure=='timeout':
        job=rig.queue.job(201)
        assert job['state']=='delivered'
        assert job['committed']==1 and job['uncertain']==1
        with rig.queue.database.connection() as c:
            saved=c.execute(
                "SELECT reply FROM steel_v2_turn WHERE user_id=1 "
                "AND event_id='telegram:201'"
            ).fetchone()
            assert json.loads(saved['reply'])['status']=='failed'
            assert c.execute(
                "SELECT count(*) FROM steel_v2_outbox "
                "WHERE update_id=201 AND state='delivered' "
                "AND message_id IS NOT NULL"
            ).fetchone()[0]>0
            assert [tuple(row) for row in c.execute(
                "SELECT kind,state FROM steel_v2_attempt "
                "WHERE update_id=201 ORDER BY number"
            )]==[('plan','execution_unknown')]
        admit(rig,202,text='????????? ??????')
        assert rig.queue.claim()['update_id']==202
        assert len(calls)==1
    else:
        assert rig.queue.job(201)['state']=='delivered'


def test_resolve_existing_fragment_then_apply_explicit_correction_and_undo(tmp_path):
    source,bad=damaged('duplicate_characteristic')
    _,repo,_=run(tmp_path,bad,bad,source=source);partial=repo.load(1)
    unresolved=next(r for r in partial.records if r.kind=='unresolved')
    _,good,_=sample(ROWS);ex=good['extraction'];record=ex['records'][1];item=ex['items'][1]
    resolution={'records':[record],'items':[item], 'groups':[{'id':'resolved-group','label':'Уголок',
                 'item_ids':['i2'],'rationale':'Разбор сохранённого фрагмента'}]}
    plan=edit(partial,[{'kind':'resolve','target_id':unresolved.id,'resolution':resolution},
                       set_op('i2','characteristic','09Г2С',name='Марка',evidence='09Г2С')])
    plan['remaining_questions']=[]
    result=SteelAgent(Fake(plan),repo).handle(1,'Разбери фрагмент 1, марка 09Г2С',event_id='resolve')
    assert result.status=='ok' and repo.load(1).revision==2
    complete=repo.load(1);RequestEngine().validate(complete)
    assert len(complete.items)==3 and not any(r.kind=='unresolved' for r in complete.records)
    assert complete.original_text==source and complete.items[-1].id=='i2'
    assert complete.items[-1].characteristics[1].value=='09Г2С'
    assert complete.items[:2]==partial.items  # Existing display numbers unchanged.
    assert totals(complete.items)==({'м':'240','т':'2.142625'},[])
    undo=edit(complete,[{'kind':'undo'}])
    SteelAgent(Fake(undo),repo).handle(1,'Отмени',event_id='undo-resolve')
    assert repo.load(1).items==partial.items and repo.load(1).records==partial.records
    assert repo.load(1).revision==3


@pytest.mark.parametrize('damage',['outside','duplicate_id','missing_quantity_evidence','missing_coverage'])
def test_unsafe_fragment_resolution_is_atomic(tmp_path,damage):
    source,bad=damaged('duplicate_characteristic');_,repo,_=run(tmp_path,bad,bad,source=source)
    state=repo.load(1);before=state.model_dump_json();fragment=next(r for r in state.records if r.kind=='unresolved')
    _,good,_=sample(ROWS);ex=good['extraction']
    resolution={'records':[ex['records'][1]],'items':[ex['items'][1]],'groups':[
        {'id':'new-group','label':'Уголок','item_ids':['i2'],'rationale':'Synthetic'}]}
    if damage=='outside':resolution['records'][0]['start']=0
    if damage=='duplicate_id':resolution['items'][0]['id']='i1';resolution['groups'][0]['item_ids']=['i1']
    if damage=='missing_quantity_evidence':resolution['items'][0]['quantity']='100'
    if damage=='missing_coverage':resolution['records'][0]['start']+=2
    plan=edit(state,[{'kind':'resolve','target_id':fragment.id,'resolution':resolution}])
    reply=SteelAgent(Fake(plan,plan),repo).handle(1,'Разбери фрагмент',event_id='invalid-resolve')
    assert reply.status=='failed' and repo.load(1).model_dump_json()==before


def test_mass_change_all_angles_and_only_fourth_pipe_wall_are_evidence_checked(tmp_path):
    rows=[('Труба 159×6 мм',[('Размер','159×6 мм','159×6 мм'),('Стенка','6 мм','6 мм')],'240','м'),
          ('Уголок',[('Марка','Ст3','Ст3')],'1','т'),
          ('Уголок',[('Марка','Ст3','Ст3')],'2','т'),
          ('Труба 159×6 мм',[('Размер','159×6 мм','159×6 мм'),('Стенка','6 мм','6 мм')],'10','м')]
    source,plan,_=sample(rows);repo=Repository(Database(tmp_path/'group.sqlite3'))
    SteelAgent(Fake(plan),repo).handle(1,source,event_id='create');before=repo.load(1)
    patch=edit(before,[set_op('i2','characteristic','09Г2С',name='Марка',evidence='09Г2С'),
                       set_op('i3','characteristic','09Г2С',name='Марка',evidence='09Г2С')])
    SteelAgent(Fake(patch),repo).handle(1,'У всех уголков явно замени марку на 09Г2С',event_id='mass')
    current=repo.load(1)
    assert current.items[0]==before.items[0] and current.items[3]==before.items[3]
    assert [i.characteristics[0].value for i in current.items[1:3]]==['09Г2С','09Г2С']
    patch=edit(current,[set_op('i4','characteristic','8 мм',name='Стенка',evidence='8 мм'),
                        dict(set_op('i4','characteristic','8',name='Размер',evidence='8 мм'),replace_from='6')])
    reply=SteelAgent(Fake(patch),repo).handle(1,'У трубы номер 4 стенка 8 мм, остальные не трогай',event_id='wall')
    after=repo.load(1)
    assert after.items[:3]==current.items[:3] and after.items[3].characteristics[1].value=='8 мм'
    assert after.items[3].characteristics[0].value=='159×8 мм'
    assert reply.text.splitlines()[3].startswith('Труба 159×8 мм')
    assert after.original_text==source


def test_current_label_follows_explicit_grade_change_without_mutating_original(tmp_path):
    source,plan,_=sample([('Лист Ст3',[('Марка','Ст3','Ст3')],'1','т')])
    repo=Repository(Database(tmp_path/'label.sqlite3'));SteelAgent(Fake(plan),repo).handle(1,source,event_id='create')
    old=repo.load(1);patch=edit(old,[set_op('i1','characteristic','09Г2С',name='Марка',evidence='09Г2С')])
    reply=SteelAgent(Fake(patch),repo).handle(1,'Марка 09Г2С',event_id='grade')
    assert reply.text=='Лист 09Г2С — 1 т' and repo.load(1).originals==old.originals
    assert old.original_text==source


def test_partial_queue_duplicate_is_idempotent_and_other_user_remains_isolated(rig):
    source,bad=damaged('duplicate_characteristic');fake=Fake(bad,bad)
    admit(rig,311,text=source)
    Executor(rig.queue,rig.access,lambda:rig.api,lambda:fake).execute(rig.queue.claim())
    repo=Repository(rig.queue.database);state=repo.load(1);assert len(state.items)==2
    assert admit(rig,311,text=source)==312
    assert rig.queue.claim() is None and len(fake.calls)==2
    assert repo.load(2) is None
    saved=state.model_dump_json()
    pending=SteelAgent(Fake('{bad','{bad'),repo).handle(2,'Вымышленный исходник',event_id='bad-other')
    assert repo.load(1).model_dump_json()==saved and repo.pending(1) is None and repo.pending(2)


def test_long_partial_fallback_preserves_every_input_character_and_durable_chunk(rig):
    rows=[('Лист',[('Толщина',f'{n} мм',f'{n} мм')],str(n),'кг') for n in range(1,201)]
    source,bad,_=sample(rows);bad['extraction']['items'][50]['characteristics']*=2
    admit(rig,312,text=source);fake=Fake(bad,bad)
    Executor(rig.queue,rig.access,lambda:rig.api,lambda:fake).execute(rig.queue.claim())
    state=Repository(rig.queue.database).load(1);RequestEngine().validate(state)
    assert len(state.items)==199 and state.original_text==source
    assert sum(i.quantity for i in state.items)==sum(Decimal(n) for n in range(1,201) if n!=51)
    assert any(r.kind=='unresolved' and '51 мм' in source[r.start:r.end] for r in state.records)
    parts=[p['text'] for method,p in rig.api.calls if method=='sendMessage']
    assert len(parts)>1 and ''.join(parts)==render(state,expert=True)
    assert all(len(p.encode('utf-16-le'))//2<=3200 for p in parts)


def test_reply_default_remains_byte_compatible_with_pre_patch_durable_receipt():
    from steel_agent_v2.contracts import Reply
    old={'text':'Ответ','status':'ok','intent':'free_chat','request_id':None,'revision':None,
         'sources':[],'diagnostics':[],'calls':1}
    assert Reply.model_validate(old).model_dump(mode='json')==old


@pytest.mark.parametrize('old,value', [('59','8'),('missing','8'),('6','99')])
def test_unsafe_or_unproven_literal_replacement_does_not_change_other_geometry(tmp_path,old,value):
    source,plan,_=sample([('Труба',[('Размер','159×6 мм','159×6 мм')],'240','м')])
    repo=Repository(Database(tmp_path/'replace.sqlite3'));SteelAgent(Fake(plan),repo).handle(1,source,event_id='create')
    state=repo.load(1);before=state.model_dump_json()
    patch=edit(state,[dict(set_op('i1','characteristic',value,name='Размер',evidence='8 мм'),replace_from=old)])
    reply=SteelAgent(Fake(patch,patch),repo).handle(1,'Стенка 8 мм',event_id='replace')
    assert reply.status=='failed' and repo.load(1).model_dump_json()==before


def test_quantity_and_explicit_unit_correction_keep_exact_originals(tmp_path):
    _,repo,_=run(tmp_path);state=repo.load(1)
    patch=edit(state,[set_op('i3','quantity','3',evidence='3'),
                       set_op('i3','unit','тонны',evidence='тонны')])
    reply=SteelAgent(Fake(patch),repo).handle(1,'В позиции 3 не прежнее количество, а 3 тонны',event_id='quantity-unit')
    result=repo.load(1)
    assert result.items[2].quantity==Decimal(3) and result.items[2].unit=='тонны'
    assert result.originals==state.originals and result.items[:2]==state.items[:2]
    assert '3 т' in reply.text and 'исходно: 17,625 кг' in reply.text


def test_unknown_ambiguous_and_conflicting_characteristics_do_not_remove_other_products(tmp_path):
    source,plan,_=sample([('Изделие непонятного вида',[('Тип',None,'тип неясен')],None,None),
                         ('Лист',[('Марка',None,'С245 или Ст3')],'1.125','т'),
                         ('Труба',[('Размер','159×6 мм','159×6 мм')],'240','м')],
                        questions=['Уточните вид и количество первой позиции; какая марка нужна для второй?'])
    reply,repo,_=run(tmp_path,plan,source=source)
    state=repo.load(1)
    assert reply.status=='partial' and len(state.items)==3 and state.original_text==source
    assert state.items[1].characteristics[0].value is None
    assert '240 м' in reply.text and 'С245 или Ст3' in reply.text
    assert state.originals[1].characteristics[0].raw=='С245 или Ст3'


@pytest.mark.parametrize('key',['wrong-event',''])
def test_bad_pending_reference_never_shadows_saved_original(tmp_path,key):
    source,good,_=sample(ROWS);_,repo,_=run(tmp_path,'{bad','{bad',source=source)
    before=repo.pending(1);good['pending_event_id']=key
    reply=SteelAgent(Fake(good,good),repo).handle(1,'Продолжи',event_id='bad-key')
    assert reply.status=='failed' and repo.pending(1)==before and repo.load(1) is None


def test_repair_cannot_switch_from_original_pending_source_to_short_current_message(tmp_path):
    source,bad=damaged('duplicate_characteristic');_,repo,_=run(tmp_path,'{bad','{bad',source=source)
    bad['pending_event_id']='create'
    short='Продолжи';_,wrong,_=sample([('Продолжи',[],None,None)])
    wrong['extraction']['records'][0]['end']=len(short)  # Even a contract-valid wrong source must be rejected.
    before=repo.pending(1)
    reply=SteelAgent(Fake(bad,wrong),repo).handle(1,short,event_id='wrong-source-repair')
    state=repo.load(1)
    assert reply.status=='partial' and state.original_text==source and len(state.items)==2
    assert 'repair_cannot_change_source' in reply.diagnostics and repo.pending(1) is None


def test_actual_sdk_uses_only_the_final_answer_phase(tmp_path):
    source,plan,_=sample(ROWS)
    value=response(plan);value['output'][0]['phase']='final_answer'
    value['output'].insert(0,{'id':'comment','type':'message','role':'assistant','status':'completed',
        'phase':'commentary','content':[{'type':'output_text','text':'Not a plan; never parse this commentary','annotations':[]}]})
    repo=Repository(Database(tmp_path/'phase.sqlite3'));seen=[]
    def respond(request):seen.append(request);return httpx2.Response(200,json=value)
    with DefaultHttpxClient(transport=httpx2.MockTransport(respond)) as http:
        reply=SteelAgent(OpenAITransport(OpenAI(api_key='offline-test',http_client=http)),repo).handle(1,source,event_id='phase')
    assert reply.status=='partial' and len(seen)==1 and len(repo.load(1).items)==3


def test_unreadable_continuation_does_not_replace_original_pending_with_short_message(tmp_path):
    source,_,_=sample(ROWS);_,repo,_=run(tmp_path,'{bad','{bad',source=source)
    before=repo.pending(1)
    reply=SteelAgent(Fake('{bad','{bad'),repo).handle(1,'Продолжи',event_id='malformed-continuation')
    assert reply.status=='failed' and repo.pending(1)==before and source in reply.text
    assert repo.load(1) is None
    with repo.database.connection() as c:
        assert c.execute("SELECT message FROM steel_v2_turn WHERE event_id='malformed-continuation'").fetchone()[0]=='Продолжи'


def test_totals_of_recovered_request_are_explicitly_only_known_part(tmp_path):
    source,bad=damaged('duplicate_characteristic');_,repo,_=run(tmp_path,bad,bad,source=source)
    before=repo.load(1).model_dump_json();fake=Fake({'intent':'answer_question','view':'totals'})
    reply=SteelAgent(fake,repo).handle(1,'Сколько сейчас?',event_id='totals')
    assert reply.status=='partial' and reply.text.startswith('Известная часть количества:')
    assert 'Неразобранные фрагменты в итог не включены' in reply.text
    assert '240 м' in reply.text and '0,017625 т' in reply.text and '2,142625' not in reply.text
    assert fake.calls[0][0]['totals_are_partial'] is True
    assert repo.load(1).model_dump_json()==before
