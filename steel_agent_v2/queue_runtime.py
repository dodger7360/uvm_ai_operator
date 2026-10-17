"""One poller, bounded concurrent executions, per-user durable FIFO.

No entrypoint: called only inside the existing worker process lock.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import json
import logging
import sqlite3
from types import SimpleNamespace
from threading import Event, Thread
from database import Database
from diagnostics import operation, emit
from telegram_access import ADMIN_COMMANDS
from windows_runtime import maintenance_requested, wait_or_maintenance
from .agent import SteelAgent
from .repository import Repository
from .queue import DurableQueue, QueueConfig, Backpressure, QueueConflict, minimal_update
from .transport import OpenAITransport

LOG = logging.getLogger('steel_agent_v2_queue')


class AttemptTransport:
    def __init__(self, delegate, queue, number, api_guard=nullcontext):
        self.delegate,self.queue,self.number=delegate,queue,number
        self.api_guard=api_guard

    def _call(self, kind, method, *args, **kwargs):
        with self.api_guard():attempt=self.queue.start_api(self.number,kind)
        try:
            result=method(*args,**kwargs)
        except Exception as error:
            # HTTP error responses and parsing/validation failures have known outcomes.
            # A generic timeout/network error is deliberately NOT retryable.
            from openai import APIStatusError
            known=getattr(error,'outcome_known',isinstance(error,(APIStatusError,ValueError)))
            self.queue.end_api(self.number,attempt,known=known)
            raise
        self.queue.end_api(self.number,attempt,known=True)
        return result

    def plan(self,*args,**kwargs):return self._call('plan',self.delegate.plan,*args,**kwargs)
    def search(self,*args,**kwargs):return self._call('search',self.delegate.search,*args,**kwargs)


class Executor:
    def __init__(self, queue, access, api_factory, transport_factory=None, stopping=lambda:False):
        self.queue,self.access,self.api_factory=queue,access,api_factory
        self.transport_factory=transport_factory or (lambda:OpenAITransport.from_environment(model='gpt-6-luna',reasoning='high'))
        self.stopping=stopping

    def execute(self, job):
        number,user=job['update_id'],job['user_id']
        delegate=None
        with operation() as trace:
            emit('steel_v2_queue_execution',operation=trace.operation_id,update_id=number,state='claimed')
            try:
                if self.stopping():
                    self.queue.change(number,'claimed','queued')
                    return
                update=json.loads(job['payload'])
                message=update.get('message',{})
                if 'claim_ciphertext' in message:
                    from .windows_secrets import unprotect
                    message['text']=unprotect(message.pop('claim_ciphertext'))
                api=self.api_factory()
                text=update.get('message',{}).get('text','')
                command=text.split(maxsplit=1)[0].split('@')[0] if text.strip() else ''
                administrative=('callback_query' in update or command in ADMIN_COMMANDS | {'/claim'} or
                    not job['admission_allowed'] or not self.access.allowed(user))
                if administrative:
                    # Unknown cross-database administrative outcomes are quarantined, never replayed.
                    attempt=self.queue.start_api(number,'access')
                    proxy=SimpleNamespace(call=lambda method,**payload:self.queue.administrative_call(number,method,payload,api))
                    privileged=(command in ADMIN_COMMANDS or 'callback_query' in update and update['callback_query'].get('data')!='access:request')
                    if privileged and not job['admission_admin']:
                        proxy.call('sendMessage',chat_id=user,text='Административное действие не было разрешено при получении сообщения. Права не изменены.')
                    elif not self.access.route(update,proxy):
                        if job['admission_allowed']:raise QueueConflict('access_route_changed')
                        proxy.call('sendMessage',chat_id=user,text='Сообщение получено до предоставления доступа и не обрабатывалось ИИ. Пришлите его повторно, если оно актуально.')
                    self.queue.end_api(number,attempt,known=True)
                    self.queue.complete_access(number)
                    return
                if not self.access.allowed(user):
                    self.queue.change(number,'claimed','denied')
                    return
                grant=self.access.repository.get(user)
                revision=grant['revision']
                if job['admission_revision'] is not None and job['admission_revision']!=revision:
                    self.queue.deny(number)
                    return
                self.queue.bind_permission(number,revision)
                guard=lambda:self.access.repository.commit_permission(user,expected_revision=revision)
                if not isinstance(text,str) or not text.strip():
                    # Unsupported attachments are a durable local response, not an AI operation.
                    with self.queue.write() as c:
                        c.execute("INSERT INTO steel_v2_outbox VALUES(?,0,'sendMessage',?,'delivery_pending',NULL)",
                            (number,json.dumps({'chat_id':user,'text':'Пришлите текст сообщения; вложения пока не поддерживаются.'})))
                        c.execute("UPDATE steel_v2_inbox SET state='committed',committed=1 WHERE update_id=?",(number,))
                else:
                    delegate=self.transport_factory()  # Independent SDK client for every execution.
                    repo=Repository(self.queue.database,authorize=self.access.allowed,
                        commit_guard=lambda _:guard(),finish_hook=self.queue.finish_hook(number))
                    service=SteelAgent(AttemptTransport(delegate,self.queue,number,api_guard=guard),repo)
                    reply=service.handle(user,text,event_id='telegram:'+str(number))
                    if trace.result=='ok' and reply.status!='ok':trace.result=reply.status
                    if not self.queue.job(number)['committed']:
                        # A previously completed pre-queue turn can be replayed without a new finish.
                        # Reuse only the exact durable reply already verified by Repository.begin.
                        with guard():
                            with self.queue.write() as c:
                                old=c.execute('SELECT reply FROM steel_v2_turn WHERE user_id=? AND event_id=?',
                                    (user,'telegram:'+str(number))).fetchone()
                                if old is None or old[0]!=reply.model_dump_json():raise QueueConflict('execution_not_committed')
                                self.queue.finish_hook(number)(c,user,'telegram:'+str(number),reply,None)
                                # The pre-queue release had no delivery journal. Its cached reply
                                # proves a commit, NOT that sending never started. Quarantine it.
                                c.execute("UPDATE steel_v2_inbox SET state='delivery_unknown' WHERE update_id=?",(number,))
                                c.execute("UPDATE steel_v2_outbox SET state='delivery_unknown' WHERE update_id=?",(number,))
                self.queue.deliver(number,api,self.access)
            except PermissionError:
                self.queue.deny(number)
            except BaseException as error:
                if not (isinstance(error,sqlite3.OperationalError) and self.queue.delivery_retry_safe(number)):
                    self.queue.unknown(number)
                LOG.warning('queue_execution_stopped exception=%s',type(error).__name__)
            finally:
                if delegate is not None:
                    client=getattr(delegate,'client',None)
                    if client is not None:
                        try:client.close()
                        except Exception:LOG.warning('queue_client_close_failed')
                try:final_state=self.queue.job(number)['state']
                except Exception:final_state='queue_state_unavailable'
                if trace.result=='ok' and final_state in {'execution_unknown','delivery_unknown','denied','rejected','queue_state_unavailable'}:
                    trace.result=final_state
                emit('steel_v2_queue_state',operation=trace.operation_id,update_id=number,state=final_state)


class Scheduler:
    """No speculative in-memory backlog: at most workers durable claims are submitted."""
    def __init__(self, queue, executor, stopping=lambda:False):
        self.queue,self.executor=queue,executor
        self.pool=ThreadPoolExecutor(max_workers=queue.config.workers,thread_name_prefix='steel-v2')
        self.running={}
        self.halt=Event()
        self.stopping=lambda:self.halt.is_set() or stopping()
        previous_stop=executor.stopping
        executor.stopping=lambda:self.stopping() or previous_stop()
        self.thread=None
        self.failure=None

    def start(self):
        """Scheduling must continue while the sole poller is in long getUpdates."""
        def loop():
            try:
                while not self.stopping():
                    self.tick()
                    self.halt.wait(.02)
            except BaseException as error:
                self.failure=type(error).__name__
                self.halt.set()
                LOG.warning('queue_scheduler_stopped exception=%s',self.failure)
        self.thread=Thread(target=loop,name='steel-v2-scheduler')
        self.thread.start()

    def _resume_delivery(self, number):
        if self.stopping():return
        with operation() as trace:
            emit('steel_v2_queue_execution',operation=trace.operation_id,update_id=number,state='resume_delivery')
            try:self.queue.deliver(number,self.executor.api_factory(),self.executor.access)
            except BaseException as error:
                if not (isinstance(error,sqlite3.OperationalError) and self.queue.delivery_retry_safe(number)):
                    self.queue.unknown(number)
                LOG.warning('queue_delivery_stopped exception=%s',type(error).__name__)
            finally:
                try:trace.result=self.queue.job(number)['state']
                except Exception:trace.result='queue_state_unavailable'

    def tick(self):
        for number,future in list(self.running.items()):
            if future.done():
                try:future.result()
                finally:del self.running[number]
        if self.stopping():return
        try:
            self.queue.compact()
            candidates=self.queue.delivery_candidates()
        except sqlite3.OperationalError:return  # No new claim or external call has started.
        for number in candidates:
            if len(self.running)>=self.queue.config.workers or self.stopping():break
            if number not in self.running:
                self.running[number]=self.pool.submit(self._resume_delivery,number)
        while len(self.running)<self.queue.config.workers and not self.stopping():
            try:job=self.queue.claim()
            except sqlite3.OperationalError:break
            if job is None:break
            self.running[job['update_id']]=self.pool.submit(self.executor.execute,job)

    def close(self):
        # Cooperative drain, no kill and no new tasks; stop script may report its existing timeout.
        self.halt.set()
        if self.thread is not None:self.thread.join()
        self.pool.shutdown(wait=True,cancel_futures=True)


def run_queue(root, poll_api, access, legacy, database, *, api_factory):
    queue=DurableQueue(Database(database),QueueConfig.load(root))
    offset=legacy.get_offset()
    queue.align_offset(offset)
    queue.recover()
    stopping=lambda:maintenance_requested(root)
    scheduler=Scheduler(queue,Executor(queue,access,api_factory,stopping=stopping),stopping=stopping)
    if queue.cursor()>offset:
        # Recover a crash after admission but before the legacy mirror write.
        offset=queue.cursor()
        legacy.set_offset(offset)
    failures=0
    connected=False
    scheduler.start()
    try:
        while not maintenance_requested(root) and scheduler.failure is None:
            try:
                if stopping() or scheduler.failure is not None:break
                updates=poll_api.call('getUpdates',offset=offset,timeout=25,limit=1,
                    allowed_updates=['message','callback_query'],_attempt=failures+1)
                if not connected or failures:LOG.info('polling_connected recovery=%s',bool(failures))
                connected=True
                for update in updates:
                    if maintenance_requested(root) or scheduler.failure is not None:break
                    _,actor,_,_=minimal_update(update)
                    grant=access.repository.get(actor) if type(actor) is int else None
                    if grant and grant['status']=='approved':
                        try:
                            with access.repository.commit_permission(actor,expected_revision=grant['revision']):
                                cursor=queue.admit(update,allowed=True,revision=grant['revision'],is_admin=grant['role']=='admin')
                        except PermissionError:cursor=queue.admit(update,allowed=False)
                    else:cursor=queue.admit(update,allowed=False,revision=grant['revision'] if grant else None)
                    # This is deliberately after the inbox transaction, never before it.
                    legacy.set_offset(max(offset,cursor))
                    offset=max(offset,cursor)
                failures=0
            except Backpressure:
                wait_or_maintenance(root,0.1)
            except sqlite3.OperationalError:
                # No accepted event is lost; reretrieve with the unchanged committed offset.
                wait_or_maintenance(root,0.1)
            except Exception as error:
                from telegram_bot import TelegramError
                if not isinstance(error,TelegramError):raise
                failures+=1
                if error.code in {401,403,407,409}:return 5
                wait_or_maintenance(root,min(5*2**min(failures-1,4),60))
        return 1 if scheduler.failure is not None else 0
    finally:
        scheduler.close()
