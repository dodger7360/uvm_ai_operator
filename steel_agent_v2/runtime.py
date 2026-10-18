"""Persistent v2-only selection for the existing single Telegram worker.

Reading configuration does not create clients, open databases or start polling.
"""
import json
from pathlib import Path

CONFIG = 'telegram-handler.json'


def selection(root):
    root = Path(root).resolve()
    path = root / CONFIG
    if not path.exists():
        raise ValueError('v2_handler_configuration_required')
    config = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(config, dict) or set(config) - {'handler', 'database'}:
        raise ValueError('invalid_handler_configuration')
    handler = config.get('handler')
    if handler != 'v2' or not isinstance(config.get('database'), str) or not config['database'].strip():
        raise ValueError('invalid_handler_configuration')
    database = Path(config['database'])
    return handler, (root / database).resolve()


def connect(database, access):
    from .telegram import build_service, handle_update
    from .transport import OpenAITransport
    transport = OpenAITransport.from_environment(model='gpt-6-luna', reasoning='high')
    service = build_service(transport, database_path=database, access=access)
    return service, handle_update
