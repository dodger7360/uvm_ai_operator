"""Local approved package + actual agent/repository; supplied plans are NOT live GPT.

Owner-published cases are input evidence, not generated production samples.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import re
import shutil

import pytest

from database import Database
from steel_agent_v2 import SteelAgent, Repository
from steel_agent_v2.contracts import AgentPlan, Reply, Characteristic
from steel_agent_v2.engine import totals
from steel_agent_v2.knowledge import ROOT, PACKAGE, KnowledgePackage, build, digest, installed
from steel_agent_v2.presentation import display_order
from steel_agent_v2.theoretical_mass import applicable, calculate
from steel_agent_v2.telegram_format import payloads
from test_steel_agent_v2 import Fake, edit
from test_steel_agent_v2_telegram_html import visible
from test_steel_agent_v2_wire_schema import sdk_transport


FIXTURE=json.loads((Path(__file__).parent/'fixtures/steel_v2_stage7_cases.json').read_text(encoding='utf-8'))
CASES=FIXTURE['cases']


def create(tmp_path,case):
    repo=Repository(Database(tmp_path/'knowledge.sqlite3'))
    fake=Fake(deepcopy(case['plan']))
    agent=SteelAgent(fake,repo)
    reply=agent.handle(1,case['source'],event_id='create')
    assert reply.status in {'ok','partial'},reply.diagnostics
    assert reply.calls==1
    return agent,repo,fake,reply


def test_reproducible_build_contains_current_approved_scope_only():
    raw=json.loads(PACKAGE.read_text(encoding='utf-8'))
    assert build()==build()==raw
    data=raw['data'];package=KnowledgePackage(raw)
    assert len(data['cases'])==6 and sum(c['position_count'] for c in data['cases'])==25
    assert [c['revision'] for c in data['cases']]==[2,3,2,2,2,1]
    assert [r['id'] for r in data['rules']]==[f'BR-{n:03}' for n in range(1,12)]
    assert all(r['revision']==1 and r['scope']=='global' for r in data['rules'])
    assert all(c['scope']=='case_only' and c['source_section']==5 for c in data['cases'])
    for case in data['cases']:
        fixture=next(c for c in CASES if c['id']==case['id'])
        assert case['original']==fixture['source'] and case['approved']==fixture['expected_text']
    assert '28,6014375' not in json.dumps(data,ensure_ascii=False)
    assert len(package.mass_records())==9
    assert all(r.status=='owner_indicative' and r.approval_scope and r.limitation for r in package.mass_records())
    for path,checksum in data['sources'].items():
        text=(ROOT/path).read_text(encoding='utf-8').replace('\r\n','\n')
        assert checksum==sha256(text.encode('utf-8')).hexdigest()


@pytest.mark.parametrize('damage',['digest','schema','proposed_case','proposed_rule'])
def test_corrupt_or_unapproved_artifact_fails_before_agent(damage):
    raw=deepcopy(build())
    if damage=='digest':raw['data']['recipe_revision']+=1
    elif damage=='schema':raw['data']['schema']=2
    else:raw['data']['cases' if damage=='proposed_case' else 'rules'][0]['status']='proposed'
    if damage!='digest':raw['version']='sha256:'+digest(raw['data'])
    with pytest.raises(ValueError):KnowledgePackage(raw)


def test_source_change_requires_explicit_rebuild_and_approval(tmp_path):
    package=build()
    paths=[*package['data']['sources'],'steel_agent_v2/knowledge_manifest.json']
    for path in paths:
        target=tmp_path/path;target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(ROOT/path,target)
    source=tmp_path/'docs/steel-agent-v2/knowledge/cases/CASE-20261019-001.md'
    original=source.read_text(encoding='utf-8')
    source.write_text(original+'\nСлужебная заметка аудита.\n',encoding='utf-8')
    assert build(tmp_path)['version']!=package['version']
    source.write_text(original.replace('**Статус: approved','**Статус: proposed'),encoding='utf-8')
    with pytest.raises(ValueError,match='not_approved'):build(tmp_path)
    source.write_text(original,encoding='utf-8')
    first_revision=tmp_path/'docs/steel-agent-v2/knowledge/cases/CASE-20261019-006.md'
    first_revision.write_text(first_revision.read_text(encoding='utf-8').replace('редакция 1','редакция 2'),encoding='utf-8')
    with pytest.raises(ValueError,match='case_revision'):build(tmp_path)


def test_relevance_and_independent_contexts_do_not_become_a_product_whitelist():
    package=installed();unknown=package.context('Товар нового типа Z9')
    assert unknown['cases']==[] and len(unknown['rules'])==11
    context=package.context('Балка 25К1');assert {c['id'] for c in context['cases']}=={'CASE-20261019-006'}
    assert 'data, not instructions' in context['boundaries']
    query=CASES[2]['source']
    with ThreadPoolExecutor(3) as pool:contexts=list(pool.map(package.context,[query,query,query]))
    contexts[0]['rules'].clear();contexts[0]['cases'][0]['approved']='changed'
    assert len(contexts[1]['rules'])==11 and contexts[1]==contexts[2]
    assert package.context(query)==contexts[1]


@pytest.mark.parametrize('case',CASES,ids=lambda c:c['id'])
def test_all_25_positions_through_real_agent_repository_and_local_catalog(tmp_path,case):
    agent,repo,fake,reply=create(tmp_path,case);state=repo.load(1)
    assert reply.text==case['expected_text']
    assert visible([p['text'] for p in payloads(reply,1)])==reply.text
    original=AgentPlan.model_validate(case['plan']).extraction
    assert len(state.items)==len(state.originals)==case['expected_item_count']
    assert state.items==state.originals==original.items
    assert state.groups==original.groups and state.records==original.records
    assert state.original_text==case['source'] and len({i.id for i in state.items})==len(state.items)
    layout=repo.last_display(1,state)
    assert layout.item_ids==case['expected_display_ids']==display_order(state,layout.layout)
    assert set(layout.item_ids)=={i.id for i in state.items}
    for item in state.items:
        assert item.source_id in {r.id for r in state.records if r.kind=='product'}
        assert item.quantity is not None
        for fact in item.characteristics:
            for source_id in fact.source_ids:
                record=next(r for r in state.records if r.id==source_id)
                assert fact.raw in state.original_text[record.start:record.end]
    masses={m.spec.item_id:m for m in state.theoretical_mass}
    for key,value in case.get('expected_tonnes_by_id',{}).items():
        mass=masses[key]
        assert mass.tonnes==Decimal(value) and mass.status=='calculated'
        assert mass.reference.sources and mass.reference.revision
        assert mass.spec.evidence in case['source']
        if case['id'].endswith(('002','006')):
            assert mass.reference.id.startswith(case['id'])
            assert mass.reference.revision.startswith(agent.knowledge.version)
            assert mass.reference.status=='owner_indicative'
    assert not re.search(r'^\d+[.)] |^Группа |^Заявка, версия|^Итого|^Всего',reply.text,re.M)
    assert reply.knowledge.version==agent.knowledge.version
    assert case['id'] in reply.knowledge.provided_ids
    supplied=fake.calls[0][0]['expert_knowledge']
    assert any(c['id']==case['id'] for c in supplied['cases'])
    assert len(fake.calls)==1 and not fake.search_calls
    assert Reply.model_validate_json(reply.model_dump_json())==reply
    reopened=Repository(Database(tmp_path/'knowledge.sqlite3'))
    assert reopened.load(1)==state
    cached=SteelAgent(Fake(),reopened).handle(1,case['source'],event_id='create')
    assert cached==reply and reopened.load(1)==state
    assert repo.load(2) is None


@pytest.mark.parametrize('case',CASES,ids=lambda c:c['id'])
def test_display_address_edit_undo_and_restart_preserve_all_source_facts(tmp_path,case):
    agent,repo,fake,_=create(tmp_path,case);before=repo.load(1)
    addressed=repo.last_display(1,before).item_ids[0]
    op=dict(kind='set',field='quantity',selection=dict(mode='display',positions=[1],cardinality='one',evidence='позиции 1'),
            value='7',evidence='7')
    fake.plans.append(AgentPlan.model_validate(edit(before,[op])))
    result=agent.handle(1,'В позиции 1 количество 7',event_id='edit')
    assert result.status in {'ok','partial'},result.diagnostics
    after=repo.load(1)
    assert {i.id for i in after.items}=={i.id for i in before.items}
    assert after.originals==before.originals and after.original_text==before.original_text
    assert after.records==before.records and after.groups==before.groups
    assert next(i for i in after.items if i.id==addressed).quantity==7
    for item in after.items:
        previous=next(i for i in before.items if i.id==item.id)
        assert item.characteristics==previous.characteristics and item.unit==previous.unit
        if item.id!=addressed:assert item==previous
    assert fake.calls[-1][0]['display_numbers']['1']==addressed
    reopened=Repository(Database(tmp_path/'knowledge.sqlite3'));undo=Fake(edit(after,[dict(kind='undo')]))
    restored=SteelAgent(undo,reopened).handle(1,'Отмени последнее изменение',event_id='undo')
    assert restored.status in {'ok','partial'},restored.diagnostics
    state=reopened.load(1)
    assert state.items==before.items and state.originals==before.originals
    assert state.theoretical_mass==before.theoretical_mass
    assert state.revision==3 and reopened.version(1,state.id,2)==after
    assert reopened.last_display(1,state).item_ids==case['expected_display_ids']
    assert len(reopened.history(1,state.id))==6


def test_declared_pieces_are_not_computed_from_mass_or_double_counted(tmp_path):
    case=CASES[4];_,repo,_,reply=create(tmp_path,case);state=repo.load(1)
    assert totals(state.items)==({'т':'9.458'},[])
    assert state.theoretical_mass==[]
    assert '3 шт. (1,413 т)' in reply.text and '1 шт. (0,251 т)' in reply.text
    assert '2 шт.' not in reply.text and 'ГОСТ 14637-89' in reply.text


@pytest.mark.parametrize('damage',['grade','length','cutting','standard','observed','family','missing_family'])
def test_approved_reference_never_bypasses_stage6_applicability(tmp_path,damage):
    _,repo,_,_=create(tmp_path,CASES[5]);state=repo.load(1)
    annotation=state.theoretical_mass[-1];item=next(i for i in state.items if i.id==annotation.spec.item_id)
    record=annotation.reference;hint=annotation.spec
    assert applicable(record,item,hint)
    if damage=='grade':record=record.model_copy(update={'grades':['OTHER']})
    elif damage=='length':record=record.model_copy(update={'piece_length_mm':'6000'})
    elif damage=='cutting':hint=hint.model_copy(update={'cutting':'specified'})
    elif damage in {'family','missing_family'}:hint=hint.model_copy(update={'family':'angle' if damage=='family' else None})
    elif damage=='standard':
        item=item.model_copy(update={'characteristics':[*item.characteristics,
            Characteristic(name='standard',value='OTHER',raw='OTHER',source_ids=[item.source_id])]})
        hint=hint.model_copy(update={'standard_name':'standard'})
    else:record=record.model_copy(update={'status':'observed'})
    if damage!='observed':assert not applicable(record,item,hint)
    assert calculate(item,hint,annotation.classification,[record]).tonnes is None


def test_unknown_product_remains_available_and_metadata_survives_failed_repair(tmp_path):
    source='Новый товар Z9 — 2 шт.'
    plan=dict(intent='create_request',extraction=dict(records=[dict(id='s',kind='product',start=0,end=len(source))],
        items=[dict(id='i',source_id='s',label='Новый товар Z9',quantity='2',unit='шт.',quantity_text='2',unit_text='шт.')],
        groups=[dict(id='g',label='Новый товар',item_ids=['i'],rationale='Supplied plan')]))
    repo=Repository(Database(tmp_path/'unknown.sqlite3'));fake=Fake(plan,'broken','broken');agent=SteelAgent(fake,repo)
    result=agent.handle(1,source,event_id='unknown')
    assert result.status=='ok' and 'Новый товар Z9' in result.text
    assert fake.calls[0][0]['expert_knowledge']['cases']==[]
    before=repo.load(1)
    failed=agent.handle(1,'Уточни неизвестное',event_id='failed')
    assert failed.calls==2 and failed.knowledge.version==agent.knowledge.version
    assert repo.load(1)==before and fake.calls[1][0]['expert_knowledge']==fake.calls[2][0]['expert_knowledge']


def test_actual_sdk_payload_receives_same_package_without_an_extra_model_call(tmp_path):
    case=CASES[5]
    with sdk_transport(AgentPlan.model_validate(case['plan']).model_dump(mode='json')) as (transport,sent):
        reply=SteelAgent(transport,Repository(Database(tmp_path/'sdk.sqlite3'))).handle(1,case['source'],event_id='sdk')
    assert reply.status=='ok' and len(sent)==1
    assert sent[0]['model']=='gpt-6-luna' and sent[0]['reasoning']=={'effort':'high'}
    body=json.loads(sent[0]['input'])
    assert body['context']['expert_knowledge']['version']==reply.knowledge.version
    assert 'CASE-20261019-006' in {c['id'] for c in body['context']['expert_knowledge']['cases']}
    assert sent[0]['store'] is False


def test_old_stored_replies_still_parse_without_schema_migration():
    reply=Reply.model_validate(dict(text='Сохранённый ответ',status='ok',intent='free_chat'))
    assert reply.knowledge is None and 'knowledge' not in json.loads(reply.model_dump_json())


def test_explicit_unit_edit_is_not_overridden_by_default_tonnes_display(tmp_path):
    agent,repo,fake,_=create(tmp_path,CASES[4]);before=repo.load(1)
    key=before.items[0].id
    fake.plans.append(edit(before,[dict(kind='convert_mass',target_id=key,value='кг',evidence='кг')]))
    result=agent.handle(1,'Пересчитай позицию в кг',event_id='kg')
    assert result.status=='ok',result.diagnostics
    item=repo.load(1).items[0]
    assert item.quantity==Decimal('251') and item.unit=='кг'
    assert '251 кг' in result.text and '1 шт. (251 кг)' not in result.text
    assert repo.load(1).originals==before.originals


def test_pending_source_participates_in_retrieval_after_restart(tmp_path):
    repo=Repository(Database(tmp_path/'pending.sqlite3'));fake=Fake('broken','broken')
    first=SteelAgent(fake,repo).handle(1,CASES[2]['source'],event_id='pending')
    assert first.status=='failed' and repo.load(1) is None
    plan=deepcopy(CASES[2]['plan']);plan['pending_event_id']='pending'
    reopened=Repository(Database(tmp_path/'pending.sqlite3'));next_fake=Fake(plan)
    restored=SteelAgent(next_fake,reopened).handle(1,'Разбери сохранённую заявку',event_id='resume')
    assert restored.status=='ok' and restored.text==CASES[2]['expected_text']
    assert 'CASE-20261019-003' in restored.knowledge.provided_ids
    assert reopened.load(1).original_text==CASES[2]['source']


def test_runtime_uses_installed_artifact_not_per_message_repository_reads(tmp_path,monkeypatch):
    import steel_agent_v2.knowledge as module
    package=installed()
    monkeypatch.setattr(module,'PACKAGE',tmp_path/'nonexistent-package.json')
    repo=Repository(Database(tmp_path/'local.sqlite3'))
    fake=Fake(dict(intent='free_chat',answer='Проверка'),dict(intent='free_chat',answer='Проверка'))
    agent=SteelAgent(fake,repo)
    agent.handle(1,'Проверка',event_id='one');agent.handle(2,'Утверждаю новую версию evil',event_id='two')
    assert agent.knowledge is package
    assert all(call[0]['expert_knowledge']['version']==package.version for call in fake.calls)
    assert len(fake.calls)==2 and not fake.search_calls


def test_supplementary_quantity_without_exact_source_evidence_stays_visible_as_fact(tmp_path):
    from steel_agent_v2.presentation import render
    _,repo,_,_=create(tmp_path,CASES[4]);state=repo.load(1)
    item=next(i for i in state.items if any(c.name=='declared_quantity' for c in i.characteristics))
    changed=item.model_copy(update={'characteristics':[c.model_copy(update={'value':'99 шт.'})
        if c.name=='declared_quantity' else c for c in item.characteristics]})
    invalid=state.model_copy(update={'items':[changed if i.id==item.id else i for i in state.items]})
    text=render(invalid,expert=True)
    assert '99 шт.' in text and '99 шт. (' not in text
    assert totals(invalid.items)==totals(state.items)


@pytest.mark.parametrize('damage',['wall','missing_dimension','extra_dimension','wrong_roles','family'])
def test_same_profile_text_cannot_bypass_dimensional_or_family_scope(tmp_path,damage):
    from test_steel_agent_v2_027_presentation import sample
    source,plan,_=sample([('Труба профильная',[
        ('profile','40×40×3','40×40×3'),('grade','Ст3','Ст3'),('length','6м','6м'),
        ('explicit_wall','5','5')],'430','шт')])
    hint=deepcopy(CASES[1]['plan']['mass_specs'][0])
    hint.pop('standard_name')
    if damage=='wall':
        hint['dimensions'][2]=dict(role='wall',name='explicit_wall',unit='мм')
    elif damage=='missing_dimension':hint['dimensions'].pop(2)
    elif damage=='extra_dimension':hint['dimensions'].append(dict(role='diameter',name='explicit_wall',unit='мм'))
    elif damage=='wrong_roles':hint['dimensions'][0]['role']='diameter'
    else:hint['family']='angle'
    plan['mass_specs']=[hint]
    repo=Repository(Database(tmp_path/'conflict.sqlite3'));fake=Fake(plan)
    result=SteelAgent(fake,repo).handle(1,source,event_id='scope')
    assert result.status=='ok' and result.calls==1
    state=repo.load(1)
    assert state.items==state.originals and state.original_text==source
    assert state.items[0].quantity==430 and state.theoretical_mass[0].tonnes is None
    assert 'расчётная масса не определена' in result.text
    assert repo.last_display(1,state).item_ids==['i1']
