"""Telegram long polling adapter; no business rules or SQL in handlers."""
import json
import logging
import os
from pathlib import Path
import time
import urllib.request
import urllib.error
from dotenv import load_dotenv
from ai import AIService
from database import Database
from repositories.telegram_repository import TelegramRepository
from telegram_service import TelegramService
from repositories.access_repository import AccessRepository, AuthorizedRequestRepository
from telegram_access import TelegramAccess
from diagnostics import PipelineError, safe_code

LOG = logging.getLogger('telegram_operator')


class TelegramError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(f'Telegram API status {code}')


class TelegramAPI:
    def __init__(self, token):
        self._base = 'https://api.telegram.org/bot' + token + '/'

    def call(self, method, **payload):
        request = urllib.request.Request(self._base + method,
            data=json.dumps(payload).encode(), headers={'Content-Type':'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            raise TelegramError(exc.code) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise TelegramError(0) from None
        if not result.get('ok'):
            raise TelegramError(result.get('error_code', 0))
        return result['result']


def handle_update(update, service, api, access=None):
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
            messages = service.process(user_id, text)
        except PipelineError as exc:
            messages = [exc.public_message]
        except ValueError as exc:
            LOG.warning('validation_error class=%s reason=%s',type(exc).__name__,safe_code(exc))
            messages = ['Ответ модели не прошёл проверку данных. Повторите заявку, сохраняя каждую позицию отдельной строкой. Активная заявка не изменена.']
        except Exception as exc:
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
        api.call('sendMessage', chat_id=chat_id, text=text)
    LOG.info('message_processed response_parts=%s', len(messages))


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
    # A process-held OS lock survives crashes correctly; stale files are harmless.
    import msvcrt
    with (root / 'telegram.lock').open('a+b') as lock:
        lock.seek(0)
        if not lock.read(1):
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            LOG.error('another_local_instance_running')
            return 3
        api = TelegramAPI(token)
        try:
            me = api.call('getMe')
            if api.call('getWebhookInfo').get('url'):
                LOG.error('existing_webhook_requires_review')
                return 4
            LOG.info('bot_started username=%s pid=%s model=%s reasoning=%s fallback=%s', me['username'], os.getpid(), os.getenv('OPENAI_MODEL','gpt-6-luna'), os.getenv('OPENAI_REASONING_EFFORT','low'), os.getenv('OPENAI_FALLBACK_REASONING_EFFORT','medium'))
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
            service = TelegramService(AIService(), AuthorizedRequestRepository(repository, access_repository))
            offset = repository.get_offset()
            while True:
                try:
                    updates = api.call('getUpdates', offset=offset, timeout=25, allowed_updates=['message', 'callback_query'])
                    for update in updates:
                        handle_update(update, service, api, access)
                        offset = update['update_id'] + 1
                        repository.set_offset(offset)
                except TelegramError as exc:
                    LOG.warning('telegram_status=%s', exc.code)
                    if exc.code in {401,409}:
                        return 5
                    time.sleep(5)
                except Exception:
                    LOG.error('polling_error')
                    time.sleep(5)
        except TelegramError as exc:
            LOG.error('startup_telegram_status=%s', exc.code)
            return 6


if __name__ == '__main__':
    raise SystemExit(main())
