"""One GPT coordinator. No keyword/regex intent router and no legacy pipeline."""
import json
from pydantic import ValidationError
from .contracts import AgentPlan, Reply, PendingClarification, Clarification, DisplayOrder
from .engine import RequestEngine, EngineError, SelectionError, totals, unit_evidence_in_message
from .repository import Busy
from .presentation import render, display_order, display_layout, literal_fragments
from .telegram_format import prose, literal
from .contracts import PresentationRun
from .recovery import partial_from_proposal
from .transport import SearchUnavailable, safe_source, SEARCH_PROVIDERS


def search_status_text(status):
    if not status:return 'Нет сохранённой диагностики поиска. Причина недоступности не установлена.'
    if status['status']=='ok':return 'Последний поиск завершился с подтверждёнными источниками.'
    reasons={
        'no_sources':'Поиск не предоставил подтверждённых источников для ответа.',
        'no_tool':'В ответе API не подтверждено выполнение инструмента поиска.',
        'tool_incomplete':'Инструмент поиска не завершил выполнение.',
        'tool_status_unknown':'API не сообщил распознаваемый статус одного из действий поиска; завершение не подтверждено.',
        'no_final_message':'После поисковых действий не сформировано завершённое сообщение с ответом.',
        'empty_text':'Поиск не вернул текст ответа.',
        'incomplete_response':'API вернул незавершённый результат поиска.',
        'invalid_response':'Ответ поиска не удалось корректно разобрать.',
        'api_error':'Запрос поиска завершился ошибкой API или соединения. Более точная причина пользователю не подтверждена.'}
    return reasons.get(status['reason'],'Точная техническая причина отказа поиска не установлена.')+' Актуальные сведения проверить не удалось; заявка не изменена.'
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
        calls=0;clarification=None
        try:
            pending=self.repository.pending(user_id)
            clarification=self.repository.clarification(user_id,current)
            displayed=self.repository.last_display(user_id,current)
            display_ids=displayed.item_ids if displayed else None
            used_layout=None
            def present(state, view='normalized', proposed=None):
                nonlocal used_layout
                prior=displayed.layout if displayed and state and state.id==displayed.request_id else None
                used_layout=display_layout(state,prior,proposed)
                return render(state,view,layout=used_layout)
            if pending:pending['source_lines']=source_lines(pending['source_text'])
            context={'pending_request':pending,'unresolved_fragments':[
                {'number':n,'id':r.id,'start':r.start,'end':r.end,'text':current.original_text[r.start:r.end]}
                for n,r in enumerate((r for r in current.records if r.kind=='unresolved'),1)] if current else [],
                'message':message,'source_lines':source_lines(message),
                'active_request': current.model_dump(mode='json',exclude={'undo_stack','changes','originals'}) if current else None,
                'recent_changes':[c.model_dump() for c in current.changes[-10:]] if current else [],
                'can_undo': bool(current and current.undo_stack),
                'display_numbers':{str(n):key for n,key in enumerate(display_ids,1)} if display_ids else {},
                'display_order_available':displayed is not None,
                'last_display_layout':displayed.layout.model_dump(mode='json') if displayed and displayed.layout else None,
                'totals':totals(current.items) if current else ({},[]),
                'totals_are_partial':bool(current and self.engine.partial(current)),
                'history':self.repository.history(user_id,current.id if current else None),
                'last_search':self.repository.last_search(user_id),
                'previous_operation':self.repository.last_operation(user_id),
                'pending_clarification':clarification.model_dump(mode='json') if clarification else None,
                'supported_edit_operations':['set','regroup','undo','resolve','convert_mass',
                                             'set_quantity','add_quantity','set_length'],'append_supported':False}
            context['last_search_explanation']=search_status_text(context['last_search'])
            notes=[];previous=None;updated=None;reply=None;first_intent=None;proposals=[];source_pending=None
            shown_normalized=False
            selected_scope=None
            unselected=object();chosen_source=unselected
            for attempt in range(2):
                # A rejected proposal must never leak into a readonly repair.
                updated=None
                shown_normalized=False
                used_layout=None
                candidate=None;plan=None;validation_stage='structured_response'
                try:
                    calls+=1
                    prior_proposal=previous
                    raw=self.transport.plan(context,diagnostics=notes,previous=previous)
                    previous=raw.model_dump(mode='json') if isinstance(raw,AgentPlan) else raw
                    candidate=json.loads(raw) if isinstance(raw,str) else previous
                    if isinstance(candidate,dict) and first_intent is None:
                        intent=candidate.get('intent')
                        if intent in {'create_request','edit_request','show_request','answer_question','web_search','free_chat','clarify'}:
                            first_intent=intent
                    validation_stage='plan_contract'
                    plan=AgentPlan.model_validate_json(raw) if isinstance(raw,str) else AgentPlan.model_validate(raw)
                    validation_stage='request_validation'
                    if plan.intent in {'create_request','edit_request'} and first_intent != plan.intent:
                        raise EngineError('repair_cannot_change_to_different_mutation_intent')
                    if plan.intent=='create_request':
                        if chosen_source is unselected:chosen_source=plan.pending_event_id
                        if chosen_source!=plan.pending_event_id:
                            raise EngineError('repair_cannot_change_source')
                        source=message
                        if plan.pending_event_id is not None:
                            if not pending or pending['event_id']!=plan.pending_event_id:
                                raise EngineError('pending_source_changed_or_unavailable')
                            source=pending['source_text']
                            source_pending=plan.pending_event_id
                        updated=self.engine.create(plan.extraction,source,f'{user_id}:{event}')
                        text=present(updated,proposed=plan.display_layout)
                    elif plan.intent=='edit_request':
                        has_selection=any(op.selection is not None for op in plan.operations)
                        plan=self.engine.resolve_plan(current,plan,message,display_ids)
                        scope=sorted((op.kind,op.field or '',op.name or '',op.target_id or '') for op in plan.operations)
                        conversion_repair=(selected_scope is not None and
                            all(kind=='convert_mass' for kind,_,_,_ in scope) and
                            all((kind=='convert_mass' or kind=='set' and field in {'quantity','unit'})
                                and not name for kind,field,name,_ in selected_scope) and
                            {row[3] for row in scope}=={row[3] for row in selected_scope})
                        if selected_scope is not None and selected_scope!=scope and not conversion_repair:
                            raise EngineError('semantic_edit_scope_changed')
                        if has_selection:selected_scope=scope
                        if attempt and any(op.kind=='convert_mass' for op in plan.operations):
                            prior=json.loads(prior_proposal) if isinstance(prior_proposal,str) else prior_proposal
                            old_plan=AgentPlan.model_validate(prior)
                            old_ops=[op.model_dump() for op in self.engine.resolve_plan(
                                current,old_plan,message,display_ids).operations]
                            if (not old_ops or any(not isinstance(op,dict) or
                                not (op.get('kind')=='convert_mass' or
                                     op.get('kind')=='set' and op.get('field') in {'quantity','unit'})
                                for op in old_ops) or
                                any(op.kind!='convert_mass' for op in plan.operations) or
                                {op.get('target_id') for op in old_ops}!={op.target_id for op in plan.operations}):
                                raise EngineError('mass_conversion_repair_scope_mismatch')
                        edit_message=message
                        if plan.clarification_event_id is not None:
                            if clarification is None or plan.clarification_event_id!=clarification.event_id:
                                raise EngineError('clarification_stale_or_missing')
                            proposal=clarification.proposal
                            saved=clarification.operation
                            inherited=False
                            for op in plan.operations:
                                matches=(op.kind==(saved.kind if saved else 'set') and op.target_id in proposal.target_ids and
                                    op.field==proposal.field and op.name==proposal.name and
                                    op.value==proposal.value and op.evidence==proposal.evidence and
                                    op.replace_from is None and
                                    (op.unit,op.unit_evidence)==((saved.unit,saved.unit_evidence) if saved else (None,None)))
                                if matches:inherited=True
                                elif (not op.evidence or op.evidence not in message or
                                      op.unit_evidence is not None and not unit_evidence_in_message(message,op.unit_evidence)):
                                    raise EngineError('clarification_scope_mismatch')
                            if not inherited:raise EngineError('clarification_scope_mismatch')
                            edit_message=clarification.source_message+'\n'+message
                        updated=self.engine.edit(current,plan,edit_message,
                            lambda key,rev:self.repository.version(user_id,key,rev))
                        # History records the actual confirming message; inherited
                        # evidence remains separately recorded in each Change.
                        if plan.clarification_event_id is not None:
                            updated=updated.model_copy(update={'changes':updated.changes[:len(current.changes)]+[
                                change.model_copy(update={'message':message})
                                for change in updated.changes[len(current.changes):]]})
                        text=present(updated,proposed=plan.display_layout)
                    elif plan.view=='search_status':
                        text=search_status_text(context['last_search'])
                    elif plan.view=='custom':
                        text=('Заявка требует уточнения.\n' if current and self.engine.partial(current) else '')+plan.answer
                    elif plan.intent=='show_request' or plan.intent=='answer_question' and plan.view in {'original','totals','explanation','forward'}:
                        text=present(current,plan.view,plan.display_layout)
                    elif plan.intent=='web_search':
                        # Search has no mutation tools and its text is NEVER parsed as a plan.
                        calls+=1
                        try:
                            result=self.transport.search(plan.search_query)
                            sources=list(result.sources)
                            providers=getattr(result,'providers',())
                            if (not sources and not providers or any(not safe_source(s.url) for s in sources)
                                    or any(p not in SEARCH_PROVIDERS for p in providers)):
                                raise SearchUnavailable('no_sources')
                            text=result.text+'\n\nИсточники:\n'+'\n'.join(
                                [s.title+' — '+s.url if s.title!=s.url else s.url for s in sources]+
                                ['Сервис web_search: '+p for p in providers])
                            reply=Reply(text=text,status='ok',intent=plan.intent,request_id=current.id if current else None,
                                revision=current.revision if current else None,sources=sources,calls=calls)
                            protected=[message,*literal_fragments(current)]
                            runs=prose(result.text,protected)+literal('\n\nИсточники:\n')
                            for n,source in enumerate(sources):
                                if n:runs+=literal('\n')
                                runs.append(PresentationRun(text=source.title,url=source.url))
                            for n,provider in enumerate(providers):
                                if sources or n:runs+=literal('\n')
                                runs+=literal('Сервис web_search: '+provider)
                            reply=reply.model_copy(update={'presentation':runs})
                        except Exception as error:
                            emit('steel_v2_search_unavailable', reason=safe_code(error))
                            reason=error.reason if isinstance(error,SearchUnavailable) else 'unknown'
                            reply=Reply(text=search_status_text({'status':'failed','reason':reason}),
                                status='failed',intent=plan.intent,request_id=current.id if current else None,
                                revision=current.revision if current else None,calls=calls,diagnostics=['search_unavailable','search:'+reason])
                        break
                    else:
                        if not plan.answer.strip():raise EngineError('answer_or_clarification_required')
                        text=plan.answer
                    state=updated or current
                    next_clarification=None
                    if plan.intent=='clarify':
                        next_clarification=clarification
                        if plan.clarification is not None:
                            proposal=plan.clarification
                            ids={i.id for i in current.items} if current else set()
                            if (not current or len(set(proposal.target_ids))!=len(proposal.target_ids)
                                    or not set(proposal.target_ids)<=ids
                                    or (proposal.field=='characteristic')!=bool(proposal.name)):
                                raise EngineError('invalid_clarification_target')
                            inherited=clarification and (
                                proposal.field,proposal.name,proposal.value,proposal.evidence)==(
                                clarification.proposal.field,clarification.proposal.name,
                                clarification.proposal.value,clarification.proposal.evidence) and set(
                                    proposal.target_ids)<=set(clarification.proposal.target_ids)
                            source=clarification.source_message if inherited else message
                            if proposal.evidence not in source or proposal.value not in proposal.evidence:
                                raise EngineError('clarification_value_evidence')
                            next_clarification=PendingClarification(
                                event_id=clarification.event_id if inherited else event,
                                request_id=current.id,revision=current.revision,proposal=proposal,
                                source_message=source,question=plan.answer,
                                operation=clarification.operation if inherited else None)
                        elif next_clarification:
                            next_clarification=next_clarification.model_copy(update={'question':plan.answer})
                    request_view=plan.intent=='show_request' or plan.intent=='answer_question' and plan.view in {'original','totals','explanation','forward','custom'}
                    partial=state and self.engine.partial(state) and (updated is not None or request_view)
                    reply=Reply(text=text,status='partial' if partial else 'ok',
                        intent=plan.intent,request_id=state.id if state else None,revision=state.revision if state else None,calls=calls,
                        diagnostics=['pending_resolved:'+source_pending] if updated is not None and source_pending else [],
                        clarification=next_clarification)
                    shown_normalized=(updated is not None or plan.intent=='show_request' and plan.view in {'normalized','forward'}
                                      or plan.intent=='answer_question' and plan.view=='forward')
                    break
                except (ValidationError,EngineError,json.JSONDecodeError,ValueError) as error:
                    if isinstance(error,SelectionError):
                        saved=None;op=error.operation
                        if (error.candidates and op.kind in {'set','set_quantity','add_quantity','set_length'}
                                and op.field in {'quantity','unit','characteristic'} and op.value
                                and op.evidence and op.evidence in message and op.value in op.evidence):
                            saved=PendingClarification(event_id=event,request_id=current.id,
                                revision=current.revision,source_message=message,question=error.question,
                                proposal=Clarification(target_ids=error.candidates,field=op.field,name=op.name,
                                    value=op.value,evidence=op.evidence,alternatives=['Уточните один из подходящих товаров']),
                                operation=op.model_copy(update={'selection':None,'target_id':None}))
                        reply=Reply(text=error.question,status='ok',intent='clarify',calls=calls,
                            request_id=current.id,revision=current.revision,diagnostics=[str(error)],
                            clarification=saved or clarification)
                        updated=None
                        break
                    # A completed HTTP response may have failed wire validation before
                    # transport.plan returned. Preserve it only in private repair memory.
                    raw_proposal=getattr(error,'proposal',None)
                    if raw_proposal is not None:
                        previous=raw_proposal
                        try:candidate=json.loads(raw_proposal)
                        except (ValueError,TypeError):candidate=None
                    if isinstance(candidate,dict):
                        if first_intent is None and candidate.get('intent') in {'create_request','edit_request','show_request','answer_question','web_search','free_chat','clarify'}:
                            first_intent=candidate['intent']
                        if candidate.get('intent')=='create_request' and not candidate.get('operations'):
                            key=candidate.get('pending_event_id')
                            if chosen_source is unselected:chosen_source=key
                            same_source=key==chosen_source
                            if key is None and same_source:proposals.append((candidate,message))
                            elif same_source and pending and key==pending['event_id']:
                                proposals.append((candidate,pending['source_text']))
                                source_pending=key
                    notes=validation_notes(error)
                    # Actionable PRIVATE repair details; diagnostics emitted below
                    # still use only the allowlisted codes, never error messages.
                    context['validation_repair']=(error.errors(include_input=False,include_context=False)
                        if isinstance(error,ValidationError) else [{'type':'request_validation','msg':str(error)}])
                    trace=CURRENT.get()
                    safe_intents={'create_request','edit_request','show_request','answer_question','web_search','free_chat','clarify'}
                    intent=candidate.get('intent') if isinstance(candidate,dict) else None
                    extraction=candidate.get('extraction') if isinstance(candidate,dict) else None
                    items=extraction.get('items') if isinstance(extraction,dict) else None
                    # Strict allowlist: no model IDs, characteristic names, input or error text.
                    known={'duplicate_characteristic','repair_cannot_change_to_different_mutation_intent',
                           'edit_evidence_not_in_message','edited_quantity_evidence_mismatch',
                           'new_value_missing_from_edit_evidence','invalid_decimal_evidence',
                           'invalid_mass_conversion','mass_conversion_unit_evidence','unsupported_mass_conversion',
                           'mass_conversion_repair_scope_mismatch',
                           'mass_conversion_requires_known_quantity_and_unit','clarification_stale_or_missing',
                           'clarification_scope_mismatch','clarification_value_evidence','invalid_clarification_target',
                           'groups_must_partition_all_items_once'}
                    codes=[n.split(':',1)[0] if n.split(':',1)[0] in known else
                           'schema_validation' if n.startswith('schema:') else 'request_validation' for n in notes[:10]]
                    emit('steel_v2_validation',operation=trace.operation_id if trace else None,
                         attempt=attempt+1,reason=type(error).__name__,issue_count=len(notes),
                         intent=intent if intent in safe_intents else None,
                         first_intent=first_intent,extracted_count=len(items) if isinstance(items,list) else None,
                         validation_stage=validation_stage,diagnostic_codes=codes)
                    if attempt:
                        reply=Reply(text='Ответ модели не прошёл проверку структуры или полноты. Активная заявка не изменена. '
                            'Исходное сообщение сохранено в истории. Уточните действие.',
                            status='failed',intent='clarify',request_id=current.id if current else None,
                            revision=current.revision if current else None,diagnostics=notes,calls=calls)
                        updated=None
            if updated is None and notes and first_intent in {None,'create_request'}:
                # Only known, completed validation failures reach here. Never applied
                # after a network/storage exception or an uncertain external outcome.
                # If both responses are unreadable, the current short continuation
                # cannot silently supersede an already saved original.
                if pending and first_intent is None and chosen_source is unselected:
                    source_pending=pending['event_id']
                recovered=None
                for proposal,source in proposals:
                    candidate_state=partial_from_proposal(proposal,source,f'{user_id}:{event}')
                    if candidate_state and (recovered is None or len(candidate_state.items)>=len(recovered.items)):
                        recovered=candidate_state
                invalid_reference=chosen_source is not unselected and chosen_source is not None and (
                    not pending or chosen_source!=pending['event_id'])
                if invalid_reference:
                    reply=Reply(text='Не удалось однозначно выбрать сохранённый исходник. Уточните, какую заявку продолжить. '
                        'Действующая заявка не изменена.',status='failed',intent='clarify',
                        request_id=current.id if current else None,revision=current.revision if current else None,
                        diagnostics=notes,calls=calls)
                elif recovered is not None and current is None:
                    updated=recovered
                    reply=Reply(text=present(updated),status='partial',intent='create_request',
                        request_id=updated.id,revision=updated.revision,calls=calls,
                        diagnostics=notes+['partial_recovered']+(['pending_resolved:'+source_pending] if source_pending else []))
                else:
                    source=pending['source_text'] if source_pending and pending else message
                    prefix='Исходник сохранён и ожидает разбора. '
                    if current:prefix+='Действующая заявка не изменена. '
                    if recovered is not None:
                        text=prefix+'\n\nПроверенная часть новой заявки (ещё не активна):\n'+present(recovered)
                    else:
                        text=prefix+('\n\nДействующая заявка:\n'+present(current) if current else '')+'\n\nОжидает разбора:\n'+source
                    text+='\n\nМожно попросить продолжить разбор сохранённого исходника; отправлять его целиком повторно не нужно.'
                    reply=Reply(text=text,status='failed',intent='clarify',request_id=current.id if current else None,
                        revision=current.revision if current else None,calls=calls,
                        diagnostics=notes+['pending_source'],pending_snapshot=recovered)
                    # A continuation of the same pending source keeps its original
                    # durable turn as the source; don't relabel this short message.
                    if source_pending:
                        reply=reply.model_copy(update={'diagnostics':notes})
            if updated is None and reply.status=='failed' and clarification:
                reply=reply.model_copy(update={'clarification':clarification})
            state=updated or current
            if state and (updated is not None or shown_normalized):
                reply=reply.model_copy(update={'display':DisplayOrder(
                    request_id=state.id,revision=state.revision,item_ids=display_order(state,used_layout),layout=used_layout)})
            if reply.presentation is None:
                protected=[message,*literal_fragments(state),*literal_fragments(reply.pending_snapshot)]
                if pending:protected.append(pending['source_text'])
                protected.extend(turn['content'] for turn in context['history'] if turn['role']=='user')
                original_view=plan is not None and plan.intent in {'show_request','answer_question'} and plan.view=='original'
                reply=reply.model_copy(update={'presentation':literal(reply.text) if original_view else prose(reply.text,protected)})
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
                diagnostics=[code],calls=calls,clarification=clarification)
            try:
                return self.repository.finish(user_id,event,sequence,message,reply)
            except Exception:
                # Do not falsely claim persistence when SQLite cannot commit.
                return Reply(text='Не удалось подтвердить сохранение сообщения. Сохраните его исходный текст; '
                    'результат операции не подтверждён. Сообщите администратору.',status='failed',intent='clarify',calls=calls,
                    diagnostics=['storage_unconfirmed'])
