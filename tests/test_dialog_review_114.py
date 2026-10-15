"""Review regressions: independent calculations, bounded history, typed replies."""
from unittest.mock import Mock
from decimal import Decimal
import pytest
from database import Database
from repositories.telegram_repository import TelegramRepository
from telegram_service import TelegramService, MessagePlan
from request_operations import request_fingerprint
from work_assistant import WorkAssistantService, AssistantAnswer
from tests.test_dialog_context_114 import context, ANGLE, PIPE, MASS


def working_snapshot(repo, user_id):
    with repo.database.connection() as c:
        return (repo.load(user_id).model_dump_json(), list(c.execute(
            'SELECT id,request_json FROM telegram_history WHERE user_id=? ORDER BY id',(user_id,))))


@pytest.mark.parametrize('source', [ANGLE, PIPE], ids=['ready','pending'])
@pytest.mark.parametrize('restart', [False, True])
def test_calculation_preserves_working_request_and_history(tmp_path,source,restart):
    service, ai, repo, assistant = context(tmp_path)
    service.process(1,source)
    before = working_snapshot(repo,1)
    service.process(1,MASS)
    assert working_snapshot(repo,1) == before
    if restart:
        service = TelegramService(ai,TelegramRepository(Database(tmp_path/'dialog.sqlite3')),reliable=True,assistant=assistant)
    assistant.answer.return_value = AssistantAnswer('405 × 11,7 × 0,888 / 1000 = 4,207788 т')
    messages = service.process(1,'11.7м')
    assert any('4,207788' in m for m in messages)
    assert working_snapshot(repo,1) == before
    calculation = assistant.answer.call_args.kwargs['current']
    assert calculation.items[0].quantity == Decimal('405')
    assert calculation.items[0].diameter_mm == 12
    assert calculation.items[0].length_mm == 11700


def test_repository_history_skips_long_recent_turns_and_fills_from_older(tmp_path):
    repo = TelegramRepository(Database(tmp_path/'history.sqlite3'))
    for n in range(12):repo.save_dialog(1,f'q{n}',f'a{n}')
    for n in range(12):repo.save_dialog(1,'long'+str(n),'X'*9000)
    messages = repo.load_dialog(1)
    assert len(messages) == 20
    assert messages[0]['content'] == 'q2' and messages[-1]['content'] == 'a11'


def test_assistant_history_skips_oversized_turn_without_splitting_pairs():
    history = [{'role':'user','content':'first'},{'role':'assistant','content':'answer'},
               {'role':'user','content':'oversized'},{'role':'assistant','content':'X'*13000},
               {'role':'user','content':'last'},{'role':'assistant','content':'latest'}]
    assert WorkAssistantService._history(history) == history[:2]+history[-2:]


@pytest.mark.parametrize('field,reply,expected', [
    ('grade','Ст3сп5','Ст3сп5'),('grade','09Г2С','09Г2С'),('grade','С345-6','С345-6'),
    ('gost_tu','ГОСТ 27772-2021','ГОСТ 27772-2021')])
@pytest.mark.parametrize('interruption',[False,True])
def test_obvious_technical_reply_uses_bound_field_without_ai(tmp_path,field,reply,expected,interruption):
    service, ai, repo, assistant = context(tmp_path)
    service.process(1,ANGLE)
    old = repo.load(1)
    question = 'Уточните марку стали исходной позиции 1.' if field=='grade' else 'Уточните ГОСТ исходной позиции 1.'
    service._begin_clarification(1,old,ANGLE,MessagePlan(intent='clarify',question=question))
    if interruption:service.process(1,'Какие комбинации в покере?')
    messages = service.process(1,reply)
    active = repo.load(1)
    assert getattr(active.items[0],field) == expected
    assert active.request_id == old.request_id
    assert active.items[0].original == old.items[0].original
    ai.interpret_message.assert_not_called()
    ai.classify_dialog.assert_not_called()
    ai.parse_request.assert_called_once()


@pytest.mark.parametrize('reply,amount,unit',[('5 т','5','т'),('120 шт','120','шт'),('350 кг','350','кг')])
def test_quantity_and_unit_reply_is_applied_together(tmp_path,reply,amount,unit):
    service, ai, repo, assistant = context(tmp_path)
    service.process(1,'Уголок 50х50х5 Ст3')
    before = repo.load(1).request_id
    service.process(1,reply)
    active = repo.load(1)
    assert active.request_id == before
    assert active.items[0].quantity == Decimal(amount) and active.items[0].unit == unit
    ai.interpret_message.assert_not_called()
    ai.classify_dialog.assert_not_called()



def test_new_calculation_does_not_inherit_previous_or_other_user(tmp_path):
    service, ai, repo, assistant = context(tmp_path)
    service.process(1,MASS)
    first = repo.load_calculation(1)
    assert repo.load_calculation(2) is None
    service.process(2,MASS.replace('405','120'))
    service.process(1,MASS.replace('405','10'))
    latest = repo.load_calculation(1)
    assert latest.calculation_id != first.calculation_id
    assert latest.items[0].quantity == Decimal('10') and latest.items[0].length_mm is None
    service.process(1,'11.7м')
    assert repo.load_calculation(2).pending is not None
    assert repo.load_calculation(2).items[0].quantity == Decimal('120')
    assert repo.load(1) is None and repo.load(2) is None


def test_calculation_can_ask_quantity_then_length_without_working_state(tmp_path):
    service, ai, repo, assistant = context(tmp_path)
    messages = service.process(1,'Сколько весит арматура ф12 А240?')
    assert any('количеств' in m.casefold() for m in messages)
    service.process(1,'120 шт')
    assert repo.load_calculation(1).pending.field == 'length_mm'
    service = TelegramService(ai,TelegramRepository(Database(tmp_path/'dialog.sqlite3')),reliable=True,assistant=assistant)
    service.process(1,'11.7м')
    assert repo.load_calculation(1).items[0].quantity == Decimal('120')
    assert repo.load_calculation(1).items[0].length_mm == 11700
    assert repo.load(1) is None


def test_new_working_spec_suspends_old_calculation(tmp_path):
    service, ai, repo, assistant = context(tmp_path)
    service.process(1,MASS)
    service.process(1,PIPE)
    assert repo.load_calculation(1).suspended
    service.process(1,'6 мм')
    assert repo.load(1).items[0].wall_thickness_mm == 6
    assert repo.load_calculation(1).items[0].length_mm is None


def test_history_pagination_gaps_scope_and_restart(tmp_path):
    repo = TelegramRepository(Database(tmp_path/'history.sqlite3'))
    for n in range(40):
        repo.save_dialog(1,f'q{n}',f'a{n}','latest')
        repo.save_dialog(1,'OLD SPECIFICATION','old answer','previous')
        repo.save_dialog(2,'OTHER USER','private')
    repo.save_dialog(1,'TOO LONG','X'*9000,'latest')
    with repo.database.connection() as c:
        c.execute("DELETE FROM telegram_state WHERE key IN ('dialog:1:0:000000000079','dialog:1:0:000000000077')")
    repo = TelegramRepository(Database(tmp_path/'history.sqlite3'))
    messages = repo.load_dialog(1,'latest')
    assert len(messages) == 20
    text = str(messages)
    assert 'OLD SPECIFICATION' not in text and 'OTHER USER' not in text and 'TOO LONG' not in text
    assert messages[-1]['content'] == 'a37'
    assert repo.load_dialog(1,'latest',epoch=100) == []


def test_history_character_budget_keeps_whole_pairs_and_uses_older_small_turns():
    history = [m for q,a in [('old-small','answer'),('large','X'*7900),('latest','Y'*7900)]
               for m in [{'role':'user','content':q},{'role':'assistant','content':a}]]
    result = WorkAssistantService._history(history)
    assert [m['content'] for m in result] == ['old-small','answer','latest','Y'*7900]
    assert sum(len(m['content']) for m in result) <= 12000


@pytest.mark.parametrize('reply,expected',[('09Г2С вместо С345','09Г2С'),('допускается С355','С345 (или С355)')])
def test_explicit_alternative_is_not_an_automatic_material_alias(tmp_path,reply,expected):
    service, ai, repo, assistant = context(tmp_path)
    source = ANGLE.replace('Ст3','С345')
    service.process(1,source)
    old = repo.load(1)
    service._begin_clarification(1,old,source,MessagePlan(intent='clarify',question='Уточните марку стали исходной позиции 1.'))
    service.process(1,reply)
    active = repo.load(1)
    assert active.items[0].grade == expected
    assert active.items[0].original.grade == 'С345' and active.source_text == source
    ai.interpret_message.assert_not_called()
    ai.classify_dialog.assert_not_called()


@pytest.mark.parametrize('bad_binding',['request','fingerprint','user'])
def test_stale_or_wrong_owner_grade_reply_cannot_change_request(tmp_path,bad_binding):
    service, ai, repo, assistant = context(tmp_path)
    service.process(1,ANGLE)
    service._begin_clarification(1,repo.load(1),ANGLE,MessagePlan(intent='clarify',question='Уточните марку стали исходной позиции 1.'))
    active = repo.load(1)
    if bad_binding=='request':active.pending_operation.request_id='other'
    elif bad_binding=='fingerprint':active.items[0].quantity=Decimal('99')
    else:active.pending_operation.user_id=2
    repo.save(1,active)
    before = working_snapshot(repo,1)
    service.process(1,'09Г2С')
    assert working_snapshot(repo,1)==before


def test_multiple_grade_targets_require_address(tmp_path):
    service, ai, repo, assistant = context(tmp_path)
    service.process(1,ANGLE)
    service.process(1,'Дополни заявку: '+ANGLE.replace('2 т','3 т'))
    active = repo.load(1)
    service._begin_clarification(1,active,ANGLE,MessagePlan(intent='clarify',question='Уточните марки стали обеих исходных позиций.'))
    before = working_snapshot(repo,1)
    service.process(1,'09Г2С')
    assert working_snapshot(repo,1)==before
    ai.interpret_message.assert_not_called()


def test_conflicting_known_quantity_is_not_replaced_by_unit_answer(tmp_path):
    service, ai, repo, assistant = context(tmp_path)
    service.process(1,'Уголок 50х50х5 Ст3')
    active = repo.load(1)
    active.items[0].quantity=Decimal('405')
    active.pending_clarification.fingerprint=request_fingerprint(active)
    repo.save(1,active)
    before=working_snapshot(repo,1)
    service.process(1,'5 т')
    assert working_snapshot(repo,1)==before



def test_missing_field_binding_survives_display_sorting(tmp_path):
    from schemas import RequestItem, PurchaseRequest
    service, ai, repo, assistant = context(tmp_path)
    ai.parse_request.side_effect = None
    ai.parse_request.return_value = PurchaseRequest(items=[
        RequestItem(product='Уголок',product_group='angle',size_designation='50х50х5',source_text=ANGLE),
        RequestItem(product='Труба профильная',product_group='profile_pipe',width_mm=140,height_mm=140,size_designation='140х140',source_text=PIPE)])
    service.process(1,ANGLE+'\n'+PIPE)
    service.process(1,'6 мм')
    active = repo.load(1)
    pipe = next(i for i in active.items if i.product_group=='profile_pipe')
    angle = next(i for i in active.items if i.product_group=='angle')
    assert pipe.wall_thickness_mm == 6 and angle.wall_thickness_mm is None



def test_quantity_question_rejects_stale_version(tmp_path):
    service, ai, repo, assistant = context(tmp_path)
    service.process(1,'Уголок 50х50х5 Ст3')
    active=repo.load(1)
    assert active.pending_clarification.fingerprint
    active.items[0].grade='09Г2С'
    repo.save(1,active)
    before=working_snapshot(repo,1)
    service.process(1,'5 т')
    assert working_snapshot(repo,1)==before


def test_standard_cannot_be_changed_by_unbound_model_edit(tmp_path):
    from telegram_service import Edit
    from diagnostics import PipelineError
    service, ai, repo, assistant = context(tmp_path)
    service.process(1,ANGLE)
    active=repo.load(1)
    before=working_snapshot(repo,1)
    with pytest.raises(PipelineError):
        service._edit(1,active,MessagePlan(intent='edit',edits=[Edit(item_id=active.items[0].item_id,field='gost_tu',value='ГОСТ 27772-2021')]),command='ГОСТ 27772-2021')
    assert working_snapshot(repo,1)==before



@pytest.mark.parametrize('reply',['Ст3сп5','5','ГОСТ 27772-2021'])
def test_calculation_reply_never_falls_into_working_pending_field(tmp_path,reply):
    service, ai, repo, assistant = context(tmp_path)
    service.process(1,ANGLE)
    service._begin_clarification(1,repo.load(1),ANGLE,MessagePlan(intent='clarify',question='Уточните марку стали исходной позиции 1.'))
    before=working_snapshot(repo,1)
    service.process(1,MASS)
    service.process(1,reply)
    assert working_snapshot(repo,1)==before
    service.process(1,'Уточнение текущей')
    service.process(1,'Ст3сп5')
    assert repo.load(1).items[0].grade=='Ст3сп5'
