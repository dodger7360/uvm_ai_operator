"""Stage 6: exact arithmetic, provenance and real agent/repository/outbox transactions.

The sheet text is explicitly approved in the stage-6 task; other inputs synthetic.
Mock plans do not measure live GPT classification or reliability of internet facts.
"""
from copy import deepcopy
from decimal import Decimal, localcontext
import json

import pytest

from database import Database
from steel_agent_v2 import SteelAgent, Repository, RequestEngine
from steel_agent_v2.contracts import AgentPlan, Reply, MassReference
from steel_agent_v2.theoretical_mass import MassCatalog, coefficient, display, product
from steel_agent_v2.transport import SearchAnswer, SearchUnavailable
from steel_agent_v2.presentation import render
from test_steel_agent_v2 import Fake, edit, set_op
from test_steel_agent_v2_027_presentation import sample
from test_steel_agent_v2_queue import rig, admit, Executor
from test_steel_agent_v2_telegram_html import messages, stored, visible
from test_steel_agent_v2_wire_schema import sdk_plan, subset_issues


SHEET='Лист ГК 2 1,25х2,5 ст08пс(ст3) -583 шт'


def spec(kind='solid_carbon_sheet',item='i1',size='size',grade='grade',profile=None,standard=None,cutting='none'):
    roles={'solid_carbon_sheet':['thickness','width','length'],'round_welded_pipe':['diameter','wall']}.get(kind,[])
    return dict(item_id=item,kind=kind,dimensions=[dict(role=role,name=size,unit='мм',separator='×',component=n)
             for n,role in enumerate(roles,1)],profile_name=profile,standard_name=standard,grade_name=grade,
             cutting=cutting,evidence={'solid_carbon_sheet':'Лист','round_welded_pipe':'Труба','ibeam':'Балка'}.get(kind,'Изделие'))


def sheet_plan():
    return dict(intent='create_request',mass_specs=[spec()],extraction=dict(
        records=[dict(id='s1',kind='product',start=0,end=len(SHEET))],
        items=[dict(id='i1',source_id='s1',label='Лист г/к',quantity='583',unit='шт',quantity_text='583',unit_text='шт',
            characteristics=[dict(name='size',value='2×1250×2500 мм',raw='2 1,25х2,5',source_ids=['s1']),
                dict(name='grade',value='08пс',raw='08пс',source_ids=['s1']),
                dict(name='alternative_grade',value='допускается Ст3',raw='ст3',source_ids=['s1'])])],
        groups=[dict(id='g1',label='Лист',item_ids=['i1'],rationale='Отдельная позиция')]))


def pipe_plan(diameter='159',wall='6',unit='м',amount='240',length=None):
    facts=[('size',f'{diameter}×{wall} мм',f'{diameter}×{wall} мм'),('grade','Ст20','Ст20')]
    if length:facts.append(('length',length,length))
    source,plan,_=sample([('Труба э/с',facts,amount,unit)])
    hint=spec('round_welded_pipe')
    if length:hint['dimensions'].append(dict(role='piece_length',name='length',unit='мм'))
    plan['mass_specs']=[hint]
    return source,plan


def beam_plan(length=None,cutting='none',grade='С255',raw_grade='Ст3'):
    facts=[('profile','30Ш1','30Ш1'),('grade',grade,raw_grade)]
    if length:facts.append(('length',length,length))
    if cutting!='none':facts.append(('cutting','Резка','Резка'))
    source,plan,_=sample([('Балка',facts,'40','шт')])
    hint=spec('ibeam',profile='profile',cutting=cutting)
    if length:hint['dimensions']=[dict(role='piece_length',name='length',unit='мм')]
    plan['mass_specs']=[hint]
    return source,plan


def setup(tmp_path,source=SHEET,plan=None,catalog=None):
    repo=Repository(Database(tmp_path/'mass.sqlite3'));fake=Fake(plan or sheet_plan())
    agent=SteelAgent(fake,repo,mass_catalog=catalog)
    reply=agent.handle(1,source,event_id='create');assert reply.status in {'ok','partial'}
    return agent,repo,fake,reply


def mass(repo,index=0):return repo.load(1).theoretical_mass[index]


def calc_plan(current,message='Рассчитай',query=None):
    return dict(intent='edit_request',expected_request_id=current.id,expected_revision=current.revision,
                operations=[dict(kind='calculate_mass',target_id='i1',evidence=message)],search_query=query)


def test_approved_sheet_exact_value_alternative_grade_original_and_no_total(tmp_path):
    agent,repo,fake,reply=setup(tmp_path)
    state=repo.load(1);m=mass(repo)
    assert m.tonnes==Decimal('28.6034375') and '≈28,603 т' in reply.text and '583 шт' in reply.text
    assert reply.text=='Лист г/к 2×1250×2500 мм 08пс (допускается Ст3) — 583 шт. (≈28,603 т)'
    assert state.original_text==SHEET and state.items[0].quantity==583 and state.items[0].unit=='шт'
    assert state.items[0].id==state.originals[0].id=='i1' and '08пс' in reply.text and 'допускается Ст3' in reply.text
    assert not any(value in reply.text for value in ('ГОСТ','Итого','Суммар','Группа ','версия','1. '))
    assert m.reference.value=='7850' and m.reference.status=='reviewed' and m.method=='rectangular_sheet_nominal_geometry'
    assert m.reference.sources and len(fake.calls)==1 and not fake.search_calls
    assert repo.version(1,state.id,1)==state


def test_sheet_edit_quantity_geometry_grade_undo_and_restart(tmp_path):
    agent,repo,fake,_=setup(tmp_path);first=repo.load(1)
    fake.plans.append(edit(first,[set_op(field='quantity',name=None,value='600',evidence='600')]))
    assert agent.handle(1,'Нужно 600 штук',event_id='quantity').status=='ok'
    assert mass(repo).tonnes==Decimal('29.4375') and repo.load(1).items[0].quantity==600
    second=repo.load(1)
    fake.plans.append(edit(second,[set_op(name='size',value='3×1250×2500 мм',evidence='3×1250×2500 мм')]))
    agent.handle(1,'Размер 3×1250×2500 мм',event_id='size')
    assert mass(repo).tonnes==Decimal('44.15625')
    third=repo.load(1)
    fake.plans.append(edit(third,[set_op(name='grade',value='Алюминий',evidence='Алюминий')]))
    agent.handle(1,'Марка Алюминий',event_id='grade')
    assert mass(repo).tonnes is None and mass(repo).status=='unavailable'
    changed=repo.load(1);fake.plans.append(edit(changed,[dict(kind='undo')]))
    restored=agent.handle(1,'Отмени',event_id='undo')
    assert mass(repo).tonnes==Decimal('44.15625') and '≈44,156 т' in restored.text
    assert repo.load(1).originals==first.originals and repo.load(1).records==first.records
    reopened=Repository(Database(tmp_path/'mass.sqlite3'))
    assert reopened.load(1)==repo.load(1) and reopened.load(2) is None
    assert SteelAgent(Fake(),reopened).handle(1,'Отмени',event_id='undo')==restored
    assert len(fake.calls)==5 and not fake.search_calls


@pytest.mark.parametrize('source_plan,expected',[
    (lambda:pipe_plan(),Decimal('5.4336')),
    (lambda:pipe_plan(unit='шт',amount='40',length='6000 мм'),Decimal('5.4336')),
    (lambda:beam_plan(),Decimal('27.264')),
    (lambda:beam_plan('6000 мм'),Decimal('13.632')),
    (lambda:beam_plan('6000 мм','confirmed_piece_length'),Decimal('13.632'))])
def test_metres_pieces_and_beam_length_basis_are_independent(tmp_path,source_plan,expected):
    source,plan=source_plan();agent,repo,fake,reply=setup(tmp_path,source,plan)
    m=mass(repo);state=repo.load(1)
    assert m.status=='calculated' and m.tonnes==expected and len(state.items)==1
    assert state.items[0].quantity==state.originals[0].quantity and state.items[0].unit==state.originals[0].unit
    assert 'ГОСТ Р 57837' not in reply.text
    assert ('С255' in reply.text) if 'Балка' in source else ('Ст20' in reply.text)
    if 'Балка' in source:
        assert state.originals[0].characteristics[1].raw=='Ст3' and 'Ст3' in state.original_text
        assert ('UVM_default' in m.method)==('length' not in [c.name for c in state.items[0].characteristics])
    assert not fake.search_calls


@pytest.mark.parametrize('cutting,length',[('specified',None),('specified','6000 мм'),('unknown',None),('confirmed_piece_length',None)])
def test_cutting_never_implicitly_uses_twelve_metre_stock(tmp_path,cutting,length):
    source,plan=beam_plan(length,cutting);_,repo,_,reply=setup(tmp_path,source,plan)
    assert mass(repo).tonnes is None and '≈' not in reply.text and repo.load(1).items[0].quantity==40


@pytest.mark.parametrize('change', ['diameter','standard','grade','missing_length'])
def test_unknown_or_incompatible_pipe_has_no_guessed_coefficient(tmp_path,change):
    source,plan=pipe_plan(diameter='200' if change=='diameter' else '159',unit='шт' if change=='missing_length' else 'м')
    if change in {'standard','grade'}:
        item=plan['extraction']['items'][0]
        # Rebuild a coherent synthetic original, never bypass source validation.
        facts=[('size','159×6 мм','159×6 мм'),('grade','Ст20' if change=='standard' else '12Х18Н10Т','Ст20' if change=='standard' else '12Х18Н10Т')]
        if change=='standard':facts.append(('standard','ГОСТ 8732-78','ГОСТ 8732-78'))
        source,plan,_=sample([('Труба э/с',facts,'240','м')]);plan['mass_specs']=[spec('round_welded_pipe',standard='standard' if change=='standard' else None)]
    _,repo,_,reply=setup(tmp_path,source,plan)
    assert mass(repo).tonnes is None and '≈' not in reply.text and '240' in reply.text


@pytest.mark.parametrize('missing',[0,1,2])
def test_incomplete_sheet_geometry_is_preserved_without_false_mass(tmp_path,missing):
    source,plan,_=sample([('Лист',[('size','2×1250×2500 мм','2×1250×2500 мм'),('grade','Ст3','Ст3')],'583','шт')])
    hint=spec();hint['dimensions'].pop(missing);plan['mass_specs']=[hint]
    _,repo,_,reply=setup(tmp_path,source,plan)
    assert mass(repo).tonnes is None and repo.load(1).items[0].quantity==583 and '583 шт' in reply.text


@pytest.mark.parametrize('label',['Лист ПВЛ','Лист рифлёный'])
def test_non_solid_sheet_is_not_given_rectangular_formula(tmp_path,label):
    source,plan,_=sample([(label,[('size','2×1250×2500 мм','2×1250×2500 мм'),('grade','Ст3','Ст3')],'583','шт')])
    hint=spec('other');hint['evidence']='Лист';plan['mass_specs']=[hint]
    _,repo,_,reply=setup(tmp_path,source,plan)
    assert mass(repo).tonnes is None and label in reply.text and '≈' not in reply.text


def test_reviewed_unit_mass_interface_and_catalog_conflict(tmp_path):
    record=MassReference(id='synthetic-unit',revision='1',kind='fixed_unit',profile='X',value='12.125',unit='kg_per_piece',
        status='reviewed',method='fixed_unit_mass',checked_at='synthetic',limitation='Test only; fixed article, not cut stock',
        sources=[dict(url='https://example.org/unit',title='Test unit mass')])
    source,plan,_=sample([('Изделие',[('profile','X','X')],'2.5','шт')])
    plan['mass_specs']=[spec('fixed_unit',grade=None,profile='profile')]
    agent,repo,fake,reply=setup(tmp_path,source,plan,MassCatalog([record]))
    assert mass(repo).tonnes==Decimal('.0303125') and '2,5 шт' in reply.text
    conflict=record.model_copy(update={'id':'other','value':'13'})
    other=Repository(Database(tmp_path/'conflict.sqlite3'))
    response=SteelAgent(Fake(plan),other,mass_catalog=MassCatalog([record,conflict])).handle(1,source,event_id='conflict')
    assert other.load(1).theoretical_mass[0].status=='conflicting' and '≈' not in response.text


def test_profile_change_drops_coefficient_and_undo_restores_it(tmp_path):
    source,plan=beam_plan();agent,repo,fake,_=setup(tmp_path,source,plan);old=repo.load(1)
    fake.plans.append(edit(old,[set_op(name='profile',value='40Ш1',evidence='40Ш1')]))
    agent.handle(1,'Профиль 40Ш1',event_id='profile');assert mass(repo).tonnes is None
    fake.plans.append(edit(repo.load(1),[dict(kind='undo')]))
    agent.handle(1,'Отмена',event_id='undo');assert mass(repo).tonnes==Decimal('27.264')


def test_decimal_context_and_small_nonzero_display():
    with localcontext() as ctx:
        ctx.prec=3
        assert product(Decimal('583'),Decimal('2'),Decimal('1250'),Decimal('2500'),Decimal('7850'),exponent=-12)==Decimal('28.6034375')
    source,plan=pipe_plan(amount='0.000001')
    state=RequestEngine().create(AgentPlan.model_validate(plan).extraction,source,'tiny',AgentPlan.model_validate(plan).mass_specs)
    assert state.theoretical_mass[0].tonnes>0 and '< 0,001 т' in render(state) and '≈ 0,000' not in render(state)


def test_mass_spec_wire_schema_and_legacy_snapshot_json(tmp_path):
    _,sent=sdk_plan(AgentPlan.model_validate(sheet_plan()).model_dump(mode='json'))
    assert not subset_issues(sent[0]['text']['format']['schema'])
    source,plan,_=sample();repo=Repository(Database(tmp_path/'old.sqlite3'))
    SteelAgent(Fake(plan),repo).handle(1,source,event_id='old')
    assert 'theoretical_mass' not in repo.load(1).model_dump() and 'pending_mass' not in Reply(text='Old',status='ok',intent='free_chat').model_dump()


def test_mass_atomic_group_failure_leaves_original_version_and_history(tmp_path):
    agent,repo,fake,_=setup(tmp_path);before=repo.load(1)
    bad=edit(before,[set_op(field='quantity',name=None,value='600',evidence='600'),
        dict(kind='calculate_mass',target_id='not-an-item',evidence='рассчитай')])
    fake.plans.extend([bad,bad]);reply=agent.handle(1,'600; рассчитай',event_id='bad')
    assert reply.status=='failed' and repo.load(1)==before and repo.version(1,before.id,2) is None
    assert mass(repo).tonnes==Decimal('28.6034375')


def evidence_plan(text='Труба 219×6: масса 1 м, кг 31,52',url='https://example.org/table',value='31,52'):
    return dict(intent='answer_question',mass_candidates=[dict(item_id='i1',value=value,unit='kg_per_m',
                source_url=url,quote=text,profile_evidence='219×6',suitability='applicable')])


def pending_search(agent,repo,fake,bad=None):
    plan=calc_plan(repo.load(1),'Найди и рассчитай','synthetic pipe 219x6 mass')
    fake.plans.extend([plan,bad or evidence_plan()])
    fake.search_result=SearchAnswer('Труба 219×6: масса 1 м, кг 31,52',
        [dict_source('https://example.org/table')])
    return agent.handle(1,'Найди и рассчитай',event_id='research')


def dict_source(url):
    from steel_agent_v2.contracts import SourceLink
    return SourceLink(url=url,title='Синтетический справочник')


def test_search_confirmation_applies_to_same_item_after_restart_without_new_search(tmp_path):
    source,plan=pipe_plan(diameter='219');agent,repo,fake,_=setup(tmp_path,source,plan);before=repo.load(1)
    reply=pending_search(agent,repo,fake)
    assert reply.status=='ok' and reply.pending_mass is not None and repo.load(1)==before
    assert len(fake.search_calls)==1 and len(fake.calls)==3
    assert 'Подтвердите коэффициент' in reply.text and '31,52 кг/м' in reply.text
    reopened=Repository(Database(tmp_path/'mass.sqlite3'));pending=reopened.mass_pending(1,reopened.load(1))
    confirmed=calc_plan(before,'Подтверждаю');confirmed.update(mass_confirmation_event_id='research',mass_approval_ids=[pending.candidates[0].id])
    follow=Fake(confirmed);answer=SteelAgent(follow,reopened).handle(1,'Подтверждаю 31,52 кг/м',event_id='confirm')
    result=reopened.load(1);m=result.theoretical_mass[0]
    assert answer.status=='ok' and m.tonnes==Decimal('7.5648') and result.revision==before.revision+1
    assert m.reference.status=='owner_confirmed' and m.reference.sources[0].url=='https://example.org/table'
    assert result.items==before.items and result.originals==before.originals and result.groups==before.groups
    assert result.changes[-1].field=='theoretical_mass' and not follow.search_calls and len(follow.calls)==1
    assert SteelAgent(Fake(),reopened).handle(1,'Подтверждаю 31,52 кг/м',event_id='confirm')==answer
    follow.plans.append(edit(result,[dict(kind='undo')]))
    SteelAgent(follow,reopened).handle(1,'Отмена',event_id='undo-confirm')
    assert reopened.load(1).theoretical_mass==before.theoretical_mass


@pytest.mark.parametrize('damage',['url','quote','profile','number','unit','mutation'])
def test_bad_research_never_applies_mutation_or_repeats_paid_search(tmp_path,damage):
    source,plan=pipe_plan(diameter='219');agent,repo,fake,_=setup(tmp_path,source,plan);before=repo.load(1)
    evidence=evidence_plan();candidate=evidence['mass_candidates'][0]
    if damage=='url':candidate['source_url']='https://other.example/never-returned'
    if damage=='quote':candidate['quote']='fabricated text'
    if damage=='profile':candidate['profile_evidence']='159×6'
    if damage=='number':candidate['value']='56,8'
    if damage=='unit':candidate['unit']='kg_per_piece'
    if damage=='mutation':evidence=edit(before,[set_op(name='grade',value='Ст3',evidence='Ст3')])
    result=pending_search(agent,repo,fake,evidence)
    assert result.status=='failed' and result.pending_mass is None and repo.load(1)==before
    assert len(fake.search_calls)==1 and len(fake.calls)==3 and repo.version(1,before.id,2) is None


def test_known_coefficient_skips_search_even_if_model_proposes_query(tmp_path):
    source,plan=pipe_plan();agent,repo,fake,_=setup(tmp_path,source,plan)
    fake.plans.append(calc_plan(repo.load(1),query='unneeded'))
    result=agent.handle(1,'Рассчитай',event_id='known')
    assert result.status=='ok' and '5,434' in result.text and not fake.search_calls and len(fake.calls)==2


def test_search_failure_preserves_request_and_never_calls_evidence_model(tmp_path):
    source,plan=pipe_plan(diameter='219');agent,repo,fake,_=setup(tmp_path,source,plan);before=repo.load(1)
    fake.plans.append(calc_plan(before,'Найди',query='synthetic'));fake.search_result=SearchUnavailable('no_sources')
    assert agent.handle(1,'Найди',event_id='failed-search').status=='failed'
    assert repo.load(1)==before and len(fake.calls)==2 and len(fake.search_calls)==1


def test_outbox_annotation_display_addressing_restart_dedup_and_unknown(rig,monkeypatch):
    fake=Fake(sheet_plan());admit(rig,100,text=SHEET)
    Executor(rig.queue,rig.access,lambda:rig.api,lambda:fake).execute(rig.queue.claim())
    repo=Repository(rig.queue.database);old=repo.load(1)
    assert visible([p['text'] for p in messages(rig)])==render(old,expert=True)
    assert repo.last_display(1,old).item_ids==['i1']
    selected=dict(kind='set_quantity',selection=dict(mode='display',positions=[1],cardinality='one',evidence='позиция 1'),
                  field='quantity',value='600',evidence='600',unit='шт',unit_evidence='шт')
    fake.plans.append(edit(old,[selected]));rig.api.fail=True
    admit(rig,101,text='позиция 1 — 600 шт');Executor(rig.queue,rig.access,lambda:rig.api,lambda:fake).execute(rig.queue.claim())
    assert rig.queue.job(101)['state']=='delivery_unknown' and repo.load(1).theoretical_mass[0].tonnes==Decimal('29.4375')
    before=deepcopy(messages(rig));from steel_agent_v2.queue import DurableQueue
    reopen=DurableQueue(rig.queue.database);reopen.recover();reopen.deliver(101,rig.api,rig.access)
    assert messages(rig)==before and reopen.claim() is None and len(fake.calls)==2
    assert stored(reopen,101)[0]['state']=='delivery_unknown' and repo.load(2) is None


def test_explicit_standard_or_length_cannot_be_bypassed_by_missing_semantic_pointer(tmp_path):
    source,plan=beam_plan('6000 мм');plan['mass_specs'][0]['dimensions']=[]
    _,repo,_,reply=setup(tmp_path,source,plan)
    assert mass(repo).tonnes is None and '≈' not in reply.text
    source,plan,_=sample([('Труба э/с',[('size','159×6 мм','159×6 мм'),('grade','Ст20','Ст20'),
                                                    ('standard','ГОСТ 8732-78','ГОСТ 8732-78')],'240','м')])
    plan['mass_specs']=[spec('round_welded_pipe')]
    repo2=Repository(Database(tmp_path/'standard.sqlite3'))
    result=SteelAgent(Fake(plan),repo2).handle(1,source,event_id='create')
    assert repo2.load(1).theoretical_mass[0].tonnes is None and '≈' not in result.text


def mixed_plan(repeats=1,annotations=True):
    rows=[];hints=[]
    for _ in range(repeats):
        rows.extend([('Лист',[('size','2×1250×2500 мм','2×1250×2500 мм'),('grade','Ст3','Ст3')],'583','шт'),
            ('Труба э/с',[('size','159×6 мм','159×6 мм'),('grade','Ст20','Ст20')],'240','м'),
            ('Балка',[('profile','30Ш1','30Ш1'),('grade','С255','Ст3')],'40','шт')])
        start=len(rows)-2
        hints.extend([spec(item='i'+str(start)),spec('round_welded_pipe',item='i'+str(start+1)),
                      spec('ibeam',item='i'+str(start+2),profile='profile')])
    source,plan,_=sample(rows)
    if annotations:plan['mass_specs']=hints
    return source,plan


def test_mixed_profiles_sources_ids_originals_and_unrequested_totals(tmp_path):
    source,plan=mixed_plan();_,repo,_,reply=setup(tmp_path,source,plan)
    state=repo.load(1)
    assert [m.tonnes for m in state.theoretical_mass]==[Decimal('28.6034375'),Decimal('5.4336'),Decimal('27.264')]
    assert [m.spec.item_id for m in state.theoretical_mass]==['i1','i2','i3']
    assert len(state.items)==len(state.originals)==len(state.records)==3
    assert state.items==state.originals and state.original_text==source
    assert len(reply.text.splitlines())==3 and reply.text.count('≈')==3
    assert reply.text.count(' / ≈')==0 and reply.text.count('(≈')==3
    assert 'Итого' not in reply.text and '61,301' not in reply.text
    assert all(m.reference.sources for m in state.theoretical_mass)


def test_piece_length_edit_recomputes_beam_and_keeps_stock_policy_out_of_facts(tmp_path):
    source,plan=beam_plan();agent,repo,fake,_=setup(tmp_path,source,plan);old=repo.load(1)
    changed=edit(old,[dict(kind='set_length',target_id='i1',field='characteristic',name='length',
                          value='6',evidence='6',unit='м',unit_evidence='м')])
    hint=spec('ibeam',profile='profile');hint['dimensions']=[dict(role='piece_length',name='length',unit='мм')]
    changed['mass_specs']=[hint];fake.plans.append(changed)
    result=agent.handle(1,'Длина 6 м',event_id='length')
    assert mass(repo).tonnes==Decimal('13.632') and '6м' in result.text
    assert next(c.value for c in repo.load(1).items[0].characteristics if c.name=='length')=='6000 мм'
    assert repo.load(1).originals==old.originals and old.items[0].characteristics==old.originals[0].characteristics
    fake.plans.append(edit(repo.load(1),[dict(kind='undo')]))
    agent.handle(1,'Отмена',event_id='undo-length')
    assert mass(repo).tonnes==Decimal('27.264') and repo.load(1).items==old.items


def test_new_mass_spec_cannot_change_unselected_items(tmp_path):
    source,plan=mixed_plan();agent,repo,fake,_=setup(tmp_path,source,plan);old=repo.load(1)
    bad=calc_plan(old);bad['mass_specs']=[spec('round_welded_pipe',item='i2')]
    fake.plans.extend([bad,bad])
    assert agent.handle(1,'Рассчитай',event_id='outside').status=='failed' and repo.load(1)==old


def test_mass_addressing_uses_sorted_delivered_id_map(tmp_path):
    source,plan=mixed_plan()
    # Put the beam before the other products without changing facts/group membership.
    plan['extraction']['groups']=[dict(id='g1',label='Смешанная',item_ids=['i3','i2','i1'],rationale='Синтетика')]
    plan['display_layout']=dict(items=[dict(item_id='i3',kind='beam',grade_name='grade'),
                                       dict(item_id='i2',kind='pipe',grade_name='grade'),
                                       dict(item_id='i1',kind='sheet',grade_name='grade')])
    agent,repo,fake,first=setup(tmp_path,source,plan);old=repo.load(1)
    assert first.text.splitlines()[0].startswith('Балка') and repo.last_display(1,old).item_ids[0]=='i3'
    patch=dict(kind='set_quantity',selection=dict(mode='display',positions=[1],cardinality='one',evidence='позиция 1'),
               field='quantity',value='50',evidence='50',unit='шт',unit_evidence='шт')
    fake.plans.append(edit(old,[patch]));agent.handle(1,'позиция 1 — 50 шт',event_id='position')
    current=repo.load(1)
    assert current.items[2].quantity==50 and current.items[:2]==old.items[:2]
    assert {m.spec.item_id:m.tonnes for m in current.theoretical_mass}['i3']==Decimal('34.08')


@pytest.mark.parametrize('failure',[TimeoutError('synthetic uncertainty'),ValueError('synthetic parse error'),SearchUnavailable('tool_incomplete')])
def test_every_search_failure_is_terminal_for_this_event(tmp_path,failure):
    source,plan=pipe_plan(diameter='219');agent,repo,fake,_=setup(tmp_path,source,plan);old=repo.load(1)
    fake.plans.append(calc_plan(old,query='synthetic'));fake.search_result=failure
    result=agent.handle(1,'Рассчитай',event_id='terminal')
    assert result.status=='failed' and repo.load(1)==old and len(fake.calls)==2 and len(fake.search_calls)==1
    assert agent.handle(1,'Рассчитай',event_id='terminal')==result and len(fake.search_calls)==1


@pytest.mark.parametrize('quote,value,profile',[
    ('Труба 219×6: масса 1 м, кг 31,52; масса единицы 189,12 кг/шт','189,12','219×6'),
    ('Труба 219×6\nТруба 159×6: 22,64 кг/м','22,64','219×6'),
])
def test_research_cannot_mix_unit_values_or_rows(tmp_path,quote,value,profile):
    source,plan=pipe_plan(diameter='219');agent,repo,fake,_=setup(tmp_path,source,plan);old=repo.load(1)
    fake.plans.extend([calc_plan(old,query='synthetic'),evidence_plan(quote,value=value)])
    fake.search_result=SearchAnswer(quote,[dict_source('https://example.org/table')])
    assert agent.handle(1,'Рассчитай',event_id='mixed-quote').status=='failed' and repo.load(1)==old
    assert len(fake.search_calls)==1


def test_conflicting_web_candidates_require_one_explicit_choice(tmp_path):
    source,plan=pipe_plan(diameter='219');agent,repo,fake,_=setup(tmp_path,source,plan);old=repo.load(1)
    a=evidence_plan()['mass_candidates'][0];b=deepcopy(a)
    a['suitability']='conflicting';b.update(value='32',quote='Труба 219×6: 32 кг/м',suitability='conflicting')
    fake.plans.extend([calc_plan(old,query='synthetic'),dict(intent='answer_question',mass_candidates=[a,b])])
    fake.search_result=SearchAnswer(a['quote']+'\n'+b['quote'],[dict_source('https://example.org/table')])
    pending=agent.handle(1,'Рассчитай',event_id='conflict-web')
    assert pending.pending_mass is not None and repo.load(1)==old and 'противоречат' in pending.text
    approval=calc_plan(old,'Подтверждаю');approval.update(mass_confirmation_event_id='conflict-web',
        mass_approval_ids=[r.id for r in pending.pending_mass.candidates])
    fake.plans.extend([approval,approval])
    assert agent.handle(1,'Подтверждаю',event_id='ambiguous-approval').status=='failed' and repo.load(1)==old
    approval['mass_approval_ids']=[pending.pending_mass.candidates[0].id];fake.plans.append(approval)
    assert agent.handle(1,'Подтверждаю',event_id='one-choice').status=='ok' and mass(repo).tonnes==Decimal('7.5648')
    assert len(fake.search_calls)==1


@pytest.mark.parametrize('scope',['other_user','changed_revision','wrong_event','wrong_candidate','changed_spec'])
def test_pending_mass_approval_cannot_cross_scope(tmp_path,scope):
    source,plan=pipe_plan(diameter='219');agent,repo,fake,_=setup(tmp_path,source,plan)
    pending=pending_search(agent,repo,fake).pending_mass
    if scope=='other_user':
        fake.plans.append(plan);agent.handle(2,source,event_id='other-create')
    if scope=='changed_revision':
        fake.plans.append(edit(repo.load(1),[set_op(field='quantity',name=None,value='250',evidence='250')]))
        agent.handle(1,'Нужно 250 м',event_id='new-revision')
    user=2 if scope=='other_user' else 1;old=repo.load(user)
    approval=calc_plan(old,'Подтверждаю');approval.update(mass_confirmation_event_id='research',mass_approval_ids=[pending.candidates[0].id])
    if scope=='wrong_event':approval['mass_confirmation_event_id']='never-existed'
    if scope=='wrong_candidate':approval['mass_approval_ids']=['never-existed']
    if scope=='changed_spec':approval['mass_specs']=[spec('other',grade=None)]
    fake.plans.extend([approval,approval])
    assert agent.handle(user,'Подтверждаю',event_id='bad-approval').status=='failed' and repo.load(user)==old
    assert len(fake.search_calls)==1


def test_group_known_and_unknown_mass_commits_together_after_confirmation(tmp_path):
    rows=[('Труба э/с',[('size','159×6 мм','159×6 мм'),('grade','Ст20','Ст20')],'240','м'),
          ('Труба э/с',[('size','219×6 мм','219×6 мм'),('grade','Ст20','Ст20')],'240','м')]
    source,plan,_=sample(rows);agent,repo,fake,_=setup(tmp_path,source,plan);old=repo.load(1)
    query=calc_plan(old,query='synthetic unknown pipe');query['operations'].append(dict(kind='calculate_mass',target_id='i2',evidence='Рассчитай'))
    query['mass_specs']=[spec('round_welded_pipe'),spec('round_welded_pipe',item='i2')]
    evidence=evidence_plan();evidence['mass_candidates'][0]['item_id']='i2'
    fake.plans.extend([query,evidence]);fake.search_result=SearchAnswer('Труба 219×6: масса 1 м, кг 31,52',[dict_source('https://example.org/table')])
    pending=agent.handle(1,'Рассчитай',event_id='group-research').pending_mass
    assert repo.load(1)==old and {s.item_id for s in pending.requested_specs}=={'i1','i2'}
    query['search_query']=None;query['mass_specs']=[];query.update(mass_confirmation_event_id='group-research',mass_approval_ids=[pending.candidates[0].id])
    fake.plans.append(query);answer=agent.handle(1,'Подтверждаю. Рассчитай',event_id='group-confirm')
    assert answer.status=='ok' and repo.load(1).revision==old.revision+1
    assert [m.tonnes for m in repo.load(1).theoretical_mass]==[Decimal('5.4336'),Decimal('7.5648')]
    assert repo.load(1).items==old.items and len(fake.search_calls)==1


def test_long_mass_reply_is_saved_as_immutable_multipart_html(rig,monkeypatch):
    from steel_agent_v2.queue import DurableQueue
    source,plan=mixed_plan(48);fake=Fake(plan);admit(rig,300,text=source)
    monkeypatch.setattr(rig.queue,'deliver',lambda *args:None)
    Executor(rig.queue,rig.access,lambda:rig.api,lambda:fake).execute(rig.queue.claim())
    saved=[json.loads(row['payload']) for row in stored(rig.queue,300)]
    state=Repository(rig.queue.database).load(1)
    assert len(render(state).encode('utf-16-le'))//2>3200
    assert len(saved)>1 and len(state.theoretical_mass)==144 and visible([p['text'] for p in saved])==render(state,expert=True)
    assert all(part['parse_mode']=='HTML' for part in saved) and len(fake.calls)==1
    reopened=DurableQueue(rig.queue.database);reopened.recover();reopened.deliver(300,rig.api,rig.access)
    assert messages(rig)==saved
    admit(rig,300,text=source);assert reopened.claim() is None and messages(rig)==saved


def test_unknown_paid_search_is_quarantined_by_existing_queue_and_not_replayed(rig):
    from steel_agent_v2.queue import DurableQueue
    source,plan=pipe_plan(diameter='219');fake=Fake(plan)
    executor=Executor(rig.queue,rig.access,lambda:rig.api,lambda:fake)
    admit(rig,400,text=source);executor.execute(rig.queue.claim())
    old=Repository(rig.queue.database).load(1)
    fake.plans.append(calc_plan(old,query='synthetic'));fake.search_result=TimeoutError('fake unknown API outcome')
    admit(rig,401,text='Рассчитай');executor.execute(rig.queue.claim())
    assert rig.queue.job(401)['state']=='execution_unknown' and Repository(rig.queue.database).load(1)==old
    reopened=DurableQueue(rig.queue.database);reopened.recover()
    admit(rig,402,text='Следующее сообщение');assert reopened.claim() is None and len(fake.search_calls)==1
    with rig.queue.database.connection() as c:
        attempts=[tuple(r) for r in c.execute('SELECT kind,state FROM steel_v2_attempt WHERE update_id=401 ORDER BY number')]
    assert attempts==[('plan','result_received'),('search','execution_unknown')]


def test_unit_mass_is_bound_to_piece_length_and_does_not_survive_a_different_length(tmp_path):
    source,plan=pipe_plan(diameter='219',unit='шт',amount='40',length='6000 мм')
    reference=MassReference(id='test-six-metre-pipe',revision='1',kind='round_welded_pipe',dimensions_mm=['219','6'],
        piece_length_mm='6000',grades=['Ст20'],value='189.12',unit='kg_per_piece',status='reviewed',method='test-only',
        sources=[dict_source('https://example.org/pipe')],checked_at='synthetic',limitation='Synthetic fixture, not catalog seed')
    agent,repo,fake,_=setup(tmp_path,source,plan,MassCatalog([reference]));old=repo.load(1)
    assert mass(repo).tonnes==Decimal('7.5648')
    patch=edit(old,[dict(kind='set_length',target_id='i1',field='characteristic',name='length',value='8',evidence='8',unit='м',unit_evidence='м')])
    fake.plans.append(patch);agent.handle(1,'Длина 8 м',event_id='other-length')
    assert mass(repo).tonnes is None and repo.load(1).items[0].quantity==40
    fake.plans.append(edit(repo.load(1),[dict(kind='undo')]))
    agent.handle(1,'Отмена',event_id='undo-unit-length');assert mass(repo).tonnes==Decimal('7.5648')


@pytest.mark.parametrize('length_proof',[None,'8 м','6 м'])
def test_web_kg_per_piece_checks_length_before_confirmation(tmp_path,length_proof):
    source,plan=pipe_plan(diameter='219',unit='шт',amount='40',length='6000 мм')
    agent,repo,fake,_=setup(tmp_path,source,plan);old=repo.load(1)
    quote='Труба 219×6, длина '+(length_proof or 'не указана')+': 189,12 кг/шт'
    candidate=evidence_plan(quote,value='189,12')['mass_candidates'][0]
    candidate.update(unit='kg_per_piece',piece_length_evidence=length_proof)
    fake.plans.extend([calc_plan(old,query='synthetic'),dict(intent='answer_question',mass_candidates=[candidate])])
    fake.search_result=SearchAnswer(quote,[dict_source('https://example.org/table')])
    reply=agent.handle(1,'Рассчитай',event_id='piece-evidence')
    assert (reply.pending_mass is not None)==(length_proof=='6 м') and repo.load(1)==old


@pytest.mark.parametrize('bad_field,bad_value',[('value','NaN'),('value','0'),('dimensions_mm',['-1','6']),('piece_length_mm','0')])
def test_reference_adapter_rejects_invalid_numeric_values(bad_field,bad_value):
    record=MassCatalog().records[1].model_dump()
    record[bad_field]=bad_value
    with pytest.raises(ValueError):MassCatalog([record])


def test_later_failed_search_invalidates_older_mass_proposal(tmp_path):
    source,plan=pipe_plan(diameter='219');agent,repo,fake,_=setup(tmp_path,source,plan)
    pending=pending_search(agent,repo,fake).pending_mass;old=repo.load(1)
    fake.plans.append(dict(intent='web_search',search_query='a different query'))
    fake.search_result=SearchUnavailable('no_sources')
    agent.handle(1,'Другой поиск',event_id='later-failed-search')
    assert repo.mass_pending(1,old) is None
    approval=calc_plan(old,'Подтверждаю');approval.update(mass_confirmation_event_id=pending.event_id,
                                                      mass_approval_ids=[pending.candidates[0].id])
    fake.plans.extend([approval,approval])
    assert agent.handle(1,'Подтверждаю',event_id='old-proposal').status=='failed' and repo.load(1)==old
    assert len(fake.search_calls)==2


def test_mass_only_request_cannot_silently_remove_other_product_questions(tmp_path):
    plan=sheet_plan();plan['extraction']['questions']=['Уточните условия поставки']
    agent,repo,fake,_=setup(tmp_path,plan=plan);old=repo.load(1)
    invalid=calc_plan(old);invalid['remaining_questions']=[];fake.plans.extend([invalid,invalid])
    assert agent.handle(1,'Рассчитай',event_id='keep-questions').status=='failed'
    assert repo.load(1)==old and repo.load(1).questions==['Уточните условия поставки']
