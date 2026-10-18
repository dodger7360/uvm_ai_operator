"""Telegram long polling adapter; no business rules or SQL in handlers."""
import json
import logging
import os
from pathlib import Path
import time
import threading
import urllib.request
import urllib.error
import socket
import ssl
from windows_runtime import AlreadyRunning, ProcessLock, maintenance_requested, wait_or_maintenance
from dotenv import load_dotenv
from database import Database
from repositories.telegram_repository import TelegramRepository
from repositories.access_repository import AccessRepository
from telegram_access import TelegramAccess
from diagnostics import safe_code, emit

LOG = logging.getLogger('telegram_operator')


def network_failure(error):
    """Classify without retaining exception text, URLs, headers or credentials."""
    reason = getattr(error, 'reason', error)
    if isinstance(error, ValueError):
        return 'configuration', 'configuration'
    if isinstance(reason, ssl.SSLError):
        return 'tls', 'tls'
    if isinstance(reason, socket.gaierror):
        return 'dns', 'dns'
    if isinstance(reason, (TimeoutError, socket.timeout)) or getattr(reason, 'errno', None) in {60, 110, 10060}:
        return 'timeout', 'request'
    if isinstance(error, urllib.error.HTTPError):
        return ('proxy_authentication' if error.code == 407 else 'http'), 'response'
    if isinstance(reason, str) and any(word in reason.casefold() for word in ('proxy', 'socks')):
        return 'proxy', 'connection'
    return 'network', 'request'


class TelegramError(RuntimeError):
    def __init__(self, code, category=None, stage='request', exception_type='TelegramError'):
        self.code = code
        self.category = category or ('http' if code else 'network')
        self.stage, self.exception_type = stage, exception_type
        super().__init__(f'Telegram API status {code}')


class TelegramAPI:
    def __init__(self, token):
        self._base = 'https://api.telegram.org/bot' + token + '/'

    def call(self, method, _timeout=45, _attempt=1, **payload):
        safe_method = method if method in {'getMe', 'getWebhookInfo', 'getUpdates', 'sendMessage', 'sendChatAction', 'answerCallbackQuery'} else 'other'
        try:
            request = urllib.request.Request(self._base + method,
                data=json.dumps(payload).encode(), headers={'Content-Type':'application/json'})
            with urllib.request.urlopen(request, timeout=_timeout) as response:
                result = json.load(response)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
            category, stage = network_failure(error)
            code = error.code if isinstance(error, urllib.error.HTTPError) else 0
            reason = getattr(error, 'reason', error)
            exception_type = type(error).__name__
            LOG.warning('telegram_failure method=%s exception=%s cause=%s category=%s stage=%s attempt=%s retryable=%s',
                safe_method, exception_type, type(reason).__name__, category, stage, _attempt, code not in {401, 403, 407, 409})
            raise TelegramError(code, category, stage, exception_type) from None
        if not result.get('ok'):
            code = result.get('error_code', 0)
            LOG.warning('telegram_failure method=%s exception=TelegramAPIResponse category=http stage=response attempt=%s retryable=%s',
                safe_method, _attempt, code not in {401, 403, 407, 409})
            raise TelegramError(code, 'http', 'response', 'TelegramAPIResponse')
        return result['result']


class TypingIndicator:
    """Keep Telegram's short-lived typing action active during processing."""
    def __init__(self, api, chat_id, interval=4, stopping=lambda:False):
        self.api, self.chat_id, self.interval = api, chat_id, interval
        self.stopping=stopping
        self.stopped=threading.Event()
        self.thread=None

    def _pulse(self):
        started = time.monotonic()
        try:
            self.api.call('sendChatAction', chat_id=self.chat_id, action='typing', _timeout=3)
        except Exception:
            # Indicator failure never cancels the actual request.
            LOG.warning('typing_indicator_unavailable')
        finally:
            emit('typing_action', duration_ms=round((time.monotonic()-started)*1000,3))

    def _refresh(self):
        if self.stopped.is_set() or self.stopping():return
        self._pulse()
        deadline=time.monotonic()+self.interval
        while not self.stopped.wait(min(0.1,max(0,deadline-time.monotonic()))):
            if self.stopping():return
            if time.monotonic()>=deadline:
                self._pulse()
                deadline=time.monotonic()+self.interval

    def __enter__(self):
        self.thread=threading.Thread(target=self._refresh,daemon=True,name='telegram-typing')
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stopped.set()
        if self.thread:
            # A slow network call for the indicator must not delay the reply.
            self.thread.join(timeout=0.05)
        return False



def main():
    root = Path(__file__).resolve().parent
    load_dotenv(root / '.env')
    logging.basicConfig(filename=root / 'telegram.log', level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s', encoding='utf-8')
    logging.getLogger('httpx2').setLevel(logging.WARNING)
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('openai').setLevel(logging.WARNING)
    token = os.getenv('TELEGRAM_TOKEN') or os.getenv('TELEGRAM_BOT_TOKEN')
    if not token:
        LOG.error('missing_telegram_token')
        return 2
    try:
        with ProcessLock(root / 'telegram.lock'):
            return run_bot(root, token)
    except AlreadyRunning:
        LOG.error('another_local_instance_running')
        return 3


def run_bot(root, token):
    ready = root / 'telegram.ready'
    if maintenance_requested(root):
        LOG.info('maintenance_stop no_polling=true')
        return 0
    api = TelegramAPI(token)
    try:
        from steel_agent_v2.runtime import selection, connect
        handler_name, v2_database = selection(root)
        me = api.call('getMe')
        if api.call('getWebhookInfo').get('url'):
            LOG.error('existing_webhook_requires_review')
            return 4
        repository = TelegramRepository(Database(os.getenv('OPERATOR_DB', str(root / 'operator.sqlite3'))))
        access_repository = AccessRepository(repository.database)
        configured_admin = os.getenv('ADMIN_TELEGRAM_ID')
        if configured_admin:
            try:
                access_repository.bootstrap_configured_admin(int(configured_admin))
            except (ValueError, PermissionError):
                LOG.error('administrator_configuration_invalid')
                return 7
        access = TelegramAccess(access_repository)
        if access_repository.admin_id() is None:
            LOG.warning('administrator_not_bound_access_closed')
        service, _ = connect(v2_database, access)
        LOG.info('telegram_handler=%s', handler_name)
        ready.write_text(str(os.getpid()), encoding='ascii')
        LOG.info('bot_started username=%s pid=%s model=%s reasoning=%s fallback=%s', me['username'], os.getpid(),
                 getattr(service.transport, 'model', 'gpt-6-luna'),
                 getattr(service.transport, 'reasoning', 'high'), 'none')
        from steel_agent_v2.queue_runtime import run_queue
        return run_queue(root, api, access, repository, v2_database,
                         api_factory=lambda:TelegramAPI(token))
    except TelegramError as error:
        LOG.error('startup_telegram_status=%s category=%s stage=%s exception=%s',
            error.code, error.category, error.stage, error.exception_type)
        return 5 if error.code in {401, 403, 407, 409} or error.category in {'configuration', 'proxy'} else 6
    except Exception as error:
        LOG.error('startup_error exception=%s category=%s', type(error).__name__, safe_code(error))
        return 7
    finally:
        ready.unlink(missing_ok=True)


if __name__ == '__main__':
    raise SystemExit(main())
