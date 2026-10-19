"""Human-facing views of immutable facts; GPT supplies semantic display roles."""
from decimal import Decimal
from collections import Counter
import re
from .engine import totals, differences, RequestEngine
from .contracts import DisplayLayout


def number(value):
    text=format(Decimal(value), 'f')
    if '.' in text:text=text.rstrip('0').rstrip('.')
    return text.replace('.', ',')


def display_layout(request, previous=None, proposed=None):
    """Reuse references, never cached grade/dimension values, across edits and undo.

    Hints cannot add/remove products or edit a snapshot. Duplicate hints are
    ambiguous: drop that hint, preserving the product in the unclassified bucket.
    """
    if request is None:return None
    ids={item.id for item in request.items}
    hints={};orders={}
    for layout in (previous,proposed):
        if layout is None:continue
        duplicates={key for key,count in Counter(hint.item_id for hint in layout.items).items() if count>1}
        for key in duplicates:hints.pop(key,None)
        for hint in layout.items:
            if hint.item_id in ids and hint.item_id not in duplicates:hints[hint.item_id]=hint
        duplicate_orders={key for key,count in Counter(
            order.kind.strip().casefold() for order in layout.grade_order).items() if count>1}
        for key in duplicate_orders:orders.pop(key,None)
        for order in layout.grade_order:
            key=order.kind.strip().casefold()
            if key not in duplicate_orders:orders[key]=order
    if not hints:return None
    return DisplayLayout(items=list(hints.values()),grade_order=list(orders.values()))


def _fact(item,name):
    values=[c.value for c in item.characteristics if c.name==name]
    return values[0] if len(values)==1 else None


def _dimension(item,reference):
    """Only a whole numeric scalar/component with a known length unit is sortable."""
    text=_fact(item,reference.name)
    if text is None:return None
    unit=reference.unit
    suffix=re.search(r'(?<![а-яА-Яa-zA-Z])(мм|см|м)\s*$',text)
    if suffix:
        if unit is not None and suffix[1]!=unit:return None
        unit=suffix[1]
    if reference.component is not None:
        parts=text.split(reference.separator)
        if len(parts)<2 or reference.component>len(parts):return None
        text=parts[reference.component-1]
    match=re.fullmatch(r'\s*(\d+(?:[.,]\d+)?)\s*(мм|см|м)?\s*',text)
    if not match:return None
    if match[2]:
        if unit is not None and match[2]!=unit:return None
        unit=match[2]
    if unit is None:return None
    value=Decimal(match[1].replace(',','.'))
    sign,digits,exponent=value.as_tuple()
    return reference.role.strip().casefold(),Decimal((sign,digits,exponent+{'мм':0,'см':1,'м':3}[unit]))


def _dimensions(item,hint):
    references=hint.dimensions
    if hint.kind and hint.kind.strip().casefold()=='sheet':
        # BR-005 explicitly makes thickness primary. GPT identifies the field;
        # Python must not accidentally prioritize a supplied width/length first.
        primary=[ref for ref in references if ref.role.strip().casefold()=='thickness']
        if len(primary)!=1:return []
        references=primary+[ref for ref in references if ref is not primary[0]]
    return [_dimension(item,ref) for ref in references]


def display_order(request, layout=None):
    """The sole normalized/forward order for text AND addressing, without mutation."""
    base=[key for group in request.groups for key in group.item_ids]
    layout=display_layout(request,proposed=layout)
    if layout is None:return base
    hints={hint.item_id:hint for hint in layout.items}
    items={item.id:item for item in request.items}
    kinds={}
    for key in base:
        hint=hints.get(key)
        kind=(hint.kind.strip().casefold() or None) if hint and hint.kind else None
        kinds.setdefault(kind,[]).append(key)
    result=[]
    for kind,keys in kinds.items():
        if kind is None:
            result.extend(keys);continue
        grades={}
        for key in keys:
            hint=hints[key]
            grade=_fact(items[key],hint.grade_name) if hint.grade_name else None
            grades.setdefault(grade.strip().casefold() if grade else None,[]).append(key)
        grade_keys=list(grades)
        # Explicit, scoped order from this same coordinator call, not a Python
        # steel ranking. Reorder only the named slots, retaining all other grades.
        orders=[order for order in layout.grade_order if order.kind.strip().casefold()==kind]
        if len(orders)==1:
            requested=[grade.strip().casefold() for grade in orders[0].grades]
            if len(set(requested))==len(requested) and set(requested)<=set(grades):
                replacements=iter(requested)
                grade_keys=[next(replacements) if grade in requested else grade for grade in grade_keys]
        for grade in grade_keys:
            members=grades[grade]
            dimensions={key:_dimensions(items[key],hints[key]) for key in members}
            columns=0
            for index in range(max((len(row) for row in dimensions.values()),default=0)):
                roles={row[index][0] for row in dimensions.values() if index<len(row) and row[index] is not None}
                if len(roles)!=1:break  # Incomparable/unknown roles must not invent a size order.
                columns+=1
            def numeric_key(key):
                row=dimensions[key]
                return tuple((False,row[i][1]) if i<len(row) and row[i] is not None else (True,Decimal(0))
                             for i in range(columns))
            result.extend(sorted(members,key=numeric_key))
    return result


def totals_text(items, positions=None):
    sums, unknown=totals(items)
    lines=['Известная часть количества:' if unknown else 'Количество:']
    lines.extend(f'{number(amount)} {unit}' for unit,amount in sums.items())
    if not sums:lines.append('Нет подтверждённых количеств.')
    if unknown:
        numbers=positions or {item.id:n for n,item in enumerate(items,1)}
        unknown_numbers=[str(numbers[item.id]) for item in items if item.id in unknown]
        lines.append('Количество или единица не определены в позициях: '+', '.join(unknown_numbers))
    return '\n'.join(lines)


def _cities(request):
    values=[[c.value for c in item.characteristics if c.name.casefold() in {'city','город'} and c.value]
            for item in request.items]
    flat={v for row in values for v in row}
    return next(iter(flat)) if len(flat)==1 and all(values) else None


def _declared_count(item):
    """Additional source count is a fact, not a theoretical conversion of mass."""
    from .engine import decimal_text, PIECE_UNITS
    rows=[c for c in item.characteristics if c.name=='declared_quantity']
    if len(rows)!=1 or set(rows[0].source_ids)!={item.source_id}:return None
    def parse(text):
        match=re.fullmatch(r'\s*(\d+(?:[.,]\d+)?)\s*(\w+\.?)\s*',text or '')
        if not match or match[2].casefold() not in PIECE_UNITS:return None
        return decimal_text(match[1])
    value=parse(rows[0].value)
    return value if value is not None and value==parse(rows[0].raw) else None


def _item_text(item, original, city, changes=(), mass=None, compact=False, hint=None):
    label=item.label
    # Refresh a redundant label token from explicit, evidence-checked edits only.
    # No product grammar, guessed dimensions or mutation of the saved snapshot.
    for change in changes:
        if change.item_id!=item.id or not change.field.startswith('characteristic:') or not change.before or not change.after:
            continue
        name=change.field.split(':',1)[1]
        shared=any(c.name!=name and c.value==change.before for c in item.characteristics)
        pattern=r'(?<!\w)'+re.escape(change.before)+r'(?!\w)'
        if not shared and len(re.findall(pattern,label,re.I))==1:
            label=re.sub(pattern,lambda _:change.after,label,flags=re.I)
    if (mass and mass.spec.kind=='ibeam') or (compact and hint and hint.kind in {'beam','ibeam'}):
        label=re.sub(r'\bдвутавровая\b\s*','',label,flags=re.I).strip()
    if mass or compact:
        from .theoretical_mass import dimensions, product
        dims=dimensions(item,mass.spec) if mass else dict(value for ref in (hint.dimensions if hint else [])
                                                          if (value:=_dimension(item,ref)) is not None)
        if dims.get('width') is not None and dims.get('width')==dims.get('height'):
            label=re.sub(r'\bквадратная\b\s*','',label,flags=re.I).strip()
        label=re.sub(r',\s+', ' ',label)
    facts=[]
    from .engine import measured_unit, convert_mass
    unit_info=measured_unit(item.unit)
    count=_declared_count(item) if (compact and unit_info and unit_info[0]=='mass'
        and item.quantity==original.quantity and item.unit==original.unit) else None
    for characteristic in item.characteristics:
        if city and characteristic.name.casefold() in {'city','город'}:continue
        if count is not None and characteristic.name=='declared_quantity':continue
        value=characteristic.value
        if value is None:
            if (mass and mass.status=='calculated' and mass.reference.status=='owner_indicative'
                    and characteristic.name==mass.spec.standard_name):
                continue  # Explicit indicative basis tolerates unknown standard; audit retains it.
            facts.append(characteristic.name+': не уточнено'+
                         (' (в исходнике: '+characteristic.raw+')' if characteristic.raw else ''))
        else:
            shown=value
            if mass or compact:
                references=mass.spec.dimensions if mass else hint.dimensions if hint else []
                length_fields={d.name for d in references if d.role=='piece_length' and d.component is None}
                if characteristic.name in length_fields and dims.get('piece_length') is not None:
                    shown=number(product(dims['piece_length'],exponent=-3))+'м'
                elif mass and not compact and mass.spec.kind=='solid_carbon_sheet' and characteristic.name==mass.spec.grade_name:
                    shown='сталь '+value
                elif value.startswith('допускается '):shown='('+value+')'
            if re.search(r'(?<!\w)'+re.escape(value)+r'(?!\w)',label,re.I):
                if shown!=value:label=re.sub(r'(?<!\w)'+re.escape(value)+r'(?!\w)',lambda _:shown,label,flags=re.I)
            elif shown not in facts:facts.append(shown)
    text=label+(' '+ (' ' if mass or compact else '; ').join(facts) if facts else '')
    amount=number(item.quantity) if item.quantity is not None else 'количество не уточнено'
    from .theoretical_mass import PIECES
    unit='шт.' if (mass or compact) and item.unit and item.unit.strip().casefold() in PIECES else item.unit
    explicit_unit=any(change.item_id==item.id and change.field=='unit' for change in changes)
    if compact and not explicit_unit and item.quantity is not None and unit_info and unit_info[0]=='mass':
        converted,unit=convert_mass(item.quantity,item.unit,'т');amount=number(converted)
        if unit_info[2]==3 and item.quantity_text:
            from .engine import decimal_text
            if re.fullmatch(r'\d+(?:[.,]\d+)?',item.quantity_text) and decimal_text(item.quantity_text)==item.quantity:
                amount=item.quantity_text.replace('.',',')
    if count is not None:text+=' — '+number(count)+' шт. ('+amount+' '+unit+')'
    else:text+=' — '+amount+(' '+unit if unit else ' (единица не уточнена)')
    if original.unit and original.unit!=item.unit:
        original_amount=original.quantity_text or (number(original.quantity) if original.quantity is not None else 'количество не уточнено')
        text+=' (исходно: '+original_amount+' '+original.unit+')'
    return ' '.join(text.split())


def _humanize(text, request, layout=None):
    mapping={request.id:'текущая заявка'}
    mapping.update({key:f'позиция {n}' for n,key in enumerate(display_order(request,layout),1)})
    mapping.update({group.id:f'группа {n}' for n,group in enumerate(request.groups,1)})
    mapping.update({record.id:f'фрагмент {n}' for n,record in enumerate(request.records,1)})
    for key,value in sorted(mapping.items(),key=lambda pair:-len(pair[0])):
        text=re.sub(r'(?<!\w)'+re.escape(key)+r'(?!\w)',lambda _:value,text)
    return text


def literal_fragments(request):
    """Data boundaries for the delivery formatter, independent of view ordering."""
    if request is None:return []
    city=_cities(request);originals={item.id:item for item in request.originals}
    values=[request.original_text]
    for item in request.items:
        values.extend([item.label,_item_text(item,originals[item.id],city,request.changes)])
        values.append(_item_text(item,originals[item.id],city,request.changes,compact=True))
        mass=next((m for m in request.theoretical_mass if m.spec.item_id==item.id),None)
        if mass:values.append(_item_text(item,originals[item.id],city,request.changes,mass))
        values.extend(value for c in item.characteristics for value in (c.value,c.raw) if value)
    return values


def render(request, view='normalized', *, layout=None, expert=False):
    if request is None:return 'Активной заявки нет.'
    if view=='original':return 'Исходный текст (без изменений):\n'+request.original_text
    if request.theoretical_mass:
        from .theoretical_mass import validated_for_display
        request=validated_for_display(request)
    order=display_order(request,layout)
    positions={key:n for n,key in enumerate(order,1)}
    if view=='totals':
        text=totals_text(request.items,positions)
        if RequestEngine.partial(request) and text.startswith('Количество:'):
            text='Известная часть количества:'+text[len('Количество:'):]
        if any(r.kind=='unresolved' for r in request.records):
            text+='\nНеразобранные фрагменты в итог не включены.'
        return text
    detailed=view=='explanation'
    partial=RequestEngine.partial(request)
    masses={m.spec.item_id:m for m in request.theoretical_mass}
    # An expressly approved indicative basis can tolerate an unknown standard.
    # Keep the unknown fact and partial status; do not manufacture a mandatory
    # clarification in an otherwise approved compact preliminary request.
    if partial and not request.questions and not any(r.kind=='unresolved' for r in request.records):
        partial=any(item.quantity is None or not item.unit or any(c.value is None and not (
            item.id in masses and masses[item.id].status=='calculated'
            and masses[item.id].reference.status=='owner_indicative'
            and c.name==masses[item.id].spec.standard_name) for c in item.characteristics) for item in request.items)
    lines=[f'Заявка, версия {request.revision}.'] if detailed else []
    if partial:lines.append('Заявка требует уточнения.')
    city=_cities(request)
    by_id={item.id:item for item in request.items}
    originals={item.id:item for item in request.originals}
    if not detailed:
        from .theoretical_mass import display as mass_text
        hints={hint.item_id:hint for hint in layout.items} if layout else {}
        lines.extend(_item_text(by_id[key],originals[key],city,request.changes,masses.get(key),expert,hints.get(key))+
                     (mass_text(masses[key],compact=expert) if key in masses else '') for key in order)
    for n,group in enumerate(request.groups if detailed else [],1):
        members=[by_id[key] for key in group.item_ids]
        if not detailed or len(members)==1:
            for item in members:
                prefix=f'{positions[item.id]}. ' if detailed else ''
                lines.append(prefix+_item_text(item,originals[item.id],city,request.changes))
        else:
            lines.append(f'Группа {n}: '+group.label)
            for item in members:
                lines.append(f'  {positions[item.id]}. '+_item_text(item,originals[item.id],city,request.changes))
            lines.append('  '+totals_text(members,positions).replace('\n',' '))
            if differences(members) or group.differences:
                lines.append('  В группе есть технические отличия; взаимозаменяемость не подтверждена.')
        if detailed:
            lines.append('Обоснование группировки: '+_humanize(group.rationale,request,layout))
            lines.extend(_humanize(value,request,layout) for value in differences(members)+group.differences)
    if detailed and masses:
        from .theoretical_mass import explanation
        for key in order:
            if key in masses:
                lines.append(f'Расчёт позиции {positions[key]}:\n'+explanation(by_id[key],masses[key]))
    if city:lines.append('\nГород: '+city)
    fragment_number=0
    for record in request.records:
        if record.kind=='unresolved':
            fragment_number+=1
            lines.append(f'Неразобранный фрагмент {fragment_number}. Не обработано: '+request.original_text[record.start:record.end])
        elif detailed and record.kind!='product':
            lines.append('Дополнительный исходный текст: '+request.original_text[record.start:record.end])
    if request.questions:lines.append('\nУточните: '+'; '.join(_humanize(q,request,layout) for q in request.questions))
    return '\n'.join(lines)
