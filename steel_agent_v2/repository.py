"""Versioned v2 tables over existing Database transactions; explicit DB only.

No legacy constructors or schema initialization; never auto-discovers a user DB.
"""
from hashlib import sha256
from contextlib import nullcontext
from .contracts import RequestSnapshot, Reply
from .engine import EngineError
from dialog_history import bounded_history


class Busy(EngineError):
    pass


class Repository:
    def __init__(self, database, *, authorize=None, commit_guard=None, finish_hook=None):
        self.database = database
        self.authorize = authorize or (lambda _: True)  # Trusted offline/core caller.
        self.commit_guard = commit_guard or (lambda _: nullcontext())
        self.finish_hook = finish_hook
        if str(database.path) == ':memory:':
            raise ValueError('Use an explicit isolated SQLite file with short-lived connections')
        with database.connection() as c:
            tables={row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables - {'steel_v2_active','steel_v2_version','steel_v2_turn','steel_v2_inflight',
                         'steel_v2_inbox','steel_v2_attempt','steel_v2_outbox','steel_v2_queue_meta'}:
                raise ValueError('Refusing to initialize v2 over a non-v2 database')
            c.executescript('''
                CREATE TABLE IF NOT EXISTS steel_v2_active(
                    user_id INTEGER PRIMARY KEY, sequence INTEGER NOT NULL, payload TEXT);
                CREATE TABLE IF NOT EXISTS steel_v2_version(
                    user_id INTEGER NOT NULL, request_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    payload TEXT NOT NULL, PRIMARY KEY(user_id,request_id,revision));
                CREATE TABLE IF NOT EXISTS steel_v2_turn(
                    user_id INTEGER NOT NULL, event_id TEXT NOT NULL, input_hash TEXT NOT NULL,
                    message TEXT NOT NULL, reply TEXT NOT NULL, request_id TEXT,
                    sequence INTEGER NOT NULL, PRIMARY KEY(user_id,event_id));
                CREATE TABLE IF NOT EXISTS steel_v2_inflight(
                    user_id INTEGER PRIMARY KEY, event_id TEXT NOT NULL, input_hash TEXT NOT NULL,
                    message TEXT NOT NULL);
            ''')

    def _allowed(self, user):
        if not self.authorize(user):
            raise PermissionError('Access denied')

    def begin(self, user, event, message):
        digest = sha256(message.encode()).hexdigest()
        with self.database.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            self._allowed(user)
            old = c.execute('SELECT input_hash,reply FROM steel_v2_turn WHERE user_id=? AND event_id=?', (user,event)).fetchone()
            if old:
                if old['input_hash'] != digest:
                    raise EngineError('event_id_reused_with_different_message')
                return Reply.model_validate_json(old['reply']), None, None
            if c.execute('SELECT 1 FROM steel_v2_inflight WHERE user_id=?', (user,)).fetchone():
                raise Busy('previous_operation_inflight; inspect interrupted operation before retry')
            c.execute('INSERT INTO steel_v2_inflight VALUES(?,?,?,?)', (user,event,digest,message))
            row = c.execute('SELECT sequence,payload FROM steel_v2_active WHERE user_id=?', (user,)).fetchone()
            return None, (RequestSnapshot.model_validate_json(row['payload']) if row and row['payload'] else None), (row['sequence'] if row else 0)

    def load(self, user):
        self._allowed(user)
        with self.database.connection() as c:
            row = c.execute('SELECT payload FROM steel_v2_active WHERE user_id=?',(user,)).fetchone()
        return RequestSnapshot.model_validate_json(row[0]) if row and row[0] else None

    def version(self, user, request_id, revision):
        self._allowed(user)
        with self.database.connection() as c:
            row = c.execute('SELECT payload FROM steel_v2_version WHERE user_id=? AND request_id=? AND revision=?',
                            (user,request_id,revision)).fetchone()
        return RequestSnapshot.model_validate_json(row[0]) if row else None

    def history(self, user, request_id):
        self._allowed(user)
        with self.database.connection() as c:
            rows = c.execute('SELECT message,reply FROM steel_v2_turn WHERE user_id=? '
                'AND (request_id IS NULL OR request_id=?) ORDER BY sequence DESC LIMIT 20', (user,request_id)).fetchall()
        messages = []
        for row in reversed(rows):
            messages.extend([{'role':'user','content':row['message']},
                             {'role':'assistant','content':Reply.model_validate_json(row['reply']).text}])
        return bounded_history(messages)

    def last_search(self, user):
        """Bounded technical summary from existing history, never model speculation."""
        self._allowed(user)
        with self.database.connection() as c:
            row=c.execute("SELECT reply FROM steel_v2_turn WHERE user_id=? "
                          "AND json_extract(reply,'$.intent')='web_search' "
                          "ORDER BY sequence DESC LIMIT 1",(user,)).fetchone()
        if not row:return None
        reply=Reply.model_validate_json(row[0])
        from .transport import SEARCH_REASONS
        reason=next((code[7:] for code in reply.diagnostics
                     if code.startswith('search:') and code[7:] in SEARCH_REASONS),'unknown')
        return {'status':reply.status,'reason':reason}

    def last_operation(self, user):
        """Immediate previous turn metadata; no message text or stale-search inference."""
        self._allowed(user)
        with self.database.connection() as c:
            row=c.execute('SELECT reply FROM steel_v2_turn WHERE user_id=? ORDER BY sequence DESC LIMIT 1',
                          (user,)).fetchone()
        if not row:return None
        reply=Reply.model_validate_json(row[0])
        return {'intent':reply.intent,'status':reply.status}

    def finish(self, user, event, sequence, message, reply, updated=None):
        with self.commit_guard(user):
            return self._finish(user, event, sequence, message, reply, updated)

    def _finish(self, user, event, sequence, message, reply, updated=None):
        digest = sha256(message.encode()).hexdigest()
        with self.database.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            self._allowed(user)
            claim = c.execute('SELECT event_id,input_hash FROM steel_v2_inflight WHERE user_id=?',(user,)).fetchone()
            if not claim or tuple(claim) != (event,digest):
                raise EngineError('operation_claim_mismatch')
            row = c.execute('SELECT sequence,payload FROM steel_v2_active WHERE user_id=?',(user,)).fetchone()
            if (row['sequence'] if row else 0) != sequence:
                raise EngineError('concurrent_state_change')
            payload = updated.model_dump_json() if updated is not None else (row['payload'] if row else None)
            if updated is not None:
                c.execute('INSERT INTO steel_v2_version VALUES(?,?,?,?)', (user,updated.id,updated.revision,payload))
            c.execute('INSERT INTO steel_v2_active VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET '
                      'sequence=excluded.sequence,payload=excluded.payload', (user,sequence+1,payload))
            c.execute('INSERT INTO steel_v2_turn VALUES(?,?,?,?,?,?,?)',
                      (user,event,digest,message,reply.model_dump_json(),
                       None if reply.intent == 'free_chat' else reply.request_id,sequence+1))
            c.execute('DELETE FROM steel_v2_inflight WHERE user_id=? AND event_id=?',(user,event))
            if self.finish_hook:
                self.finish_hook(c, user, event, reply, updated)
        return reply

    def abandon(self, user, event):
        """Release this exact claim; never apply/retry an interrupted operation.

        A trusted operator may use this after confirming a crashed worker is gone.
        """
        with self.database.connection() as c:
            c.execute('DELETE FROM steel_v2_inflight WHERE user_id=? AND event_id=?', (user,event))
