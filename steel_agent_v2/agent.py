"""One GPT coordinator. No keyword/regex intent router and no legacy pipeline."""
import json
from pydantic import ValidationError
from .contracts import AgentPlan, Reply
from .engine import RequestEngine, EngineError, totals
from .repository import Busy
from .presentation import render
from .transport import SearchUnavailable, safe_source
from diagnostics import emit, safe_code, CURRENT


def source_lines(text):
    result=[];offset=0
    for line in text.splitlines(keepends=True):
        content=line.rstrip('\r\n')
        if content.strip():result.append({'start':offset,'end':offset+len(content),'text':content})
        offset+=len(line)
    return result


def validation_notes(error):
    if isinstance(error, ValidationError):
        return ['schema:' + '.'.join(map(str,e['loc'])) + ':' + e['type']
                for e in error.errors(include_input=False,include_context=False)]
    if isinstance(error, EngineError):return [str(error)]
    return ['invalid_json_or_structured_response']


class SteelAgent:
    def __init__(self, transport, repository):
        self.transport,self.repository=transport,repository
        self.engine=RequestEngine()

    def handle(self, user_id, message, *, event_id):
        if not isinstance(message,str) or not message.strip() or not str(event_id):
            raise ValueError('nonempty message and stable event_id required')
        event=str(event_id)
        try:
            cached,current,sequence=self.repository.begin(user_id,event,message)
        except Busy:
            return Reply(text='Предыдущая обработка ещё выполняется или была прервана. Заявка не изменена; сообщите администратору.',status='busy',intent='clarify')
        except EngineError as error:
            return Reply(text='Повторное сообщение имеет конфликтующий идентификатор. Заявка не изменена.',
                         status='failed',intent='clarify',diagnostics=[str(error)])
        if cached is not None:return cached
        calls=0
        try:
            context={'message':message,'source_lines':source_lines(message),
                'active_request': current.model_dump(mode='json',exclude={'undo_stack','changes','originals'}) if current else None,
                'recent_changes':[c.model_dump() for c in current.changes[-10:]] if current else [],
                'can_undo': bool(current and current.undo_stack),
                'totals':totals(current.items) if current else ({},[]),
                'history':self.repository.history(user_id,current.id if current else None)}
            notes=[];previous=None;updated=None;reply=None;first_intent=None
            for attempt in range(2):
                # A rejected proposal must never leak into a readonly repair.
                updated=None
                try:
                    calls+=1
                    raw=self.transport.plan(context,diagnostics=notes,previous=previous)
                    previous=raw.model_dump(mode='json') if isinstance(raw,AgentPlan) else raw
                    candidate=json.loads(raw) if isinstance(raw,str) else previous
                    if isinstance(candidate,dict) and first_intent is None:
                        intent=candidate.get('intent')
                        if intent in {'create_request','edit_request','show_request','answer_question','web_search','free_chat','clarify'}:
                            first_intent=intent
                    plan=AgentPlan.model_validate_json(raw) if isinstance(raw,str) else AgentPlan.model_validate(raw)
                    if plan.intent in {'create_request','edit_request'} and first_intent != plan.intent:
                        raise EngineError('repair_cannot_change_to_different_mutation_intent')
                    if plan.intent=='create_request':
                        updated=self.engine.create(plan.extraction,message,f'{user_id}:{event}')
                        text=render(updated)
                    elif plan.intent=='edit_request':
                        updated=self.engine.edit(current,plan,message,
                            lambda key,rev:self.repository.version(user_id,key,rev))
                        text=render(updated)
                    elif plan.intent=='show_request' or plan.intent=='answer_question' and plan.view in {'original','totals','explanation'}:
                        text=render(current,plan.view)
                    elif plan.intent=='web_search':
                        # Search has no mutation tools and its text is NEVER parsed as a plan.
                        calls+=1
                        try:
                            result=self.transport.search(plan.search_query)
                            sources=list(result.sources)
                            if not sources or any(not safe_source(s.url) for s in sources):
                                raise SearchUnavailable('no_safe_sources')
                            text='Интернет-поиск (заявка не изменена):\n' + result.text + '\n\nИсточники:\n' + '\n'.join(s.title+' — '+s.url for s in sources)
                            reply=Reply(text=text,status='ok',intent=plan.intent,request_id=current.id if current else None,
                                revision=current.revision if current else None,sources=sources,calls=calls)
                        except Exception as error:
                            emit('steel_v2_search_unavailable', reason=safe_code(error))
                            reply=Reply(text='Интернет-поиск недоступен или не подтверждён источниками. Актуальность не проверена. Заявка не изменена.',
                                status='failed',intent=plan.intent,request_id=current.id if current else None,
                                revision=current.revision if current else None,calls=calls,diagnostics=['search_unavailable'])
                        break
                    else:
                        if not plan.answer.strip():raise EngineError('answer_or_clarification_required')
                        text=plan.answer
                    state=updated or current
                    reply=Reply(text=text,status='partial' if updated and self.engine.partial(updated) else 'ok',
                        intent=plan.intent,request_id=state.id if state else None,revision=state.revision if state else None,calls=calls)
                    break
                except (ValidationError,EngineError,json.JSONDecodeError,ValueError) as error:
                    notes=validation_notes(error)
                    emit('steel_v2_validation',attempt=attempt+1,reason=type(error).__name__,issue_count=len(notes))
                    if attempt:
                        reply=Reply(text='Ответ модели не прошёл проверку структуры или полноты. Активная заявка не изменена. '
                            'Исходное сообщение сохранено в истории. Требуется уточнение или повторная подача исходника.\nПроверка: ' + '; '.join(notes),
                            status='failed',intent='clarify',request_id=current.id if current else None,
                            revision=current.revision if current else None,diagnostics=notes,calls=calls)
                        updated=None
            return self.repository.finish(user_id,event,sequence,message,reply,updated)
        except PermissionError:
            raise
        except Exception as error:
            # No SDK retries; no automatic replay after timeout/transport/storage failure.
            code=safe_code(error)
            trace=CURRENT.get()
            if trace:
                trace.result=code
            emit('steel_v2_failure',reason=code,exception=type(error).__name__,calls=calls)
            reply=Reply(text='Обработка не завершена. Активная заявка не изменена; исходное сообщение сохранено в истории. '
                'Повторите позже или сообщите администратору.',status='failed',intent='clarify',
                request_id=current.id if current else None,revision=current.revision if current else None,
                diagnostics=[code],calls=calls)
            try:
                return self.repository.finish(user_id,event,sequence,message,reply)
            except Exception:
                # Do not falsely claim persistence when SQLite cannot commit.
                return Reply(text='Не удалось подтвердить сохранение сообщения. Сохраните его исходный текст; '
                    'результат операции не подтверждён. Сообщите администратору.',status='failed',intent='clarify',calls=calls,
                    diagnostics=['storage_unconfirmed'])
