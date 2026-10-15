from unittest.mock import Mock
import pytest

from telegram_bot import handle_update


@pytest.mark.parametrize('configured,expected', [(None,'1.01'), ('','1.01'), ('  ','1.01'), ('1.02','1.02')])
@pytest.mark.parametrize('command', ['/version', '/version@UVM_SteelBot'])
def test_version_is_local_and_does_not_process_request(monkeypatch, configured, expected, command):
    if configured is None:
        monkeypatch.delenv('APP_VERSION',raising=False)
    else:
        monkeypatch.setenv('APP_VERSION',configured)
    api, service, access = Mock(), Mock(), Mock()
    access.route.return_value=False
    access.allowed.return_value=True
    handle_update({'message':{'chat':{'type':'private','id':1},'from':{'id':1},'text':command}},service,api,access)
    api.call.assert_called_once_with('sendMessage', chat_id=1, text=f'UVM_Steel — версия {expected}')
    service.process.assert_not_called()
    service.repository.save.assert_not_called()


def test_version_keeps_access_gate():
    api, service, access=Mock(),Mock(),Mock()
    access.route.return_value=True
    handle_update({'message':{'chat':{'type':'private','id':1},'from':{'id':1},'text':'/version'}},service,api,access)
    api.call.assert_not_called()
    service.process.assert_not_called()
