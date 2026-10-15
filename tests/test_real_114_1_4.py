"""Real-message regressions: mock transports and temporary SQLite only."""
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock
import pytest
from database import Database
from schemas import RequestItem, PurchaseRequest
from repositories.telegram_repository import TelegramRepository
from telegram_service import TelegramService, exact_source_attributes, ActiveRequest
from formatter import format_normalized_request
from work_assistant import AssistantAnswer
from tests.test_dialog_review_114 import working_snapshot

FIXTURES=Path(__file__).parent/'fixtures'
ANGLES=(FIXTURES/'real_114_1_4_angles.txt').read_text(encoding='utf-8').strip().splitlines()
PIPES=(FIXTURES/'real_114_1_4_pipes.txt').read_text(encoding='utf-8').strip().splitlines()
MASS='Сколько весит 405 шт арматуры А240 диаметром 12 мм?'
AMOUNTS=['57.2','1220.8','204.8','721.5','1533.5','624']
GRADES=['Ст3сп5','С245-12','С245','С255-12','С245','С245']


def service_at(tmp_path):
    ai,assistant=Mock(),Mock()
    ai.parse_request.side_effect=RuntimeError('offline synthetic transport')
    assistant.answer.return_value=AssistantAnswer('Информация из источников',searched=True,sources=('https://example.org/standard',))
    repo=TelegramRepository(Database(tmp_path/'isolated.sqlite3'))
    return TelegramService(ai,repo,reliable=True,commercial_view=True,assistant=assistant),ai,repo,assistant


def test_strict_calculation_contract_rejects_narrow_ai_fragment(tmp_path):
    from diagnostics import SemanticExtractionError
    s,ai,r,a=service_at(tmp_path)
    ai.parse_request.side_effect=None
    ai.parse_request.return_value=PurchaseRequest(items=[RequestItem(product='Арматура',product_group='rebar',
        diameter_mm=12,grade='А240',source_text='арматуры А240 диаметром 12 мм')])
    with pytest.raises(SemanticExtractionError) as failure: s._build_active(MASS,MASS,[])
    assert failure.value.code=='missing_positions'


@pytest.mark.parametrize('reply',['11.7м','11,7 м','11700 мм'])
def test_real_mass_question_and_exact_local_result_survive_restart(tmp_path,reply):
    s,ai,r,a=service_at(tmp_path)
    messages=s.process(1,MASS)
    assert 'длин' in ' '.join(messages).casefold()
    c=r.load_calculation(1)
    assert c is not None and c.items[0].quantity==Decimal('405') and c.items[0].diameter_mm==12
    assert r.load(1) is None
    s=TelegramService(ai,TelegramRepository(Database(tmp_path/'isolated.sqlite3')),reliable=True,assistant=a)
    messages=s.process(1,reply)
    c=r.load_calculation(1)
    assert c.pending is None and c.result.total_t==Decimal('4.207788')
    assert c.result.per_metre_kg==Decimal('0.888') and c.result.basis
    assert '4,208' in ' '.join(messages) and 'теоретич' in ' '.join(messages).casefold()
    ai.parse_request.assert_not_called()
    a.answer.assert_not_called()


@pytest.mark.parametrize('message',['ты у меня спрашиваешь?','так дай вес','возьми инф из интернета, если не поддерживается из БД'])
def test_calculation_followup_keeps_expected_length_and_working_request(tmp_path,message):
    s,ai,r,a=service_at(tmp_path)
    s.process(1,ANGLES[0])
    work=working_snapshot(r,1)
    s.process(1,MASS)
    c=r.load_calculation(1)
    assert c is not None
    before=c.model_dump_json()
    messages=s.process(1,message)
    assert 'длин' in ' '.join(messages).casefold()
    assert r.load_calculation(1).model_dump_json()==before and working_snapshot(r,1)==work
    if 'интернет' in message:
        assert a.answer.call_args.kwargs['current'].calculation_id==c.calculation_id
    else: a.answer.assert_not_called()


@pytest.mark.parametrize('index',range(6))
def test_export_row_length_and_reverse_mass_are_independent(tmp_path,index):
    s,ai,r,a=service_at(tmp_path)
    s.process(1,ANGLES[index])
    active=r.load(1)
    assert len(active.items)==1
    item=active.items[0]
    assert item.length_mm==(6000 if index==0 else 12000)
    assert item.quantity==Decimal(AMOUNTS[index]) and item.unit=='кг'
    assert item.grade==GRADES[index] and item.gost_tu.count('ГОСТ')==2
    assert item.original.source_text==ANGLES[index]
    assert not active.fallback_status
    ai.parse_request.assert_not_called()


def test_six_export_rows_keep_all_sources(tmp_path):
    s,ai,r,a=service_at(tmp_path)
    text='\n'.join(ANGLES)
    messages=s.process(1,text)
    active=r.load(1)
    assert len(active.items)==6 and active.source_text==text
    by_source={i.source_position:i for i in active.items}
    for n in range(1,7):
        assert by_source[n].quantity==Decimal(AMOUNTS[n-1])
        assert by_source[n].length_mm==(6000 if n==1 else 12000)
        assert by_source[n].grade==GRADES[n-1]
    assert '0,0572 т' in '\n'.join(messages) and '1,5335 т' in '\n'.join(messages)


@pytest.mark.parametrize('index',range(8))
def test_pipe_fitting_row_keeps_quantity_and_geometry(tmp_path,index):
    s,ai,r,a=service_at(tmp_path)
    s.process(1,PIPES[index])
    active=r.load(1)
    assert len(active.items)==1
    item=active.items[0]
    assert item.diameter_mm==[159,159,133,133,133,57,57,57][index]
    assert item.quantity==Decimal([22,4,30,10,8,30,8,4][index])
    assert item.unit==('м' if index in (0,2,5) else 'шт')
    assert item.original.source_text==PIPES[index]
    if index==2: assert item.wall_thickness_mm==4
    if index in (0,5): assert item.wall_thickness_mm is None and item.wall_thickness_requirement
    assert not any(f.endswith(('.quantity','.unit')) for f in active.missing_fields)


def test_eight_rows_preserve_conditions_and_pipe_links(tmp_path):
    s,ai,r,a=service_at(tmp_path)
    s.process(1,'\n'.join(PIPES))
    active=r.load(1)
    assert len(active.items)==8
    by_source={i.source_position:i for i in active.items}
    for n,target in [(2,1),(4,3),(5,3),(7,6),(8,6)]:
        links=[x for x in by_source[n].source_conditions if x.kind=='related_pipe']
        assert links[0].related_source_position==target
        assert links[0].origin==('context' if n==8 else 'source')
    assert any(x.kind=='substitution' and x.bounds_unspecified for x in by_source[3].source_conditions)
    assert not active.unprocessed_source
    assert by_source[3].wall_thickness_mm==4
    ai.parse_request.assert_not_called()


@pytest.mark.parametrize('amount,expected',[('9.8','9,8'),('5','5'),('1.25','1,25'),('0.0572','0,0572')])
def test_commercial_quantity_display_preserves_exact_amount(amount,expected):
    from telegram_service import ActiveItem
    item=RequestItem(product='Арматура',product_group='rebar',diameter_mm=12,grade='А500С',quantity=Decimal(amount),unit='т',source_text=f'Арматура А500С ф12 — {amount} т')
    active=ActiveRequest(request_id='synthetic',source_text=item.source_text,commercial_view=True,
        items=[ActiveItem(**item.model_dump(),item_id='one',original=item)])
    assert format_normalized_request(active).endswith('— '+expected+' т')
    assert active.items[0].quantity==Decimal(amount)


@pytest.mark.parametrize('text,expected',[('Уголок 40х40х4 Ст3 — 6000 кг','6000'),
 ('Уголок 40х40х4 Ст3 — 6 000 кг','6000'),('Арматура А500С ф12 — 1 220,8 кг','1220.8')])
def test_ordinary_mass_does_not_become_length(text,expected):
    item=RequestItem(source_text=text,product_group='angle' if text.startswith('Уголок') else 'rebar')
    exact_source_attributes(item)
    assert item.quantity==Decimal(expected) and item.length_mm is None

@pytest.mark.parametrize('separator',[' ','\u00a0','\u202f'])
def test_grouped_quantity_separators_are_exact(separator):
    item=RequestItem(source_text='Арматура А500С ф12 — 1'+separator+'220,8 кг',product_group='rebar')
    exact_source_attributes(item)
    assert item.quantity==Decimal('1220.8')


@pytest.mark.parametrize('spelling',['э\\с','э/с','электросварная'])
@pytest.mark.parametrize('diameter',['д133','д 133','Ø133'])
def test_pipe_spelling_never_creates_a_steel_grade(tmp_path,spelling,diameter):
    s,ai,r,a=service_at(tmp_path)
    s.process(1,f'Труба {spelling} {diameter} стенка 4 30 метров')
    item=r.load(1).items[0]
    assert item.diameter_mm==133 and item.wall_thickness_mm==4
    assert item.grade is None and item.quantity==Decimal('30') and item.unit=='м'
    assert 'с159' not in item.product


def test_repeated_mass_question_does_not_replace_pending_calculation(tmp_path):
    s,ai,r,a=service_at(tmp_path)
    s.process(1,MASS)
    before=r.load_calculation(1).model_dump_json()
    s.process(1,MASS)
    assert r.load_calculation(1).model_dump_json()==before
    ai.parse_request.assert_not_called()


def test_unknown_coefficient_is_not_invented_or_silently_asked_of_ai(tmp_path):
    s,ai,r,a=service_at(tmp_path)
    s.process(1,MASS.replace('А240','А500С'))
    result=' '.join(s.process(1,'11,7 м'))
    assert 'не подтверждён' in result and 'расчёт не выполнен' in result
    assert r.load_calculation(1).result is None
    a.answer.assert_not_called()
    s.process(1,'найди коэффициент в интернете')
    assert a.answer.call_args.kwargs['current'].items[0].grade=='А500С'
    assert r.load_calculation(1).result is None


def test_unrelated_internet_question_is_not_intercepted_by_calculation(tmp_path):
    s,ai,r,a=service_at(tmp_path)
    s.process(1,MASS)
    before=r.load_calculation(1).model_dump_json()
    s.process(1,'Найди в интернете правила шахмат')
    assert a.answer.call_args.kwargs['current'] is None
    assert r.load_calculation(1).model_dump_json()==before


def test_unconfirmed_reference_does_not_modify_standard_or_grade(tmp_path):
    s,ai,r,a=service_at(tmp_path)
    s.process(1,MASS)
    s.process(1,'11.7м')
    c=r.load_calculation(1)
    assert c.items[0].gost_tu is None and c.items[0].grade=='А240'
    assert c.result.basis and c.result.status=='estimate'
    assert r.load(2) is None and r.load_calculation(2) is None


def test_ambiguous_quantities_and_wall_remain_in_original(tmp_path):
    s,ai,r,a=service_at(tmp_path)
    text='Труба э/с д133 стенка 4 стенка 5 22 метра 30 метров'
    messages=s.process(1,text)
    active=r.load(1)
    assert active.source_text==text and active.unprocessed_source==[text]
    assert text in '\n'.join(messages)
    assert not active.items


def test_pipe_conditions_and_source_links_survive_reload(tmp_path):
    s,ai,r,a=service_at(tmp_path)
    s.process(1,'\n'.join(PIPES))
    initial=r.load(1).model_dump_json()
    s=TelegramService(ai,TelegramRepository(Database(tmp_path/'isolated.sqlite3')),reliable=True,assistant=a)
    assert s.repository.load(1).model_dump_json()==initial
    by_source={i.source_position:i for i in s.repository.load(1).items}
    assert by_source[3].original.wall_thickness_mm==4
    assert any(c.kind=='substitution' for c in by_source[3].original.source_conditions)


def test_two_preceding_matching_pipes_do_not_create_automatic_link(tmp_path):
    s,ai,r,a=service_at(tmp_path)
    s.process(1,'Труба э/с д133 стенка 4 10 метров\nТруба э/с д133 стенка 5 20 метров\nОтвод д133 к этой трубе 4 шт')
    fitting=next(i for i in r.load(1).items if i.product_group=='fitting')
    assert fitting.source_conditions[0].related_source_position is None


def test_full_input_mixed_with_unknown_row_is_preserved(tmp_path):
    s,ai,r,a=service_at(tmp_path)
    text=PIPES[2]+'\nСпецизделие XYZ непонятный тип — 3 шт'
    messages=s.process(1,text)
    active=r.load(1)
    assert active.source_text==text
    assert any(i.quantity==Decimal('30') for i in active.items)
    assert active.unprocessed_source
    assert text in '\n'.join(messages)



def test_real_coil_example_has_compact_quantity_and_preserves_original(tmp_path):
    s,ai,r,a=service_at(tmp_path)
    text='Арматура А500С 12 бухта - 9,8т'
    ai.parse_request.side_effect=None
    ai.parse_request.return_value=PurchaseRequest(items=[RequestItem(product='Арматура',product_group='rebar',
        grade='А500С',diameter_mm=12,form='бухта',source_text=text)])
    messages=s.process(1,text)
    assert '9,8 т' in '\n'.join(messages) and '9,800' not in '\n'.join(messages)
    item=r.load(1).items[0]
    assert item.quantity==Decimal('9.8') and item.form=='бухта' and item.original.source_text==text


def test_coated_rebar_does_not_reuse_unqualified_reference_coefficient():
    from quantity import calculate_rebar_mass
    item=RequestItem(product='Арматура',product_group='rebar',grade='А240',diameter_mm=12,length_mm=11700,
                     quantity=405,unit='шт',processing='оцинкованная')
    assert calculate_rebar_mass(item).status=='unavailable'


def test_metres_are_calculated_without_inventing_piece_length(tmp_path):
    s,ai,r,a=service_at(tmp_path)
    messages=s.process(1,'Сколько весит 10 метров арматуры А240 диаметром 12 мм?')
    assert r.load_calculation(1).result.total_t==Decimal('0.00888')
    assert r.load_calculation(1).items[0].length_mm is None
    assert 'теоретич' in '\n'.join(messages).casefold()


def test_failed_new_calculation_keeps_the_previous_one(tmp_path):
    s,ai,r,a=service_at(tmp_path)
    s.process(1,MASS)
    before=r.load_calculation(1).model_dump_json()
    s.process(1,'Сколько весит неизвестное изделие 123?')
    assert r.load_calculation(1).model_dump_json()==before
