import threading
import time
import unittest
from unittest.mock import Mock, patch
from telegram_bot import TypingIndicator
from legacy_telegram_adapter import handle_update


class TypingTests(unittest.TestCase):
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

    def test_handler_shows_typing_before_processing_and_reply(self):
        api,service,access=Mock(),Mock(),Mock()
        access.route.return_value=False
        access.allowed.return_value=True
        indicator_started = threading.Event()
        api.call.side_effect = lambda method, **payload: indicator_started.set() if method == 'sendChatAction' else None
        def process(*args):
            self.assertTrue(indicator_started.wait(1))
            return ['готовая заявка']
        service.process.side_effect=process
        update={'message':{'chat':{'type':'private','id':5},'from':{'id':5},'text':'заявка'}}
        handle_update(update,service,api,access)
        self.assertEqual(api.call.call_args.args[0],'sendMessage')
        self.assertEqual(api.call.call_args.kwargs['text'],'готовая заявка')

    def test_unauthorized_user_does_not_start_indicator(self):
        api,service,access=Mock(),Mock(),Mock()
        access.route.return_value=True
        handle_update({'message':{'chat':{'type':'private','id':5},'from':{'id':5},'text':'заявка'}},service,api,access)
        service.process.assert_not_called()
        api.call.assert_not_called()


if __name__=='__main__':
    unittest.main()
