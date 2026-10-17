"""Durable admission/execution/outbox primitives. No network or polling on import."""
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from telegram_operator import safe_chunks


TERMINAL = ('delivered', 'denied', 'rejected')
UNKNOWN = ('execution_unknown', 'delivery_unknown')
TABLES = ('steel_v2_inbox', 'steel_v2_attempt', 'steel_v2_outbox', 'steel_v2_queue_meta')


class Backpressure(RuntimeError):pass
class QueueConflict(RuntimeError):pass


@dataclass(frozen=True)
class QueueConfig:
    workers: int = 3
    max_pending: int = 100
    max_pending_per_user: int = 20
    max_pending_bytes: int = 8 * 1024 * 1024

    def __post_init__(self):
        for value in vars(self).values():
            if type(value) is not int or value <= 0:raise ValueError('invalid_queue_configuration')
        if self.workers > 32 or self.max_pending_per_user > self.max_pending:
            raise ValueError('invalid_queue_configuration')

    @classmethod
    def load(cls, root):
        path = Path(root)/'telegram-queue.json'
        return cls(**json.loads(path.read_text(encoding='utf-8-sig'))) if path.exists() else cls()


def minimal_update(update):
    """Preserve source text exactly, bound ancillary Telegram metadata. Never log it."""
    if not isinstance(update,dict):raise ValueError('invalid_update')
    number = update.get('update_id')
    if type(number) is not int or number < 0:raise ValueError('invalid_update_id')
    kind = 'callback_query' if 'callback_query' in update else 'message'
    event = update.get(kind, {})
    if not isinstance(event,dict):return number,0,dict(update_id=number),'rejected'
    actor = event.get('from', {})
    if not isinstance(actor,dict):return number,0,dict(update_id=number),'rejected'
    user = actor.get('id')
    msg = event.get('message', {}) if kind == 'callback_query' else event
    if not isinstance(msg,dict):return number,0,dict(update_id=number),'rejected'
    chat = msg.get('chat', {})
    if not isinstance(chat,dict):return number,0,dict(update_id=number),'rejected'
    if (type(user) is not int or user <= 0 or actor.get('is_bot') or
            chat.get('id') != user or chat.get('type') != 'private'):
        return number, 0, dict(update_id=number), 'rejected'
    person = {'id':user}
    for key, limit in [('first_name',128),('last_name',128),('username',64)]:
        if isinstance(actor.get(key),str):person[key]=actor[key][:limit]
    message = {'from':person, 'chat':{'id':user,'type':'private'}}
    if kind == 'message':
        text = msg.get('text')
        if isinstance(text,str):
            message['text']=text
        value = dict(update_id=number, message=message)
    else:
        data, callback_id = event.get('data'), event.get('id')
        if not isinstance(data,str) or len(data.encode()) > 64 or not isinstance(callback_id,str) or len(callback_id)>128:
            return number,user,dict(update_id=number),'rejected'
        message['message_id']=msg.get('message_id')
        value = dict(update_id=number, callback_query={'from':person,'id':callback_id,'data':data,'message':message})
    return number, user, value, 'queued'


class DurableQueue:
    def __init__(self, database, config=None):
        self.database, self.config = database, config or QueueConfig()
        with self.write() as c:
            tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            core={'steel_v2_active','steel_v2_version','steel_v2_turn','steel_v2_inflight'}
            if not core.issubset(tables) or tables-core-set(TABLES):raise ValueError('not_an_initialized_v2_database')
            if tables.intersection(TABLES) and not set(TABLES).issubset(tables):raise ValueError('incomplete_queue_schema')
            if 'steel_v2_queue_meta' in tables:
                version=c.execute("SELECT value FROM steel_v2_queue_meta WHERE key='schema'").fetchone()
                if not version or version[0]!=1:raise ValueError('unsupported_queue_schema')
                self._validate_schema(c)
            # executescript ends its preceding transaction; explicitly start the DDL transaction.
            c.executescript('''BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS steel_v2_queue_meta(key TEXT PRIMARY KEY,value INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS steel_v2_inbox(
                    update_id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL,input_hash TEXT NOT NULL,
                    payload TEXT,bytes INTEGER NOT NULL,state TEXT NOT NULL CHECK(state IN
                    ('queued','claimed','api_started','result_received','committed','delivery_pending','delivery_started',
                    'delivered','denied','rejected','execution_unknown','delivery_unknown')),admission_allowed INTEGER NOT NULL,
                    served INTEGER NOT NULL DEFAULT 0,committed INTEGER NOT NULL DEFAULT 0,
                    uncertain INTEGER NOT NULL DEFAULT 0,admission_revision INTEGER,access_revision INTEGER,
                    admission_admin INTEGER NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS steel_v2_queue_head ON steel_v2_inbox(user_id,update_id,state);
                CREATE TABLE IF NOT EXISTS steel_v2_attempt(
                    update_id INTEGER NOT NULL,number INTEGER NOT NULL,kind TEXT NOT NULL CHECK(kind IN ('plan','search','access')),
                    state TEXT NOT NULL CHECK(state IN ('api_started','result_received','execution_unknown')),
                    PRIMARY KEY(update_id,number));
                CREATE TABLE IF NOT EXISTS steel_v2_outbox(
                    update_id INTEGER NOT NULL,part INTEGER NOT NULL,method TEXT NOT NULL,payload TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('delivery_pending','delivery_started','delivery_unknown','delivered','rejected')),
                    message_id INTEGER,PRIMARY KEY(update_id,part));
            ''')
            version = c.execute("SELECT value FROM steel_v2_queue_meta WHERE key='schema'").fetchone()
            if version and version[0] != 1:raise ValueError('unsupported_queue_schema')
            c.execute("INSERT OR IGNORE INTO steel_v2_queue_meta VALUES('schema',1)")
            c.execute("INSERT OR IGNORE INTO steel_v2_queue_meta VALUES('cursor',0)")
            c.execute("INSERT OR IGNORE INTO steel_v2_queue_meta VALUES('served',0)")
            self._validate_schema(c)

    @staticmethod
    def _validate_schema(c):
        columns={
            'steel_v2_inbox':{'update_id','user_id','input_hash','payload','bytes','state','admission_allowed','served',
                'committed','uncertain','admission_revision','access_revision','admission_admin'},
            'steel_v2_attempt':{'update_id','number','kind','state'},
            'steel_v2_outbox':{'update_id','part','method','payload','state','message_id'},
            'steel_v2_queue_meta':{'key','value'}}
        for table,expected in columns.items():
            if {r[1] for r in c.execute('PRAGMA table_info('+table+')')}!=expected:
                raise ValueError('invalid_queue_schema')

    @contextmanager
    def write(self):
        with self.database.connection() as c:
            c.execute('PRAGMA busy_timeout=1000')
            c.execute('BEGIN IMMEDIATE')
            yield c

    def job(self, number):
        with self.database.connection() as c:
            row = c.execute('SELECT * FROM steel_v2_inbox WHERE update_id=?',(number,)).fetchone()
        return dict(row) if row else None

    def cursor(self):
        with self.database.connection() as c:
            return c.execute("SELECT value FROM steel_v2_queue_meta WHERE key='cursor'").fetchone()[0]

    def align_offset(self, legacy_offset):
        """Adopt a pre-queue baseline once; never hide a later offset/inbox inconsistency."""
        with self.write() as c:
            cursor=c.execute("SELECT value FROM steel_v2_queue_meta WHERE key='cursor'").fetchone()[0]
            initialized=c.execute("SELECT value FROM steel_v2_queue_meta WHERE key='initialized'").fetchone()
            if legacy_offset>cursor:
                if initialized or cursor or c.execute('SELECT 1 FROM steel_v2_inbox').fetchone():
                    raise QueueConflict('legacy_offset_ahead_of_durable_inbox')
                c.execute("UPDATE steel_v2_queue_meta SET value=? WHERE key='cursor'",(legacy_offset,))
                cursor=legacy_offset
            c.execute("INSERT OR IGNORE INTO steel_v2_queue_meta VALUES('initialized',1)")
        return cursor

    def admit(self, update, *, allowed, revision=None, is_admin=False):
        number,user,value,state = minimal_update(update)
        payload = json.dumps(value,ensure_ascii=False,separators=(',',':'))
        digest = sha256(payload.encode()).hexdigest()
        text=value.get('message',{}).get('text','')
        if text.strip() and text.split(maxsplit=1)[0].split('@')[0]=='/claim':
            from .windows_secrets import protect
            value['message']['claim_ciphertext']=protect(text)
            value['message']['text']='/claim'
            payload=json.dumps(value,ensure_ascii=False,separators=(',',':'))
        size = len(payload.encode())
        if size > self.config.max_pending_bytes:raise Backpressure('single_event_exceeds_capacity')
        with self.write() as c:
            old = c.execute('SELECT input_hash FROM steel_v2_inbox WHERE update_id=?',(number,)).fetchone()
            cursor = c.execute("SELECT value FROM steel_v2_queue_meta WHERE key='cursor'").fetchone()[0]
            if old:
                if old[0]!=digest:raise QueueConflict('update_id_payload_conflict')
                return max(cursor,number+1)
            # A pruned terminal event below the durable watermark is never executed again.
            if number < cursor:return cursor
            count,total = c.execute("SELECT count(*),coalesce(sum(bytes),0) FROM steel_v2_inbox WHERE state NOT IN ('delivered','denied','rejected')").fetchone()
            user_count = c.execute("SELECT count(*) FROM steel_v2_inbox WHERE user_id=? AND state NOT IN ('delivered','denied','rejected')",(user,)).fetchone()[0]
            if state == 'queued' and (count>=self.config.max_pending or user_count>=self.config.max_pending_per_user or total+size>self.config.max_pending_bytes):
                raise Backpressure('queue_capacity')
            ticket=c.execute('SELECT max(served) FROM steel_v2_inbox WHERE user_id=?',(user,)).fetchone()[0]
            if ticket is None:ticket=c.execute("SELECT value FROM steel_v2_queue_meta WHERE key='served'").fetchone()[0]
            c.execute('INSERT INTO steel_v2_inbox(update_id,user_id,input_hash,payload,bytes,state,admission_allowed,admission_revision,served,admission_admin) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (number,user,digest,payload,size,state,int(bool(allowed)),revision,ticket,int(bool(allowed and is_admin))))
            c.execute("UPDATE steel_v2_queue_meta SET value=? WHERE key='cursor'",(max(cursor,number+1),))
        return max(cursor,number+1)

    def claim(self):
        with self.write() as c:
            row = c.execute('''SELECT j.* FROM steel_v2_inbox j WHERE j.state='queued'
                AND NOT EXISTS(SELECT 1 FROM steel_v2_inbox p WHERE p.user_id=j.user_id AND p.update_id<j.update_id
                    AND p.state NOT IN ('delivered','denied','rejected'))
                ORDER BY (SELECT coalesce(max(p.served),0) FROM steel_v2_inbox p WHERE p.user_id=j.user_id),j.update_id LIMIT 1''').fetchone()
            if row is None:return None
            c.execute("UPDATE steel_v2_queue_meta SET value=value+1 WHERE key='served'")
            tick = c.execute("SELECT value FROM steel_v2_queue_meta WHERE key='served'").fetchone()[0]
            changed=c.execute("UPDATE steel_v2_inbox SET state='claimed',served=? WHERE update_id=? AND state='queued'",(tick,row['update_id'])).rowcount
            if changed!=1:raise QueueConflict('claim_conflict')
            return dict(row)

    def change(self, number, old, new):
        if (old,new) not in {('claimed','queued'),('claimed','denied'),('committed','delivery_pending'),
                            ('delivery_pending','delivered'),('delivery_pending','execution_unknown')}:
            raise QueueConflict('forbidden_queue_transition')
        with self.write() as c:
            if c.execute('UPDATE steel_v2_inbox SET state=? WHERE update_id=? AND state=?',(new,number,old)).rowcount!=1:
                raise QueueConflict('invalid_queue_transition')

    def bind_permission(self, number, revision):
        with self.write() as c:
            if c.execute("UPDATE steel_v2_inbox SET access_revision=? WHERE update_id=? AND state='claimed'",(revision,number)).rowcount!=1:
                raise QueueConflict('permission_binding_conflict')

    def start_api(self, number, kind):
        with self.write() as c:
            if c.execute("UPDATE steel_v2_inbox SET state='api_started' WHERE update_id=? AND state IN ('claimed','result_received')",(number,)).rowcount!=1:
                raise QueueConflict('api_start_conflict')
            attempt=c.execute('SELECT coalesce(max(number),0)+1 FROM steel_v2_attempt WHERE update_id=?',(number,)).fetchone()[0]
            c.execute('INSERT INTO steel_v2_attempt VALUES(?,?,?,?)',(number,attempt,kind,'api_started'))
        return attempt

    def end_api(self, number, attempt, *, known):
        with self.write() as c:
            if c.execute("UPDATE steel_v2_attempt SET state=? WHERE update_id=? AND number=? AND state='api_started'",
                    ('result_received' if known else 'execution_unknown',number,attempt)).rowcount!=1:
                raise QueueConflict('attempt_receipt_conflict')
            if c.execute("UPDATE steel_v2_inbox SET state=? WHERE update_id=? AND state='api_started'",
                    ('result_received' if known else 'execution_unknown',number)).rowcount!=1:
                raise QueueConflict('api_receipt_conflict')

    def finish_hook(self, number):
        def hook(c, user, event, reply, updated):
            row=c.execute('SELECT state,user_id FROM steel_v2_inbox WHERE update_id=?',(number,)).fetchone()
            if row is None or row['user_id']!=user or event!='telegram:'+str(number):raise QueueConflict('job_identity_conflict')
            if row['state'] not in {'claimed','result_received','execution_unknown'}:raise QueueConflict('unconfirmed_execution')
            if row['state']=='execution_unknown' and updated is not None:raise QueueConflict('unknown_cannot_mutate')
            for n,part in enumerate(safe_chunks(reply.text)):
                c.execute('INSERT INTO steel_v2_outbox VALUES(?,?,?,?,?,NULL)',
                    (number,n,'sendMessage',json.dumps({'chat_id':user,'text':part},ensure_ascii=False),'delivery_pending'))
            c.execute("UPDATE steel_v2_inbox SET committed=1,state='committed',uncertain=? WHERE update_id=?",
                (int(row['state']=='execution_unknown'),number))
        return hook

    def administrative_call(self, number, method, payload, api):
        """Journal actual access notifications; admission is not a successful notification.

        AccessRepository may hold its existing legacy transaction while calling this.
        A crash of the enclosing administrative route is NEVER automatically replayed.
        """
        if method not in {'sendMessage','answerCallbackQuery','editMessageReplyMarkup'}:raise ValueError('unsupported_access_api')
        with self.write() as c:
            part=c.execute('SELECT coalesce(max(part),-1)+1 FROM steel_v2_outbox WHERE update_id=?',(number,)).fetchone()[0]
            c.execute('INSERT INTO steel_v2_outbox VALUES(?,?,?,?,?,NULL)',
                (number,part,method,json.dumps(payload,ensure_ascii=False),'delivery_started'))
        try:
            # Existing approval holds the legacy write transaction across notification.
            # Bound that notification below the normal SQLite wait; never wait for GPT here.
            result=api.call(method,_timeout=3,**payload)
            if method=='sendMessage' and (not isinstance(result,dict) or type(result.get('message_id')) is not int):
                raise QueueConflict('missing_telegram_receipt')
        except BaseException:
            with self.write() as c:
                c.execute("UPDATE steel_v2_outbox SET state='delivery_unknown' WHERE update_id=? AND part=?",(number,part))
            raise
        with self.write() as c:
            c.execute("UPDATE steel_v2_outbox SET state='delivered',message_id=? WHERE update_id=? AND part=?",
                (result.get('message_id') if isinstance(result,dict) else None,number,part))
        return result

    def complete_access(self, number):
        with self.write() as c:
            unknown=c.execute("SELECT 1 FROM steel_v2_outbox WHERE update_id=? AND state!='delivered'",(number,)).fetchone()
            self._archive_input(c,number)
            if c.execute("UPDATE steel_v2_inbox SET state=? WHERE update_id=? AND state='result_received'",('delivery_unknown' if unknown else 'delivered',number)).rowcount!=1:
                raise QueueConflict('access_completion_conflict')

    @staticmethod
    def _archive_input(c, number):
        """Do not discard accepted original text when closing a job without an agent finish."""
        from .contracts import Reply, RequestSnapshot
        from telegram_access import ADMIN_COMMANDS
        row=c.execute('SELECT user_id,payload FROM steel_v2_inbox WHERE update_id=?',(number,)).fetchone()
        if row is None or row['payload'] is None:return
        text=json.loads(row['payload']).get('message',{}).get('text')
        if not isinstance(text,str) or not text.strip():return
        if text.split(maxsplit=1)[0].split('@')[0] in ADMIN_COMMANDS | {'/claim'}:return
        user,event=row['user_id'],'telegram:'+str(number)
        if c.execute('SELECT 1 FROM steel_v2_turn WHERE user_id=? AND event_id=?',(user,event)).fetchone():return
        active=c.execute('SELECT sequence,payload FROM steel_v2_active WHERE user_id=?',(user,)).fetchone()
        sequence=(active['sequence'] if active else 0)+1
        payload=active['payload'] if active else None
        snapshot=RequestSnapshot.model_validate_json(payload) if payload else None
        reply=Reply(text='Операция закрыта без изменения заявки. Исходное сообщение сохранено; автоматического повторения нет.',
            status='failed',intent='clarify',request_id=snapshot.id if snapshot else None,revision=snapshot.revision if snapshot else None)
        c.execute('INSERT INTO steel_v2_active VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET sequence=excluded.sequence',
            (user,sequence,payload))
        c.execute('INSERT INTO steel_v2_turn VALUES(?,?,?,?,?,?,?)',
            (user,event,sha256(text.encode()).hexdigest(),text,reply.model_dump_json(),reply.request_id,sequence))

    def deny(self, number):
        with self.write() as c:
            row=c.execute('SELECT user_id,state FROM steel_v2_inbox WHERE update_id=?',(number,)).fetchone()
            if row['state'] in {'api_started','execution_unknown','delivery_started','delivery_unknown'}:
                c.execute('UPDATE steel_v2_inbox SET state=? WHERE update_id=?',
                    ('delivery_unknown' if row['state'].startswith('delivery') else 'execution_unknown',number))
                return
            if row['state'] not in {'claimed','result_received','committed','delivery_pending'}:
                raise QueueConflict('denial_state_conflict')
            self._archive_input(c,number)
            c.execute("UPDATE steel_v2_inbox SET state='denied' WHERE update_id=?",(number,))
            c.execute("UPDATE steel_v2_outbox SET state='rejected' WHERE update_id=? AND state='delivery_pending'",(number,))
            c.execute('DELETE FROM steel_v2_inflight WHERE user_id=? AND event_id=?',(row['user_id'],'telegram:'+str(number)))

    def recover(self):
        """Caller must hold the single worker process lock; no paid replay here."""
        with self.write() as c:
            c.execute("UPDATE steel_v2_inbox SET state='execution_unknown' WHERE state IN ('api_started','result_received')")
            c.execute("UPDATE steel_v2_attempt SET state='execution_unknown' WHERE state='api_started'")
            c.execute("UPDATE steel_v2_inbox SET state='delivery_unknown' WHERE update_id IN (SELECT update_id FROM steel_v2_outbox WHERE state='delivery_started')")
            c.execute("UPDATE steel_v2_outbox SET state='delivery_unknown' WHERE state='delivery_started'")
            rows=c.execute("SELECT update_id,user_id FROM steel_v2_inbox WHERE state='claimed'").fetchall()
            for row in rows:
                number,user=row
                attempts=c.execute('SELECT 1 FROM steel_v2_attempt WHERE update_id=?',(number,)).fetchone()
                if attempts:
                    c.execute("UPDATE steel_v2_inbox SET state='execution_unknown' WHERE update_id=?",(number,))
                else:
                    c.execute('DELETE FROM steel_v2_inflight WHERE user_id=? AND event_id=?',(user,'telegram:'+str(number)))
                    c.execute("UPDATE steel_v2_inbox SET state='queued' WHERE update_id=?",(number,))
            if c.execute("SELECT 1 FROM steel_v2_inflight f WHERE NOT EXISTS(SELECT 1 FROM steel_v2_inbox j WHERE j.user_id=f.user_id AND 'telegram:'||j.update_id=f.event_id)").fetchone():
                raise QueueConflict('unmanaged_legacy_inflight_requires_operator')

    def delivery_candidates(self):
        with self.database.connection() as c:
            return [r[0] for r in c.execute("SELECT update_id FROM steel_v2_inbox WHERE committed=1 AND state IN ('committed','delivery_pending') ORDER BY update_id")]

    def delivery_retry_safe(self, number):
        with self.database.connection() as c:
            row=c.execute('SELECT state,committed FROM steel_v2_inbox WHERE update_id=?',(number,)).fetchone()
            if row is None or not row['committed'] or row['state'] not in {'committed','delivery_pending'}:return False
            return c.execute("SELECT 1 FROM steel_v2_outbox WHERE update_id=? AND state NOT IN ('delivered','delivery_pending')",(number,)).fetchone() is None

    def deliver(self, number, api, access):
        job=self.job(number)
        if job['state']=='committed':self.change(number,'committed','delivery_pending')
        while True:
            with self.database.connection() as c:
                row=c.execute("SELECT * FROM steel_v2_outbox WHERE update_id=? AND state!='delivered' ORDER BY part LIMIT 1",(number,)).fetchone()
            if row is None:
                self.change(number,'delivery_pending','execution_unknown' if job['uncertain'] else 'delivered')
                return
            if row['state']!='delivery_pending':return
            # Legacy permission and the durable send-start marker share a linearization gate.
            # Revoke after this gate cannot cancel an already accepted external send.
            try:
                with access.repository.commit_permission(job['user_id'],expected_revision=job['access_revision']):
                    with self.write() as c:
                        if c.execute("UPDATE steel_v2_outbox SET state='delivery_started' WHERE update_id=? AND part=? AND state='delivery_pending'",(number,row['part'])).rowcount!=1:
                            raise QueueConflict('send_start_conflict')
                        c.execute("UPDATE steel_v2_inbox SET state='delivery_started' WHERE update_id=? AND state='delivery_pending'",(number,))
            except PermissionError:
                with self.write() as c:
                    c.execute("UPDATE steel_v2_inbox SET state='denied' WHERE update_id=? AND state='delivery_pending'",(number,))
                    c.execute("UPDATE steel_v2_outbox SET state='rejected' WHERE update_id=? AND state='delivery_pending'",(number,))
                return
            try:
                result=api.call(row['method'],**json.loads(row['payload']))
                if not isinstance(result,dict) or type(result.get('message_id')) is not int:
                    raise QueueConflict('missing_telegram_receipt')
            except BaseException:
                with self.write() as c:
                    c.execute("UPDATE steel_v2_outbox SET state='delivery_unknown' WHERE update_id=? AND part=?",(number,row['part']))
                    c.execute("UPDATE steel_v2_inbox SET state='delivery_unknown' WHERE update_id=?",(number,))
                raise
            with self.write() as c:
                if c.execute("UPDATE steel_v2_outbox SET state='delivered',message_id=? WHERE update_id=? AND part=? AND state='delivery_started'",(result['message_id'],number,row['part'])).rowcount!=1:
                    raise QueueConflict('delivery_receipt_conflict')
                c.execute("UPDATE steel_v2_inbox SET state='delivery_pending' WHERE update_id=? AND state='delivery_started'",(number,))

    def unknown(self, number):
        with self.write() as c:
            delivery=c.execute("SELECT 1 FROM steel_v2_outbox WHERE update_id=? AND state='delivery_started'",(number,)).fetchone()
            c.execute("UPDATE steel_v2_inbox SET state=? WHERE update_id=? AND state NOT IN ('delivered','denied','rejected','delivery_unknown')",
                ('delivery_unknown' if delivery else 'execution_unknown',number))
            c.execute("UPDATE steel_v2_outbox SET state='delivery_unknown' WHERE update_id=? AND state='delivery_started'",(number,))

    def rollback_ready(self):
        with self.database.connection() as c:
            jobs=c.execute("SELECT count(*) FROM steel_v2_inbox WHERE state NOT IN ('delivered','denied','rejected')").fetchone()[0]
            claims=c.execute('SELECT count(*) FROM steel_v2_inflight').fetchone()[0]
            sends=c.execute("SELECT count(*) FROM steel_v2_outbox WHERE state NOT IN ('delivered','rejected')").fetchone()[0]
            return jobs==0 and claims==0 and sends==0

    def compact(self):
        """Bound queue copies, retain source/history in the existing canonical turn tables."""
        with self.write() as c:
            c.execute("UPDATE steel_v2_inbox SET payload=NULL,bytes=0 WHERE state IN ('delivered','denied','rejected') AND (payload IS NOT NULL OR bytes!=0)")
            c.execute("UPDATE steel_v2_outbox SET payload='{}' WHERE state IN ('delivered','rejected') AND payload!='{}'")
            old=[r[0] for r in c.execute("SELECT update_id FROM steel_v2_inbox WHERE state IN ('delivered','denied','rejected') ORDER BY update_id DESC LIMIT -1 OFFSET 1000")]
            for number in old:
                c.execute('DELETE FROM steel_v2_outbox WHERE update_id=?',(number,))
                c.execute('DELETE FROM steel_v2_attempt WHERE update_id=?',(number,))
                c.execute('DELETE FROM steel_v2_inbox WHERE update_id=?',(number,))

    def resolve(self, number, confirmation):
        """Explicit abandon only, no retry, no send, no mutation. Offline operator only."""
        with self.write() as c:
            row=c.execute('SELECT * FROM steel_v2_inbox WHERE update_id=?',(number,)).fetchone()
            if row is None or row['state'] not in UNKNOWN or confirmation!=f"{number}:{row['state']}:{row['input_hash']}":
                raise QueueConflict('operator_confirmation_mismatch')
            self._archive_input(c,number)
            c.execute("UPDATE steel_v2_inbox SET state='rejected' WHERE update_id=?",(number,))
            c.execute("UPDATE steel_v2_outbox SET state='rejected' WHERE update_id=? AND state!='delivered'",(number,))
            c.execute('DELETE FROM steel_v2_inflight WHERE user_id=? AND event_id=?',(row['user_id'],'telegram:'+str(number)))
