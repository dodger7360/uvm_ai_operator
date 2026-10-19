"""Conservative, independently validated subplans; no product parsing or API calls."""
from collections import Counter
from bisect import bisect_left, bisect_right
from pydantic import ValidationError
from .contracts import SourceRecord, Item, Extraction, CommercialGroup
from .engine import RequestEngine, EngineError


def partial_from_proposal(proposal, source, event_key):
    """Never return the invalid proposal. Build and fully validate a NEW subplan.

    Ambiguous IDs/overlaps/product references are quarantined, not repaired.
    Every other non-whitespace source character remains explicitly unresolved.
    """
    if not isinstance(proposal, dict) or proposal.get('intent') != 'create_request' or proposal.get('operations'):
        return None
    extraction = proposal.get('extraction')
    if not isinstance(extraction, dict):
        return None
    raw_records, raw_items = extraction.get('records'), extraction.get('items')
    if not isinstance(raw_records, list) or not isinstance(raw_items, list):
        return None
    record_ids = Counter(r.get('id') for r in raw_records if isinstance(r, dict) and isinstance(r.get('id'), str))
    records = []
    for raw in raw_records:
        try:
            record = SourceRecord.model_validate(raw)
        except ValidationError:
            continue
        if record_ids[record.id] == 1 and 0 <= record.start < record.end <= len(source) and source[record.start:record.end].strip():
            records.append(record)
    # Include every valid geometry in the overlap test, even duplicate identifiers.
    geometries = [(r.get('start'), r.get('end')) for r in raw_records if isinstance(r, dict)
                  and type(r.get('start')) is int and type(r.get('end')) is int
                  and 0 <= r['start'] < r['end'] <= len(source)]
    starts=sorted(a for a,b in geometries)
    ends=sorted(b for a,b in geometries)
    records=[r for r in records if bisect_left(starts,r.end)-bisect_right(ends,r.start)==1]
    by_record = {r.id: r for r in records}
    source_ids = Counter(i.get('source_id') for i in raw_items if isinstance(i, dict) and isinstance(i.get('source_id'), str))
    item_ids = Counter(i.get('id') for i in raw_items if isinstance(i, dict) and isinstance(i.get('id'), str))
    accepted, retained = [], {}
    engine = RequestEngine()
    for raw in raw_items:
        try:
            item = Item.model_validate(raw)
            record = by_record.get(item.source_id)
            if not record or record.kind != 'product' or source_ids[item.source_id] != 1 or item_ids[item.id] != 1:
                continue
            keys = {item.source_id} | {key for c in item.characteristics for key in c.source_ids}
            if item.unit_source_id is not None:keys.add(item.unit_source_id)
            if any(key not in by_record or key != item.source_id and by_record[key].kind not in {'header', 'note'} for key in keys):
                continue
            selected = sorted((by_record[key] for key in keys), key=lambda r: r.start)
            # Small isolated validation input; global offsets/evidence are validated
            # again below against the UNCHANGED entire original, once, not N times.
            offset, local, fragments = 0, [], []
            for r in selected:
                fragment = source[r.start:r.end]
                local.append(r.model_copy(update={'start': offset, 'end': offset + len(fragment)}))
                fragments.append(fragment)
                offset += len(fragment) + 1
            group = CommercialGroup(id='recovery-group', label=item.label, item_ids=[item.id], rationale='Отдельная проверенная позиция')
            engine.create(Extraction(records=local, items=[item], groups=[group]), '\n'.join(fragments), event_key)
            accepted.append(item)
            retained.update({r.id: r for r in selected})
        except (ValidationError, EngineError):
            continue
    if not accepted:
        return None
    ordered = sorted(retained.values(), key=lambda r: r.start)
    result, start, used = [], 0, set(record_ids)
    def gap(a, b):
        if not source[a:b].strip():
            return
        key = f'recovery-unresolved-{a}-{b}'
        while key in used:
            key += '-'
        used.add(key)
        result.append(SourceRecord(id=key, kind='unresolved', start=a, end=b))
    for r in ordered:
        gap(start, r.start)
        result.append(r)
        start = r.end
    gap(start, len(source))
    groups = [CommercialGroup(id=f'recovery-group-{n}', label=item.label, item_ids=[item.id],
                rationale='Отдельная позиция после проверки; коммерческая группировка требует подтверждения')
              for n, item in enumerate(accepted, 1)]
    questions = ['сведения по неразобранным фрагментам.'] if any(r.kind == 'unresolved' for r in result) else [
        'Подтвердите раздельное представление позиций: предложенная группировка не применена.']
    try:
        return engine.create(Extraction(records=result, items=accepted, groups=groups, questions=questions), source, event_key)
    except (ValidationError, EngineError):
        return None
