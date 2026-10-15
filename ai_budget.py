"""Persistent beta allowance. Reservations are never refunded, even on timeout.

This is a conservative call allowance, not the provider's wallet balance.
Only bounded text calls to the pinned model are admitted; paid tools are blocked.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
from pathlib import Path
import sqlite3
import time

from diagnostics import PipelineError, emit

ACTOR = ContextVar('budget_actor', default=None)
MAX_OUTPUT = 24000
MAX_PAYLOAD_BYTES = 48000


class BudgetLimit(PipelineError):
    def __init__(self, reason):
        self.reason = reason
        prefix = ('Интернет-поиск временно отключён на этапе бюджетного бета-теста. '
                  if reason == 'paid_tool' else 'AI-обработка остановлена по лимиту бета-теста. ')
        super().__init__('ai_budget_limit', prefix +
                         'Текущая заявка сохранена. Сохраните новый текст и сообщите администратору. '
                         'Просмотр и локальные команды доступны.')


@contextmanager
def budget_actor(user_id):
    token = ACTOR.set({'id': str(user_id), 'calls': 0})
    try:
        yield
    finally:
        ACTOR.reset(token)


class BetaBudget:
    # 60 x $0.05 = $3, leaving ~$1 of the reported wallet as a margin.
    # Never reset automatically. Preserve this ledger alongside the working DB.
    def __init__(self, path, *, total_calls=60, user_calls=20, user_hourly_calls=6, clock=time.time):
        if (not 1 <= total_calls <= 60 or not 1 <= user_calls <= 20
                or not 1 <= user_hourly_calls <= 8):
            raise ValueError('Invalid beta allowance')
        self.path, self.total_calls, self.user_calls = Path(path), total_calls, user_calls
        self.user_hourly_calls = user_hourly_calls
        self.clock = clock
        # Deployment must retain the ledger; a missing parent is a config error.
        with sqlite3.connect(self.path) as c:
            c.execute('CREATE TABLE IF NOT EXISTS ai_reservation '
                      '(id INTEGER PRIMARY KEY, actor TEXT NOT NULL, created REAL NOT NULL, '
                      'stage TEXT NOT NULL, reserved_cents INTEGER NOT NULL CHECK(reserved_cents=5))')

    def reserve(self, *, stage, model, payload):
        actor = ACTOR.get()
        reason = None
        if not actor:
            reason = 'missing_actor'
        elif os.getenv('OPENAI_BASE_URL', 'https://api.openai.com/v1').rstrip('/') != 'https://api.openai.com/v1':
            reason = 'endpoint'
        elif model != 'gpt-6-luna':
            reason = 'model'
        elif payload.get('tools'):
            reason = 'paid_tool'
        elif len(json.dumps(payload, ensure_ascii=False).encode('utf-8')) > MAX_PAYLOAD_BYTES:
            reason = 'payload_length'
        elif actor['calls'] >= 3:
            reason = 'message_calls'
        if reason:
            emit('ai_budget_blocked', reason=reason)
            raise BudgetLimit(reason)
        try:
            now = self.clock()
            with sqlite3.connect(self.path, timeout=10) as c:
                c.execute('BEGIN IMMEDIATE')
                total, user, hourly, user_hourly, daily = c.execute(
                    'SELECT COUNT(*), COALESCE(SUM(actor=?),0), '
                    'COALESCE(SUM(created>=?),0), COALESCE(SUM(actor=? AND created>=?),0), '
                    'COALESCE(SUM(actor=? AND created>=?),0) FROM ai_reservation',
                    (actor['id'], now-3600, actor['id'], now-3600, actor['id'], now-86400)).fetchone()
                if (total >= self.total_calls or user >= self.user_calls or hourly >= 18
                        or user_hourly >= self.user_hourly_calls or daily >= 10):
                    raise BudgetLimit('persistent_allowance')
                c.execute('INSERT INTO ai_reservation(actor,created,stage,reserved_cents) VALUES(?,?,?,5)',
                          (actor['id'], now, stage))
                reservation = c.execute('SELECT last_insert_rowid()').fetchone()[0]
        except BudgetLimit:
            emit('ai_budget_blocked', reason='persistent_allowance')
            raise
        except (sqlite3.Error, OSError):
            emit('ai_budget_blocked', reason='ledger_unavailable')
            raise BudgetLimit('ledger_unavailable') from None
        actor['calls'] += 1
        emit('ai_budget_reserved', reservation=reservation, stage=stage, reserved_cents=5,
             total_reserved_cents=(total+1)*5)
        return reservation
