"""Pure validation and transactional proposal building; GPT owns interpretation."""
from decimal import Decimal, InvalidOperation
import re
from uuid import uuid5, NAMESPACE_URL
from .contracts import RequestSnapshot, Characteristic, Change
from quantity import normalize_quantity, sum_quantities


class EngineError(ValueError):
    pass


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
    'г': ('г', -3), 'грамм': ('г', -3), 'грамма': ('г', -3), 'граммы': ('г', -3), 'граммов': ('г', -3),
    'кг': ('кг', 0), 'килограмм': ('кг', 0), 'килограмма': ('кг', 0),
    'килограммы': ('кг', 0), 'килограммов': ('кг', 0),
    'т': ('т', 3), 'тонна': ('т', 3), 'тонны': ('т', 3), 'тонну': ('т', 3), 'тонн': ('т', 3),
}


def convert_mass(quantity, source, target):
    require(quantity is not None and source is not None, 'mass_conversion_requires_known_quantity_and_unit')
    src=MASS_UNITS.get(source.strip().casefold())
    dst=MASS_UNITS.get(target.strip().casefold()) if target else None
    require(src is not None and dst is not None, 'unsupported_mass_conversion')
    # Construct a Decimal tuple: no context-dependent multiplication/rounding.
    sign,digits,exponent=quantity.as_tuple()
    return Decimal((sign,digits,exponent+src[1]-dst[1])),dst[0]


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
                require(bool(item.unit_text) and item.unit_text in fragment and item.unit == item.unit_text,
                        'unit_evidence:' + item.id)
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
        return request

    def create(self, extraction, source, event_key):
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
        return self.validate(request)

    def edit(self, current, plan, message, read_version):
        require(current is not None, 'no_active_request')
        require((plan.expected_request_id, plan.expected_revision) == (current.id, current.revision), 'stale_request_revision')
        operations = plan.operations
        if any(op.kind == 'undo' for op in operations):
            require(len(operations) == 1, 'undo_must_be_standalone')
            require(plan.remaining_questions is None, 'undo_restores_questions_without_override')
            op = operations[0]
            require(op.target_id is None and op.field is None and op.name is None and op.value is None
                    and op.evidence is None and not op.groups, 'unexpected_undo_fields')
            require(bool(current.undo_stack), 'nothing_to_undo')
            old = read_version(current.id, current.undo_stack[-1])
            require(old is not None, 'undo_version_missing')
            return self.validate(old.model_copy(deep=True, update={'revision': current.revision+1,
                'undo_stack': current.undo_stack[:-1]}))
        changed = current.model_copy(deep=True)
        by_id = {item.id: item for item in changed.items}
        touched = set()
        regrouped = False
        for op in operations:
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
        return self.validate(changed)

    @staticmethod
    def partial(request):
        return bool(request.questions or any(r.kind == 'unresolved' for r in request.records)
                    or any(i.quantity is None or not i.unit or any(c.value is None for c in i.characteristics) for i in request.items))
