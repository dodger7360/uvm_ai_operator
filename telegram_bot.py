"""Telegram long polling adapter; no business rules or SQL in handlers."""
import json
import logging
import os
from pathlib import Path
import time
import threading
from html import escape
import urllib.request
import urllib.error
import socket
import ssl
from windows_runtime import AlreadyRunning, ProcessLock, maintenance_requested, wait_or_maintenance
from dotenv import load_dotenv
from ai import AIService
from database import Database
from repositories.telegram_repository import TelegramRepository
from telegram_service import TelegramService
from telegram_operator import ClarificationMessage, safe_chunks
from work_assistant import WorkAssistantService
from repositories.access_repository import AccessRepository, AuthorizedRequestRepository
from telegram_access import TelegramAccess
from diagnostics import PipelineError, safe_code, operation, phase, emit, record_error

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
    def __init__(self, api, chat_id, interval=4):
        self.api, self.chat_id, self.interval = api, chat_id, interval
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
        self._pulse()
        deadline=time.monotonic()+self.interval
        while not self.stopped.wait(max(0,deadline-time.monotonic())):
            self._pulse()
            deadline+=self.interval

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


def handle_update(update, service, api, access=None):
    with operation():
        return _handle_update(update, service, api, access)


def _handle_update(update, service, api, access=None):
    phase('authorization')
    # Fail closed even when a caller forgets to supply the access controller.
    if access is not None:
        if access.route(update, api):
            return
    else:
        message = update.get('message', {})
        if message.get('chat', {}).get('type') == 'private':
            api.call('sendMessage', chat_id=message['chat']['id'], text='Доступ к боту ограничен. Администратор ещё не настроен.')
        return
    message = update.get('message', {})
    if message.get('chat', {}).get('type') != 'private' or message.get('from', {}).get('is_bot'):
        return
    chat_id, user_id = message['chat']['id'], message['from']['id']
    text = message.get('text')
    if not text or not text.strip():
        api.call('sendMessage', chat_id=chat_id, text='Пришлите заявку текстом. Вложения пока не поддерживаются.')
        return
    if text.split()[0].split('@')[0] in {'/start', '/help'}:
        messages = ['Отправьте заявку на металлопрокат обычным сообщением. После ответа можно написать: «Везде С255 замени на Ст3» или «В пятой позиции измени количество на 2,5 т».']
    else:
        try:
            with TypingIndicator(api,chat_id):
                messages = service.process(user_id, text)
        except PipelineError as exc:
            record_error(exc)
            messages = [exc.public_message]
        except ValueError as exc:
            record_error(exc)
            LOG.warning('validation_error class=%s reason=%s',type(exc).__name__,safe_code(exc))
            messages = ['Ответ модели не прошёл проверку данных. Повторите заявку, сохраняя каждую позицию отдельной строкой. Активная заявка не изменена.']
        except Exception as exc:
            record_error(exc)
            code=safe_code(exc)
            LOG.error('processing_error class=%s reason=%s',type(exc).__name__,code)
            messages = [{'api_connection':'Не удалось подключиться к сервису AI. Повторите сообщение позже.',
                'api_timeout':'AI не ответил вовремя. Повторите сообщение позже.',
                'api_authentication':'AI временно недоступен из-за конфигурации доступа. Сообщите администратору.',
                'api_bad_request':'API отклонил настройки обработки. Сообщите администратору.',
                'api_model_not_found':'Настроенная модель AI недоступна. Сообщите администратору.',
                'api_response_length':'Ответ AI оборвался по лимиту длины. Разделите заявку на меньшие части.'}.get(code,
                'Не удалось завершить обработку. Активная заявка не изменена. Сообщите администратору.')]
    if not access.allowed(user_id):
        api.call('sendMessage', chat_id=chat_id, text='Доступ к боту ограничен.')
        return
    for text in messages:
        phase('sending')
        send_operator_message(api, chat_id, text)
    LOG.info('message_processed response_parts=%s', len(messages))


def send_operator_message(api, chat_id, text):
    """Only trusted presentation metadata enables markup; all content escapes.

    Retry plain text only on a rejected HTML request (400), never on an
    uncertain network delivery. Escape expansion is accounted for in chunks.
    """
    if not isinstance(text, ClarificationMessage):
        for part in safe_chunks(text):
            api.call('sendMessage', chat_id=chat_id, text=part)
        return
    for part in safe_chunks(text, limit=600):
        html = '<b>Уточнение</b>\n' + escape(part)
        try:
            api.call('sendMessage', chat_id=chat_id, text=html, parse_mode='HTML')
        except TelegramError as error:
            if error.code != 400:
                raise
            emit('telegram_plain_text_retry', reason='html_rejected')
            api.call('sendMessage', chat_id=chat_id, text=part)


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
        ai = AIService()
        service = TelegramService(ai, AuthorizedRequestRepository(repository, access_repository),
                                  commercial_view=True, reliable=True,
                                  assistant=WorkAssistantService(ai.assistant_transport))
        offset = repository.get_offset()
        ready.write_text(str(os.getpid()), encoding='ascii')
        LOG.info('bot_started username=%s pid=%s model=%s reasoning=%s fallback=%s', me['username'], os.getpid(), os.getenv('OPENAI_MODEL','gpt-6-luna'), os.getenv('OPENAI_REASONING_EFFORT','low'), os.getenv('OPENAI_FALLBACK_REASONING_EFFORT','medium'))
        failures, connected = 0, False
        while not maintenance_requested(root):
            try:
                updates = api.call('getUpdates', offset=offset, timeout=25,
                    allowed_updates=['message', 'callback_query'], _attempt=failures+1)
                if not connected or failures:
                    LOG.info('polling_connected recovery=%s', bool(failures))
                connected, failures = True, 0
                for update in updates:
                    if maintenance_requested(root):
                        break  # Leave unprocessed update IDs unacknowledged.
                    handle_update(update, service, api, access)
                    offset = update['update_id'] + 1
                    repository.set_offset(offset)
            except TelegramError as error:
                failures += 1
                LOG.warning('telegram_status=%s category=%s stage=%s exception=%s',
                    error.code, error.category, error.stage, error.exception_type)
                if error.code in {401, 403, 407, 409} or error.category in {'configuration', 'proxy'}:
                    return 5
                delay = min(5 * 2 ** min(failures-1, 4), 60)
                LOG.warning('telegram_retry method=getUpdates attempt=%s delay_seconds=%s', failures+1, delay)
                if wait_or_maintenance(root, delay):
                    break
            except Exception as error:
                LOG.error('polling_error exception=%s category=%s', type(error).__name__, safe_code(error))
                return 1
        LOG.info('maintenance_stop request_finished=true')
        return 0
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
