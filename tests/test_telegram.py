import tempfile
import unittest
from pathlib import Path
from decimal import Decimal
from unittest.mock import Mock
from schemas import PurchaseRequest, RequestItem
from database import Database
from repositories.telegram_repository import TelegramRepository
from telegram_service import TelegramService, MessagePlan, Edit, split_positions, exact_source_attributes
from telegram_bot import handle_update
from formatter import format_normalized_request, sort_items


def item(grade='С345-6', quantity='0.0015', size='75×75×6', group='angle', unit='т'):
    return RequestItem(product='Уголок', product_group=group, size_designation=size,
        grade=grade, quantity=quantity, unit=unit,
        source_text=f'Уголок {size} {grade} ГОСТ 8509-93 — {quantity.replace(".", ",")} {unit}', gost_tu='ГОСТ 8509-93')


class TelegramTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = TelegramRepository(Database(Path(self.temp.name) / 'test.sqlite3'))
        self.ai = Mock()
        self.service = TelegramService(self.ai, self.repo)

    def create(self, user=1, items=None):
        items = items or [item()]
        self.ai.parse_request.return_value = PurchaseRequest(items=items)
        return self.service.process(user, '\n'.join(i.source_text for i in items))

    def test_comma_quantity_from_ai_is_exact_decimal(self):
        self.assertEqual(RequestItem(quantity='0,0015').quantity, Decimal('0.0015'))
        self.assertEqual(RequestItem(quantity='560,46').quantity, Decimal('560.46'))

    def test_omitted_duplicate_rejected(self):
        self.ai.parse_request.return_value = PurchaseRequest(items=[item()])
        with self.assertRaises(ValueError):
            self.service.process(1, item().source_text + '\n' + item().source_text)

    def test_missing_quantity_not_invented_from_size(self):
        data = item()
        data.source_text = 'Уголок 75×75×6 С345-6 ГОСТ 8509-93'
        data.quantity = Decimal('75')
        exact_source_attributes(data)
        self.assertIsNone(data.quantity)

    def test_excel_unit_before_quantity(self):
        data = item()
        data.source_text = 'Уголок 75×75×6 С345-6\tт\t0,0015'
        exact_source_attributes(data)
        self.assertEqual(data.quantity, Decimal('0.0015'))
        self.assertEqual(data.unit, 'т')

    def test_tonnage_format_command_does_not_change_quantity(self):
        self.create()
        before = self.repo.load(1)
        output = self.service.process(1, 'тоннаж приведи к виду 0,100т , только три знака после запятой, либо два')
        after = self.repo.load(1)
        self.assertTrue(output[0].endswith('0,002т'))
        self.assertEqual(after.items, before.items)
        self.assertEqual(after.tonnage_decimals, 3)
        self.ai.interpret_message.assert_not_called()
        output = self.service.process(1, 'Тоннаж отображай с двумя знаками после запятой')
        self.assertTrue(output[0].endswith('0,00т'))
        self.assertEqual(self.repo.load(1).items[0].quantity, Decimal('0.0015'))

    def test_tonnage_format_is_fixed_and_persists_through_edits(self):
        self.create(items=[item(quantity='0.1')])
        output = self.service.process(1, 'Тоннаж формат 0,100т')
        self.assertTrue(output[0].endswith('0,100т'))
        current = self.repo.load(1)
        self.ai.interpret_message.return_value = MessagePlan(intent='edit', edits=[Edit(item_id=current.items[0].item_id, field='grade', value='09Г2С')])
        self.assertTrue(self.service.process(1, 'Замени марку на 09Г2С')[0].endswith('0,100т'))
        self.ai.interpret_message.return_value = MessagePlan(intent='new')
        self.create(items=[item(quantity='0.1')])
        self.assertIsNone(self.repo.load(1).tonnage_decimals)

    def test_initial_preserves_steel_category_and_quantity(self):
        output = self.create()[0]
        saved = self.repo.load(1)
        self.assertIn('С345-6', output)
        self.assertNotIn('09Г2С', output)
        self.assertEqual(saved.items[0].quantity, Decimal('0.0015'))
        self.assertEqual(saved.items[0].original.grade, 'С345-6')
        self.assertIn('ГОСТ 8509-93', output)

    def test_never_substitutes_s255_4(self):
        self.assertIn('С255-4', self.create(items=[item('С255-4')])[0])
        self.assertNotIn('Ст3', format_normalized_request(self.repo.load(1)))

    def test_recovers_full_grade_when_model_truncated_it(self):
        data = item('С255-4')
        data.grade = 'Ст3'
        self.create(items=[data])
        self.assertEqual(self.repo.load(1).items[0].grade, 'С255-4')

    def test_duplicates_kept_with_unique_stable_ids(self):
        self.create(items=[item(), item()])
        saved = self.repo.load(1)
        self.assertEqual(len(saved.items), 2)
        self.assertNotEqual(saved.items[0].item_id, saved.items[1].item_id)
        self.assertEqual(saved, self.repo.load(1))

    def test_field_edit_returns_full_request_and_restore(self):
        self.create(items=[item(), item('С255-4')])
        original = self.repo.load(1)
        target = original.items[0]
        self.ai.interpret_message.return_value = MessagePlan(intent='edit', edits=[Edit(item_id=target.item_id, field='grade', value='09Г2С')])
        output = self.service.process(1, 'В первой позиции замени С345-6 на 09Г2С')[0]
        self.assertEqual(len(output.splitlines()), 2)
        self.assertIn('09Г2С', output)
        self.assertIn('С255-4', output)
        self.assertEqual(self.repo.load(1).items[0].original, target.original)
        self.ai.interpret_message.return_value = MessagePlan(intent='edit', edits=[Edit(item_id=target.item_id, field='grade', restore=True)])
        self.service.process(1, 'Верни исходную марку первой позиции')
        self.assertEqual(self.repo.load(1).items[0].grade, target.original.grade)

    def test_quantity_edit_preserves_all_other_fields(self):
        self.create()
        saved = self.repo.load(1)
        before = saved.items[0].model_dump()
        self.ai.interpret_message.return_value = MessagePlan(intent='edit', edits=[Edit(item_id=saved.items[0].item_id, field='quantity', value='2,5')])
        output = self.service.process(1, 'В первой позиции измени количество на 2,5 т')[0]
        after = self.repo.load(1).items[0].model_dump()
        self.assertEqual(after.pop('quantity'), Decimal('2.5'))
        before.pop('quantity')
        self.assertEqual(before, after)
        self.assertIn('2,5 т', output)

    def test_new_request_resets_replacements_and_keeps_history(self):
        self.create()
        first_id = self.repo.load(1).request_id
        self.ai.interpret_message.return_value = MessagePlan(intent='new')
        self.create(items=[item('С255-4')])
        self.assertNotEqual(first_id, self.repo.load(1).request_id)
        with self.repo.database.connection() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM telegram_history').fetchone()[0], 2)

    def test_context_isolation_two_users(self):
        self.create(1)
        self.create(2, [item('С255-4')])
        self.assertEqual(self.repo.load(1).items[0].grade, 'С345-6')
        self.assertEqual(self.repo.load(2).items[0].grade, 'С255-4')
        self.assertIsNone(self.repo.load(3))

    def test_ambiguous_command_keeps_active(self):
        self.create()
        before = self.repo.load(1)
        self.ai.interpret_message.return_value = MessagePlan(intent='clarify', question='Какие позиции?')
        self.assertEqual(self.service.process(1, 'Замени сталь'), ['Какие позиции?'])
        self.assertEqual(before, self.repo.load(1))

    def test_invalid_patch_is_atomic(self):
        self.create()
        before = self.repo.load(1)
        self.ai.interpret_message.return_value = MessagePlan(intent='edit', edits=[
            Edit(item_id=before.items[0].item_id, field='grade', value='Ст3'),
            Edit(item_id='missing', field='quantity', value='3')])
        with self.assertRaises(ValueError):
            self.service.process(1, 'Замени марку и количество')
        self.assertEqual(before, self.repo.load(1))

    def test_missing_quantity_keeps_position_and_asks(self):
        data = item()
        data.quantity = None
        data.source_text = 'Уголок 75×75×6 С345-6 ГОСТ 8509-93'
        output = self.create(items=[data])
        self.assertEqual(len(self.repo.load(1).items), 1)
        self.assertIsNone(self.repo.load(1).items[0].quantity)
        self.assertGreater(len(output), 1)

    def test_units_and_decimal_precision(self):
        for amount, unit in [('0.0015','т'),('0.0072','т'),('0.004','т'),('560.46','пог. м'),('620','м²'),('3.2','кг'),('2','шт'),('10','м')]:
            with self.subTest(unit=unit, amount=amount):
                output = self.create(user=hash((amount,unit)), items=[item(quantity=amount, unit=unit)])
                self.assertIn(amount.replace('.', ','), output[0])
                self.assertEqual(self.repo.load(hash((amount,unit))).items[0].unit, unit)

    def test_long_response_splits_between_positions(self):
        lines = [f'Уголок 75×75×6 С345-6 ГОСТ 8509-93 — {n},0015 т' for n in range(150)]
        messages = split_positions('\n'.join(lines))
        self.assertGreater(len(messages), 1)
        self.assertEqual('\n'.join(messages).splitlines(), lines)
        self.assertTrue(all(len(s.encode('utf-16-le')) // 2 <= 4000 for s in messages))

    def test_oversized_line_rejected(self):
        with self.assertRaises(ValueError):
            split_positions('x' * 4001)

    def test_numeric_sheet_sort_ignores_rolling_type(self):
        rows = [RequestItem(product=p, product_group='sheet', size_designation=s, thickness_mm=float(s.replace(',','.'))) for p,s in [('Лист Г/К','10'),('Лист Х/К','0,8'),('Лист Г/К','8'),('Лист Х/К','1,6')]]
        self.assertEqual([i.size_designation for i in sort_items(rows)], ['0,8','1,6','8','10'])

    def test_preserves_long_grade_variants(self):
        for grade in ['С245','С255','С255-4','С345-6','С355-6','345-9-09Г2С','09Г2С-12','Ст3пс5','Ст3сп5','ВСт3сп5','Ст09Г2С','Ст09Г2С-12']:
            with self.subTest(grade=grade):
                data = item(grade)
                exact_source_attributes(data)
                self.assertEqual(data.grade, grade)

    def test_clean_plain_text_no_numbering(self):
        output = self.create()[0]
        self.assertTrue(output.startswith('Уголок '))
        self.assertNotIn('\n\n', output)
        self.assertNotIn('Прошу', output)

    def test_handler_routes_private_and_sends_all_parts(self):
        api, service = Mock(), Mock()
        service.process.return_value = ['one', 'two']
        update = {'message': {'chat': {'type':'private','id':1}, 'from': {'id':5}, 'text':'заявка'}}
        handle_update(update, service, api)
        service.process.assert_called_once_with(5, 'заявка')
        self.assertEqual([c.kwargs['text'] for c in api.call.call_args_list], ['one','two'])
        self.assertTrue(all('parse_mode' not in c.kwargs for c in api.call.call_args_list))

    def test_handler_ignores_groups(self):
        api, service = Mock(), Mock()
        handle_update({'message':{'chat':{'type':'group'}}}, service, api)
        api.call.assert_not_called()
        service.process.assert_not_called()

    def test_persistent_polling_offset(self):
        self.assertEqual(self.repo.get_offset(), 0)
        self.repo.set_offset(456)
        self.assertEqual(TelegramRepository(self.repo.database).get_offset(), 456)

    def test_omitted_source_position_rejected(self):
        self.ai.parse_request.return_value = PurchaseRequest(items=[item()])
        with self.assertRaises(ValueError):
            self.service.process(1, item().source_text + '\nБалка 20Б1 С245 — 1 т')
        self.assertIsNone(self.repo.load(1))

    def test_strip_compound_size_preserved(self):
        from normalizer import normalize_request
        data = RequestItem(product='Лента', product_group='strip', size_designation='0,7×20-М-НТ-О-12Х18Н10Т-3-Д', quantity='8.035', unit='т')
        normalize_request(PurchaseRequest(items=[data]))
        self.assertEqual(data.size_designation, '0,7×20-М-НТ-О-12Х18Н10Т-3-Д')


if __name__ == '__main__':
    unittest.main()
