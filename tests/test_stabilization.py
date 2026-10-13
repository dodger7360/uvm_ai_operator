from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch
import httpx2
from openai import APIConnectionError, AuthenticationError, BadRequestError
from pydantic import ValidationError
from ai import AIService
from database import Database
from repositories.telegram_repository import TelegramRepository
from telegram_service import TelegramService, MessagePlan, Edit, is_new_request
from schemas import PurchaseRequest, PurchaseRequestExtraction, RequestItem
from diagnostics import SemanticExtractionError, PipelineError
from clarifications import PendingClarification, ClarificationOption, question_state


FIXTURES=Path(__file__).parent/'fixtures'


def response(extraction):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=extraction,refusal=None))],model='gpt-6-luna',
        usage=SimpleNamespace(prompt_tokens=120,completion_tokens=300,total_tokens=420,
                             prompt_tokens_details=SimpleNamespace(cached_tokens=20),completion_tokens_details=SimpleNamespace(reasoning_tokens=100)))


class StabilizationTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.repo=TelegramRepository(Database(Path(self.folder.name)/'requests.sqlite3'))
        self.ai=Mock()
        self.service=TelegramService(self.ai,self.repo)

    def pipes(self):
        rows=[]
        for line in (FIXTURES/'pipes_5_original.txt').read_text(encoding='utf-8').splitlines():
            size=line.split()[1]
            quantity=line.rsplit('-',1)[1].strip().split()[0]
            rows.append(RequestItem(product='Труба',product_group='round_pipe',size_designation=size,
                grade='Ст3 (или Ст20)',gost_tu='ГОСТ 10704-91',quantity=quantity,unit='тн',source_text=line))
        return PurchaseRequest(items=rows)

    def start(self,user=1):
        text='Уголок 75×75×6 С345-6 ГОСТ 8509-93 — 0,0015 т'
        self.ai.parse_request.return_value=PurchaseRequest(items=[RequestItem(product='Уголок',product_group='angle',size_designation='75×75×6',grade='С345-6',gost_tu='ГОСТ 8509-93',quantity='0.0015',unit='т',source_text=text)])
        self.service.process(user,text)
        return self.repo.load(user)

    def pending_pipe(self,user=1):
        text='Труба профильная 70×70×5'
        self.ai.parse_request.return_value=PurchaseRequest(items=[RequestItem(product='Труба профильная',product_group='profile_pipe',size_designation='70×70×5',source_text=text)])
        self.service.process(user,text)
        return self.repo.load(user)

    def test_no_medium_when_missing_quantity_even_if_model_source_failed(self):
        self.ai.parse_request.side_effect=SemanticExtractionError('source_fragment_mismatch','Cannot map source')
        with self.assertRaises(SemanticExtractionError):
            self.service.process(1,'Труба профильная 70×70×5')
        self.assertEqual(self.ai.parse_request.call_count,1)

    def test_explicit_grade_edit_not_consumed_by_pending_quantity(self):
        active=self.pending_pipe()
        self.ai.interpret_message.return_value=MessagePlan(intent='edit',edits=[Edit(item_id=active.items[0].item_id,field='grade',value='Ст3')])
        self.service.process(1,'В первой позиции измени марку на Ст3')
        self.assertEqual(self.repo.load(1).items[0].grade,'Ст3')
        self.assertIsNotNone(self.repo.load(1).pending_clarification)

    def test_model_quantity_question_accepts_number_once(self):
        self.start()
        self.ai.interpret_message.return_value=MessagePlan(intent='clarify',question='Сколько требуется единиц? Укажите количество.')
        self.service.process(1,'Хочу уточнить количество')
        self.assertEqual(self.repo.load(1).pending_clarification.field,'quantity')
        self.service.process(1,'28')
        self.assertEqual(self.repo.load(1).items[0].quantity,Decimal(28))
        self.assertIsNone(self.repo.load(1).pending_clarification)

    def test_legacy_active_request_gets_missing_quantity_question(self):
        active=self.pending_pipe()
        active.pending_clarification=None
        self.repo.save(1,active)
        self.service.process(1,'в штуках')
        self.service.process(1,'28')
        self.assertEqual(self.repo.load(1).items[0].quantity,Decimal(28))
        self.assertIsNone(self.repo.load(1).pending_clarification)

    def test_five_original_pipes_exact_result(self):
        self.ai.parse_request.return_value=self.pipes()
        output=self.service.process(1,(FIXTURES/'pipes_5_original.txt').read_text(encoding='utf-8'))
        self.assertEqual('\n'.join(output),(FIXTURES/'pipes_5_expected.txt').read_text(encoding='utf-8').strip())
        active=self.repo.load(1)
        self.assertEqual(len(active.items),5)
        self.assertTrue(all(i.grade=='Ст3 (или Ст20)' for i in active.items))
        self.assertEqual(active.items[-1].quantity,Decimal('71.380'))
        self.assertIsNotNone(active.initial_extraction)
        self.ai.interpret_message.assert_not_called()

    def test_five_pipes_replace_existing_context_without_classifier(self):
        old=self.start()
        self.ai.parse_request.return_value=self.pipes()
        self.service.process(1,(FIXTURES/'pipes_5_original.txt').read_text(encoding='utf-8'))
        self.assertNotEqual(self.repo.load(1).request_id,old.request_id)
        self.assertEqual(len(self.repo.load(1).items),5)
        self.ai.interpret_message.assert_not_called()

    def test_one_new_position_has_priority(self):
        self.start()
        text='Труба 159×6 С255-4 — 0,0072 т'
        self.ai.parse_request.return_value=PurchaseRequest(items=[RequestItem(product='Труба',product_group='round_pipe',size_designation='159×6',grade='С255-4',quantity='0.0072',unit='т',source_text=text)])
        output=self.service.process(1,text)
        self.assertIn('0,0072 т',output[0])
        self.assertEqual(self.repo.load(1).items[0].grade,'С255-4')
        self.ai.interpret_message.assert_not_called()

    def test_explicit_edit_does_not_become_new_request(self):
        self.assertFalse(is_new_request('В трубе 159×6 измени количество на 2 т'))
        self.assertTrue(is_new_request('Труба 159×6 — 2 т'))

    def test_alternative_grade_is_not_ambiguous_grade_error(self):
        from telegram_service import exact_source_attributes
        item=self.pipes().items[0]
        exact_source_attributes(item)
        self.assertEqual(item.grade,'Ст3 (или Ст20)')

    def test_two_unscoped_grades_require_user_choice_without_medium(self):
        from telegram_service import exact_source_attributes
        item=self.pipes().items[0]
        item.source_text=item.source_text.replace('(или ст.20)','ст.20')
        with self.assertRaises(PipelineError) as error:
            exact_source_attributes(item)
        self.assertNotIsInstance(error.exception,SemanticExtractionError)

    def test_quantity_not_taken_from_pipe_wall_before_unit(self):
        from telegram_service import exact_source_attributes
        item=RequestItem(product='Труба профильная',product_group='profile_pipe',size_designation='70×70×5',unit='шт',source_text='Труба профильная 70×70×5 шт')
        exact_source_attributes(item)
        self.assertIsNone(item.quantity)

    def test_quantity_unit_clarification_sequence_no_ai(self):
        current=self.pending_pipe()
        self.assertEqual(current.pending_clarification.field,'unit')
        self.ai.reset_mock()
        self.service.process(1,'70х70х5 в штуках')
        current=self.repo.load(1)
        self.assertEqual(current.items[0].unit,'шт')
        self.assertEqual(current.pending_clarification.field,'quantity')
        output=self.service.process(1,'28')
        current=self.repo.load(1)
        self.assertEqual(current.items[0].quantity,Decimal(28))
        self.assertIsNone(current.pending_clarification)
        self.assertEqual(current.items[0].size_designation,'70×70×5')
        self.assertIn('28 шт.',output[0])
        self.ai.parse_request.assert_not_called()
        self.ai.interpret_message.assert_not_called()
        self.assertEqual(self.service.process(1,'1'),['Нет ожидающего уточнения. Отправьте новую заявку или укажите позицию и поле для изменения.'])

    def test_yes_confirms_proposed_number_and_unit(self):
        active=self.pending_pipe()
        active.pending_clarification=question_state('Верно, что требуется 28 шт.?',active)
        self.repo.save(1,active)
        self.service.process(1,'да')
        active=self.repo.load(1)
        self.assertEqual(active.items[0].quantity,Decimal(28))
        self.assertEqual(active.items[0].unit,'шт')
        self.assertIsNone(active.pending_clarification)

    def test_no_rejects_proposal_without_applying_it(self):
        active=self.pending_pipe()
        active.pending_clarification=question_state('Верно, что требуется 28 шт.?',active)
        self.repo.save(1,active)
        self.service.process(1,'нет')
        active=self.repo.load(1)
        self.assertIsNone(active.items[0].quantity)
        self.assertEqual(active.pending_clarification.kind,'number')

    def test_number_selects_typed_option(self):
        active=self.pending_pipe()
        active.pending_clarification=PendingClarification(request_id=active.request_id,item_id=active.items[0].item_id,
            field='quantity',kind='choice',question='1) 28 шт.\n2) 1 т',
            options=[ClarificationOption(label='28 шт.',values={'quantity':'28','unit':'шт'}),ClarificationOption(label='1 т',values={'quantity':'1','unit':'т'})])
        self.repo.save(1,active)
        self.service.process(1,'1')
        active=self.repo.load(1)
        self.assertEqual(active.items[0].quantity,Decimal(28))
        self.assertEqual(active.items[0].unit,'шт')
        self.assertIsNone(active.pending_clarification)

    def test_unit_choice_numeric_answer_and_persistence(self):
        self.pending_pipe()
        restarted=TelegramService(self.ai,TelegramRepository(self.repo.database))
        restarted.process(1,'3')
        active=self.repo.load(1)
        self.assertEqual(active.items[0].unit,'шт')
        self.assertEqual(active.pending_clarification.field,'quantity')

    def test_pending_clarifications_isolated_between_users(self):
        self.pending_pipe(1)
        self.pending_pipe(2)
        self.service.process(1,'в штуках')
        self.assertEqual(self.repo.load(1).items[0].unit,'шт')
        self.assertIsNone(self.repo.load(2).items[0].unit)

    def test_new_request_replaces_pending_clarification(self):
        self.pending_pipe()
        self.ai.parse_request.return_value=self.pipes()
        self.service.process(1,(FIXTURES/'pipes_5_original.txt').read_text(encoding='utf-8'))
        self.assertIsNone(self.repo.load(1).pending_clarification)
        self.assertEqual(len(self.repo.load(1).items),5)

    def test_medium_once_after_completeness_failure_and_one_save(self):
        draft=self.pipes()
        incomplete=PurchaseRequest(items=draft.items[:4])
        self.ai.parse_request.side_effect=[incomplete,draft]
        self.service.process(1,(FIXTURES/'pipes_5_original.txt').read_text(encoding='utf-8'))
        self.assertEqual(self.ai.parse_request.call_count,2)
        self.assertEqual(self.ai.parse_request.call_args.kwargs['reasoning_effort'],'medium')
        with self.repo.database.connection() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM telegram_history').fetchone()[0],1)

    def test_medium_after_merged_rows(self):
        draft=self.pipes()
        merged=draft.model_copy(deep=True)
        merged.items[0].source_text+='\n'+merged.items[1].source_text
        self.ai.parse_request.side_effect=[merged,draft]
        self.service.process(1,(FIXTURES/'pipes_5_original.txt').read_text(encoding='utf-8'))
        self.assertEqual(self.ai.parse_request.call_count,2)

    def test_failed_medium_is_not_retried_forever(self):
        self.ai.parse_request.side_effect=SemanticExtractionError('source_fragment_mismatch','Cannot map source')
        with self.assertRaises(SemanticExtractionError):
            self.service.process(1,(FIXTURES/'pipes_5_original.txt').read_text(encoding='utf-8'))
        self.assertEqual(self.ai.parse_request.call_count,2)
        self.assertIsNone(self.repo.load(1))

    def test_no_medium_for_network_auth_parameter_or_program_errors(self):
        request=httpx2.Request('POST','https://api.openai.com/v1/chat/completions')
        for error in [APIConnectionError(request=request),AuthenticationError('invalid key',response=httpx2.Response(401,request=request),body={}),
                      BadRequestError('unsupported parameter',response=httpx2.Response(400,request=request),body={}),TypeError('program bug')]:
            with self.subTest(error=type(error).__name__):
                self.ai.reset_mock()
                self.ai.parse_request.side_effect=error
                with self.assertRaises(type(error)):
                    self.service.process(1,'Труба 159×6 — 1 т')
                self.assertEqual(self.ai.parse_request.call_count,1)

    def test_no_medium_for_pydantic_schema_error(self):
        try:
            RequestItem(quantity='not-number')
        except ValidationError as error:
            self.ai.parse_request.side_effect=error
        with self.assertRaises(ValidationError):
            self.service.process(1,'Труба 159×6 — 1 т')
        self.assertEqual(self.ai.parse_request.call_count,1)

    def test_missing_quantity_asks_without_medium(self):
        self.pending_pipe()
        self.assertEqual(self.ai.parse_request.call_count,1)
        self.assertIsNone(self.repo.load(1).items[0].quantity)

    def test_correction_applied_once_after_invalid_target_retry(self):
        active=self.start()
        invalid=MessagePlan(intent='edit',edits=[Edit(item_id='wrong',field='grade',value='09Г2С')])
        valid=MessagePlan(intent='edit',edits=[Edit(item_id=active.items[0].item_id,field='grade',value='09Г2С')])
        self.ai.interpret_message.side_effect=[invalid,valid]
        self.service.process(1,'Замени С345-6 на 09Г2С')
        self.assertEqual(self.repo.load(1).items[0].grade,'09Г2С')
        self.assertEqual(self.repo.load(1).items[0].original.grade,'С345-6')
        self.assertEqual(self.ai.interpret_message.call_count,2)
        with self.repo.database.connection() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM telegram_history').fetchone()[0],2)

    def test_api_calls_use_same_model_low_and_medium(self):
        with patch('ai.load_dotenv'),patch('ai.OpenAI') as constructor:
            client=Mock()
            client.beta.chat.completions.parse.return_value=response(PurchaseRequestExtraction())
            constructor.return_value=client
            service=AIService()
            service.parse_request('test')
            service.parse_request('test',reasoning_effort='medium')
            calls=client.beta.chat.completions.parse.call_args_list
            self.assertEqual([call.kwargs['model'] for call in calls],['gpt-6-luna','gpt-6-luna'])
            self.assertEqual([call.kwargs['reasoning_effort'] for call in calls],['low','medium'])
            self.assertTrue(all(call.kwargs['response_format'] is PurchaseRequestExtraction for call in calls))

    def test_classification_uses_gpt6_low_strict_existing_plan(self):
        active=self.start()
        with patch('ai.load_dotenv'),patch('ai.OpenAI') as constructor:
            client=Mock()
            client.beta.chat.completions.parse.return_value=response(MessagePlan(intent='clarify',question='Уточните позицию.'))
            constructor.return_value=client
            AIService().interpret_message('замени марку',active)
            sent=client.beta.chat.completions.parse.call_args.kwargs
            self.assertEqual(sent['model'],'gpt-6-luna')
            self.assertEqual(sent['reasoning_effort'],'low')
            self.assertIs(sent['response_format'],MessagePlan)

    def test_model_confirmation_question_creates_typed_pending(self):
        self.start()
        self.ai.interpret_message.return_value=MessagePlan(intent='clarify',question='Верно, что требуется 28 шт.?')
        self.service.process(1,'Хочу изменить количество, уточни')
        self.assertEqual(self.repo.load(1).pending_clarification.kind,'confirmation')
        self.service.process(1,'да')
        self.assertEqual(self.repo.load(1).items[0].quantity,Decimal(28))

    def test_source_snapshot_does_not_get_modified(self):
        self.ai.parse_request.return_value=self.pipes()
        self.service.process(1,(FIXTURES/'pipes_5_original.txt').read_text(encoding='utf-8'))
        active=self.repo.load(1)
        self.assertEqual(active.source_text,(FIXTURES/'pipes_5_original.txt').read_text(encoding='utf-8'))
        self.assertEqual(active.initial_extraction.items[0].size_designation,'530*10')
        self.assertEqual(active.items[-1].size_designation,'530×10')

    def test_diagnostics_does_not_log_request_or_raw_exception(self):
        with self.assertLogs('operator_diagnostics',level='INFO') as log:
            self.ai.parse_request.side_effect=TypeError('PRIVATE_SECRET_AND_REQUEST')
            with self.assertRaises(TypeError):
                self.service.process(1,'Труба 159×6 — 1 т')
        content='\n'.join(log.output)
        self.assertNotIn('PRIVATE_SECRET_AND_REQUEST',content)
        self.assertNotIn('Труба 159',content)
        self.assertIn('program_error',content)
        self.assertIn('extraction',content)

    def test_usage_and_configured_cost_are_logged(self):
        text='Уголок 75×75×6 — 1 т'
        extraction=PurchaseRequestExtraction(items=[RequestItem(product='Уголок',product_group='angle',size_designation='75×75×6',quantity='1',unit='т',source_text=text)])
        with patch.dict('os.environ',{'OPENAI_INPUT_USD_PER_MILLION':'0.1','OPENAI_CACHED_INPUT_USD_PER_MILLION':'0.01','OPENAI_OUTPUT_USD_PER_MILLION':'0.5'}),patch('ai.load_dotenv'),patch('ai.OpenAI') as constructor:
            client=Mock()
            client.beta.chat.completions.parse.return_value=response(extraction)
            constructor.return_value=client
            service=TelegramService(AIService(),self.repo)
            with self.assertLogs('operator_diagnostics',level='INFO') as log:
                service.process(1,text)
            content='\n'.join(log.output)
            self.assertIn('"prompt_tokens": 120',content)
            self.assertIn('"reasoning_tokens": 100',content)
            self.assertIn('estimated_usd',content)
            self.assertIn('"calls": 1',content)
            self.assertNotIn(text,content)


if __name__=='__main__':
    unittest.main()
