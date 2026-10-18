"""Retained legacy adapter for offline comparison only; no startup or polling.

Never imported by main.py, telegram_bot or Steel Agent v2. Kept for selective reuse.
"""
import logging
from html import escape
from telegram_bot import TypingIndicator, TelegramError
from telegram_operator import ClarificationMessage, safe_chunks
from diagnostics import PipelineError, safe_code, operation, phase, emit, record_error

LOG=logging.getLogger('legacy_telegram_reference')


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
