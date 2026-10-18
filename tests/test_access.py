import concurrent.futures
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from database import Database
from repositories.access_repository import AccessRepository, AuthorizedRequestRepository, AccessNotificationFailed, StaleAccessAction
from repositories.telegram_repository import TelegramRepository
from telegram_access import TelegramAccess
from legacy_telegram_adapter import handle_update
from telegram_service import ActiveRequest, ActiveItem, TelegramService
from schemas import RequestItem, PurchaseRequest

ADMIN = 1001
EMPLOYEE = 2002
TOKEN = 'unit-test-binding-secret-0123456789'


def person(user_id=EMPLOYEE):
    return {'id':user_id,'first_name':'Тест','last_name':'Сотрудник','username':'test_employee'}


def message(user_id=EMPLOYEE, text='Уголок 75×75×6 — 1 т'):
    return {'message':{'chat':{'type':'private','id':user_id},'from':person(user_id),'text':text}}


def callback(user_id=EMPLOYEE, data='access:request'):
    return {'callback_query':{'id':'callback-test','from':person(user_id),'data':data,
            'message':{'chat':{'type':'private','id':user_id},'message_id':1}}}


def request_data(user_id=EMPLOYEE, tag='old'):
    original=RequestItem(product='Уголок',product_group='angle',size_designation='75×75×6',quantity='1',unit='т',source_text='Уголок 75×75×6 — 1 т')
    return ActiveRequest(request_id=tag,source_text=original.source_text,items=[ActiveItem(**original.model_dump(),item_id=f'{user_id}-{tag}',original=original)])


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.database=Database(Path(self.folder.name)/'access.sqlite3')
        self.requests=TelegramRepository(self.database)
        self.repo=AccessRepository(self.database)
        self.access=TelegramAccess(self.repo)
        self.api,self.service=Mock(),Mock()
        self.service.process.return_value=['Нормализованная заявка']
        self.repo.bootstrap_configured_admin(ADMIN)

    def ask(self, user_id=EMPLOYEE):
        self.access.route(callback(user_id),self.api)
        return self.repo.get(user_id)

    def approve(self,user_id=EMPLOYEE):
        row=self.ask(user_id)
        self.access.route(callback(ADMIN,f'access:approve:{user_id}:{row["revision"]}'),self.api)
        return self.repo.get(user_id)

    def test_actual_service_only_parses_after_approval(self):
        ai = Mock()
        ai.parse_request.return_value = PurchaseRequest(items=[request_data().items[0].original])
        requests = AuthorizedRequestRepository(self.requests, self.repo)
        service = TelegramService(ai, requests)
        handle_update(message(),service,self.api,self.access)
        ai.parse_request.assert_not_called()
        self.approve()
        handle_update(message(),service,self.api,self.access)
        ai.parse_request.assert_called_once()
        self.assertIsNotNone(requests.load(EMPLOYEE))
        self.assertEqual(requests.load(EMPLOYEE).items[0].quantity, 1)

    def test_actual_service_denied_read_and_save_after_revocation(self):
        row = self.approve()
        requests = AuthorizedRequestRepository(self.requests,self.repo)
        requests.save(EMPLOYEE,request_data())
        self.repo.transition(ADMIN,EMPLOYEE,'revoke',row['revision'])
        with self.assertRaises(PermissionError):
            requests.save(EMPLOYEE,request_data(tag='blocked'))
        self.assertEqual(self.requests.load(EMPLOYEE).request_id,'old')

    def test_claim_route_binds_actual_sender_without_ai(self):
        database = Database(Path(self.folder.name)/'claim-route.sqlite3')
        repository = AccessRepository(database)
        repository.issue_binding(TOKEN)
        handle_update(message(ADMIN, '/claim '+TOKEN),self.service,self.api,TelegramAccess(repository))
        self.assertTrue(repository.is_admin(ADMIN))
        self.service.process.assert_not_called()

    def test_unknown_user_has_no_access_or_ai_request(self):
        handle_update(message(),self.service,self.api,self.access)
        self.assertFalse(self.repo.allowed(EMPLOYEE))
        self.service.process.assert_not_called()

    def test_start_shows_access_request_button(self):
        handle_update(message(text='/start'),self.service,self.api,self.access)
        call=self.api.call.call_args
        self.assertEqual(call.kwargs['reply_markup']['inline_keyboard'][0][0]['text'],'Запросить доступ')
        self.service.process.assert_not_called()

    def test_start_does_not_appoint_first_visitor(self):
        self.repo=AccessRepository(Database(Path(self.folder.name)/'unbound.sqlite3'))
        access=TelegramAccess(self.repo)
        handle_update(message(text='/start'),self.service,self.api,access)
        self.assertIsNone(self.repo.admin_id())
        self.service.process.assert_not_called()

    def test_request_creates_pending_and_notifies_admin(self):
        row=self.ask()
        self.assertEqual(row['status'],'pending')
        notices=[call for call in self.api.call.call_args_list if call.args[0]=='sendMessage' and call.kwargs['chat_id']==ADMIN]
        self.assertEqual(len(notices),1)
        self.assertIn(str(EMPLOYEE),notices[0].kwargs['text'])
        self.assertIn('test_employee',notices[0].kwargs['text'])

    def test_repeated_request_no_duplicate_and_no_spam(self):
        self.ask()
        self.ask()
        self.assertEqual(len(self.repo.list_users()),1)
        notices=[call for call in self.api.call.call_args_list if call.args[0]=='sendMessage' and call.kwargs['chat_id']==ADMIN]
        self.assertEqual(len(notices),1)

    def test_admin_approval_allows_normalizer(self):
        row=self.approve()
        self.assertEqual(row['status'],'approved')
        self.assertEqual(row['role'],'employee')
        self.assertEqual(row['acted_by'],ADMIN)
        handle_update(message(),self.service,self.api,self.access)
        self.service.process.assert_called_once_with(EMPLOYEE,message()['message']['text'])

    def test_rejected_user_cannot_call_ai(self):
        row=self.ask()
        self.access.route(callback(ADMIN,f'access:reject:{EMPLOYEE}:{row["revision"]}'),self.api)
        handle_update(message(),self.service,self.api,self.access)
        self.assertEqual(self.repo.get(EMPLOYEE)['status'],'rejected')
        self.service.process.assert_not_called()

    def test_revocation_immediately_blocks_context_and_ai(self):
        row=self.approve()
        self.requests.save(EMPLOYEE,request_data())
        self.access.route(callback(ADMIN,f'access:revoke:{EMPLOYEE}:{row["revision"]}'),self.api)
        handle_update(message(text='Верни исходную марку'),self.service,self.api,self.access)
        self.assertEqual(self.repo.get(EMPLOYEE)['status'],'revoked')
        self.service.process.assert_not_called()
        with self.assertRaises(PermissionError):
            AuthorizedRequestRepository(self.requests,self.repo).load(EMPLOYEE)

    def test_stranger_cannot_use_admin_callback(self):
        row=self.ask()
        self.access.route(callback(3003,f'access:approve:{EMPLOYEE}:{row["revision"]}'),self.api)
        self.assertEqual(self.repo.get(EMPLOYEE)['status'],'pending')

    def test_employee_cannot_manage_other_users(self):
        self.approve()
        other=self.ask(3003)
        self.access.route(callback(EMPLOYEE,f'access:approve:3003:{other["revision"]}'),self.api)
        self.assertEqual(self.repo.get(3003)['status'],'pending')
        for command in ['/users','/pending','/revoke','/approve']:
            handle_update(message(text=command),self.service,self.api,self.access)
        self.service.process.assert_not_called()

    def test_permissions_persist_after_restart(self):
        self.approve()
        restarted=AccessRepository(Database(self.database.path))
        self.assertTrue(restarted.allowed(EMPLOYEE))
        self.assertTrue(restarted.is_admin(ADMIN))

    def test_request_context_isolation(self):
        self.approve()
        self.approve(3003)
        requests=AuthorizedRequestRepository(self.requests,self.repo)
        requests.save(EMPLOYEE,request_data(EMPLOYEE,'one'))
        requests.save(3003,request_data(3003,'two'))
        self.assertEqual(requests.load(EMPLOYEE).request_id,'one')
        self.assertEqual(requests.load(3003).request_id,'two')

    def test_unapproved_does_not_read_active_request(self):
        self.service.repository=Mock()
        for text in ['/start','заявка','/users','Верни марку']:
            handle_update(message(text=text),self.service,self.api,self.access)
        self.service.process.assert_not_called()
        self.service.repository.load.assert_not_called()

    def test_missing_access_controller_is_closed(self):
        handle_update(message(),self.service,self.api)
        self.service.process.assert_not_called()

    def test_stale_and_duplicate_approval_is_rejected(self):
        old=self.ask()
        self.approve()
        with self.assertRaises(StaleAccessAction):
            self.repo.transition(ADMIN,EMPLOYEE,'approve',old['revision'],notify=lambda _:True)

    def test_admin_cannot_be_revoked(self):
        row=self.repo.get(ADMIN)
        with self.assertRaises(StaleAccessAction):
            self.repo.transition(ADMIN,ADMIN,'revoke',row['revision'])
        self.assertTrue(self.repo.is_admin(ADMIN))

    def test_failed_employee_notification_does_not_grant_access(self):
        row=self.ask()
        with self.assertRaises(AccessNotificationFailed):
            self.repo.transition(ADMIN,EMPLOYEE,'approve',row['revision'],notify=lambda _:False)
        self.assertEqual(self.repo.get(EMPLOYEE)['status'],'pending')
        self.assertFalse(self.repo.allowed(EMPLOYEE))

    def test_callback_failed_employee_notification_stays_pending(self):
        row=self.ask()
        def fail(method,**kwargs):
            if method=='sendMessage' and kwargs['chat_id']==EMPLOYEE:
                raise RuntimeError('Blocked bot')
        self.api.call.side_effect=fail
        self.access.route(callback(ADMIN,f'access:approve:{EMPLOYEE}:{row["revision"]}'),self.api)
        self.assertEqual(self.repo.get(EMPLOYEE)['status'],'pending')

    def test_failed_revoke_notification_cannot_restore_access(self):
        row=self.approve()
        self.api.call.side_effect=RuntimeError('Network error')
        self.access.route(callback(ADMIN,f'access:revoke:{EMPLOYEE}:{row["revision"]}'),self.api)
        self.assertFalse(self.repo.allowed(EMPLOYEE))

    def test_rejected_requests_cannot_spam_admin(self):
        row=self.ask()
        self.repo.transition(ADMIN,EMPLOYEE,'reject',row['revision'])
        self.api.reset_mock()
        self.ask()
        self.assertEqual(self.repo.get(EMPLOYEE)['status'],'rejected')
        notices=[call for call in self.api.call.call_args_list if call.args[0]=='sendMessage' and call.kwargs['chat_id']==ADMIN]
        self.assertEqual(notices,[])

    def test_reapproval_hides_old_request_but_keeps_history(self):
        row=self.approve()
        self.requests.save(EMPLOYEE,request_data())
        self.repo.transition(ADMIN,EMPLOYEE,'revoke',row['revision'])
        revoked=self.repo.get(EMPLOYEE)
        self.repo.transition(ADMIN,EMPLOYEE,'approve',revoked['revision'],notify=lambda _:True)
        requests=AuthorizedRequestRepository(self.requests,self.repo)
        self.assertIsNone(requests.load(EMPLOYEE))
        self.assertEqual(self.requests.load(EMPLOYEE).request_id,'old')
        requests.save(EMPLOYEE,request_data(tag='new'))
        self.assertEqual(requests.load(EMPLOYEE).request_id,'new')
        with self.database.connection() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM telegram_history WHERE user_id=?',(EMPLOYEE,)).fetchone()[0],2)

    def test_configured_admin_keeps_existing_context(self):
        self.requests.save(ADMIN,request_data(ADMIN))
        self.assertEqual(AuthorizedRequestRepository(self.requests,self.repo).load(ADMIN).request_id,'old')

    def test_repository_enforces_actor_not_just_button_data(self):
        row=self.ask()
        with self.assertRaises(PermissionError):
            self.repo.transition(3003,EMPLOYEE,'approve',row['revision'],notify=lambda _:True)

    def test_callback_request_uses_actual_sender(self):
        self.access.route(callback(3003,'access:request:2002'),self.api)
        self.assertIsNone(self.repo.get(3003))
        self.assertIsNone(self.repo.get(EMPLOYEE))

    def test_parallel_approvals_only_commit_once(self):
        row=self.ask()
        def approve_once():
            try:
                return self.repo.transition(ADMIN,EMPLOYEE,'approve',row['revision'],notify=lambda _:True)
            except StaleAccessAction:
                return 'stale'
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results=list(executor.map(lambda _:approve_once(),range(2)))
        self.assertEqual(sorted(results),['approved','stale'])

    def test_missing_admin_never_grants_access(self):
        database=Database(Path(self.folder.name)/'missing.sqlite3')
        TelegramRepository(database)
        repository=AccessRepository(database)
        controller=TelegramAccess(repository)
        handle_update(message(),self.service,self.api,controller)
        handle_update(callback(),self.service,self.api,controller)
        self.assertIsNone(repository.admin_id())
        self.assertFalse(repository.allowed(EMPLOYEE))
        self.service.process.assert_not_called()

    def test_binding_code_is_required_single_use_and_hashed(self):
        database=Database(Path(self.folder.name)/'bootstrap.sqlite3')
        repository=AccessRepository(database)
        repository.issue_binding(TOKEN)
        self.assertFalse(repository.claim_admin(person(),'wrong'))
        self.assertIsNone(repository.admin_id())
        self.assertTrue(repository.claim_admin(person(ADMIN),TOKEN))
        self.assertFalse(repository.claim_admin(person(3003),TOKEN))
        self.assertTrue(repository.is_admin(ADMIN))

    def test_expired_binding_and_guess_limit(self):
        database=Database(Path(self.folder.name)/'expired.sqlite3')
        repository=AccessRepository(database)
        with patch('repositories.access_repository.time.time',return_value=1000):
            repository.issue_binding(TOKEN,ttl=10)
        with patch('repositories.access_repository.time.time',return_value=1020):
            self.assertFalse(repository.claim_admin(person(),TOKEN))
            repository.issue_binding(TOKEN)
            for _ in range(4):
                self.assertFalse(repository.claim_admin(person(),'wrong'))
            self.assertFalse(repository.claim_admin(person(),TOKEN))

    def test_conflicting_config_cannot_replace_existing_admin(self):
        with self.assertRaises(PermissionError):
            self.repo.bootstrap_configured_admin(3003)
        self.assertEqual(self.repo.admin_id(),ADMIN)

    def test_admin_commands_do_not_expose_requests(self):
        self.approve()
        self.requests.save(EMPLOYEE,request_data())
        self.api.reset_mock()
        handle_update(message(ADMIN,'/users'),self.service,self.api,self.access)
        for call in self.api.call.call_args_list:
            self.assertNotIn('75×75×6',call.kwargs.get('text',''))
        self.service.process.assert_not_called()

    def test_admin_commands_have_correct_buttons(self):
        self.ask()
        handle_update(message(ADMIN,'/pending'),self.service,self.api,self.access)
        self.assertEqual(self.api.call.call_args.kwargs['reply_markup']['inline_keyboard'][0][0]['text'],'Одобрить')
        self.approve()
        handle_update(message(ADMIN,'/revoke'),self.service,self.api,self.access)
        self.assertEqual(self.api.call.call_args.kwargs['reply_markup']['inline_keyboard'][0][0]['text'],'Отозвать доступ')

    def test_username_change_does_not_change_access(self):
        self.approve()
        changed=person()
        changed['username']='someone_else'
        self.repo.request_access(changed)
        self.assertTrue(self.repo.allowed(EMPLOYEE))
        impersonator=person(3003)
        impersonator['username']='test_employee'
        self.repo.request_access(impersonator)
        self.assertFalse(self.repo.allowed(3003))


if __name__=='__main__':
    unittest.main()
