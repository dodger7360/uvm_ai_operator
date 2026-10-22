"""Pure validation and transactional proposal building; GPT owns interpretation."""
from decimal import Decimal, InvalidOperation, localcontext
import re
from uuid import uuid5, NAMESPACE_URL
from .contracts import RequestSnapshot, Characteristic, Change, UnitProposal
from quantity import normalize_quantity, sum_quantities


class EngineError(ValueError):
    pass


class SelectionError(EngineError):
    def __init__(self, code, question, operation, candidates=()):
        self.question,self.operation,self.candidates=question,operation,list(candidates)
        super().__init__(code)


def require(condition, code):
    if not condition:
        raise EngineError(code)


def unique(values, code):
    require(len(values) == len(set(values)), code)


def decimal_text(value):
    # Numeric spelling only, never a steel/product grammar.
    try:
        result = Decimal(value.replace(',', '.').replace(' ', '').replace('\u00a0', '').replace('\u202f', ''))
    except (InvalidOperation, AttributeError):
        raise EngineError('invalid_decimal_evidence') from None
    require(result.is_finite() and result >= 0, 'invalid_decimal_evidence')
    return result


# Exact SI mass scaling only. No geometry, density or product-specific assumptions.
MASS_UNITS = {
    'тн': ('т', 3),
    'г': ('г', -3), 'грамм': ('г', -3), 'грамма': ('г', -3), 'граммы': ('г', -3), 'граммов': ('г', -3),
    'кг': ('кг', 0), 'килограмм': ('кг', 0), 'килограмма': ('кг', 0),
    'килограммы': ('кг', 0), 'килограммов': ('кг', 0),
    'т': ('т', 3), 'тонна': ('т', 3), 'тонны': ('т', 3), 'тонну': ('т', 3), 'тонн': ('т', 3), 'тоннах': ('т', 3),
}


def convert_mass(quantity, source, target):
    require(quantity is not None and source is not None, 'mass_conversion_requires_known_quantity_and_unit')
    src=MASS_UNITS.get(source.strip().casefold())
    dst=MASS_UNITS.get(target.strip().casefold()) if target else None
    require(src is not None and dst is not None, 'unsupported_mass_conversion')
    # Construct a Decimal tuple: no context-dependent multiplication/rounding.
    sign,digits,exponent=quantity.as_tuple()
    return Decimal((sign,digits,exponent+src[1]-dst[1])),dst[0]


LENGTH_UNITS={'мм':('мм',0),'миллиметр':('мм',0),'миллиметра':('мм',0),'миллиметров':('мм',0),
              'см':('см',1),'сантиметр':('см',1),'сантиметра':('см',1),'сантиметров':('см',1),'м':('м',3),
              'метр':('м',3),'метра':('м',3),'метры':('м',3),'метров':('м',3)}
PIECE_UNITS={key:('шт',0) for key in ('шт','шт.','штука','штуки','штук','хлыст','хлыста','хлыстов')}


def measured_unit(value):
    key=value.strip().casefold() if value else ''
    return ('mass',*MASS_UNITS[key]) if key in MASS_UNITS else (
           ('length',*LENGTH_UNITS[key]) if key in LENGTH_UNITS else (
           ('count',*PIECE_UNITS[key]) if key in PIECE_UNITS else None))


def unit_evidence_in_message(message, evidence):
    """A unit may follow a number without a space, but not hide inside a word."""
    if not evidence:return False
    return any((match.start()==0 or not message[match.start()-1].isalpha())
               and (match.end()==len(message) or not message[match.end()].isalpha())
               for match in re.finditer(re.escape(evidence),message))


def exact_add(left, right):
    # The span, not just the larger operand's digit count, determines precision.
    low=min(left.as_tuple().exponent,right.as_tuple().exponent)
    high=max(left.adjusted(),right.adjusted())
    with localcontext() as context:
        context.prec=max(1,high-low+2)
        return left+right


def select_ids(request, operation, message, display_ids=None):
    selection=operation.selection
    require(selection.evidence in message,'selection_evidence_not_in_message')
    items={item.id:item for item in request.items}
    if selection.mode=='display':
        if (display_ids is None or len(display_ids)!=len(items) or set(display_ids)!=set(items)
                or any(n>len(display_ids) for n in selection.positions)):
            raise SelectionError('display_order_unavailable',
                'Сначала покажите актуальную заявку, затем укажите позицию или опишите товар.',operation)
        chosen=[display_ids[n-1] for n in selection.positions]
    else:
        def matches(item,predicate):
            values=[item.label] if predicate.field=='label' else [
                c.value for c in item.characteristics if c.name==predicate.name and c.value is not None]
            value=predicate.value.strip().casefold()
            if predicate.match=='component':
                parts=[v.split(predicate.separator) for v in values]
                # No product grammar or inferred role: GPT supplies the existing
                # field, literal delimiter and component; Python checks it exactly.
                values=[row[predicate.component-1] for row in parts if len(row)>=predicate.component
                        and len(row)>1]
                return any(v.strip().casefold()==value for v in values)
            return any((v.strip().casefold()==value if predicate.match=='equals' else
                        re.search(r'(?<!\w)'+re.escape(value)+r'(?!\w)',v.casefold()) is not None)
                       for v in values)
        chosen=[item.id for item in request.items
                if all(matches(item,p) for p in selection.predicates)]
    if not chosen:
        raise SelectionError('selection_no_match',
            'Не удалось однозначно найти товар по этим признакам. Уточните размер, марку или вид.',operation)
    if selection.cardinality=='one' and len(chosen)!=1:
        raise SelectionError('selection_ambiguous',
            'Подходят несколько позиций. Уточните, какую выбрать: размер, марку или другую характеристику.',
            operation,chosen)
    return chosen


def totals(items):
    amounts, unknown = {}, []
    for item in items:
        if item.quantity is None or not item.unit:
            unknown.append(item.id)
            continue
        try:
            amount, unit = normalize_quantity(item.quantity, item.unit, allow_zero=True)
        except ValueError:
            # Unknown units remain visible, never interpreted as tonnes.
            amount, unit = item.quantity, item.unit
        amounts.setdefault(unit, []).append(amount)
    return {unit: str(sum_quantities(values)) for unit, values in amounts.items()}, unknown


def differences(items):
    """Literal comparison, NOT a determination of material equivalence."""
    fields = {'unit'} | {'characteristic:' + c.name for i in items for c in i.characteristics}
    result = []
    for field in sorted(fields):
        values = [(i.id, i.unit if field == 'unit' else next(
            (c.value for c in i.characteristics if c.name == field.split(':', 1)[1]), None)) for i in items]
        if len({v for _, v in values}) > 1 or any(v is None for _, v in values):
            result.append(field + ': ' + '; '.join(f'{key}={value if value is not None else "неизвестно"}' for key, value in values))
    return result


class RequestEngine:
    def __init__(self,mass_catalog=None):
        self.mass_catalog=mass_catalog

    def resolve_plan(self, current, plan, message, display_ids=None):
        require(current is not None,'no_active_request')
        require((plan.expected_request_id,plan.expected_revision)==(current.id,current.revision),'stale_request_revision')
        expanded=[]
        for operation in plan.operations:
            if operation.selection is None:expanded.append(operation)
            else:
                expanded.extend(operation.model_copy(update={'target_id':key,'selection':None})
                                for key in select_ids(current,operation,message,display_ids))
        return plan.model_copy(update={'operations':expanded})

    def validate(self, request):
        records, originals, items = request.records, request.originals, request.items
        require(bool(originals), 'no_product_items')
        unique([r.id for r in records], 'duplicate_source_id')
        unique([i.id for i in originals], 'duplicate_original_id')
        unique([i.id for i in items], 'duplicate_item_id')
        require({i.id for i in originals} == {i.id for i in items}, 'item_set_changed')
        coverage = bytearray(len(request.original_text))
        by_record = {r.id: r for r in records}
        for record in records:
            require(record.start < record.end <= len(coverage), 'invalid_source_range:' + record.id)
            require(bool(request.original_text[record.start:record.end].strip()), 'empty_source_range:' + record.id)
            for offset in range(record.start, record.end):
                require(coverage[offset] == 0, 'overlapping_source_range:' + str(offset))
                coverage[offset] = 1
        missing = [n for n, ch in enumerate(request.original_text) if not ch.isspace() and not coverage[n]]
        require(not missing, 'uncovered_source_offsets:' + ','.join(map(str, missing[:20])))
        product_ids = {r.id for r in records if r.kind == 'product'}
        require(len(originals) == len(product_ids) and {i.source_id for i in originals} == product_ids,
                'every_product_record_requires_exactly_one_item')
        source_by_item = {i.id: i.source_id for i in originals}
        for item in originals:
            record = by_record[item.source_id]
            fragment = request.original_text[record.start:record.end]
            if item.quantity is not None:
                require(bool(item.quantity_text) and item.quantity_text in fragment, 'quantity_evidence:' + item.id)
                require(decimal_text(item.quantity_text) == item.quantity, 'quantity_mismatch:' + item.id)
            if item.unit is not None:
                if item.unit_source_id is None:
                    require(bool(item.unit_text) and item.unit_text in fragment and item.unit == item.unit_text,
                            'unit_evidence:' + item.id)
                else:
                    # Explicit header provenance, not a fallback search across the
                    # entire original. No quantity conversion or product inference.
                    header=by_record.get(item.unit_source_id)
                    require(header is not None and header.kind=='header' and header.end<=record.start,
                            'unit_source:' + item.id)
                    source=request.original_text[header.start:header.end]
                    require(item.unit_text and unit_evidence_in_message(source,item.unit_text), 'unit_evidence:' + item.id)
                    source_unit=MASS_UNITS.get(item.unit_text.strip().casefold())
                    require(source_unit is not None and source_unit[0]==item.unit,
                            'header_unit_mismatch:' + item.id)
            else:
                require(item.unit_source_id is None, 'unit_source_without_unit:' + item.id)
            for c in item.characteristics:
                unique(c.source_ids, 'duplicate_attribute_source:' + item.id)
                require(bool(c.source_ids) and all(key in by_record for key in c.source_ids), 'attribute_source:' + item.id)
                raw_sources = '\n'.join(request.original_text[by_record[key].start:by_record[key].end] for key in c.source_ids)
                require(c.raw is not None and bool(c.raw.strip()) and c.raw in raw_sources, 'attribute_evidence:' + item.id + ':' + c.name)
        for item in items:
            require(item.source_id == source_by_item[item.id], 'source_link_changed:' + item.id)
            unique([c.name for c in item.characteristics], 'duplicate_characteristic:' + item.id)
        unique([g.id for g in request.groups], 'duplicate_group_id')
        members = [key for group in request.groups for key in group.item_ids]
        require(sorted(members) == sorted(i.id for i in items), 'groups_must_partition_all_items_once')
        require(all(g.item_ids for g in request.groups), 'empty_group')
        if request.theoretical_mass:
            from .theoretical_mass import validate_annotations
            validate_annotations(request)
        return request

    def create(self, extraction, source, event_key, mass_specs=()):
        # Only disjoint, unreferenced *service* whitespace records can be discarded.
        # Validate identity and geometry BEFORE filtering, so this cannot hide damage.
        records=extraction.records
        unique([r.id for r in records],'duplicate_source_id')
        for record in records:
            require(0 <= record.start < record.end <= len(source),'invalid_source_range:'+record.id)
        referenced={i.source_id for i in extraction.items} | {
            key for i in extraction.items for char in i.characteristics for key in char.source_ids}
        records=[r for r in records if not (r.kind in {'header','note'} and r.id not in referenced
            and not source[r.start:r.end].strip()
            and all(r is other or r.end <= other.start or other.end <= r.start for other in records))]
        request = RequestSnapshot(id=str(uuid5(NAMESPACE_URL, event_key)), revision=1,
            original_text=source, records=records, originals=[i.model_copy(deep=True) for i in extraction.items],
            items=[i.model_copy(deep=True) for i in extraction.items], groups=extraction.groups, questions=extraction.questions)
        self.validate(request)
        from .theoretical_mass import refresh
        return self.validate(refresh(request,mass_specs,self.mass_catalog))

    def append(self, current, plan, source, event_key):
        """Validate only the new source, then compose an immutable version in memory."""
        require(current is not None,'no_active_request')
        require((plan.expected_request_id,plan.expected_revision)==(current.id,current.revision),'stale_request_revision')
        segment=self.create(plan.extraction,source,event_key,plan.mass_specs)
        prefix='append_'+str(uuid5(NAMESPACE_URL,event_key))+'_'+str(current.revision)+'_'
        record_ids={r.id:prefix+r.id for r in segment.records}
        item_ids={i.id:prefix+i.id for i in segment.items}
        offset=len(current.original_text)+1
        def remap(item):
            return item.model_copy(update={'id':item_ids[item.id],'source_id':record_ids[item.source_id],
                'characteristics':[c.model_copy(update={'source_ids':[record_ids[k] for k in c.source_ids]})
                                   for c in item.characteristics]})
        changed=current.model_copy(deep=True,update={
            'revision':current.revision+1,'undo_stack':[*current.undo_stack,current.revision],
            'original_text':current.original_text+'\n'+source,
            'records':[*current.records,*[r.model_copy(update={'id':record_ids[r.id],
                'start':r.start+offset,'end':r.end+offset}) for r in segment.records]],
            'originals':[*current.originals,*map(remap,segment.originals)],
            'items':[*current.items,*map(remap,segment.items)],
            'groups':[*current.groups,*[g.model_copy(update={'id':prefix+g.id,
                'item_ids':[item_ids[k] for k in g.item_ids]}) for g in segment.groups]],
            'questions':list(dict.fromkeys([*current.questions,*segment.questions])),
            'changes':[*current.changes,*[Change(item_id=item_ids[i.id],field='append',before=None,
                after=i.label,evidence=source,message=source) for i in segment.items]],
            'theoretical_mass':[*current.theoretical_mass,*[m.model_copy(update={
                'spec':m.spec.model_copy(update={'item_id':item_ids[m.spec.item_id]})}) for m in segment.theoretical_mass]]})
        return self.validate(changed)

    def edit(self, current, plan, message, read_version, display_ids=None, mass_approved=(),unit_consent=None):
        require(current is not None, 'no_active_request')
        require((plan.expected_request_id, plan.expected_revision) == (current.id, current.revision), 'stale_request_revision')
        operations = self.resolve_plan(current,plan,message,display_ids).operations
        mass_targets={op.target_id for op in operations if op.target_id}
        mass_targets.update(i.id for op in operations if op.resolution for i in op.resolution.items)
        require({s.item_id for s in plan.mass_specs}<=mass_targets,'mass_spec_outside_edit_scope')
        if any(op.kind == 'undo' for op in operations):
            require(len(operations) == 1, 'undo_must_be_standalone')
            require(plan.remaining_questions is None, 'undo_restores_questions_without_override')
            op = operations[0]
            require(op.target_id is None and op.field is None and op.name is None and op.value is None
                    and op.evidence is None and not op.groups, 'unexpected_undo_fields')
            require(bool(current.undo_stack), 'nothing_to_undo')
            old = read_version(current.id, current.undo_stack[-1])
            require(old is not None, 'undo_version_missing')
            from .theoretical_mass import revalidate_historical
            restored=old.model_copy(deep=True, update={'revision': current.revision+1,
                'undo_stack': current.undo_stack[:-1]})
            return self.validate(revalidate_historical(restored))
        changed = current.model_copy(deep=True)
        by_id = {item.id: item for item in changed.items}
        touched = set()
        regrouped = False
        for op in operations:
            if op.kind=='confirm_unit':
                require(unit_consent is not None and isinstance(unit_consent.proposal,UnitProposal),
                        'unit_confirmation_missing')
                proposal=unit_consent.proposal
                require((unit_consent.event_id,unit_consent.request_id,unit_consent.revision)==(
                    plan.clarification_event_id,current.id,current.revision),'unit_confirmation_stale')
                require(len(operations)==1 and op.target_id==proposal.target_id and op.value==proposal.unit
                        and op.field is None and op.name is None and op.selection is None and not op.groups
                        and op.unit is None and op.unit_evidence is None and op.replace_from is None
                        and plan.remaining_questions is None and not plan.mass_specs,
                        'unit_confirmation_scope')
                require(op.target_id in by_id and by_id[op.target_id].unit is None,
                        'unit_confirmation_target')
                require(bool(op.evidence) and op.evidence==message.strip() and op.evidence in message,
                        'unit_confirmation_consent')
                require(changed.questions.count(proposal.question)==1,'unit_confirmation_question')
                # Consent is evidence of this decision, never evidence from a source row.
                by_id[op.target_id]=by_id[op.target_id].model_copy(update={'unit':proposal.unit})
                changed.questions.remove(proposal.question)
                changed.changes.append(Change(item_id=op.target_id,field='unit',before=None,after=proposal.unit,
                    evidence=op.evidence,message=message,confirmation_event_id=unit_consent.event_id))
                continue
            if op.kind=='calculate_mass':
                require(op.target_id in by_id and op.field is None and op.name is None and op.value is None
                        and not op.groups and op.evidence and op.evidence in message,'invalid_calculate_mass')
                require((op.target_id,'theoretical_mass',None) not in touched,'contradictory_duplicate_patch')
                touched.add((op.target_id,'theoretical_mass',None))
                changed.changes.append(Change(item_id=op.target_id,field='theoretical_mass',before=None,
                    after='recalculate',evidence=op.evidence,message=message))
                continue
            if op.kind in {'set_quantity','add_quantity','set_length'}:
                length=op.kind=='set_length'
                require(op.target_id in by_id and not op.groups and op.replace_from is None
                        and op.field==('characteristic' if length else 'quantity')
                        and (bool(op.name) if length else op.name is None),'invalid_measured_edit')
                require(bool(op.evidence) and op.evidence in message
                        and unit_evidence_in_message(message,op.unit_evidence),'edit_evidence_not_in_message')
                amount=decimal_text(op.value)
                require(decimal_text(op.evidence)==amount,'edited_quantity_evidence_mismatch')
                unit=measured_unit(op.unit)
                require(unit is not None and measured_unit(op.unit_evidence)==unit,'measured_unit_evidence')
                item=by_id[op.target_id]
                if length:
                    require(unit[0]=='length','length_unit_required')
                    names=[c.name for c in item.characteristics
                           if c.name.casefold() in {'length','length_mm','длина'}]
                    require(op.name.casefold() in {'length','length_mm','длина'}
                            and len(names)<=1 and (not names or names==[op.name]),
                            'ambiguous_length_characteristic')
                    sign,digits,exponent=amount.as_tuple()
                    value=format(Decimal((sign,digits,exponent+unit[2])),'f')+' мм'
                    fields={'characteristic:'+op.name:value}
                else:
                    if op.kind=='add_quantity':
                        old_unit=measured_unit(item.unit)
                        require(item.quantity is not None and old_unit is not None
                                and old_unit[0]==unit[0],'addition_requires_compatible_known_quantity')
                        sign,digits,exponent=amount.as_tuple()
                        amount=exact_add(item.quantity,Decimal((sign,digits,exponent+unit[2]-old_unit[2])))
                        new_unit=item.unit
                    else:new_unit=unit[1]
                    fields={'quantity':amount,'unit':new_unit}
                for field,value in fields.items():
                    name=op.name if length else None
                    key=(item.id,'characteristic' if length else field,name)
                    require(key not in touched,'contradictory_duplicate_patch')
                    touched.add(key)
                    before=next((c.value for c in item.characteristics if c.name==name),None) if length else getattr(item,field)
                    if length:
                        chars=[c.model_copy(update={'value':value}) if c.name==name else c for c in item.characteristics]
                        if not any(c.name==name for c in chars):chars.append(Characteristic(name=name,value=value))
                        item=item.model_copy(update={'characteristics':chars})
                    else:item=item.model_copy(update={field:value})
                    changed.changes.append(Change(item_id=item.id,field=field,
                        before=str(before) if before is not None else None,after=str(value),
                        evidence=op.evidence+' '+op.unit_evidence,message=message))
                by_id[item.id]=item
                continue
            if op.kind == 'convert_mass':
                require(op.target_id in by_id and op.field is None and op.name is None
                        and not op.groups and op.replace_from is None and op.resolution is None,
                        'invalid_mass_conversion')
                require(bool(op.evidence) and op.evidence in message, 'edit_evidence_not_in_message')
                target=MASS_UNITS.get(op.value.strip().casefold()) if op.value else None
                evidence=MASS_UNITS.get(op.evidence.strip().casefold())
                require(target is not None and evidence==target, 'mass_conversion_unit_evidence')
                keys={(op.target_id,'quantity',None),(op.target_id,'unit',None)}
                require(not touched.intersection(keys), 'contradictory_duplicate_patch')
                touched.update(keys)
                item=by_id[op.target_id]
                quantity,unit=convert_mass(item.quantity,item.unit,op.value)
                by_id[item.id]=item.model_copy(update={'quantity':quantity,'unit':unit})
                for field,value in [('quantity',quantity),('unit',unit)]:
                    changed.changes.append(Change(item_id=item.id,field=field,
                        before=str(getattr(item,field)),after=str(value),evidence=op.evidence,message=message))
                continue
            if op.kind == 'resolve':
                require(op.field is None and op.name is None and op.value is None and op.evidence is None and not op.groups,
                        'unexpected_resolve_fields')
                old = next((r for r in changed.records if r.id == op.target_id and r.kind == 'unresolved'), None)
                require(old is not None, 'resolve_requires_existing_unresolved')
                ex = op.resolution
                require(bool(ex.items) and all(old.start <= r.start < r.end <= old.end for r in ex.records),
                        'resolution_outside_existing_fragment')
                # Original facts come ONLY from the previously stored source. A set
                # operation may subsequently apply an explicit current-message edit.
                changed = changed.model_copy(update={
                    'records': [r for r in changed.records if r.id != old.id] + ex.records,
                    'originals': changed.originals + ex.items,
                    'items': changed.items + ex.items,
                    'groups': changed.groups + ex.groups,
                    'questions': list(dict.fromkeys(changed.questions + ex.questions))})
                unique([i.id for i in changed.items], 'duplicate_item_id')
                by_id.update({i.id: i for i in ex.items})
                changed.changes.append(Change(item_id=old.id, field='resolve', before='unresolved',
                    after='resolved', evidence=current.original_text[old.start:old.end], message=message))
                continue
            if op.kind == 'regroup':
                require(not regrouped and op.target_id is None and op.field is None and op.name is None
                        and op.value is None and op.evidence is None and bool(op.groups), 'invalid_regroup')
                changed = changed.model_copy(update={'groups': op.groups})
                regrouped = True
                continue
            require(op.kind == 'set' and not op.groups and op.target_id in by_id and op.field is not None,
                    'invalid_set_target_or_field')
            require(bool(op.evidence) and op.evidence in message, 'edit_evidence_not_in_message')
            require(op.name is None if op.field != 'characteristic' else bool(op.name), 'invalid_characteristic_name')
            key = (op.target_id, op.field, op.name)
            require(key not in touched, 'contradictory_duplicate_patch')
            touched.add(key)
            item = by_id[op.target_id]
            before = getattr(item, op.field) if op.field != 'characteristic' else next(
                (c.value for c in item.characteristics if c.name == op.name), None)
            if op.field == 'quantity':
                value = None if op.value is None else decimal_text(op.value)
                require(value is None or decimal_text(op.evidence) == value, 'edited_quantity_evidence_mismatch')
            else:
                value = op.value
                require(value is None or bool(value.strip()) and value in op.evidence, 'new_value_missing_from_edit_evidence')
            if op.replace_from is not None:
                require(bool(op.replace_from.strip()) and before is not None and value is not None,
                        'literal_replacement_requires_existing_value')
                pattern = r'(?<!\w)' + re.escape(op.replace_from) + r'(?!\w)'
                require(len(re.findall(pattern, before)) == 1, 'literal_replacement_ambiguous_or_missing')
                replacement = value
                value = re.sub(pattern, lambda _: replacement, before)
            if op.field == 'characteristic':
                updated = [c.model_copy(update={'value': value}) if c.name == op.name else c for c in item.characteristics]
                if not any(c.name == op.name for c in updated):
                    updated.append(Characteristic(name=op.name, value=value))
                new_item = item.model_copy(update={'characteristics': updated})
            else:
                require(op.field != 'label' or value is not None, 'label_cannot_be_empty')
                new_item = item.model_copy(update={op.field: value})
            by_id[item.id] = new_item
            changed.changes.append(Change(item_id=item.id, field=op.field + (':' + op.name if op.name else ''),
                before=str(before) if before is not None else None, after=str(value) if value is not None else None,
                evidence=op.evidence, message=message))
        changed = changed.model_copy(update={'items': [by_id[i.id] for i in changed.items],
            'revision': current.revision+1, 'undo_stack': current.undo_stack + [current.revision],
            'questions': plan.remaining_questions if plan.remaining_questions is not None else changed.questions})
        from .theoretical_mass import refresh
        changed=refresh(changed,plan.mass_specs,self.mass_catalog,mass_approved)
        for op in operations:
            if op.kind=='calculate_mass':require(any(m.spec.item_id==op.target_id for m in changed.theoretical_mass),'mass_spec_required')
        before_mass={m.spec.item_id:m.tonnes for m in current.theoretical_mass}
        after_mass={m.spec.item_id:m.tonnes for m in changed.theoretical_mass}
        changed=changed.model_copy(update={'changes':[
            c.model_copy(update={
                'before':str(before_mass[c.item_id]) if before_mass.get(c.item_id) is not None else None,
                'after':str(after_mass[c.item_id]) if after_mass.get(c.item_id) is not None else None})
            if n>=len(current.changes) and c.field=='theoretical_mass' else c
            for n,c in enumerate(changed.changes)]})
        return self.validate(changed)

    @staticmethod
    def partial(request):
        return bool(request.questions or any(r.kind == 'unresolved' for r in request.records)
                    or any(i.quantity is None or not i.unit or any(c.value is None for c in i.characteristics) for i in request.items))
