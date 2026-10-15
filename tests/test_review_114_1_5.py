from decimal import Decimal
import pytest
from schemas import RequestItem, PurchaseRequest
from telegram_operator import local_request_draft, bind_pipe_relations
from telegram_service import TelegramService
from tests.test_real_114_1_4 import service_at, ANGLES, PIPES, MASS
from tests.test_dialog_review_114 import working_snapshot


@pytest.mark.parametrize('explicit', [False, True])
@pytest.mark.parametrize('pipes,after,target', [([133],False,1), ([133,133],False,None), ([57],False,None), ([133],True,None)])
def test_relation_evidence_and_ambiguity(explicit,pipes,after,target):
    fitting=local_request_draft('Отвод д133 '+('к этой трубе ' if explicit else '')+'4 шт').items[0]
    tubes=[local_request_draft(f'Труба э/с д{d} стенка 4 10 метров').items[0] for d in pipes]
    items=[fitting,*tubes] if after else [*tubes,fitting]
    bind_pipe_relations(items)
    links=[c for c in fitting.source_conditions if c.kind=='related_pipe']
    if explicit or target:
        assert len(links)==1
        assert links[0].origin==('source' if explicit else 'context')
        assert links[0].related_source_position==target
    else: assert not links
    assert fitting.wall_thickness_mm is None and fitting.gost_tu is None and fitting.grade is None


def incomplete_ai_draft():
    rows=[PIPES[2],PIPES[3],ANGLES[0]]
    items=[local_request_draft(row).items[0] for row in rows]
    items[0].wall_thickness_mm=None
    items[0].source_conditions=[]
    items[1].source_conditions=[]
    items[2].length_mm=999
    items[2].quantity=Decimal('6000')
    items[2].grade=None
    items[2].gost_tu=None
    # Round bar is deliberately outside the local recognizer's supported set.
    row='Круг 20 Ст3 — 2 т'
    items.append(RequestItem(product='Круг',product_group='round_bar',diameter_mm=20,grade='Ст3',quantity=2,unit='т',source_text=row))
    return '\n'.join([*rows,row]),PurchaseRequest(items=items)


@pytest.mark.parametrize('recover',[False,True])
@pytest.mark.parametrize('reverse',[False,True])
def test_ai_fallback_restores_literal_conditions_and_source_links(tmp_path,recover,reverse):
    s,ai,r,a=service_at(tmp_path)
    text,draft=incomplete_ai_draft()
    assert local_request_draft(text) is None
    if reverse: draft.items.reverse()
    if recover:
        text+='\nСпецизделие XYZ непонятный тип — 3 шт'
    ai.parse_request.side_effect=None
    ai.parse_request.return_value=draft
    messages=s.process(1,text)
    active=r.load(1)
    by_source={i.source_text:i for i in active.items}
    pipe=by_source[PIPES[2]]
    assert pipe.wall_thickness_mm==4
    assert any(c.kind=='substitution' and c.origin=='source' for c in pipe.source_conditions)
    assert pipe.original.wall_thickness_mm==4 and pipe.quantity==Decimal('30') and pipe.unit=='м'
    fitting=by_source[PIPES[3]]
    assert any(c.kind=='related_pipe' and c.origin=='source' and c.related_source_position==1 for c in fitting.source_conditions)
    angle=by_source[ANGLES[0]]
    assert angle.length_mm==6000 and angle.quantity==Decimal('57.2') and angle.grade=='Ст3сп5'
    assert angle.gost_tu.count('ГОСТ')==2
    assert active.source_text==text and len(active.items)==4
    if recover:
        assert active.fallback_status=='partial' and not active.is_valid
        assert text in '\n'.join(messages)
    from telegram_service import ActiveRequest
    assert ActiveRequest.model_validate_json(active.model_dump_json())==active
    assert sorted(i.source_position for i in active.items)==[1,2,3,4]
    ai.parse_request.assert_called_once()


def test_calculation_full_chain_keeps_context_after_restart_and_search(tmp_path):
    s,ai,r,a=service_at(tmp_path)
    s.process(1,ANGLES[0])
    before=working_snapshot(r,1)
    s.process(1,MASS)
    assert r.load_calculation(1).items[0].length_mm is None
    s.process(1,'11,7 м')
    c=r.load_calculation(1)
    assert c.result.total_t==Decimal('4.207788')
    assert 'теоретич' in ' '.join(s.process(1,'так дай вес')).casefold()
    s=TelegramService(ai,r,reliable=True,commercial_view=True,assistant=a)
    s.process(1,'Найди нормативный источник в интернете')
    assert a.answer.call_args.kwargs['current'].calculation_id==c.calculation_id
    assert r.load_calculation(1).model_dump_json()==c.model_dump_json()
    s.process(1,MASS)
    assert r.load_calculation(1).model_dump_json()==c.model_dump_json()
    assert working_snapshot(r,1)==before
    assert r.load_calculation(2) is None
    s.process(1,'Уточнение текущей')
    assert working_snapshot(r,1)==before
    ai.parse_request.assert_not_called()


@pytest.mark.parametrize('source',[
    'Труба э/с д133 стенка 4 стенка 5 30 метров',
    'Труба э/с д133 стенка 4 специальное исполнение XYZ 30 метров',
])
def test_ai_cannot_hide_conflicting_or_unprojected_source_features(tmp_path,source):
    s,ai,r,a=service_at(tmp_path)
    ai.parse_request.side_effect=None
    ai.parse_request.return_value=PurchaseRequest(items=[RequestItem(product='Труба э/с',
        product_group='round_pipe',diameter_mm=133,wall_thickness_mm=4,
        quantity=30,unit='м',source_text=source)])
    messages=s.process(1,source)
    active=r.load(1)
    assert active.source_text==source and active.unprocessed_source==[source]
    assert not active.is_valid and source in '\n'.join(messages)
    assert not active.items


def test_unrelated_normative_source_question_is_not_calculation_followup(tmp_path):
    s,ai,r,a=service_at(tmp_path)
    s.process(1,MASS)
    before=r.load_calculation(1).model_dump_json()
    s.process(1,'Найди нормативный источник для листа г/к в интернете')
    assert a.answer.call_args.kwargs['current'] is None
    assert r.load_calculation(1).model_dump_json()==before


def test_parallel_users_keep_independent_calculation_answers(tmp_path):
    s,ai,r,a=service_at(tmp_path)
    s.process(1,MASS)
    s.process(2,MASS)
    s.process(1,'11,7 м')
    assert r.load_calculation(1).result.total_t==Decimal('4.207788')
    assert r.load_calculation(2).pending.field=='length_mm'
    assert r.load_calculation(2).items[0].length_mm is None
    s.process(2,'6 м')
    assert r.load_calculation(2).result.total_t==Decimal('2.15784')
    assert r.load_calculation(1).result.total_t==Decimal('4.207788')
    ai.parse_request.assert_not_called()



def test_all_pipe_conditions_survive_successful_mixed_ai_extraction(tmp_path):
    s,ai,r,a=service_at(tmp_path)
    text,draft=incomplete_ai_draft()
    circle=draft.items[-1]
    items=[local_request_draft(row).items[0] for row in PIPES]
    for item in items:
        item.diameter_mm=None
        item.wall_thickness_mm=None
        item.wall_thickness_requirement=None
        item.source_conditions=[]
        item.quantity=None
        item.unit=None
    source='\n'.join([*PIPES,circle.source_text])
    ai.parse_request.side_effect=None
    ai.parse_request.return_value=PurchaseRequest(items=[circle,*reversed(items)])
    s.process(1,source)
    saved=r.load(1)
    by_source={i.source_position:i for i in saved.items}
    assert len(by_source)==9 and not saved.fallback_status
    for n in (1,6):
        assert by_source[n].wall_thickness_mm is None
        assert any(c.kind=='wall_selection' and c.bounds_unspecified for c in by_source[n].source_conditions)
    for n,target in [(2,1),(4,3),(5,3),(7,6),(8,6)]:
        link=next(c for c in by_source[n].source_conditions if c.kind=='related_pipe')
        assert link.related_source_position==target and link.origin==('context' if n==8 else 'source')
        assert by_source[n].original.source_conditions==by_source[n].source_conditions
    assert by_source[3].wall_thickness_mm==4
    assert [by_source[n].quantity for n in range(1,9)]==list(map(Decimal,['22','4','30','10','8','30','8','4']))
    assert [by_source[n].diameter_mm for n in range(1,9)]==[159,159,133,133,133,57,57,57]
    ai.parse_request.assert_called_once()
