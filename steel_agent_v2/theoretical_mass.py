"""Versioned calculation annotations. GPT supplies semantics; Python owns arithmetic.

No legacy recognizer, network, database or product grammar. Catalog is an injectable
read-only seed for the future Reference adapter, not a second reference database.
"""
from decimal import Decimal, localcontext, ROUND_HALF_UP
from hashlib import sha256
import json
from pathlib import Path
import re

from .contracts import MassReference, TheoreticalMass, PendingMass
from .engine import require, unique, decimal_text, measured_unit, PIECE_UNITS, LENGTH_UNITS
from .telegram_format import safe_link


PIECES=set(PIECE_UNITS)


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


class MassCatalog:
    def __init__(self,records=None):
        raw=json.loads(Path(__file__).with_name('mass_reference.json').read_text(encoding='utf-8')) if records is None else records
        self.records=tuple(MassReference.model_validate(r) for r in raw)
        unique([(r.id,r.revision) for r in self.records],'duplicate_mass_reference')
        for record in self.records:
            coefficient(record)
            require(record.unit=='kg_per_m3' or record.profile or record.dimensions_mm,'unscoped_mass_reference')
            require(record.unit!='kg_per_m3' or record.kind=='solid_carbon_sheet','unsupported_density_method')
            require(all(decimal_text(v)>0 for v in record.dimensions_mm),'invalid_mass_reference_dimension')
            require(record.piece_length_mm is None or decimal_text(record.piece_length_mm)>0,'invalid_mass_reference_length')


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
    return values


def scope(item,spec):
    dims=dimensions(item,spec)
    roles={'solid_carbon_sheet':['thickness','width','length'],
           'round_welded_pipe':['diameter','wall']}.get(spec.kind,sorted(r for r in dims if r not in {'piece_length','total_length'}))
    values=[dims.get(role) for role in roles]
    return dims,values,fact(item,spec.profile_name),fact(item,spec.standard_name)


def applicable(reference,item,spec):
    if reference.kind!=spec.kind:return False
    dims,values,profile,standard=scope(item,spec)
    if reference.profile and reference.profile!=profile:return False
    if reference.piece_length_mm is not None and dims.get('piece_length')!=Decimal(reference.piece_length_mm):return False
    if reference.unit=='kg_per_piece' and ((dims.get('piece_length') is not None and reference.piece_length_mm is None)
                                        or values and not reference.dimensions_mm):return False
    if reference.dimensions_mm and (None in values or values!=[Decimal(v) for v in reference.dimensions_mm]):return False
    if reference.standards and standard and standard not in reference.standards:return False
    if spec.standard_name and standard is None:return False
    if reference.grades and fact(item,spec.grade_name) not in reference.grades:return False
    return True


def calculate(item,spec,signature,records):
    base=dict(spec=spec,classification=signature,method='not_calculated')
    def unavailable(reason,status='unavailable'):
        return TheoreticalMass(**base,status=status,reason=reason)
    if classification(item,spec)!=signature:return unavailable('Характеристики изменены: расчётное основание требует проверки')
    # A missing semantic pointer must not silently bypass an explicit standard.
    norm_names={c.name for c in item.characteristics if c.name.casefold() in {'standard','gost','гост','норматив'}}
    if norm_names and norm_names!={spec.standard_name}:return unavailable('Уточните применимый норматив для расчёта')
    unit=measured_unit(item.unit)
    piece=bool(item.unit and item.unit.strip().casefold() in PIECES)
    if item.quantity is None:return unavailable('Количество не определено')
    if not piece and (not unit or unit[0]!='length'):return unavailable('Для расчёта нужны метры или штуки')
    candidates=[r for r in records if r.status in {'reviewed','owner_confirmed'} and applicable(r,item,spec)]
    if not candidates:return unavailable('Нет проверенного коэффициента для этих характеристик')
    if len({(Decimal(r.value),r.unit) for r in candidates})>1:
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
        length=dims.get('piece_length')
        if spec.cutting not in {'none','confirmed_piece_length'}:return unavailable('Для резки подтвердите длину готовой детали и схему поставки')
        if length is None and spec.kind=='ibeam' and spec.cutting=='none' and not any(d.role=='piece_length' for d in spec.dimensions):
            if any(c.name.casefold() in {'length','length_mm','длина','piece_length','cut_length'} for c in item.characteristics):
                return unavailable('Уточните указанную длину одного изделия')
            length=Decimal(12000);method+='; UVM_default_piece_length_12000_mm'
        if length is None:return unavailable('Уточните длину одного изделия, не общий метраж')
        result=product(item.quantity,length,value,exponent=-6)
        values=[*values,length]
    return TheoreticalMass(spec=spec,classification=signature,status='calculated',tonnes=result,
        reference=reference,dimensions_mm=[format(v,'f') for v in values],method=method)


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
        require(all(name is None or name in names for name in (spec.profile_name,spec.standard_name,spec.grade_name)),
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


def display(mass):
    if mass.status!='calculated':return ' (расчётная масса не определена: '+mass.reason+')'
    value=mass.tonnes
    with localcontext() as ctx:
        ctx.prec=max(32,len(value.as_tuple().digits)+abs(value.as_tuple().exponent)+4)
        rounded=value.quantize(Decimal('.001'),rounding=ROUND_HALF_UP)
    if value>0 and rounded==0:
        return ' / < 0,001 т (теоретическая)'
    return ' / ≈ '+format(rounded,'f').replace('.',',')+' т'


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
        if candidate.unit=='kg_per_piece' and spec.kind in {'ibeam','round_welded_pipe','linear'}:
            length_text=candidate.piece_length_evidence
            match=re.fullmatch(r'(\d+(?:[.,]\d+)?)\s*(мм|см|м)',length_text.strip()) if length_text else None
            require(match and length_text in candidate.quote and dims.get('piece_length') is not None,
                    'mass_research_piece_length_missing')
            length=product(decimal_text(match[1]),exponent=LENGTH_UNITS[match[2]][1])
            require(length==dims['piece_length'],'mass_research_piece_length_mismatch')
        if profile:require(re.search(r'(?<!\w)'+re.escape(profile)+r'(?!\w)',candidate.profile_evidence),
                           'mass_research_profile_mismatch')
        else:
            require(bool(values) and None not in values,'mass_research_dimensions_missing')
            tokens=[Decimal(t.replace(',','.')) for t in re.findall(r'\d+(?:[.,]\d+)?',candidate.profile_evidence)]
            require(any(tokens[n:n+len(values)]==values for n in range(len(tokens))),'mass_research_dimensions_mismatch')
        if standard:require(candidate.standard_evidence and standard in candidate.standard_evidence
                            and candidate.standard_evidence in candidate.quote,'mass_research_standard_mismatch')
        # Applicability is checked against actual dimensions/profile, not the query.
        record=MassReference(id='research:'+event_id+':'+str(len(records)+1),revision='1',kind=spec.kind,
            profile=profile,dimensions_mm=[str(v) for v in values],standards=[standard] if standard else [],
            piece_length_mm=str(dims['piece_length']) if candidate.unit=='kg_per_piece' and dims.get('piece_length') else None,
            grades=[fact(item,spec.grade_name)] if fact(item,spec.grade_name) else [],
            value=str(value),unit=candidate.unit,status='observed',method='web_observation: '+candidate.suitability+'; '+candidate.quote,
            sources=[sources[candidate.source_url]],checked_at='event:'+event_id,
            limitation=({'conflicting':'Источники противоречат друг другу. ',
                         'uncertain':'Применимость не установлена. '}.get(candidate.suitability,'')+
                         'Требует явного подтверждения коэффициента и применимости пользователем; не нормативная гарантия'))
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
        chosen.append((item.id,pending.candidates[n].model_copy(update={'status':'owner_confirmed'})))
    unique([key for key,_ in chosen],'conflicting_mass_approvals')
    require({key for key,_ in chosen}==set(pending.item_ids),'mass_confirmation_incomplete_candidates')
    expected={s.item_id for s in pending.requested_specs} or set(pending.item_ids)
    require(expected=={op.target_id for op in plan.operations},'mass_confirmation_scope')
    return chosen
