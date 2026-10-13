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


def handle_update(update, service, api):
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
        except ValueError as exc:
            # Only locally controlled validation errors are shown to users.
            LOG.warning('validation_error user=%s', user_id)
            messages = ['Не удалось безопасно обработать сообщение. Уточните марки, количество и номера изменяемых позиций. Активная заявка сохранена.']
        except Exception:
            LOG.error('processing_error user=%s', user_id)
            messages = ['Сервис обработки временно недоступен. Повторите сообщение чуть позже.']
    for text in messages:
        api.call('sendMessage', chat_id=chat_id, text=text)
    LOG.info('message_processed user=%s response_parts=%s', user_id, len(messages))


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
            LOG.info('bot_started username=%s pid=%s', me['username'], os.getpid())
            repository = TelegramRepository(Database(os.getenv('OPERATOR_DB', str(root / 'operator.sqlite3'))))
            service = TelegramService(AIService(), repository)
            offset = repository.get_offset()
            while True:
                try:
                    updates = api.call('getUpdates', offset=offset, timeout=25, allowed_updates=['message'])
                    for update in updates:
                        handle_update(update, service, api)
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
