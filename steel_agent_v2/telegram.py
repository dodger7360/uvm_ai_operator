"""Opt-in handler/factory only. No entrypoint, getUpdates, process start or switch."""
from pathlib import Path
from database import Database
from telegram_bot import TypingIndicator
from .telegram_format import payloads
from diagnostics import operation, emit, safe_code
from .agent import SteelAgent
from .repository import Repository


def build_service(transport, *, database_path, access):
    path=Path(database_path).resolve()
    # Require an explicitly isolated v2 DB. No accidental legacy migration.
    legacy=access.repository.database.path.resolve()
    if path == legacy:
        raise ValueError('v2 requires an explicit separate database; legacy DB migration is not part of this step')
    repository=Repository(Database(path),authorize=access.allowed)
    return SteelAgent(transport,repository)


def handle_update(update, service, api, access):
    """Reuse existing TelegramAccess and delivery; no mode switch in the chat."""
    if access is None or access.route(update,api):return
    message=update.get('message',{})
    actor=message.get('from',{})
    if (message.get('chat',{}).get('type')!='private' or actor.get('is_bot')
            or actor.get('id') != message.get('chat',{}).get('id')):return
    user=actor['id'];text=message.get('text')
    if not access.allowed(user):return
    if not isinstance(text,str) or not text.strip():
        api.call('sendMessage',chat_id=user,text='Пришлите текст сообщения; вложения пока не поддерживаются.')
        return
    if 'update_id' not in update:
        raise ValueError('Telegram update_id is required for idempotency')
    with operation():
        try:
            with TypingIndicator(api,user):
                reply=service.handle(user,text,event_id='telegram:'+str(update['update_id']))
        except PermissionError:return
        except Exception as error:
            emit('steel_v2_delivery_failure',reason=safe_code(error))
            if access.allowed(user):api.call('sendMessage',chat_id=user,text='Обработка недоступна. Сохраните исходный текст.')
            return
        # Recheck between chunks if permission is revoked while delivering.
        for payload in payloads(reply,user):
            if not access.allowed(user):return
            api.call('sendMessage',**payload)
