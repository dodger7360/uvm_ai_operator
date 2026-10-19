"""Versioned calculation annotations. GPT supplies semantics; Python owns arithmetic.

No legacy recognizer, network, database or product grammar. Catalog is an injectable
read-only seed for the future Reference adapter, not a second reference database.
"""
from decimal import Decimal, localcontext, ROUND_HALF_UP
from hashlib import sha256
import json
from pathlib import Path
import re

from .contracts import MassReference, TheoreticalMass, PendingMass, DisplayDimension
from .engine import require, unique, decimal_text, measured_unit, PIECE_UNITS, LENGTH_UNITS, EngineError
from .telegram_format import safe_link


PIECES=set(PIECE_UNITS)
STANDARD_NAMES={'standard','gost','гост','норматив'}
LENGTH_NAMES={'length','length_mm','длина','piece_length','cut_length'}


def standard_values(item,spec):
    # Null facts are unknown, not an explicit constraint. Inspect actual facts
    # independently of GPT's optional pointer so it cannot hide an explicit norm.
    return {c.value for c in item.characteristics if c.value is not None
            and (c.name.casefold() in STANDARD_NAMES or c.name==spec.standard_name)}


def explicit_piece_length(item,spec):
    from .presentation import _dimension
    fields=[c for c in item.characteristics if c.name.casefold() in LENGTH_NAMES]
    if len(fields)!=1:return None
    field=fields[0]
    if any(d.name==field.name for d in spec.dimensions):return None  # Never reinterpret a total/other semantic length.
    if field.name.casefold()=='cut_length' and spec.cutting!='confirmed_piece_length':return None
    # Only a structured length with an explicit unit (or explicitly named mm).
    ref=DisplayDimension(role='piece_length',name=field.name,
                         unit='мм' if field.name.casefold()=='length_mm' else None)
    value=_dimension(item,ref)
    return value[1] if value and value[1]>0 else None


def product(*values, exponent=0):
    with localcontext() as ctx:
        ctx.prec=max(32,sum(len(value.as_tuple().digits) for value in values)+2)
        result=Decimal(1)
        for value in values:result*=value
    sign,digits,power=result.as_tuple()
    return Decimal((sign,digits,power+exponent))


def coefficient(reference):
    value=decimal_text(reference.value)
    require(value>0,'mass_coefficient_must_be_positive')
    require(all(safe_link(s.url) for s in reference.sources),'unsafe_mass_source')
    return value


def long_stock(spec):
    # Unknown products cannot opt out of stock-length checks. Fixed unit mass is
    # an explicitly separate semantic method, not an unscoped kg/piece shortcut.
    return spec.kind not in {'solid_carbon_sheet','fixed_unit'}


def validate_reference(record):
    coefficient(record)
    require(record.unit=='kg_per_m3' or record.profile or record.dimensions_mm,'unscoped_mass_reference')
    require(record.unit!='kg_per_m3' or record.kind=='solid_carbon_sheet','unsupported_density_method')
    require(all(decimal_text(v)>0 for v in record.dimensions_mm),'invalid_mass_reference_dimension')
    if record.family is not None:
        require(record.dimension_roles==sorted(set(record.dimension_roles))
                and len(record.dimension_roles)==len(record.dimensions_mm),'invalid_mass_reference_roles')
    require(record.piece_length_mm is None or decimal_text(record.piece_length_mm)>0,'invalid_mass_reference_length')
    if record.unit=='kg_per_piece' and long_stock(record):
        require(record.piece_length_mm is not None,'mass_reference_piece_length_required')
    if record.kind=='fixed_unit':
        require(record.unit=='kg_per_piece' and record.method=='fixed_unit_mass','unsupported_fixed_unit_method')
    if record.status=='owner_indicative':
        require(bool(record.approval_scope and record.approval_scope.strip()),'indicative_mass_approval_required')
    return record


class MassCatalog:
    def __init__(self,records=None):
        raw=json.loads(Path(__file__).with_name('mass_reference.json').read_text(encoding='utf-8')) if records is None else records
        self.records=tuple(MassReference.model_validate(r) for r in raw)
        unique([(r.id,r.revision) for r in self.records],'duplicate_mass_reference')
        for record in self.records:
            validate_reference(record)


def fact(item,name):
    values=[c.value for c in item.characteristics if c.name==name]
    return values[0] if len(values)==1 else None


def classification(item,spec):
    # Quantity and referenced dimensions may change without changing product class.
    # Any other fact/label change needs a fresh semantic classification from GPT.
    dimensions={d.name for d in spec.dimensions}
    raw=[item.label,[(c.name,c.value) for c in item.characteristics if c.name not in dimensions]]
    return sha256(json.dumps(raw,ensure_ascii=False).encode()).hexdigest()


def dimensions(item,spec):
    from .presentation import _dimension
    values={}
    for reference in spec.dimensions:
        require(reference.role not in values,'duplicate_mass_dimension_role')
        value=_dimension(item,reference)
        if value is not None and value[1]>0:values[reference.role]=value[1]
        else:values[reference.role]=None
    if 'piece_length' not in values:
        length=explicit_piece_length(item,spec)
        if length is not None:values['piece_length']=length
    return values


def scope(item,spec):
    dims=dimensions(item,spec)
    roles={'solid_carbon_sheet':['thickness','width','length'],
           'round_welded_pipe':['diameter','wall']}.get(spec.kind,sorted(r for r in dims if r not in {'piece_length','total_length'}))
    values=[dims.get(role) for role in roles]
    standards=standard_values(item,spec)
    return dims,values,fact(item,spec.profile_name),next(iter(standards)) if len(standards)==1 else None


def _dimension_role(name):
    """Names of structured dimension fields only; never parse a product/request.

    Wall and thickness are material gauge for this consistency check, not new
    product classifications or coefficient lookup keys. Original roles stay intact.
    """
    name=re.sub(r'[_\-(),\[\]]',' ',name.casefold())
    name=' '.join(name.split())
    # Metadata qualifiers only. Unknown qualifiers retain a dimensional signal
    # below and fail closed; this never interprets user prose or chooses a fact.
    modifiers={'explicit','specified','declared','actual','measured','of','or','and',
        'pipe','tube','sheet','profile','product','наружный','внешний','outer','external',
        'фактическая','фактический','измеренная','указанная','заданная',
        'трубы','труба','листа','лист','профиля','изделия','или','и'}
    name=' '.join(token for token in name.split() if token not in modifiers)
    unit=None
    match=re.search(r' (mm|cm|m|м|мм|см)$',name)
    if match:
        unit={'mm':'мм','cm':'см','m':'м'}.get(match[1],match[1]);name=name[:match.start()]
    aliases={'width':'width','ширина':'width','height':'height','высота':'height',
        'diameter':'diameter','outer diameter':'diameter','наружный диаметр':'diameter',
        'диаметр':'diameter','диаметр наружный':'diameter',
        'wall':'thickness','thickness':'thickness','wall thickness':'thickness',
        'thickness of wall':'thickness','толщина':'thickness','стенка':'thickness',
        'толщина стенки':'thickness','толщина листа':'thickness'}
    role=aliases.get(name)
    if role is None:
        dimensional={'width','height','diameter','wall','thickness',
            'ширина','высота','диаметр','стенка','стенки','толщина'}
        if dimensional.intersection(name.split()):role='ambiguous'
    return role,unit


def dimension_consistency(item,spec):
    """All independently explicit dimensional facts must agree with the basis.

    Reuses the existing strict scalar/component converter. Unreferenced scalar
    units must be explicit in value, field name or scalar raw; no guessed mm.
    Unknown compatibility blocks only mass, leaving every source fact unchanged.
    """
    from .presentation import _dimension
    reason='Уточните противоречивые или неразобранные размерные характеристики изделия'
    primary={};composites={}
    for reference in spec.dimensions:
        role,_=_dimension_role(reference.role)
        if role is None:continue
        value=_dimension(item,reference)
        if value is None or value[1]<=0:return reason
        primary.setdefault(role,set()).add(value[1])
        if reference.component is not None:composites.setdefault(reference.name,[]).append(reference)
    values={role:set(numbers) for role,numbers in primary.items()}
    for characteristic in item.characteristics:
        role,named_unit=_dimension_role(characteristic.name)
        if role=='ambiguous':return reason
        if role is not None:
            text=characteristic.value or ''
            suffix=re.fullmatch(r'\s*\d+(?:[.,]\d+)?\s*(мм|см|м)\s*',text)
            if suffix and named_unit and suffix[1]!=named_unit:return reason
            direct=[r.unit for r in spec.dimensions if r.name==characteristic.name and r.component is None and r.unit]
            raw_unit=re.fullmatch(r'\s*\d+(?:[.,]\d+)?\s*(мм|см|м)\s*',characteristic.raw or '')
            unit=suffix[1] if suffix else named_unit or (direct[0] if len(set(direct))==1 else None) or (raw_unit[1] if raw_unit else None)
            parsed=_dimension(item,DisplayDimension(role=role,name=characteristic.name,unit=unit))
            if parsed is None or parsed[1]<=0 or role not in primary:return reason
            values.setdefault(role,set()).add(parsed[1])
        elif characteristic.name.casefold() in {'size','dimensions','размер','размеры','profile','профиль','section','сечение','geometry','геометрия'}:
            if characteristic.name in composites or characteristic.name==spec.profile_name:continue
            # A second structured size field cannot be silently ignored. Only an
            # existing component layout can establish its meaning; no new grammar.
            if len(composites)!=1:return reason
            for reference in next(iter(composites.values())):
                parsed=_dimension(item,reference.model_copy(update={'name':characteristic.name}))
                role,_=_dimension_role(reference.role)
                if parsed is None or parsed[1]<=0:return reason
                values.setdefault(role,set()).add(parsed[1])
    if any(len(numbers)!=1 for numbers in values.values()):return reason
    return None


def piece_length(item,spec,*,allow_structured=False):
    """One length policy shared by catalog, search, confirmation and replay validation."""
    if spec.cutting not in {'none','confirmed_piece_length'}:
        return None,None,'Для резки подтвердите длину готовой детали и схему поставки'
    dims=dimensions(item,spec)
    references={d.name for d in spec.dimensions if d.role=='piece_length'}
    explicit={c.name for c in item.characteristics
              if c.name.casefold() in LENGTH_NAMES}
    if explicit-references and not (allow_structured and not references and len(explicit)==1 and explicit_piece_length(item,spec) is not None):
        return None,None,'Уточните противоречивую или неразобранную длину одного изделия'
    length=dims.get('piece_length')
    if length is not None:return length,'user',None
    if (spec.kind=='ibeam' and spec.cutting=='none' and fact(item,spec.profile_name)
            and not references and 'length' not in dims):
        return Decimal(12000),'uvm_default',None
    return None,None,'Уточните длину одного изделия, не общий метраж'


def applicable(reference,item,spec):
    try:validate_reference(reference)
    except (EngineError,ValueError):return False
    if reference.kind!=spec.kind:return False
    if len(standard_values(item,spec))>1:return False
    explicit_norms={c.name for c in item.characteristics if c.value is not None and c.name.casefold() in STANDARD_NAMES}
    if reference.status!='owner_indicative' and explicit_norms and explicit_norms!={spec.standard_name}:return False
    if dimension_consistency(item,spec):return False
    if reference.family is not None and reference.family!=spec.family:return False
    execution_names={c.name for c in item.characteristics if c.name.casefold() in {'execution','исполнение','coating','покрытие','type','тип'}}
    if execution_names and execution_names!={spec.execution_name}:return False
    dims,values,profile,standard=scope(item,spec)
    if reference.family is not None and reference.dimension_roles!=sorted(
            role for role in dims if role not in {'piece_length','total_length'}):return False
    if reference.unspecified_standard_only and standard is not None:return False
    if reference.profile and reference.profile!=profile:return False
    length,_,reason=piece_length(item,spec,allow_structured=reference.status=='owner_indicative') if long_stock(spec) else (dims.get('piece_length'),None,None)
    if long_stock(spec) and item.unit and item.unit.strip().casefold() in PIECES and reason:return False
    if reference.piece_length_mm is not None and (reason or length!=decimal_text(reference.piece_length_mm)):return False
    if reference.unit=='kg_per_piece' and ((dims.get('piece_length') is not None and reference.piece_length_mm is None)
                                        or values and not reference.dimensions_mm):return False
    if reference.dimensions_mm and (None in values or values!=[decimal_text(v) for v in reference.dimensions_mm]):return False
    if reference.standards and standard and standard not in reference.standards:return False
    if spec.standard_name and standard is None and reference.status!='owner_indicative':return False
    if spec.execution_name:
        execution=fact(item,spec.execution_name)
        if execution is None or execution!=reference.execution:return False
    if reference.grades and fact(item,spec.grade_name) not in reference.grades:return False
    return True


def calculate(item,spec,signature,records):
    base=dict(spec=spec,classification=signature,method='not_calculated')
    def unavailable(reason,status='unavailable'):
        return TheoreticalMass(**base,status=status,reason=reason)
    problem=dimension_consistency(item,spec)
    if problem:return unavailable(problem)
    if classification(item,spec)!=signature:return unavailable('Характеристики изменены: расчётное основание требует проверки')
    grade_audit={}
    if spec.grade_policy:
        grade=next((c for c in item.characteristics if c.name==spec.grade_name),None)
        approved={'ст3':'С255','09г2с':'С355'}
        if (spec.kind!='ibeam' or grade is None or not grade.raw
                or approved.get(grade.raw.strip().casefold())!=grade.value):
            return unavailable('Коммерческая марка балки требует подтверждённого основания UVM')
        grade_audit=dict(source_grade=grade.raw,grade_policy=spec.grade_policy)
    # A missing semantic pointer must not silently bypass an explicit standard.
    if len(standard_values(item,spec))>1:return unavailable('Уточните применимый норматив для расчёта')
    execution_names={c.name for c in item.characteristics if c.name.casefold() in {'execution','исполнение','coating','покрытие','type','тип'}}
    if execution_names and execution_names!={spec.execution_name}:
        return unavailable('Уточните применимость коэффициента к указанному исполнению')
    unit=measured_unit(item.unit)
    piece=bool(item.unit and item.unit.strip().casefold() in PIECES)
    if item.quantity is None:return unavailable('Количество не определено')
    if not piece and (not unit or unit[0]!='length'):return unavailable('Для расчёта нужны метры или штуки')
    length=None;origin=None
    if piece and long_stock(spec):
        length,origin,reason=piece_length(item,spec,allow_structured=any(r.status=='owner_indicative' for r in records))
        if reason:return unavailable(reason)
    candidates=[r for r in records if r.status in {'reviewed','owner_confirmed','owner_indicative'} and applicable(r,item,spec)]
    if not candidates:return unavailable('Нет проверенного коэффициента для этих характеристик')
    if len({(decimal_text(r.value),r.unit) for r in candidates})>1:
        return unavailable('Справочные массы различаются: подтвердите расчётное основание','conflicting')
    reference=candidates[0];value=coefficient(reference);dims,values,_,_=scope(item,spec)
    result=None;method=reference.method
    if reference.unit=='kg_per_m3':
        if spec.kind!='solid_carbon_sheet' or not piece:return unavailable('Метод сплошного листа здесь неприменим')
        if None in values or len(values)!=3:return unavailable('Уточните толщину, ширину и длину листа')
        result=product(item.quantity,*values,value,exponent=-12)
    elif reference.unit=='kg_per_piece':
        if not piece:return unavailable('Масса единицы применима только к штукам')
        result=product(item.quantity,value,exponent=-3)
    elif not piece:
        # Measured unit exponent is relative to mm; convert to m then kg to t.
        result=product(item.quantity,value,exponent=unit[2]-6)
    else:
        if length is None:length=dims.get('piece_length')
        if length is None:return unavailable('Уточните длину одного изделия, не общий метраж')
        result=product(item.quantity,length,value,exponent=-6)
        values=[*values,length]
    if origin=='uvm_default':method+='; UVM_default_piece_length_12000_mm'
    return TheoreticalMass(spec=spec,classification=signature,status='calculated',tonnes=result,
        reference=reference,dimensions_mm=[format(v,'f') for v in values],method=method,
        piece_length_mm=length,length_origin=origin,**grade_audit)


def refresh(request,specs=(),catalog=None,approved=()):
    if not request.theoretical_mass and not specs:return request
    catalog=catalog or MassCatalog()
    items={i.id:i for i in request.items};old={m.spec.item_id:m for m in request.theoretical_mass}
    unique([s.item_id for s in specs],'duplicate_mass_item')
    for spec in specs:
        require(spec.item_id in items,'unknown_mass_item')
        item=items[spec.item_id]
        original=next(i for i in request.originals if i.id==item.id)
        record=next(r for r in request.records if r.id==original.source_id)
        fragment=request.original_text[record.start:record.end]
        require(spec.evidence in fragment or spec.evidence in item.label,'mass_classification_evidence')
        names={c.name for c in item.characteristics}
        require(all(d.name in names for d in spec.dimensions),'mass_dimension_reference')
        require(all(name is None or name in names for name in (spec.profile_name,spec.standard_name,spec.grade_name,spec.execution_name)),
                'mass_fact_reference')
        signature=classification(item,spec)
        previous=old.get(item.id)
        pinned=previous.reference if previous and previous.classification==signature else None
        old[spec.item_id]=TheoreticalMass(spec=spec,classification=signature,status='unavailable',reference=pinned,method='pending')
    result=[]
    for item_id,annotation in old.items():
        require(item_id in items,'unknown_mass_item')
        # Pin reviewed/confirmed historical coefficients through ordinary edits;
        # do not silently switch a version to a future catalog revision.
        records=[annotation.reference] if annotation.reference and applicable(annotation.reference,items[item_id],annotation.spec) else list(catalog.records)
        extras=[r for key,r in approved if key==item_id]
        if extras:records=extras
        result.append(calculate(items[item_id],annotation.spec,annotation.classification,records))
    return request.model_copy(update={'theoretical_mass':result})


def validate_annotations(request):
    unique([m.spec.item_id for m in request.theoretical_mass],'duplicate_mass_item')
    for mass in request.theoretical_mass:
        item=next((i for i in request.items if i.id==mass.spec.item_id),None)
        require(item is not None,'unknown_mass_item')
        if mass.status=='calculated':
            require(mass.reference is not None and mass.tonnes is not None,'mass_annotation_missing_basis')
            expected=calculate(item,mass.spec,mass.classification,[mass.reference])
            require(expected==mass,'invalid_or_stale_mass_annotation')
        else:require(mass.tonnes is None,'unverified_mass_annotation')


def revalidate_historical(request):
    """Upgrade only missing audit metadata on a working copy, never the saved JSON.

    Recalculate from the historical reference ONLY. Numerical/core discrepancies,
    explicitly incorrect metadata and inapplicable references are not accepted as
    compatible history. Goods may still be restored with an unavailable annotation.
    New snapshots continue through the unchanged strict validate_annotations.
    """
    working=request.model_copy(deep=True)
    items={item.id:item for item in working.items};result=[]
    audit_fields={'piece_length_mm','length_origin','source_grade','grade_policy'}
    for mass in working.theoretical_mass:
        if mass.status!='calculated' and mass.tonnes is None:
            result.append(mass);continue
        reason='Отсутствует подтверждённый коэффициент или исходная позиция'
        if mass.status=='calculated' and mass.reference and mass.spec.item_id in items:
            try:
                expected=calculate(items[mass.spec.item_id],mass.spec,mass.classification,[mass.reference])
                missing=audit_fields-mass.model_fields_set
                if expected.status=='calculated' and mass.model_dump(exclude=missing)==expected.model_dump(exclude=missing):
                    result.append(expected);continue
                reason=(expected.reason if expected.status!='calculated' else
                        'Сохранённая масса или сведения расчёта не соответствуют проверенному основанию')
            except (EngineError,ValueError):reason='Не удалось проверить коэффициент или параметры расчёта'
        result.append(TheoreticalMass(spec=mass.spec,classification=mass.classification,reference=mass.reference,
            status='unavailable',method='not_calculated',reason='Сохранённое расчётное основание требует проверки: '+reason))
    return working.model_copy(update={'theoretical_mass':result})


def validated_for_display(request):
    # Readonly views use the same compatibility boundary, without storing a version.
    return revalidate_historical(request)


def rounded_tonnes(mass):
    value=mass.tonnes
    with localcontext() as ctx:
        ctx.prec=max(32,len(value.as_tuple().digits)+abs(value.as_tuple().exponent)+4)
        rounded=value.quantize(Decimal('.001'),rounding=ROUND_HALF_UP)
    if value>0 and rounded==0:
        return '< 0,001 т'
    return '≈'+format(rounded,'f').replace('.',',')+' т'


def display(mass,compact=False):
    if mass.status!='calculated':return ' (расчётная масса не определена: '+mass.reason+')'
    value=rounded_tonnes(mass)
    return ' / '+value if mass.spec.kind=='solid_carbon_sheet' and not compact else ' ('+value+')'


def explanation(item,mass):
    from .presentation import number
    if mass.status!='calculated':return 'Расчётная масса не определена: '+(mass.reason or 'основание не подтверждено')+'.'
    reference=mass.reference
    units={'kg_per_m':'кг/м','kg_per_piece':'кг/шт.','kg_per_m3':'кг/м³'}
    statuses={'reviewed':'проверенное расчётное основание','owner_confirmed':'коэффициент подтверждён пользователем',
              'owner_indicative':'одобренный владельцем предварительный ориентир'}
    lines=[f'Расчётная масса: {number(item.quantity)} {item.unit}.']
    if mass.piece_length_mm is not None:
        origin='указана пользователем' if mass.length_origin=='user' else 'коммерческое допущение UVM: балка 12 м'
        lines.append(f'Длина штуки: {number(mass.piece_length_mm)} мм; {origin}.')
    lines.extend([f'Основание: {reference.value} {units[reference.unit]}; редакция {reference.revision}.',
                  f'Точное значение: {number(mass.tonnes)} т; отображение: {rounded_tonnes(mass)}.',
                  f'Метод: {mass.method}. Статус: {statuses[reference.status]}.',
                  'Источник: '+'; '.join(source.title for source in reference.sources)+'.',
                  'Ограничения: '+reference.limitation])
    if reference.execution:lines.append('Исполнение основания: '+reference.execution)
    if reference.approval_scope:lines.append('Область согласования: '+reference.approval_scope)
    if mass.grade_policy:lines.append('Коммерческая марка: '+mass.source_grade+' → '+fact(item,mass.spec.grade_name)+
                                      '; правило UVM только для балок, не нормативная взаимозаменяемость.')
    return '\n'.join(lines)


def research_candidates(request,specs,answer,candidates,event_id,known_specs=()):
    """Untrusted observations need explicit operator confirmation, never URL-only trust."""
    by_id={s.item_id:s for s in specs};items={i.id:i for i in request.items}
    sources={s.url:s for s in answer.sources if safe_link(s.url)}
    records=[];keys=[];bindings=[];selected_specs=[]
    for candidate in candidates:
        require(candidate.item_id in by_id and candidate.source_url in sources,'mass_research_source_or_target')
        require(candidate.quote in answer.text and candidate.profile_evidence in candidate.quote,'mass_research_quote')
        value=decimal_text(candidate.value)
        require(value>0 and re.search(r'(?<![\d.,])'+re.escape(candidate.value)+r'(?![\d.,])',candidate.quote),
                'mass_research_number_evidence')
        amount=re.escape(candidate.value)
        suffix={'kg_per_m':r'кг\s*/\s*(?:м|п\.?\s*м)', 'kg_per_piece':r'кг\s*/\s*шт\.?'}[candidate.unit]
        direct=re.search(r'(?<![\d.,])'+amount+r'\s*'+suffix+r'(?!\w)',candidate.quote,re.I)
        table=(candidate.unit=='kg_per_m' and re.search(
            r'масса\s+1\s+м\s*,\s*кг\s*[:=]?\s*'+amount+r'(?![\d.,])',candidate.quote,re.I))
        require(direct or table,'mass_research_unit_evidence')
        # Do not take the profile from one row and the coefficient from another.
        require(any(candidate.profile_evidence in line and candidate.value in line
                    for line in candidate.quote.splitlines()),'mass_research_cross_row')
        spec=by_id[candidate.item_id];item=items[candidate.item_id]
        dims,values,profile,standard=scope(item,spec)
        length=None
        if item.unit and item.unit.strip().casefold() in PIECES and long_stock(spec):
            length,_,reason=piece_length(item,spec)
            require(reason is None,'mass_research_piece_length_unconfirmed')
        if candidate.unit=='kg_per_piece' and long_stock(spec):
            length_text=candidate.piece_length_evidence
            match=re.fullmatch(r'(\d+(?:[.,]\d+)?)\s*(мм|см|м)',length_text.strip()) if length_text else None
            require(match and length_text in candidate.quote and length is not None,
                    'mass_research_piece_length_missing')
            require(any(candidate.profile_evidence in line and candidate.value in line and length_text in line
                        for line in candidate.quote.splitlines()),'mass_research_piece_length_cross_row')
            quoted_length=product(decimal_text(match[1]),exponent=LENGTH_UNITS[match[2]][1])
            require(quoted_length==length,'mass_research_piece_length_mismatch')
        if profile:require(re.search(r'(?<!\w)'+re.escape(profile)+r'(?!\w)',candidate.profile_evidence),
                           'mass_research_profile_mismatch')
        else:
            require(bool(values) and None not in values,'mass_research_dimensions_missing')
            tokens=[Decimal(t.replace(',','.')) for t in re.findall(r'\d+(?:[.,]\d+)?',candidate.profile_evidence)]
            require(any(tokens[n:n+len(values)]==values for n in range(len(tokens))),'mass_research_dimensions_mismatch')
        if standard:require(candidate.standard_evidence and standard in candidate.standard_evidence
                            and candidate.standard_evidence in candidate.quote,'mass_research_standard_mismatch')
        execution=fact(item,spec.execution_name)
        if execution:require(candidate.execution_evidence and execution in candidate.execution_evidence
                             and candidate.execution_evidence in candidate.quote,'mass_research_execution_mismatch')
        # Applicability is checked against actual dimensions/profile, not the query.
        record=MassReference(id='research:'+event_id+':'+str(len(records)+1),revision='1',kind=spec.kind,
            profile=profile,dimensions_mm=[str(v) for v in values],standards=[standard] if standard else [],
            execution=execution,
            piece_length_mm=str(length) if candidate.unit=='kg_per_piece' and length is not None else None,
            grades=[fact(item,spec.grade_name)] if fact(item,spec.grade_name) else [],
            value=str(value),unit=candidate.unit,status='observed',method='web_observation: '+candidate.suitability+'; '+candidate.quote,
            sources=[sources[candidate.source_url]],checked_at='event:'+event_id,
            limitation=({'conflicting':'Источники противоречат друг другу. ',
                         'uncertain':'Применимость не установлена. '}.get(candidate.suitability,'')+
                         'Требует явного подтверждения коэффициента и применимости пользователем; не нормативная гарантия'))
        validate_reference(record)
        require(applicable(record,item,spec),'mass_research_inapplicable')
        records.append(record);keys.append(item.id);bindings.append(classification(item,spec));selected_specs.append(spec)
    require(bool(records),'mass_research_no_valid_candidates')
    require(set(keys)==set(by_id),'mass_research_incomplete_targets')
    return PendingMass(event_id=event_id,request_id=request.id,revision=request.revision,candidates=records,
                       specs=selected_specs,item_ids=keys,classifications=bindings,requested_specs=[*known_specs,*specs])


def confirm(request,plan,pending):
    require(pending is not None and (pending.event_id,pending.request_id,pending.revision)==
            (plan.mass_confirmation_event_id,request.id,request.revision),'mass_confirmation_stale')
    unique(plan.mass_approval_ids,'duplicate_mass_approval')
    chosen=[]
    for key in plan.mass_approval_ids:
        matches=[n for n,r in enumerate(pending.candidates) if r.id==key]
        require(len(matches)==1,'mass_confirmation_unknown_candidate')
        n=matches[0];item=next(i for i in request.items if i.id==pending.item_ids[n])
        require(classification(item,pending.specs[n])==pending.classifications[n],'mass_confirmation_changed_facts')
        record=pending.candidates[n].model_copy(update={'status':'owner_confirmed'})
        validate_reference(record)
        require(calculate(item,pending.specs[n],pending.classifications[n],[record]).status=='calculated',
                'mass_confirmation_inapplicable')
        chosen.append((item.id,record))
    unique([key for key,_ in chosen],'conflicting_mass_approvals')
    require({key for key,_ in chosen}==set(pending.item_ids),'mass_confirmation_incomplete_candidates')
    expected={s.item_id for s in pending.requested_specs} or set(pending.item_ids)
    require(expected=={op.target_id for op in plan.operations},'mass_confirmation_scope')
    return chosen
