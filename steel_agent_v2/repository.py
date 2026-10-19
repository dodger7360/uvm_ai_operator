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
            # Schema/cursor checks must see one snapshot while other users admit jobs.
            c.execute('BEGIN')
            tables={row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables - {'steel_v2_active','steel_v2_version','steel_v2_turn','steel_v2_inflight',
                         'steel_v2_inbox','steel_v2_attempt','steel_v2_outbox','steel_v2_queue_meta'}:
                raise ValueError('Refusing to initialize v2 over a non-v2 database')
            from .queue import TABLES, DurableQueue
            core={'steel_v2_active','steel_v2_version','steel_v2_turn','steel_v2_inflight'}
            if tables and not core.issubset(tables):raise ValueError('incomplete_v2_schema')
            if tables.intersection(TABLES):
                if not set(TABLES).issubset(tables):raise ValueError('incomplete_queue_schema')
                version=c.execute("SELECT value FROM steel_v2_queue_meta WHERE key='schema'").fetchone()
                if not version or version[0]!=1:raise ValueError('unsupported_queue_schema')
                DurableQueue._validate_schema(c)
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

    def pending(self, user):
        """Latest unresolved, KNOWN completed validation failure, in existing history.

        It is not active state and does not authorize replacement or API replay.
        Large originals are read directly, independently of bounded chat history.
        """
        self._allowed(user)
        with self.database.connection() as c:
            rows=c.execute("SELECT event_id,message,reply FROM steel_v2_turn WHERE user_id=? "
                "AND EXISTS(SELECT 1 FROM json_each(steel_v2_turn.reply,'$.diagnostics') "
                "WHERE value='pending_source' OR value LIKE 'pending_resolved:%') ORDER BY sequence DESC", (user,)).fetchall()
        resolved=set()
        for row in rows:
            reply=Reply.model_validate_json(row['reply'])
            resolved.update(code[len('pending_resolved:'):] for code in reply.diagnostics if code.startswith('pending_resolved:'))
            if 'pending_source' in reply.diagnostics and row['event_id'] not in resolved:
                return {'event_id':row['event_id'], 'source_text':row['message'],
                        'verified_partial':reply.pending_snapshot.model_dump(mode='json') if reply.pending_snapshot else None}
        return None

    def clarification(self, user, current):
        """Only the immediate, revision-bound continuation; never cross requests/users."""
        self._allowed(user)
        if current is None:return None
        with self.database.connection() as c:
            row=c.execute('SELECT reply FROM steel_v2_turn WHERE user_id=? ORDER BY sequence DESC LIMIT 1',
                          (user,)).fetchone()
        value=Reply.model_validate_json(row[0]).clarification if row else None
        return value if value and (value.request_id,value.revision)==(current.id,current.revision) else None

    def last_display(self, user, current):
        """Durable last full normalized/forward view; never infer an old order."""
        self._allowed(user)
        if current is None:return None
        with self.database.connection() as c:
            has_queue=c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='steel_v2_inbox'").fetchone()
            delivered=(" AND NOT EXISTS(SELECT 1 FROM steel_v2_inbox q "
                       "WHERE steel_v2_turn.event_id='telegram:'||q.update_id AND q.state!='delivered')"
                       if has_queue else "")
            row=c.execute("SELECT reply FROM steel_v2_turn WHERE user_id=? "
                          "AND json_type(reply,'$.display')='object'"+delivered+" ORDER BY sequence DESC LIMIT 1",
                          (user,)).fetchone()
        value=Reply.model_validate_json(row[0]).display if row else None
        if (value and (value.request_id,value.revision)==(current.id,current.revision)
                and len(value.item_ids)==len(current.items)
                and set(value.item_ids)=={item.id for item in current.items}):
            return value
        return None

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

    def source_search(self, user, current):
        """Latest search in this request/revision, never silently substitute an older query.

        A failed or undelivered latest search is a barrier. Global chat searches
        are available only without an active request. Uses existing turn JSON.
        """
        self._allowed(user)
        with self.database.connection() as c:
            c.execute('BEGIN')
            row=c.execute("SELECT event_id,reply FROM steel_v2_turn WHERE user_id=? "
                          "AND request_id IS ? AND json_extract(reply,'$.intent')='web_search' "
                          "ORDER BY sequence DESC LIMIT 1",(user,current.id if current else None)).fetchone()
            if row is None:return None
            reply=Reply.model_validate_json(row['reply'])
            if (reply.status!='ok' or (reply.request_id,reply.revision)!=
                    ((current.id,current.revision) if current else (None,None))):return None
            has_queue=c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='steel_v2_inbox'").fetchone()
            if has_queue:
                job=c.execute("SELECT state FROM steel_v2_inbox WHERE 'telegram:'||update_id=?",
                              (row['event_id'],)).fetchone()
                if job is not None and job['state']!='delivered':return None
        return {'event_id':row['event_id'],'reply':reply}

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
