"""Superseded synchronous Telegram path; historical regression, not v2 readiness."""
import logging
from unittest.mock import Mock
import pytest
from .reference import telegram_bot
bot=telegram_bot()

def wire_service(monkeypatch, api):
    repository=Mock()
    repository.get_offset.return_value=0
    access_repository=Mock()
    access_repository.admin_id.return_value=1
    monkeypatch.setattr(bot,'TelegramAPI',lambda token:api)
    monkeypatch.setattr(bot,'Database',Mock())
    monkeypatch.setattr(bot,'TelegramRepository',lambda db:repository)
    monkeypatch.setattr(bot,'NormalizationPolicyRepository',Mock())
    monkeypatch.setattr(bot,'AccessRepository',lambda db:access_repository)
    monkeypatch.setattr(bot,'AIService',Mock())
    monkeypatch.setattr(bot,'TelegramAccess',Mock())
    monkeypatch.setattr(bot,'AuthorizedRequestRepository',Mock())
    monkeypatch.setattr(bot,'TelegramService',Mock())
    monkeypatch.delenv('ADMIN_TELEGRAM_ID',raising=False)
    return repository


def test_maintenance_finishes_current_update_and_preserves_unprocessed_offset(tmp_path,monkeypatch):
    api=Mock()
    def call(method,**kwargs):
        return {'username':'offline'} if method=='getMe' else {} if method=='getWebhookInfo' else [{'update_id':1},{'update_id':2}]
    api.call.side_effect=call
    repo=wire_service(monkeypatch,api)
    handled=[]
    def handle(update,*args):
        handled.append(update['update_id'])
        assert (tmp_path/'telegram.ready').exists()
        (tmp_path/'telegram.maintenance').touch()
    monkeypatch.setattr(bot,'handle_update',handle)
    assert bot.run_bot(tmp_path,'offline')==0
    assert handled==[1]
    repo.set_offset.assert_called_once_with(2)
    assert not (tmp_path/'telegram.ready').exists()


def test_temporary_polling_error_recovers_without_restarting_worker(tmp_path,monkeypatch,caplog):
    api=Mock()
    polls=[]
    def call(method,**kwargs):
        if method=='getMe': return {'username':'offline'}
        if method=='getWebhookInfo': return {}
        polls.append(kwargs['_attempt'])
        if len(polls)==1: raise bot.TelegramError(0,'timeout',exception_type='TimeoutError')
        (tmp_path/'telegram.maintenance').touch()
        return []
    api.call.side_effect=call
    wire_service(monkeypatch,api)
    delays=[]
    monkeypatch.setattr(bot,'wait_or_maintenance',lambda root,delay:delays.append(delay) or False)
    with caplog.at_level(logging.INFO,logger='telegram_operator'):
        assert bot.run_bot(tmp_path,'offline')==0
    assert polls==[1,2] and delays==[5]
    assert 'polling_connected recovery=True' in caplog.text


@pytest.mark.parametrize('code',[401,403,407,409])
def test_permanent_polling_failure_requires_operator(tmp_path,monkeypatch,code):
    api=Mock()
    def call(method,**kwargs):
        if method=='getMe': return {'username':'offline'}
        if method=='getWebhookInfo': return {}
        raise bot.TelegramError(code)
    api.call.side_effect=call
    wire_service(monkeypatch,api)
    wait=Mock(side_effect=AssertionError('no retry'))
    monkeypatch.setattr(bot,'wait_or_maintenance',wait)
    assert bot.run_bot(tmp_path,'offline')==5
    wait.assert_not_called()
