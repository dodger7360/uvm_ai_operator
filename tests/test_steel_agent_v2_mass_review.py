"""PR21 review: applicability boundaries and explicitly approved CASE fixtures.

No runtime knowledge loading and no live model: plans below are controlled mocks.
The CASE sources are owner-published Git documents, never production SQLite.
"""
from decimal import Decimal
import json
from pathlib import Path
import re

import pytest

from database import Database
from steel_agent_v2 import SteelAgent, Repository, RequestEngine
from steel_agent_v2.contracts import AgentPlan, MassReference, MassSpec, PendingMass, Reply
from steel_agent_v2.engine import EngineError
from steel_agent_v2.presentation import render
from steel_agent_v2.theoretical_mass import (
    MassCatalog, applicable, calculate, classification, refresh, confirm,
    research_candidates, validate_annotations,
)
from steel_agent_v2.transport import SearchAnswer
from test_steel_agent_v2 import Fake, edit, set_op
from test_steel_agent_v2_027_presentation import sample
from test_steel_agent_v2_theoretical_mass import (
    setup, mass, beam_plan, sheet_plan, pipe_plan, spec, calc_plan, dict_source, SHEET,
)
from test_steel_agent_v2_telegram_html import visible
from test_steel_agent_v2_queue import rig, admit, Executor
from steel_agent_v2.telegram_format import payloads


CASES=Path(__file__).resolve().parents[1]/'docs/steel-agent-v2/knowledge/cases'


def case_source(n):
    document=(CASES/f'CASE-20261019-{n:03}.md').read_text(encoding='utf-8')
    return re.search(r'## 1\.[\s\S]*?```text\n([\s\S]*?)\n```',document)[1]


def reference(**changes):
    raw=dict(id='test-stock',revision='1',kind='ibeam',profile='30Ш1',piece_length_mm='12000',
             value='681.6',unit='kg_per_piece',status='reviewed',method='test_stock_unit_mass',
             sources=[dict_source('https://example.org/stock')],checked_at='synthetic',
             limitation='Synthetic scoped stock coefficient, not normative evidence')
    raw.update(changes)
    return MassReference(**raw)


@pytest.mark.parametrize('kind',['ibeam','round_welded_pipe','linear','other'])
def test_catalog_rejects_lengthless_stock_piece_mass(kind):
    with pytest.raises(EngineError,match='mass_reference_piece_length_required'):
        MassCatalog([reference(kind=kind,piece_length_mm=None,value='340.8')])


@pytest.mark.parametrize('unit',['kg_per_piece','kg_per_m'])
@pytest.mark.parametrize('cutting',['specified','unknown','confirmed_piece_length'])
@pytest.mark.parametrize('length',[None,'6000 мм'])
def test_cutting_gate_is_shared_by_all_stock_piece_methods(tmp_path,unit,cutting,length):
    source,plan=beam_plan(length,cutting)
    record=reference(unit=unit,value='340.8' if unit=='kg_per_piece' else '56.8',
                     piece_length_mm='6000' if unit=='kg_per_piece' else None)
    _,repo,_,_=setup(tmp_path,source,plan,MassCatalog([record]))
    result=mass(repo);allowed=cutting=='confirmed_piece_length' and length is not None
    assert (result.status=='calculated')==allowed
    assert result.tonnes==(Decimal('13.632') if allowed else None)
    assert applicable(record,repo.load(1).items[0],result.spec)==allowed


@pytest.mark.parametrize('actual,reference_length,value,expected,origin',[
    (None,'12000','681.6','27.264','uvm_default'),
    ('12000 мм','12000','681.6','27.264','user'),
    ('6000 мм','6000','340.8','13.632','user'),
    (None,'6000','340.8',None,None),
    ('6000 мм','12000','681.6',None,None),
])
def test_beam_unit_mass_matches_effective_length_not_just_profile(tmp_path,actual,reference_length,value,expected,origin):
    source,plan=beam_plan(actual)
    _,repo,_,_=setup(tmp_path,source,plan,MassCatalog([reference(piece_length_mm=reference_length,value=value)]))
    result=mass(repo)
    assert result.tonnes==(Decimal(expected) if expected else None)
    assert result.length_origin==origin
    assert repo.load(1).items==repo.load(1).originals and repo.load(1).original_text==source
    if expected:assert result.piece_length_mm==Decimal(reference_length)


def test_ordinary_sheet_is_not_subject_to_stock_cutting_rule(tmp_path):
    plan=sheet_plan();plan['mass_specs'][0]['cutting']='specified'
    # Density remains allowed for a whole sheet despite stock cutting flags.
    _,repo,_,_=setup(tmp_path,plan=plan)
    assert mass(repo).tonnes==Decimal('28.6034375')


@pytest.mark.parametrize('kind',['linear','round_welded_pipe','other'])
def test_default_length_is_never_applied_to_other_stock(tmp_path,kind):
    source,plan=beam_plan();plan['mass_specs'][0]['kind']=kind
    _,repo,_,_=setup(tmp_path,source,plan,MassCatalog([reference(kind=kind,unit='kg_per_m',value='56.8',piece_length_mm=None)]))
    assert mass(repo).tonnes is None and mass(repo).length_origin is None


def test_unparsed_or_conflicting_length_prevents_default(tmp_path):
    source,plan,_=sample([('Балка',[('profile','30Ш1','30Ш1'),('grade','С255','С255'),
                                  ('length','6000 мм','6000 мм'),('cut_length','8000 мм','8000 мм')],'40','шт')])
    hint=spec('ibeam',profile='profile');hint['dimensions']=[dict(role='piece_length',name='length',unit='мм')]
    plan['mass_specs']=[hint]
    _,repo,_,_=setup(tmp_path,source,plan)
    assert mass(repo).tonnes is None and 'противоречивую' in mass(repo).reason


def test_saved_bad_reference_cannot_bypass_recalculation_validation_or_restart(tmp_path):
    source,plan=beam_plan();agent,repo,fake,_=setup(tmp_path,source,plan)
    good=repo.load(1);annotation=mass(repo)
    bad=annotation.model_copy(update={'reference':reference(piece_length_mm=None,value='340.8'),
                                      'tonnes':Decimal('13.632')})
    damaged=good.model_copy(update={'theoretical_mass':[bad]})
    assert not applicable(bad.reference,damaged.items[0],bad.spec)
    assert calculate(damaged.items[0],bad.spec,bad.classification,[bad.reference]).tonnes is None
    with pytest.raises(EngineError,match='invalid_or_stale_mass_annotation'):validate_annotations(damaged)
    restored=refresh(damaged,catalog=MassCatalog([]))
    assert restored.theoretical_mass[0].tonnes is None
    validate_annotations(restored)
    # Actual persistence still carries the valid snapshot, never the injected object.
    reopened=Repository(Database(tmp_path/'mass.sqlite3'))
    assert reopened.load(1)==good
    assert refresh(reopened.load(1)).theoretical_mass[0].tonnes==Decimal('27.264')
    fake.plans.append(edit(good,[set_op(field='quantity',name=None,value='1',evidence='1')]))
    assert agent.handle(1,'Нужна 1 штука',event_id='one').status=='ok'
    assert mass(repo).tonnes==Decimal('.6816')


@pytest.mark.parametrize('damage',['cutting','missing_reference_length','mismatch_reference_length'])
def test_stored_pending_proposal_cannot_bypass_confirmation_gate(tmp_path,damage):
    source,plan=beam_plan('12000 мм')
    _,repo,_,_=setup(tmp_path,source,plan)
    state=repo.load(1);hint=mass(repo).spec
    record=reference(status='observed')
    if damage=='cutting':hint=hint.model_copy(update={'cutting':'specified'})
    elif damage=='missing_reference_length':record=record.model_copy(update={'piece_length_mm':None})
    else:record=record.model_copy(update={'piece_length_mm':'6000','value':'340.8'})
    pending=PendingMass(event_id='research',request_id=state.id,revision=state.revision,candidates=[record],
        specs=[hint],item_ids=['i1'],classifications=[classification(state.items[0],hint)],requested_specs=[hint])
    approval=calc_plan(state);approval.update(mass_confirmation_event_id='research',mass_approval_ids=[record.id])
    with pytest.raises(EngineError):confirm(state,AgentPlan.model_validate(approval),PendingMass.model_validate_json(pending.model_dump_json()))


@pytest.mark.parametrize('length_proof,cutting,allowed',[('12 м','none',True),('6 м','none',False),
    (None,'none',False),('12 м','specified',False),('12 м','confirmed_piece_length',True)])
def test_beam_search_confirmation_full_path_checks_reference_stock_length(tmp_path,length_proof,cutting,allowed):
    source,plan=beam_plan('12000 мм' if cutting=='confirmed_piece_length' else None,cutting)
    agent,repo,fake,_=setup(tmp_path,source,plan,MassCatalog([]));before=repo.load(1)
    quote='Балка 30Ш1, длина '+(length_proof or 'не указана')+': 681,6 кг/шт'
    candidate=dict(item_id='i1',value='681,6',unit='kg_per_piece',source_url='https://example.org/stock',
                   quote=quote,profile_evidence='30Ш1',piece_length_evidence=length_proof,suitability='applicable')
    fake.plans.extend([calc_plan(before,query='synthetic beam unit mass'),
                       dict(intent='answer_question',mass_candidates=[candidate])])
    fake.search_result=SearchAnswer(quote,[dict_source(candidate['source_url'])])
    proposal=agent.handle(1,'Рассчитай',event_id='research')
    assert bool(proposal.pending_mass)==allowed and repo.load(1)==before
    if allowed:
        # Reopen both repository and proposal through their real JSON persistence.
        reopened=Repository(Database(tmp_path/'mass.sqlite3'))
        pending=reopened.mass_pending(1,reopened.load(1))
        approval=calc_plan(before,'Подтверждаю');approval.update(mass_confirmation_event_id='research',
            mass_approval_ids=[pending.candidates[0].id])
        confirming=Fake(approval)
        result=SteelAgent(confirming,reopened,mass_catalog=MassCatalog([])).handle(1,'Подтверждаю',event_id='confirm')
        assert result.status=='ok' and reopened.load(1).theoretical_mass[0].tonnes==Decimal('27.264')
        assert reopened.load(1).items==before.items and reopened.load(1).originals==before.originals
        assert len(confirming.calls)==1 and not confirming.search_calls
    assert len(fake.search_calls)==1 and len(fake.calls)==3


def case_plan(number):
    """Explicit semantic fixtures, not a parser or imported runtime knowledge."""
    source=case_source(number);lines=source.splitlines();records=[];items=[];specs=[];catalog=[]
    if number==2:
        rows=[('Труба профильная квадратная','40×40×3','40х40х3','430','6м','6','Ст3','ст3','linear','3.36'),
              ('Уголок','63×40×5','63х40х5','240','6м','6','Ст3','ст3','linear','3.91'),
              ('Уголок','50×50×5','50х50х5','26','6м','6','Ст3','ст3','linear','3.77'),
              ('Уголок','60×40×3','60х40х3','426','6м','6','Ст3','ст3','linear','2.23')]
        urls=['https://metmo.ru/catalog/truba/profilnaya_kvadratnaya/razmer_40/truba_profilnaya_kvadratnaya_40kh40kh3_0.html',
              'https://ptoflow.ru/sortament/ugolok/63x40x5','https://www.calcal.ru/ves-ugolka-50x50x5',
              'https://metalguide.ru/sortament/ugolok-gnutyj-neravnopolochnyj']
        order=['i1','i3','i4','i2']
    else:
        assert number==6
        rows=[('Труба профильная квадратная','100×100×4','100х100х4','50','12м','12 м','Ст3','ст3','linear','11.73'),
              ('Балка двутавровая','30Б1','30Б1','8','12м','12м','С255','ст3','ibeam','32'),
              ('Балка двутавровая','60Б2','60Б2','18','12м','12м','С355','С355','ibeam','105.5'),
              ('Швеллер','18П','18П','78','12м','12м','Ст3','ст3','linear','16.3'),
              ('Балка двутавровая','25К1','25К1','16','12м','12 м','С255','С255','ibeam','62.6')]
        urls=['https://www.calcal.ru/ves-profilnoj-truby-100x100',
              'https://metcalc.ru/calc-metalloprokat/steel/balka/table/standard-59',
              'https://elsort.ru/?id=dvutavr%2F57837-2017%2F60b2','https://base.garant.ru/3924246/',
              'https://metcalc.ru/calc-metalloprokat/steel/balka/table/standard-59']
        order=['i5','i2','i3','i4','i1']
    offset=0
    for n,(label,profile,raw_profile,amount,length,raw_length,grade,raw_grade,kind,value) in enumerate(rows,1):
        key=f'i{n}';sid=f's{n}';line=lines[n-1]
        records.append(dict(id=sid,kind='product',start=offset,end=offset+len(line)));offset+=len(line)+1
        facts=[dict(name='profile',value=profile,raw=raw_profile,source_ids=[sid]),
               dict(name='grade',value=grade,raw=raw_grade,source_ids=[sid]),
               dict(name='length',value=length,raw=raw_length,source_ids=[sid])]
        if number==2:facts.append(dict(name='standard',value=None,raw='гост',source_ids=[sid]))
        items.append(dict(id=key,source_id=sid,label=label,quantity=amount,unit='хлыстов' if number==2 else 'шт',
            quantity_text=amount,unit_text='хлыстов' if number==2 else 'шт',characteristics=facts))
        hint=spec(kind,item=key,profile='profile',standard='standard' if number==2 else None)
        if kind=='ibeam' and raw_grade.casefold() in {'ст3','09г2с'}:hint['grade_policy']='uvm_beam_commercial'
        hint['evidence']='Труба' if n==1 else 'Уголок' if number==2 else 'Швеллер' if n==4 else 'Балка'
        hint['dimensions']=[dict(role='piece_length',name='length',unit='м')]
        if '×' in profile:
            hint['dimensions'] += [dict(role=role,name='profile',unit='мм',component=i,separator='×')
                                  for i,role in enumerate(['width','height','wall'],1)]
        specs.append(hint)
        catalog.append(reference(id=f'case{number}-{key}',kind=kind,profile=profile,piece_length_mm=None,
            value=value,unit='kg_per_m',grades=[grade],status='owner_indicative',
            approval_scope=f'CASE-20261019-{number:03}; approved profile estimate, not interchangeability',
            execution='Расчётная таблица конкретного профиля; исполнение поставки не подтверждено',
            method='owner_approved_profile_estimate',sources=[dict_source(urls[n-1])],
            limitation='Предварительная оценка CASE; ГОСТ в исходнике не задан, допуски и фактический вес не подтверждены'))
    plan=dict(intent='create_request',extraction=dict(records=records,items=items,
        groups=[dict(id='g1',label='Коммерческая заявка',
                     item_ids=[item['id'] for item in items] if number==2 else order,
                     rationale='План GPT для данного CASE')]),
        display_layout=dict(items=[dict(item_id=hint['item_id'],grade_name='grade',
            kind='beam' if hint['kind']=='ibeam' else 'pipe' if row[0].startswith('Труба')
                 else 'channel' if row[0]=='Швеллер' else 'angle',
            dimensions=[d for d in hint['dimensions'] if d['role']!='piece_length'])
            for hint,row in zip(specs,rows)]),
        mass_specs=specs)
    return source,plan,MassCatalog(catalog)


@pytest.mark.parametrize('case,expected,tonnes,ids',[
    (2,'Труба профильная 40×40×3 Ст3 6м — 430 шт. (≈8,669 т)\n'
       'Уголок 50×50×5 Ст3 6м — 26 шт. (≈0,588 т)\n'
       'Уголок 60×40×3 Ст3 6м — 426 шт. (≈5,700 т)\n'
       'Уголок 63×40×5 Ст3 6м — 240 шт. (≈5,630 т)',
       ['8.6688','5.6304','.58812','5.69988'],['i1','i3','i4','i2']),
    (6,'Балка 25К1 С255 12м — 16 шт. (≈12,019 т)\n'
       'Балка 30Б1 С255 12м — 8 шт. (≈3,072 т)\n'
       'Балка 60Б2 С355 12м — 18 шт. (≈22,788 т)\n'
       'Швеллер 18П Ст3 12м — 78 шт. (≈15,257 т)\n'
       'Труба профильная 100×100×4 Ст3 12м — 50 шт. (≈7,038 т)',
       ['7.038','3.072','22.788','15.2568','12.0192'],['i5','i2','i3','i4','i1']),
])
def test_expert_cases_exact_calculation_compact_output_and_stable_addressing(tmp_path,case,expected,tonnes,ids):
    source,plan,catalog=case_plan(case);agent,repo,fake,reply=setup(tmp_path,source,plan,catalog)
    state=repo.load(1);before=state.model_dump_json()
    assert reply.text==expected
    assert [m.tonnes for m in state.theoretical_mass]==list(map(Decimal,tonnes))
    assert reply.display.item_ids==ids and len(set(ids))==len(state.items)==len(state.originals)
    assert reply.display.layout is not None and repo.last_display(1,state).item_ids==ids
    if case==2:
        assert state.groups[0].item_ids==['i1','i2','i3','i4']  # Display sort, not a rewritten commercial group.
    assert state.items==state.originals and state.original_text==source
    assert render(state,layout=reply.display.layout)==expected and state.model_dump_json()==before
    assert all(m.reference.status=='owner_indicative' and m.reference.approval_scope for m in state.theoretical_mass)
    assert not any(word in reply.text for word in ('ГОСТ','Итого','L=','двутавровая','квадратная','\n\n'))
    assert all('м)' not in row for row in reply.text.splitlines())
    if case==6:
        conversion=next(m for m in state.theoretical_mass if m.spec.item_id=='i2')
        assert conversion.source_grade=='ст3' and conversion.grade_policy=='uvm_beam_commercial'
        assert all(m.grade_policy is None for m in state.theoretical_mass if m.spec.kind!='ibeam')
    # Editing position 3 uses the actual last shown sequence, then survives undo/restart.
    target=ids[2];changed=edit(state,[dict(kind='set_quantity',selection=dict(
        mode='display',positions=[3],cardinality='one',evidence='позиция 3'),
        field='quantity',value='9',unit='шт',evidence='9',unit_evidence='шт')])
    fake.plans.append(changed)
    result=agent.handle(1,'позиция 3: 9 шт',event_id='position')
    assert result.status in {'ok','partial'}
    edited=repo.load(1)
    assert next(i for i in edited.items if i.id==target).quantity==9
    assert [i for i in edited.items if i.id!=target]==[i for i in state.items if i.id!=target]
    assert edited.originals==state.originals and edited.groups==state.groups
    fake.plans.append(edit(edited,[dict(kind='undo')]))
    undone=agent.handle(1,'Отмени',event_id='undo')
    assert undone.text==expected and undone.display.item_ids==ids
    assert repo.load(1).items==state.items and repo.load(1).theoretical_mass==state.theoretical_mass
    reopened=Repository(Database(tmp_path/'mass.sqlite3'))
    assert reopened.load(1)==repo.load(1) and reopened.load(2) is None
    assert reopened.version(1,state.id,1)==state
    assert SteelAgent(Fake(),reopened,mass_catalog=catalog).handle(1,'Отмени',event_id='undo')==undone


def test_indicative_is_narrow_approval_not_global_tolerance(tmp_path):
    source,plan,catalog=case_plan(2)
    assert len(MassCatalog().records)==3  # Expert fixtures are not runtime bulk ingestion.
    unverified=catalog.records[0].model_copy(update={'id':'internet-alternative','value':'3.30','status':'observed'})
    _,repo,_,_=setup(tmp_path,source,plan,MassCatalog([*catalog.records,unverified]))
    assert mass(repo).tonnes==Decimal('8.6688')
    conflict=unverified.model_copy(update={'status':'owner_indicative'})
    current=repo.load(1);item=current.items[0];hint=current.theoretical_mass[0].spec
    assert calculate(item,hint,classification(item,hint),[catalog.records[0],conflict]).status=='conflicting'
    unknown=catalog.records[0].model_copy(update={'status':'observed'})
    assert calculate(item,hint,classification(item,hint),[unknown]).tonnes is None
    wrong=catalog.records[0].model_copy(update={'profile':'40×40×4'})
    assert not applicable(wrong,item,hint)
    with pytest.raises(EngineError,match='indicative_mass_approval_required'):
        MassCatalog([catalog.records[0].model_copy(update={'approval_scope':None})])


def test_explanation_contains_full_audit_but_only_two_clickable_sources(tmp_path):
    source,plan,catalog=case_plan(6);agent,repo,fake,reply=setup(tmp_path,source,plan,catalog)
    before=repo.load(1)
    fake.plans.append(dict(intent='show_request',view='explanation'))
    result=agent.handle(1,'Объясни расчёт',event_id='explain')
    assert result.status=='ok' and repo.load(1)==before
    for value in ('Длина штуки: 12000 мм; указана пользователем','62.6 кг/м','12,0192 т',
                  '≈12,019 т','Метод:','Ограничения:','предварительный ориентир','Исполнение основания:'):
        assert value in result.text
    assert len(result.sources)==4 and len([r for r in result.presentation if r.url])==2
    assert visible([part['text'] for part in payloads(result,1)])==result.text
    assert len(fake.calls)==2 and not fake.search_calls


def test_default_length_explanation_and_impossible_calculation_reason(tmp_path):
    source,plan=beam_plan();agent,repo,fake,_=setup(tmp_path,source,plan)
    fake.plans.append(dict(intent='show_request',view='explanation'))
    answer=agent.handle(1,'Объясни',event_id='audit')
    assert 'коммерческое допущение UVM: балка 12 м' in answer.text and '27,264' in answer.text
    source,plan=beam_plan(None,'specified');_,other,_,_=setup(tmp_path/'cut',source,plan)
    assert 'Для резки подтвердите' in render(other.load(1),'explanation') and '≈' not in render(other.load(1),'explanation')


def test_restart_readonly_show_does_not_trust_old_unsafe_json(tmp_path):
    source,plan=beam_plan();_,repo,_,_=setup(tmp_path,source,plan)
    original=repo.load(1)
    bad=mass(repo).model_copy(update={'reference':reference(piece_length_mm=None,value='340.8'),
                                     'tonnes':Decimal('13.632')})
    damaged=original.model_copy(update={'theoretical_mass':[bad]})
    # Fault injection ONLY into this test's temporary SQLite.
    with repo.database.connection() as connection:
        connection.execute('UPDATE steel_v2_active SET payload=? WHERE user_id=1',(damaged.model_dump_json(),))
    reopened=Repository(Database(tmp_path/'mass.sqlite3'))
    fake=Fake(dict(intent='show_request'),dict(intent='show_request',view='explanation'))
    agent=SteelAgent(fake,reopened,mass_catalog=MassCatalog([]))
    for event in ('show','explanation'):
        answer=agent.handle(1,'Покажи',event_id=event)
        assert '≈' not in answer.text and 'требует проверки' in answer.text
    assert reopened.load(1)==damaged  # Readonly view neither rewrites nor discards history.
    assert reopened.version(1,original.id,1)==original
    assert len(fake.calls)==2 and not fake.search_calls
    assert all(call[0]['active_request']['theoretical_mass'][0]['tonnes'] is None for call in fake.calls)


@pytest.mark.parametrize('source_grade,target_grade,kind,allowed',[
    ('Ст3','С255','ibeam',True),('09Г2С','С355','ibeam',True),
    ('Ст3','С255','linear',False),('Ст3','С355','ibeam',False),
])
def test_beam_commercial_policy_is_explicit_scoped_and_audited(tmp_path,source_grade,target_grade,kind,allowed):
    source,plan=beam_plan('12000 мм',grade=target_grade,raw_grade=source_grade)
    plan['mass_specs'][0].update(kind=kind,grade_policy='uvm_beam_commercial')
    catalog=MassCatalog([reference(kind=kind,unit='kg_per_m',value='56.8',piece_length_mm=None)])
    _,repo,_,_=setup(tmp_path,source,plan,catalog)
    result=mass(repo)
    assert (result.tonnes is not None)==allowed
    if allowed:
        assert result.source_grade==source_grade and result.grade_policy=='uvm_beam_commercial'
        assert repo.load(1).items[0].characteristics[1].raw==source_grade
    assert repo.load(1).items==repo.load(1).originals


@pytest.mark.parametrize('case,quantities,units',[
    (1,['0,005','2025,00','5,1','791,67','198,38','10,44'],['т','кг','кг','кг','кг','кг']),
    (4,['20','20','100'],['тн','тн','тн']),
    (5,['0,251','1,413','0,589','1,226','2,359','3,620'],['т']*6),
])
def test_remaining_approved_cases_keep_sources_counts_and_provenance_without_catalog_import(tmp_path,case,quantities,units):
    source=case_source(case);lines=source.splitlines();records=[];items=[];offset=0
    # Approved semantic labels are explicit mock output, not learned behavior.
    doc=(CASES/f'CASE-20261019-{case:03}.md').read_text(encoding='utf-8')
    approved=re.search(r'## 5\.[\s\S]*?```text\n([\s\S]*?)\n```',doc)[1].splitlines()
    if case==5:approved=[approved[1],approved[0],*approved[2:]]  # Link back to original source IDs.
    for n,line in enumerate(lines,1):
        sid=f's{n}';end=offset+len(line)
        if n>len(quantities):
            records.append(dict(id=sid,kind='note',start=offset,end=end));offset=end+1;continue
        records.append(dict(id=sid,kind='product',start=offset,end=end));offset=end+1
        label=approved[n-1].split(' — ')[0]
        # This fixture checks unchanged original quantities; the separate approved
        # kg->t mechanism is not silently recast as a theoretical-mass operation.
        items.append(dict(id=f'i{n}',source_id=sid,label=label,quantity=quantities[n-1].replace(',','.'),
            unit=units[n-1],quantity_text=quantities[n-1],unit_text=units[n-1],
            characteristics=[dict(name='approved_description',value=label,raw=line,source_ids=[sid])]))
    plan=dict(intent='create_request',extraction=dict(records=records,items=items,
        groups=[dict(id='g1',label='CASE',item_ids=[i['id'] for i in items],rationale='Explicit mock fixture')]))
    _,repo,fake,reply=setup(tmp_path,source,plan)
    state=repo.load(1)
    assert len(state.items)==len(state.originals)==len(quantities)
    assert state.original_text==source and state.items==state.originals
    assert [i.quantity for i in state.items]==[Decimal(q.replace(',','.')) for q in quantities]
    assert [i.unit for i in state.items]==units and not state.theoretical_mass
    for item in state.items:
        assert item.label in reply.text and item.characteristics[0].source_ids==[item.source_id]
    assert len(reply.display.item_ids)==len(set(reply.display.item_ids))==len(items)
    assert repo.version(1,state.id,1)==state and not fake.search_calls
    assert Repository(Database(tmp_path/'mass.sqlite3')).load(1)==state


def test_all_six_knowledge_documents_remain_approved_with_25_original_positions():
    assert [len(case_source(n).splitlines()) for n in range(1,7)]==[6,4,1,4,6,5]
    # CASE004 includes a shared-grade footer, not a fourth product.
    assert sum([6,4,1,3,6,5])==25
    for n in range(1,7):
        document=(CASES/f'CASE-20261019-{n:03}.md').read_text(encoding='utf-8')
        assert 'approved' in document and '## 2.' in document and '## 5.' in document
    assert 'редакция 3' in (CASES/'CASE-20261019-002.md').read_text(encoding='utf-8')


@pytest.mark.parametrize('pointer,execution,allowed',[(False,'без покрытия',False),
    (True,'без покрытия',True),(True,'оцинкованная',False)])
def test_indicative_reference_never_erases_known_execution_difference(tmp_path,pointer,execution,allowed):
    source,plan,_=sample([('Труба профильная',[('profile','40×40×3','40×40×3'),
        ('grade','Ст3','Ст3'),('length','6м','6м'),('execution',execution,execution)],'430','шт')])
    hint=spec('linear',profile='profile');hint['evidence']='Труба'
    hint['dimensions']=[dict(role='piece_length',name='length',unit='м')]
    if pointer:hint['execution_name']='execution'
    plan['mass_specs']=[hint]
    record=reference(kind='linear',profile='40×40×3',piece_length_mm=None,unit='kg_per_m',value='3.36',
                     status='owner_indicative',approval_scope='Synthetic explicit profile approval',execution='без покрытия')
    _,repo,_,_=setup(tmp_path,source,plan,MassCatalog([record]))
    result=mass(repo)
    assert (result.tonnes is not None)==allowed
    assert applicable(record,repo.load(1).items[0],result.spec)==allowed
    assert repo.load(1).items==repo.load(1).originals


@pytest.mark.parametrize('kind',['standard','execution'])
def test_indicative_known_norm_or_execution_is_not_a_missing_value_exception(tmp_path,kind):
    line='ГОСТ TEST' if kind=='standard' else 'оцинкованная'
    # Separate synthetic source is needed to keep literal provenance honest.
    source,plan,_=sample([('Труба профильная',[('profile','40×40×3','40×40×3'),('grade','Ст3','Ст3'),
                         ('length','6м','6м'),(kind,line,line)],'430','шт')])
    hint=spec('linear',profile='profile',standard='standard' if kind=='standard' else None)
    hint.update(evidence='Труба',dimensions=[dict(role='piece_length',name='length',unit='м')])
    if kind=='execution':hint['execution_name']='execution'
    plan['mass_specs']=[hint]
    record=reference(kind='linear',profile='40×40×3',unit='kg_per_m',value='3.36',piece_length_mm=None,
        status='owner_indicative',approval_scope='Synthetic profile only',standards=['ГОСТ OTHER'],execution='без покрытия')
    _,repo,_,_=setup(tmp_path,source,plan,MassCatalog([record]))
    assert mass(repo).tonnes is None


def test_piece_length_from_another_search_row_is_rejected(tmp_path):
    source,plan=beam_plan();_,repo,_,_=setup(tmp_path,source,plan,MassCatalog([]))
    state=repo.load(1);hint=mass(repo).spec
    quote='Балка 30Ш1 6 м: 340,8 кг/шт\nДругое изделие: 12 м'
    candidate=dict(item_id='i1',value='340,8',unit='kg_per_piece',source_url='https://example.org/stock',
        quote=quote,profile_evidence='30Ш1',piece_length_evidence='12 м',suitability='applicable')
    answer=SearchAnswer(quote,[dict_source(candidate['source_url'])])
    with pytest.raises(EngineError,match='mass_research_piece_length_cross_row'):
        research_candidates(state,[hint],answer,AgentPlan.model_validate(
            dict(intent='answer_question',mass_candidates=[candidate])).mass_candidates,'cross-row')


@pytest.mark.parametrize('proof',[None,'без покрытия'])
def test_search_must_prove_known_execution_in_quote_not_copy_request(tmp_path,proof):
    source,plan,_=sample([('Изделие',[('profile','X','X'),('length','6м','6м'),
                                    ('execution','без покрытия','без покрытия')],'10','шт')])
    hint=spec('linear',profile='profile',grade=None)
    hint.update(execution_name='execution',dimensions=[dict(role='piece_length',name='length',unit='м')])
    plan['mass_specs']=[hint]
    _,repo,_,_=setup(tmp_path,source,plan,MassCatalog([]));state=repo.load(1)
    quote='Изделие X без покрытия: 2 кг/м'
    candidate=dict(item_id='i1',value='2',unit='kg_per_m',source_url='https://example.org/stock',quote=quote,
                   profile_evidence='X',execution_evidence=proof,suitability='applicable')
    answer=SearchAnswer(quote,[dict_source(candidate['source_url'])])
    operation=lambda:research_candidates(state,[MassSpec.model_validate(hint)],answer,
        AgentPlan.model_validate(dict(intent='answer_question',mass_candidates=[candidate])).mass_candidates,'execution')
    if proof:assert operation().candidates[0].execution=='без покрытия'
    else:
        with pytest.raises(EngineError,match='mass_research_execution_mismatch'):operation()


def test_catalog_length_and_value_use_the_same_decimal_validation_as_calculation(tmp_path):
    source,plan=beam_plan('12000 мм')
    _,repo,_,_=setup(tmp_path,source,plan,MassCatalog([reference(piece_length_mm='12000,0',value='681,6')]))
    assert mass(repo).tonnes==Decimal('27.264') and mass(repo).length_origin=='user'
    assert mass(repo).reference.value=='681,6'  # Preserve source spelling, never a float.


def legacy_mass_snapshot(repo,change=None):
    """Fault/compatibility fixtures only: rewrite this test's temporary initial JSON."""
    state=repo.load(1)
    raw=json.loads(state.model_dump_json())
    for annotation in raw['theoretical_mass']:
        for field in ('piece_length_mm','length_origin','source_grade','grade_policy'):
            annotation.pop(field,None)
        for field in ('grade_policy','execution_name'):
            if annotation['spec'].get(field) is None:annotation['spec'].pop(field,None)
        if annotation['reference']:
            for field in ('approval_scope','execution'):
                if annotation['reference'].get(field) is None:annotation['reference'].pop(field,None)
    if change:change(raw['theoretical_mass'][0])
    payload=json.dumps(raw,ensure_ascii=False,separators=(',',':'))
    with repo.database.connection() as connection:
        connection.execute('UPDATE steel_v2_active SET payload=? WHERE user_id=1',(payload,))
        connection.execute('UPDATE steel_v2_version SET payload=? WHERE user_id=1 AND request_id=? AND revision=1',
                           (payload,state.id))
    return repo.load(1),payload.encode('utf-8')


def version_bytes(repo,request_id,revision=1):
    with repo.database.connection() as connection:
        row=connection.execute('SELECT CAST(payload AS BLOB) FROM steel_v2_version '
                               'WHERE user_id=1 AND request_id=? AND revision=?',(request_id,revision)).fetchone()
    return row[0] if row else None


@pytest.mark.parametrize('length,origin',[('12000 мм','user'),(None,'uvm_default')])
def test_undo_legacy_mass_without_length_audit_restores_goods_and_mass(tmp_path,length,origin):
    source,plan=beam_plan(length);agent,repo,fake,_=setup(tmp_path,source,plan)
    historical,stored=legacy_mass_snapshot(repo)
    fake.plans.append(edit(historical,[set_op(field='quantity',name=None,value='1',evidence='1')]))
    assert agent.handle(1,'Нужна 1 штука',event_id='one-legacy').status=='ok'
    assert mass(repo).tonnes==Decimal('.6816')
    undo=edit(repo.load(1),[dict(kind='undo')]);fake.plans.extend([undo,undo])
    reply=agent.handle(1,'Отмени',event_id='undo-legacy')
    assert reply.status=='ok',(reply.status,reply.diagnostics)
    restored=repo.load(1)
    assert restored.items==historical.items and restored.items[0].quantity==40
    assert mass(repo).tonnes==Decimal('27.264')
    assert mass(repo).piece_length_mm==12000 and mass(repo).length_origin==origin
    assert restored.originals==historical.originals and restored.original_text==historical.original_text
    assert restored.groups==historical.groups and restored.questions==historical.questions
    assert restored.revision==3 and version_bytes(repo,historical.id)==stored
    assert repo.version(1,historical.id,3)==restored and len(fake.calls)==3


def edit_then_undo(agent,repo,fake):
    historical=repo.load(1)
    fake.plans.append(edit(historical,[set_op(field='quantity',name=None,value='1',evidence='1')]))
    assert agent.handle(1,'Нужна 1 штука',event_id='one-legacy').status in {'ok','partial'}
    undo=edit(repo.load(1),[dict(kind='undo')]);fake.plans.extend([undo,undo])
    return agent.handle(1,'Отмени',event_id='undo-legacy')


@pytest.mark.parametrize('damage',[
    'wrong_tonnes','missing_reference','unknown_unit_length','mismatched_unit_length',
    'unconfirmed_cutting','bad_coefficient','changed_classification','wrong_length_audit','wrong_origin_audit',
])
def test_undo_restores_goods_but_never_accepts_unsafe_historical_mass(tmp_path,damage):
    source,plan=beam_plan('12000 мм');agent,repo,fake,_=setup(tmp_path,source,plan)
    def corrupt(annotation):
        if damage=='wrong_tonnes':annotation['tonnes']='99'
        elif damage=='missing_reference':annotation['reference']=None
        elif damage in {'unknown_unit_length','mismatched_unit_length'}:
            annotation['reference']=reference(value='340.8',piece_length_mm=
                None if damage=='unknown_unit_length' else '6000').model_dump(mode='json')
        elif damage=='unconfirmed_cutting':annotation['spec']['cutting']='specified'
        elif damage=='bad_coefficient':annotation['reference']['value']='not-a-number'
        elif damage=='changed_classification':annotation['classification']='stale-classification'
        elif damage=='wrong_length_audit':annotation['piece_length_mm']='6000'
        elif damage=='wrong_origin_audit':annotation['length_origin']='uvm_default'
    historical,stored=legacy_mass_snapshot(repo,corrupt)
    reply=edit_then_undo(agent,repo,fake)
    assert reply.status in {'ok','partial'} and '≈' not in reply.text
    restored=repo.load(1);annotation=mass(repo)
    assert restored.items==historical.items and len(restored.items)==1 and restored.items[0].quantity==40
    assert restored.original_text==historical.original_text and restored.originals==historical.originals
    assert restored.records==historical.records and restored.groups==historical.groups
    assert restored.questions==historical.questions and restored.revision==3
    assert annotation.status=='unavailable' and annotation.tonnes is None and 'требует проверки' in annotation.reason
    assert annotation.reference==historical.theoretical_mass[0].reference
    assert version_bytes(repo,historical.id)==stored and repo.version(1,historical.id,3)==restored
    assert len(fake.calls)==3 and not fake.search_calls
    # A restart and both readonly paths must retain goods but never display old tonnes.
    reopened=Repository(Database(tmp_path/'mass.sqlite3'))
    viewer=SteelAgent(Fake(dict(intent='show_request'),dict(intent='show_request',view='explanation')),reopened)
    for event in ('show','explain'):
        answer=viewer.handle(1,'Покажи',event_id=event)
        assert '≈' not in answer.text and '40 шт' in answer.text and 'требует проверки' in answer.text
    assert reopened.load(1)==restored and version_bytes(reopened,historical.id)==stored


def test_legacy_undo_uses_historical_basis_not_new_catalog_and_keeps_editing_after_restart(tmp_path):
    source,plan=beam_plan('12000 мм');agent,repo,fake,_=setup(tmp_path,source,plan)
    historical,stored=legacy_mass_snapshot(repo)
    # Deliberately different catalog: undo is not allowed to adopt its new value.
    agent.engine.mass_catalog=MassCatalog([reference(id='new-table',revision='99',unit='kg_per_m',
                                                   value='90',piece_length_mm=None)])
    assert edit_then_undo(agent,repo,fake).status=='ok'
    restored=repo.load(1)
    assert mass(repo).reference==historical.theoretical_mass[0].reference and mass(repo).reference.value=='56.8'
    assert mass(repo).tonnes==Decimal('27.264')
    reopened=Repository(Database(tmp_path/'mass.sqlite3'));fake=Fake()
    agent=SteelAgent(fake,reopened,mass_catalog=MassCatalog([]))
    fake.plans.append(edit(restored,[dict(kind='set_length',target_id='i1',field='characteristic',name='length',
        value='6',unit='м',evidence='6',unit_evidence='м')]))
    assert agent.handle(1,'Длина 6 м',event_id='length-after-undo').status=='ok'
    assert mass(reopened).tonnes==Decimal('13.632') and mass(reopened).piece_length_mm==6000
    fake.plans.append(edit(reopened.load(1),[dict(kind='undo')]))
    answer=agent.handle(1,'Отмени',event_id='undo-length')
    assert answer.status=='ok' and mass(reopened).tonnes==Decimal('27.264')
    fake.plans.append(edit(reopened.load(1),[set_op(field='quantity',name=None,value='2',evidence='2')]))
    assert agent.handle(1,'Нужно 2 штуки',event_id='quantity-after-undo').status=='ok'
    assert mass(reopened).tonnes==Decimal('1.3632')
    fake.plans.append(edit(reopened.load(1),[dict(kind='undo')]))
    last=agent.handle(1,'Отмени',event_id='undo-quantity')
    final=reopened.load(1)
    assert last.status=='ok' and final.revision==7 and final.items==historical.items
    assert final.originals==historical.originals and final.changes==historical.changes
    assert version_bytes(reopened,historical.id)==stored
    assert len(fake.calls)==4 and not fake.search_calls and reopened.load(2) is None
    assert SteelAgent(Fake(),reopened).handle(1,'Отмени',event_id='undo-quantity')==last


def test_legacy_missing_commercial_grade_audit_is_reconstructed_from_verified_policy(tmp_path):
    source,plan=beam_plan('12000 мм');plan['mass_specs'][0]['grade_policy']='uvm_beam_commercial'
    agent,repo,fake,_=setup(tmp_path,source,plan)
    historical,stored=legacy_mass_snapshot(repo)
    assert historical.theoretical_mass[0].source_grade is None
    assert edit_then_undo(agent,repo,fake).status=='ok'
    assert mass(repo).source_grade=='Ст3' and mass(repo).grade_policy=='uvm_beam_commercial'
    assert version_bytes(repo,historical.id)==stored and repo.load(1).items==historical.items


@pytest.mark.parametrize('label',['Труба профильная','Швеллер','Уголок'])
def test_legacy_undo_never_grants_twelve_metre_default_to_other_stock(tmp_path,label):
    source,plan,_=sample([(label,[('profile','X','X')],'40','шт')])
    hint=spec('linear',profile='profile',grade=None);hint['evidence']=label
    plan['mass_specs']=[hint]
    record=reference(kind='linear',profile='X',unit='kg_per_m',piece_length_mm=None,value='56.8')
    agent,repo,fake,_=setup(tmp_path,source,plan,MassCatalog([record]))
    def corrupt(annotation):
        annotation.update(status='calculated',tonnes='27.264',reference=record.model_dump(mode='json'))
    historical,stored=legacy_mass_snapshot(repo,corrupt)
    assert edit_then_undo(agent,repo,fake).status=='ok'
    assert repo.load(1).items==historical.items and mass(repo).tonnes is None
    assert mass(repo).piece_length_mm is None and mass(repo).length_origin is None
    assert version_bytes(repo,historical.id)==stored


def test_compatible_readonly_legacy_view_does_not_weaken_new_snapshot_validation(tmp_path):
    source,plan=beam_plan('12000 мм');_,repo,_,_=setup(tmp_path,source,plan)
    historical,stored=legacy_mass_snapshot(repo)
    with pytest.raises(EngineError,match='invalid_or_stale_mass_annotation'):
        RequestEngine().validate(historical)  # Strict validation itself was NOT relaxed.
    fake=Fake(dict(intent='show_request'),dict(intent='show_request',view='explanation'))
    agent=SteelAgent(fake,repo)
    for event in ('show-old','explain-old'):
        reply=agent.handle(1,'Покажи',event_id=event)
        assert reply.status=='ok' and '27,264' in reply.text
        assert repo.load(1)==historical and version_bytes(repo,historical.id)==stored
    assert 'Длина штуки: 12000 мм; указана пользователем' in reply.text
    assert len(fake.calls)==2 and not fake.search_calls
    assert repo.version(1,historical.id,2) is None


def test_undo_commit_failure_rolls_back_snapshot_version_history_and_outbox(rig):
    import sqlite3
    source,plan=beam_plan('12000 мм');fake=Fake(plan)
    executor=Executor(rig.queue,rig.access,lambda:rig.api,lambda:fake)
    admit(rig,630,text=source);executor.execute(rig.queue.claim())
    repo=Repository(rig.queue.database)
    historical,stored=legacy_mass_snapshot(repo)
    fake.plans.append(edit(historical,[set_op(field='quantity',name=None,value='1',evidence='1')]))
    admit(rig,631,text='Нужна 1 штука');executor.execute(rig.queue.claim())
    assert mass(repo).tonnes==Decimal('.6816')
    admit(rig,632,text='Отмени');rig.queue.claim()
    hook=rig.queue.finish_hook(632)
    def fail_after_outbox(connection,*args):
        hook(connection,*args)
        raise sqlite3.OperationalError('synthetic failure after undo outbox insertion')
    transactional=Repository(rig.queue.database,finish_hook=fail_after_outbox)
    _,current,sequence=transactional.begin(1,'telegram:632','Отмени')
    restored=RequestEngine().edit(current,AgentPlan.model_validate(edit(current,[dict(kind='undo')])),
                                 'Отмени',lambda key,revision:transactional.version(1,key,revision))
    tables=('steel_v2_active','steel_v2_version','steel_v2_turn','steel_v2_inflight','steel_v2_outbox','steel_v2_inbox')
    def ledger():
        with rig.queue.database.connection() as connection:
            return {table:[tuple(row) for row in connection.execute('SELECT * FROM '+table+' ORDER BY rowid')]
                    for table in tables}
    before=ledger();deliveries=list(rig.api.calls)
    with pytest.raises(sqlite3.OperationalError,match='synthetic failure'):
        transactional.finish(1,'telegram:632',sequence,'Отмени',Reply(text=render(restored),status='ok',
            intent='edit_request',request_id=restored.id,revision=restored.revision),restored)
    assert ledger()==before and repo.load(1)==current and repo.version(1,current.id,3) is None
    assert version_bytes(repo,historical.id)==stored and rig.api.calls==deliveries
