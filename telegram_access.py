"""Access requests, administrator commands and authenticated callbacks."""
import logging
import re
from repositories.access_repository import AccessNotificationFailed, StaleAccessAction

LOG = logging.getLogger('telegram_access')
STATUS = {'pending':'Ожидает одобрения','approved':'Доступ разрешён','rejected':'Отклонён','revoked':'Доступ отозван'}
ADMIN_COMMANDS = {'/users','/pending','/revoke','/approve'}


def button(label, action, row):
    return {'text':label, 'callback_data':f'access:{action}:{row["user_id"]}:{row["revision"]}'}


def decisions(row):
    return {'inline_keyboard':[[button('Одобрить','approve',row),button('Отклонить','reject',row)]]}


def display(row, include_id=False):
    name = re.sub(r'[\r\n\t]', ' ', row['display_name'])[:80]
    username = ' @'+row['username'][:40] if row.get('username') else ''
    identifier = f'\nTelegram ID: {row["user_id"]}' if include_id else ''
    return f'{name}{username}\n{STATUS[row["status"]]}{identifier}'


class TelegramAccess:
    def __init__(self, repository):
        self.repository = repository

    def allowed(self, user_id):
        return self.repository.allowed(user_id)

    @staticmethod
    def _send(api, chat_id, text, markup=None):
        payload = {'chat_id':chat_id,'text':text}
        if markup:
            payload['reply_markup'] = markup
        try:
            api.call('sendMessage', **payload)
            return True
        except Exception:
            # Never log names, IDs, incoming callback data or API token URLs.
            LOG.warning('access_notification_failed')
            return False

    @staticmethod
    def _answer(api, callback_id, text):
        try:
            api.call('answerCallbackQuery', callback_query_id=callback_id, text=text[:200])
        except Exception:
            LOG.warning('callback_ack_failed')

    def _prompt(self, api, user_id):
        row = self.repository.get(user_id)
        if row and row['status'] == 'pending':
            self._send(api,user_id,'Запрос доступа ожидает решения администратора.')
        elif row and row['status'] in {'rejected','revoked'}:
            self._send(api,user_id,'Доступ не разрешён. Повторное предоставление доступа возможно только по решению администратора.')
        else:
            self._send(api,user_id,'Доступ к UVM Steel Bot ограничен. Отправить запрос администратору?',
                       {'inline_keyboard':[[{'text':'Запросить доступ','callback_data':'access:request'}]]})

    def _notify_request(self, api, row):
        admin_id = self.repository.admin_id()
        if not admin_id or not self.repository.reserve_notification(row['user_id']):
            return
        if self._send(api,admin_id,'Запрос доступа\n'+display(row,include_id=True),decisions(row)):
            self.repository.notification_sent(row['user_id'])

    def _admin_command(self, api, actor, command):
        if not self.repository.is_admin(actor):
            self._send(api,actor,'Команда доступна только администратору.')
            return
        rows = self.repository.list_users(status={'/pending':'pending','/revoke':'approved'}.get(command), limit=25)
        if command == '/approve':
            rows = [row for row in rows if row['status'] != 'approved']
        if not rows:
            self._send(api,actor,'Нет сотрудников с разрешённым доступом.' if command == '/revoke' else 'Сотрудников или ожидающих запросов пока нет. Попросите сотрудника открыть @UVM_SteelBot, нажать «Старт» и «Запросить доступ». Затем одобрите его в /pending.')
            return
        for row in rows:
            markup = None
            if command == '/pending':
                markup = decisions(row)
            elif command == '/revoke' or (command == '/users' and row['status']=='approved'):
                markup = {'inline_keyboard':[[button('Отозвать доступ','revoke',row)]]}
            elif command == '/approve' or command == '/users':
                markup = {'inline_keyboard':[[button('Одобрить','approve',row)]]}
            self._send(api,actor,display(row),markup)

    def _callback(self, callback, api):
        actor = callback.get('from', {})
        actor_id = actor.get('id')
        message = callback.get('message', {})
        callback_id = callback.get('id','')
        if (not actor_id or actor.get('is_bot') or message.get('chat',{}).get('type') != 'private'
                or message.get('chat',{}).get('id') != actor_id):
            self._answer(api,callback_id,'Действие недоступно.')
            return
        data = callback.get('data','')
        if data == 'access:request':
            self.repository.request_access(actor)
            row = self.repository.get(actor_id)
            if row['status'] == 'pending':
                self._notify_request(api,row)
                self._send(api,actor_id,'Запрос сохранён. Ожидайте одобрения администратора.')
                self._answer(api,callback_id,'Запрос ожидает одобрения.')
            else:
                self._answer(api,callback_id,'Запрос уже рассмотрен.')
                self._prompt(api,actor_id) if not self.allowed(actor_id) else self._send(api,actor_id,'Доступ уже предоставлен.')
            return
        if not self.repository.is_admin(actor_id):
            self._answer(api,callback_id,'Действие доступно только администратору.')
            return
        match = re.fullmatch(r'access:(approve|reject|revoke):(\d+):(\d+)', data)
        if not match:
            self._answer(api,callback_id,'Кнопка недействительна.')
            return
        action, target, revision = match[1], int(match[2]), int(match[3])
        try:
            status = self.repository.transition(actor_id,target,action,revision,
                notify=lambda user_id:self._send(api,user_id,'Доступ к UVM Steel Bot предоставлен. Отправьте новую заявку на металлопрокат.'))
        except AccessNotificationFailed:
            self._answer(api,callback_id,'Уведомление сотруднику не доставлено. Доступ не предоставлен.')
            self._send(api,actor_id,'Не удалось уведомить сотрудника. Попросите его открыть бота и повторите одобрение.')
            return
        except (PermissionError,StaleAccessAction):
            self._answer(api,callback_id,'Кнопка устарела. Обновите список командой /users или /pending.')
            return
        if action != 'approve':
            self._send(api,target, 'Запрос доступа отклонён.' if action=='reject' else 'Доступ к UVM Steel Bot отозван.')
        self._answer(api,callback_id,STATUS[status])
        self._send(api,actor_id,STATUS[status]+'.')
        try:
            api.call('editMessageReplyMarkup',chat_id=actor_id,message_id=message['message_id'],reply_markup={'inline_keyboard':[]})
        except Exception:
            # A revision check also invalidates buttons that cannot be removed.
            LOG.warning('stale_keyboard_cleanup_failed')

    def route(self, update, api):
        """True means handled/blocked; False permits the normalizer handler."""
        if 'callback_query' in update:
            self._callback(update['callback_query'],api)
            return True
        message = update.get('message',{})
        actor = message.get('from',{})
        actor_id = actor.get('id')
        if not actor_id or actor.get('is_bot') or message.get('chat',{}).get('type') != 'private' or message['chat'].get('id') != actor_id:
            return True
        text = (message.get('text') or '').strip()
        parts = text.split(maxsplit=1)
        command = parts[0].split('@')[0] if parts else ''
        if command == '/claim':
            token = parts[1].strip() if len(parts)>1 else ''
            if self.repository.claim_admin(actor,token):
                self._send(api,actor_id,'Ваш аккаунт привязан как администратор. Управление: /users, /pending, /revoke, /approve. Можно отправлять заявки на нормализацию.')
                for row in self.repository.list_users(status='pending',limit=25):
                    self._notify_request(api,row)
                LOG.info('administrator_binding_completed')
            else:
                self._send(api,actor_id,'Код недействителен, истёк или уже использован. Права не изменены.')
            return True
        if command in ADMIN_COMMANDS:
            self._admin_command(api,actor_id,command)
            return True
        if not self.allowed(actor_id):
            self._prompt(api,actor_id)
            return True
        return False
