"""Current shared primitives; original assertions retained from legacy tests."""
from decimal import Decimal, localcontext
import threading
import time
import unittest
from unittest.mock import Mock
import pytest
from telegram_bot import TypingIndicator
from dialog_history import bounded_history
from quantity import normalize_quantity, sum_quantities, to_scaled_quantity, SQLITE_INTEGER_MAX


@pytest.mark.parametrize('amount,unit,expected,canonical', [
    ('1200','кг','1.2','т'), ('350','кг','0.35','т'), ('500','кг','0.5','т'),
    ('1000','кг','1','т'), ('0.0015','т','0.0015','т'),
    ('5','шт.','5','шт'), ('12','пог. м','12','м')])
def test_exact_quantity_conversion(amount, unit, expected, canonical):
    converted, converted_unit = normalize_quantity(amount, unit)
    assert converted == Decimal(expected) and converted_unit == canonical
    assert normalize_quantity(converted, converted_unit) == (converted, converted_unit)


@pytest.mark.parametrize('value',['0','-1','NaN','Infinity','-Infinity'])
def test_invalid_quantities_are_rejected(value):
    with pytest.raises(ValueError):normalize_quantity(value,'кг')


def test_history_count_budget_keeps_complete_turns():
    history=[message for number in range(12) for message in
             [{'role':'user','content':f'q{number}'},{'role':'assistant','content':f'a{number}'}]]
    assert bounded_history(history)==history[-20:]

class SharedTypingTests(unittest.TestCase):
    def test_repeats_while_working_and_stops_before_return(self):
        api=Mock()
        repeated=threading.Event()
        count=[]
        def pulse(method, **payload):
            count.append(method)
            if len(count)>=2:
                repeated.set()
        api.call.side_effect=pulse
        with TypingIndicator(api,1,interval=0.01) as indicator:
            self.assertTrue(repeated.wait(1))
        self.assertFalse(indicator.thread.is_alive())
        old=len(count)
        time.sleep(0.03)
        self.assertEqual(len(count),old)
        self.assertTrue(all(call.args[0]=='sendChatAction' and call.kwargs['action']=='typing' for call in api.call.call_args_list))

    def test_stops_when_processing_raises(self):
        api=Mock()
        with self.assertRaises(ValueError):
            with TypingIndicator(api,1,interval=0.01) as indicator:
                raise ValueError('processing failed')
        self.assertFalse(indicator.thread.is_alive())

    def test_indicator_failure_does_not_cancel_work(self):
        api=Mock()
        api.call.side_effect=RuntimeError('telegram unavailable')
        with self.assertLogs('telegram_operator',level='WARNING'):
            with TypingIndicator(api,1) as indicator:
                completed=True
        self.assertTrue(completed)
        self.assertFalse(indicator.thread.is_alive())


class SharedQuantityTests(unittest.TestCase):
    def test_quantity_scaling_uses_exact_integer_units(self):
        self.assertEqual(to_scaled_quantity(20, "т"), (20_000_000, "т"))
        self.assertEqual(to_scaled_quantity(7000, "кг"), (7_000_000, "т"))
        self.assertEqual(to_scaled_quantity(500.5, "кг"), (500_500, "т"))
        self.assertEqual(to_scaled_quantity("1.234", "м"), (1234, "м"))
        self.assertEqual(to_scaled_quantity(5, "шт"), (5, "шт"))
        with self.assertRaisesRegex(ValueError, "точность"):
            to_scaled_quantity("1.5", "шт")


def test_quantity_precision_beyond_decimal_context_and_sqlite_scale():
    amount=Decimal('123456789012345678901234567890.1234567890123456789')
    with localcontext() as ctx:
        ctx.prec=6
        converted,unit=normalize_quantity(amount,'кг')
        assert converted==Decimal('123456789012345678901234567.8901234567890123456789')
        assert sum_quantities([amount,Decimal('0.0000000000000000001')])==Decimal('123456789012345678901234567890.1234567890123456790')
    assert normalize_quantity('0.000000001','кг')==(Decimal('0.000000000001'),'т')
    with pytest.raises(ValueError,match='точность'):to_scaled_quantity('0.000000001','кг')
    assert to_scaled_quantity(str(SQLITE_INTEGER_MAX)+'e-6','т')[0]==SQLITE_INTEGER_MAX
    with pytest.raises(ValueError,match='диапазон'):to_scaled_quantity(str(SQLITE_INTEGER_MAX+1)+'e-6','т')


def test_assistant_history_skips_oversized_turn_without_splitting_pairs():
    history = [{'role':'user','content':'first'},{'role':'assistant','content':'answer'},
               {'role':'user','content':'oversized'},{'role':'assistant','content':'X'*13000},
               {'role':'user','content':'last'},{'role':'assistant','content':'latest'}]
    assert bounded_history(history) == history[:2]+history[-2:]


def test_history_character_budget_keeps_whole_pairs_and_uses_older_small_turns():
    history = [m for q,a in [('old-small','answer'),('large','X'*7900),('latest','Y'*7900)]
               for m in [{'role':'user','content':q},{'role':'assistant','content':a}]]
    result = bounded_history(history)
    assert [m['content'] for m in result] == ['old-small','answer','latest','Y'*7900]
    assert sum(len(m['content']) for m in result) <= 12000
