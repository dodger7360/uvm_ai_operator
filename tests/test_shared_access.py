"""Shared access repository/controller assertions extracted from test_access."""
import concurrent.futures
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from database import Database
from repositories.telegram_repository import TelegramRepository
from repositories.access_repository import AccessRepository, AccessNotificationFailed, StaleAccessAction
from telegram_access import TelegramAccess
ADMIN=1001
EMPLOYEE=2002
TOKEN="unit-test-binding-secret-0123456789"

def person(user_id=EMPLOYEE):
    return {'id':user_id,'first_name':'Тест','last_name':'Сотрудник','username':'test_employee'}


def callback(user_id=EMPLOYEE, data='access:request'):
    return {'callback_query':{'id':'callback-test','from':person(user_id),'data':data,
            'message':{'chat':{'type':'private','id':user_id},'message_id':1}}}


class SharedAccessTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.database=Database(Path(self.folder.name)/'access.sqlite3')
        TelegramRepository(self.database)  # Shared access uses history generations.
        self.repo=AccessRepository(self.database)
        self.access=TelegramAccess(self.repo)
        self.api=Mock()
        self.repo.bootstrap_configured_admin(ADMIN)

    def ask(self, user_id=EMPLOYEE):
        self.access.route(callback(user_id),self.api)
        return self.repo.get(user_id)

    def approve(self,user_id=EMPLOYEE):
        row=self.ask(user_id)
        self.access.route(callback(ADMIN,f'access:approve:{user_id}:{row["revision"]}'),self.api)
        return self.repo.get(user_id)

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

    def test_stranger_cannot_use_admin_callback(self):
        row=self.ask()
        self.access.route(callback(3003,f'access:approve:{EMPLOYEE}:{row["revision"]}'),self.api)
        self.assertEqual(self.repo.get(EMPLOYEE)['status'],'pending')

    def test_permissions_persist_after_restart(self):
        self.approve()
        restarted=AccessRepository(Database(self.database.path))
        self.assertTrue(restarted.allowed(EMPLOYEE))
        self.assertTrue(restarted.is_admin(ADMIN))

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
