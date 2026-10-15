"""Shared, local technical recognition. No AI, storage, display or SKU identity.

This is an adapter over the existing literal grammars. Numeric dimensions are
read as Decimal instead of going through RequestItem's legacy float geometry.
"""
from collections.abc import Iterable
from decimal import Decimal
import re
from typing import Literal

from pydantic import Field

from nomenclature import (Attribute, ContractModel, Measurement, NomenclatureSpec,
                          NonEmpty, NormalizationContextRef, NormativeDocument,
                          Provenance, SourceRef)
from normalizer import (UNIT_ALIASES, _detect_product_group, _normalize_dimensions,
                        _normalize_grade, explicit_equal_angle, _EXPLICIT_LENGTH_RE)
from quantity import QUANTITY_RE, without_mass_annotation
from schemas import RequestItem
from source_attributes import (has_standard_evidence, parse_carbon_designation,
                               source_grade_matches, source_standard_matches,
                               source_standards)
from source_fidelity import source_product_rows
from reference_lookup import ReferenceCandidates, ReferenceLookupContext, ReferenceReader


RULE_VERSION = 'local-recognition.v1'
_SUPPORTED = {'angle', 'channel', 'rebar', 'sheet', 'profile_pipe', 'round_pipe', 'fitting'}
_N = r'\d+(?:[.,]\d+)?'
_DIM_UNIT = r'мм|mm|см|cm|м|m'
_CROSS = re.compile(
    rf'(?<![\w.,+\-−])(?P<size>{_N}(?:\s*[xхХX×*]\s*{_N}){{1,2}})'
    rf'(?:\s*(?P<unit>{_DIM_UNIT})(?!\w))?(?![\w.,]|\s*[xхХX×*])', re.I)
_DIAMETER = re.compile(
    rf'(?<!\w)(?:[фФØø⌀]|д\s*|d\s*=?|диаметр(?:ом)?\s*[:=]?|диам\.?)\s*(?P<number>{_N})'
    rf'(?:\s*(?P<unit>{_DIM_UNIT})(?!\w))?(?![\w.,])', re.I)
_CHANNEL = re.compile(rf'(?<!\w)(?P<number>{_N})\s*(?P<series>[УПЭЛСUP])(?!\w)', re.I)
_PRODUCT = re.compile(
    r'(?<!\w)(?P<name>труб\w*(?:\s+(?:профильн\w*|проф\.?))?|'
    r'(?:профильн\w*|проф\.)\s+труб\w*|уголок\w*|уг\.|'
    r'швеллер\w*|швел\.|шв\.|арматур\w*|арм\.|лист\w*|отвод\w*|фланец\w*)(?!\w)', re.I)
_EQUAL = re.compile(r'(?<!\w)(?:равнополоч\w*|равнопол\.)(?!\w)', re.I)
_UNEQUAL = re.compile(r'(?<!\w)(?:неравнополоч\w*|неравнопол\.)(?!\w)', re.I)
_REBAR_CLASS = re.compile(r'(?<!\w)[АA]\s*\d{3}(?:[СC])?(?!\w)', re.I)
_MATERIAL_LABEL = re.compile(r'(?<!\w)(?:сталь|марка|материал)\s*[:=]?\s*(?P<label>[\w.-]+)', re.I)
_GRADE_SPELLING = re.compile(r'(?<!\w)\d{2}[ГG]\d[СC](?:-\d+)?(?!\w)', re.I)
_ROLLING = re.compile(r'(?<!\w)(?:г\s*/?\s*к|х\s*/?\s*к|горячекатан\w*|холоднокатан\w*)(?!\w)', re.I)
_MANUFACTURING = re.compile(r'(?<!\w)(?:бесшовн\w*|электросварн\w*|э[\\/]с|б/ш)(?!\w)', re.I)
_COATING = re.compile(r'(?<!\w)(?:без\s+покрытия|оцинкован\w*)(?!\w)', re.I)
_DIM_UNITS = {'мм': 'mm', 'mm': 'mm', 'см': 'cm', 'cm': 'cm', 'м': 'm', 'm': 'm'}
_UNIT_SPELLINGS = dict(UNIT_ALIASES, **{'шт.': 'шт'})
_UNIT_WORDS = '|'.join(re.escape(unit).replace(r'\ ', r'\s+')
                       for unit in sorted(_UNIT_SPELLINGS, key=len, reverse=True))
_LONG_QUANTITY = re.compile(rf'(?<![\w.,xхХX×*])({_N})\s*({_UNIT_WORDS})(?!\w)', re.I)


class RecognitionRecord(ContractModel):
    """Input envelope permits blank text, which NomenclatureSpec cannot contain."""
    record_id: NonEmpty
    source: SourceRef
    original_description: str


class RecognitionPolicy(ContractModel):
    # Only supported profile/diameter grammar may use this convention. Disable
    # it to retain unknown units. Every use records a versioned rule, not a fact
    # that the original text literally supplied millimetres.
    implicit_dimension_unit: Literal['mm'] | None = 'mm'
    rule_version: Literal['local-recognition.v1'] = RULE_VERSION


class RecognitionResult(ContractModel):
    record: RecognitionRecord
    spec: NomenclatureSpec | None = None
    status: Literal['recognized', 'partial', 'unsupported', 'empty', 'ambiguous']
    issues: list[str] = Field(default_factory=list)
    reference: ReferenceCandidates | None = None

    @property
    def record_id(self):
        return self.record.record_id


class RecognitionBatch(ContractModel):
    results: list[RecognitionResult] = Field(default_factory=list)
    policy: RecognitionPolicy = Field(default_factory=RecognitionPolicy)


def _decimal(token):
    from quantity import literal_decimal
    return literal_decimal(token)


def _spelling_view(text):
    # These replacements preserve offsets. No transliteration of arbitrary
    # brands, notes or unparsed attributes: only established grade/doc grammar.
    text = re.sub(r'(?<!\w)GOST(?![A-Za-z])', 'ГОСТ', text, flags=re.I)
    return _GRADE_SPELLING.sub(lambda m: m[0].upper().replace('G', 'Г').replace('C', 'С'), text)


class _Builder:
    def __init__(self, record, context, policy):
        self.record = record
        self.text = record.original_description
        self.view = _spelling_view(self.text)
        self.policy = policy
        self.spec = NomenclatureSpec(record_id=record.record_id, source=record.source,
                                     original_description=self.text, context=context)
        self.data = self.spec.model_dump()
        self.consumed = [False] * len(self.text)
        self.issues = []

    def evidence(self, span, rule=None):
        start, end = span
        return Provenance(source_id=self.record.source.source_id, record_id=self.record.record_id,
                          locator=f'{self.record.source.locator or self.record.record_id}#chars={start}:{end}',
                          fragment=self.text[start:end], method='rule' if rule else 'source',
                          rule_id=rule, rule_version=self.policy.rule_version if rule else None)

    def consume(self, span):
        start, end = span
        self.consumed[start:end] = [True] * (end - start)

    def claim(self, value, span, normalized=None, rule=None):
        return Attribute[type(value)](state='present', value=value,
                                      raw_value=self.text[slice(*span)], normalized_text=normalized,
                                      provenance=[self.evidence(span, rule)])

    def choose(self, candidates, value_type, issue):
        """Candidates carry (value, span, normalized spelling, rule ID)."""
        if not candidates:
            return Attribute[value_type]()
        values = []
        for value, span, normalized, rule in candidates:
            self.consume(span)
            if value not in values:
                values.append(value)
        evidence = [self.evidence(span, rule) for _, span, _, rule in candidates]
        if len(values) > 1:
            self.issues.append(issue)
            return Attribute[value_type](state='ambiguous', alternatives=values, provenance=evidence,
                                          reason='Несколько исходных значений; выбор не сделан.')
        result = self.claim(*candidates[0])
        return result.model_copy(update={'provenance': evidence})

    def masked(self):
        return ''.join(' ' if used else ch for ch, used in zip(self.view, self.consumed))

    def measure(self, token, unit, span):
        canonical = _DIM_UNITS.get((unit or '').lower()) if unit else self.policy.implicit_dimension_unit
        amount = _decimal(token)
        if amount <= 0:
            self.issues.append('invalid_geometry')
        rule = None if unit else 'profile-dimension-unit-convention'
        if canonical in {'cm', 'm'}:
            sign, digits, exponent = amount.as_tuple()
            amount = Decimal((sign, digits, exponent + {'cm': 1, 'm': 3}[canonical]))
            canonical, rule = 'mm', 'dimension-unit-conversion'
        return Measurement(amount=amount, unit=canonical), span, None, rule

    def finish(self, status=None):
        residue = ''.join(' ' if used else ch for ch, used in zip(self.text, self.consumed))
        features = [part.strip() for part in re.split(r'[\n;]', residue)
                    if re.sub(r'[\s,.:/\-—–"\'«»()\[\]*×]+', '', part)]
        self.data['unparsed_features'] = features
        if features:
            self.issues.append('unparsed_features')
        spec = NomenclatureSpec.model_validate(self.data)
        return RecognitionResult(record=self.record, spec=spec,
                                 status=status or ('partial' if self.issues else 'recognized'),
                                 issues=list(dict.fromkeys(self.issues)))


class RecognitionService:
    """Stateless local service; a batch neither merges nor removes input records."""

    def __init__(self, reference_reader: ReferenceReader | None = None,
                 reference_context: ReferenceLookupContext | None = None):
        self.reference_reader = reference_reader
        self.reference_context = ReferenceLookupContext.model_validate(reference_context) if reference_context is not None else None

    def recognize_records(self, records: Iterable[RecognitionRecord | dict],
                          context: NormalizationContextRef | None = None,
                          policy: RecognitionPolicy | None = None) -> RecognitionBatch:
        policy = RecognitionPolicy.model_validate(policy or RecognitionPolicy())
        context = NormalizationContextRef.model_validate(context) if context is not None else None
        records = [RecognitionRecord.model_validate(record) for record in records]
        ids = [record.record_id for record in records]
        if len(set(ids)) != len(ids):
            raise ValueError('record_id must be unique within a recognition batch.')
        results = [self._recognize(record, context, policy) for record in records]
        if self.reference_reader is not None:
            specs = [result.spec.model_copy(deep=True) for result in results if result.spec is not None]
            # Providers receive detached snapshots. Reference lookup cannot
            # overwrite the recognized source facts, even through mutable lists.
            lookup_context = self.reference_context or ReferenceLookupContext(normalization=context)
            if lookup_context.normalization is None and context is not None:
                lookup_context = lookup_context.model_copy(update={'normalization': context})
            found = self.reference_reader.find_candidates(specs, lookup_context) if specs else []
            found = [ReferenceCandidates.model_validate(result) for result in found]
            if len(found) != len(specs) or {r.record_id for r in found} != {s.record_id for s in specs}:
                raise ValueError('Reference reader must return one result for every recognized record_id.')
            by_id = {result.record_id: result for result in found}
            results = [result.model_copy(update={'reference': by_id.get(result.record_id)}) for result in results]
        return RecognitionBatch(results=results, policy=policy)

    def recognize_record(self, record: RecognitionRecord | dict,
                         context: NormalizationContextRef | None = None,
                         policy: RecognitionPolicy | None = None) -> RecognitionResult:
        return self.recognize_records([record], context, policy).results[0]

    def _recognize(self, record, context, policy):
        if not record.original_description.strip():
            return RecognitionResult(record=record, status='empty', issues=['empty_description'])
        b = _Builder(record, context, policy)
        products = [m for m in _PRODUCT.finditer(b.text) if not re.search(r'к\s+этой\s*$',b.text[:m.start()],re.I)]
        # Existing logical row grammar is reused only for coverage. It does not
        # call AI source matching or turn a record into several output records.
        if len(source_product_rows(b.text)) > 1 or len(products) > 1:
            b.issues.append('multiple_products_in_record')
            return b.finish('ambiguous')
        group = _detect_product_group(b.text)
        product = products[0] if products else None
        if product:
            name = product['name'].casefold()
            group = _detect_product_group(name) or (
                'angle' if name == 'уг.' else 'channel' if name in {'шв.', 'швел.'} else
                'rebar' if name == 'арм.' else 'fitting' if name.startswith(('отвод','фланец')) else None)
            if group == 'pipe':
                group = 'profile_pipe' if re.search(r'проф', name) or any(
                    len(re.findall(_N, m['size'])) == 3 for m in _CROSS.finditer(b.view)) else 'round_pipe'
            b.consume(product.span())
        if group not in _SUPPORTED:
            if group:
                b.data['category'] = b.claim(group, (0, len(b.text)), rule='existing-product-group-grammar')
            else:
                b.data['category'] = Attribute[str](state='unknown', raw_value=b.text,
                    provenance=[b.evidence((0, len(b.text)))], reason='Вид изделия не распознан.')
            b.issues.append('unsupported_product')
            return b.finish('unsupported')
        if product is None:
            b.issues.append('unsupported_product_spelling')
            return b.finish('unsupported')
        b.data['category'] = b.claim(group, product.span(), rule='existing-product-group-grammar')
        b.data['product_type'] = b.claim(group, product.span(), rule='existing-product-group-grammar')
        if group == 'fitting':
            b.data['product_type'] = b.claim(product[0].capitalize(),product.span())
        self._standards(b)
        self._material(b)
        self._execution(b)
        self._geometry(b, group, product.end())
        self._length(b)
        self._quantity(b)
        from quantity import length_mass_columns
        columns=length_mass_columns(b.text,group)
        if columns:
            from nomenclature import Measurement
            b.data['geometry']['length']=b.claim(Measurement(amount=columns['length_mm'],unit='mm'),columns['span'],rule='length-unit-amount-export-v1')
            b.data['quantity']=b.claim(Measurement(amount=columns['quantity'],unit='кг'),columns['span'],rule='length-unit-amount-export-v1')
            b.consume(columns['span'])
            b.issues=[issue for issue in b.issues if issue not in {'invalid_quantity','ambiguous_quantity'}]
        return b.finish()

    @staticmethod
    def _standards(b):
        matches = source_standard_matches(b.view)
        canonical = source_standards(b.view)
        documents = []
        for match, spelling in zip(matches, canonical):
            if not has_standard_evidence(spelling, b.view):
                continue
            kind, number = re.sub(r'\s+', ' ', match['kind'].upper()), re.sub(r'\s+', '', match['number'])
            if kind == 'ГОСТР':
                kind = 'ГОСТ Р'
            edition = Attribute[str]()
            if kind.startswith('ГОСТ') and re.fullmatch(r'.+-(?:\d{2}|\d{4})', number):
                number, suffix = number.rsplit('-', 1)
                edition = b.claim(suffix, match.span(), rule='gost-edition-segment')
            documents.append(NormativeDocument(raw_designation=b.text[slice(*match.span())],
                document_type=kind, number=number, edition=edition,
                role=Attribute[str](state='unknown', reason='Назначение документа не установлено текстом.'),
                provenance=[b.evidence(match.span())]))
            b.consume(match.span())
        if documents:
            b.data['normative_documents'] = Attribute[list[NormativeDocument]](state='present', value=documents,
                provenance=[b.evidence(match.span()) for match in matches])

    @staticmethod
    def _material(b):
        matches = source_grade_matches(b.view) + list(_REBAR_CLASS.finditer(b.view))
        matches.sort(key=lambda m: m.start())
        designations = [(b.text[slice(*m.span())], m.span(), None, None) for m in matches]
        designation = b.choose(designations, str, 'ambiguous_material')
        b.data['material']['designation'] = designation
        if not designation.is_resolved:
            if not matches:
                unknown = _MATERIAL_LABEL.search(b.view)
                if unknown:
                    b.data['material']['designation'] = Attribute[str](state='unknown',
                        raw_value=b.text[slice(*unknown.span('label'))],
                        provenance=[b.evidence(unknown.span())], reason='Обозначение вне поддерживаемой грамматики.')
                    b.issues.append('unknown_material')
            return
        match = matches[0]
        raw = b.view[slice(*match.span())]
        normalized = _normalize_grade(raw)
        parsed = parse_carbon_designation(raw)
        if parsed:
            normalized = parsed.canonical()
            if parsed.supply_group == 'B':
                normalized = 'В' + normalized[1:]
            parts = {'steel_grade': 'Ст' + parsed.steel_number,
                     'deoxidation': parsed.deoxidation,
                     'supply_group': parsed.supply_group.replace('B', 'В'),
                     'category': parsed.category or parsed.suffix.lstrip('-')}
        elif _REBAR_CLASS.fullmatch(raw):
            normalized = re.sub(r'\s+', '', raw.upper()).replace('A', 'А').replace('C', 'С')
            parts = {'rebar_class': normalized}
        elif re.fullmatch(r'[СC]\s*\d{3}(?:-\d+)?', raw, re.I):
            normalized = re.sub(r'\s+', '', raw.upper()).replace('C', 'С')
            parts = {'strength_class': normalized.split('-')[0],
                     'category': normalized.partition('-')[2]}
        else:
            parts = {'steel_grade': normalized}
        b.data['material']['designation'] = designation.model_copy(update={
            'normalized_text': normalized,
            'provenance': designation.provenance + [b.evidence(match.span(), 'material-spelling')]})
        for name, value in parts.items():
            if value:
                b.data['material'][name] = b.claim(value, match.span(), rule='material-designation-components')
        if parts.get('category'):
            b.data['material']['category_kind'] = Attribute[str](state='unknown',
                raw_value=parts['category'], provenance=[b.evidence(match.span())],
                reason='Смысл категории требует нормативного контекста.')
        if parsed and parsed.suffix:
            b.data['extensions']['material_suffix'] = b.claim(parsed.suffix, match.span())

    @staticmethod
    def _execution(b):
        for key, pattern in [
            ('wall_selection',r'\bстенк\w*\s+(?:самая\s+ходовая(?:\s*\([^)]*\))?|любая)'),
            ('substitution',r'\bможно\s+замену\b'),
            ('related_pipe',r'\bк\s+этой\s+трубе\b')]:
            for match in re.finditer(pattern,b.masked(),re.I):
                if re.search(r'\b(?:не|без)\s*$',b.text[:match.start()],re.I): continue
                b.data['extensions'][key]=b.claim(match[0],match.span(),rule='explicit-procurement-condition-v1')
                b.consume(match.span())
        for field, pattern in [('rolling_method' , _ROLLING), ('manufacturing_method', _MANUFACTURING),
                               ('coating', _COATING)]:
            candidates = []
            for match in pattern.finditer(b.masked()):
                raw = re.sub(r'\s+', '', match[0].casefold())
                if field == 'rolling_method':
                    value = 'cold_rolled' if raw.startswith(('х', 'холод')) else 'hot_rolled'
                elif field == 'manufacturing_method':
                    value = 'seamless' if raw.startswith(('бесшов', 'б/ш')) else 'electric_welded'
                else:
                    value = 'none' if raw.startswith('без') else 'galvanized'
                candidates.append((value, match.span(), value, 'explicit-execution-spelling'))
            b.data['execution'][field] = b.choose(candidates, str, f'ambiguous_{field}')

    @staticmethod
    def _geometry(b, group, product_end):
        if group == 'channel':
            matches = list(_CHANNEL.finditer(b.masked()))
            for field in ('designation', 'series'):
                candidates = []
                for m in matches:
                    series = m['series'].upper().replace('U', 'У').replace('P', 'П')
                    value = b.text[slice(*m.span())] if field == 'designation' else series
                    candidates.append((value, m.span(), m['number'] + series if field == 'designation' else series,
                                       'channel-series-spelling'))
                b.data['profile'][field] = b.choose(candidates, str, 'ambiguous_profile')
            if not matches:
                b.issues.append('missing_profile')
            return
        crosses = list(_CROSS.finditer(b.masked())) if group != 'rebar' else []
        fields = {'angle': ('leg_a', 'leg_b', 'thickness'),
                  'profile_pipe': ('width', 'height', 'wall_thickness'),
                  'sheet': ('thickness', 'width', 'length')}
        geometries = []
        equal = list(_EQUAL.finditer(b.text))
        unequal = list(_UNEQUAL.finditer(b.text))
        for m in equal + unequal:
            b.consume(m.span())
        equal_standard = explicit_equal_angle(RequestItem(product_group=group, source_text=b.view))
        for m in crosses:
            tokens = re.findall(_N, m['size'])
            span = m.span()
            if group == 'angle' and len(tokens) == 2:
                if (equal or equal_standard) and not unequal:
                    tokens = [tokens[0], tokens[0], tokens[1]]
                else:
                    b.issues.append('unresolved_angle_dimensions')
                    for field in fields[group]:
                        b.data['geometry'][field] = Attribute[Measurement](state='unknown',
                            raw_value=b.text[slice(*span)], provenance=[b.evidence(span)],
                            reason='Две величины без однозначного признака равнополочного уголка.')
                    b.consume(span)
                    continue
            if len(tokens) != 3:
                b.issues.append('unresolved_dimensions')
                continue
            geometries.append((m, tokens))
        if crosses:
            b.data['profile']['designation'] = b.choose([
                (b.text[slice(*m.span('size'))], m.span(), re.sub(r'\s*×\s*', '×', _normalize_dimensions(m['size'])),
                 'dimension-separator-spelling') for m in crosses], str, 'ambiguous_profile')
        for field_index, field in enumerate(fields.get(group, ())):
            if geometries:
                attr = b.choose([
                    b.measure(tokens[field_index], m['unit'], m.span()) for m, tokens in geometries],
                    Measurement, 'ambiguous_geometry')
                if group == 'angle' and any(len(re.findall(_N, m['size'])) == 2 for m, _ in geometries):
                    proof = [(m.span(), 'equal-leg-abbreviation') for m in equal]
                    if equal_standard:
                        proof += [(m.span(), 'equal-angle-standard-grammar') for m in source_standard_matches(b.view)
                                  if re.match(r'8509(?:-|$)', re.sub(r'\s+', '', m['number']))]
                    attr = attr.model_copy(update={'provenance': attr.provenance + [b.evidence(span, rule) for span, rule in proof]})
                b.data['geometry'][field] = attr
        if geometries and group in {'angle', 'profile_pipe'}:
            values = [(('equal_leg' if _decimal(tokens[0]) == _decimal(tokens[1]) else 'unequal_leg')
                       if group == 'angle' else ('square' if _decimal(tokens[0]) == _decimal(tokens[1]) else 'rectangular'),
                       m.span(), None, 'cross-section-geometry') for m, tokens in geometries]
            if group == 'angle':
                values += [('equal_leg', m.span(), None, 'explicit-angle-execution') for m in equal]
                values += [('unequal_leg', m.span(), None, 'explicit-angle-execution') for m in unequal]
            cross_section = b.choose(values, str, 'ambiguous_cross_section')
            b.data['profile']['cross_section'] = cross_section
            if group == 'angle' and cross_section.is_resolved and cross_section.value == 'equal_leg':
                b.data['product_type'] = b.claim('equal_leg_angle', geometries[0][0].span(), rule='cross-section-geometry')
        if group in {'rebar','round_pipe','fitting'}:
            matches = list(_DIAMETER.finditer(b.masked()))
            if not matches:
                # A leading bare diameter is permitted only immediately after
                # the product name and when it is not a quantity/steel class.
                match = re.match(rf'\s+(?P<number>{_N})(?:\s*(?P<unit>{_DIM_UNIT})(?!\w))?(?![\w.,])',
                                 b.masked()[product_end:], re.I)
                if match and not _LONG_QUANTITY.match(b.view, product_end + match.start('number')):
                    span = (product_end + match.start('number'), product_end + match.end())
                    b.data['geometry']['diameter'] = b.claim(*b.measure(match['number'], match['unit'], span))
                    b.consume(span)
            if matches:
                b.data['geometry']['diameter'] = b.choose([
                    b.measure(m['number'], m['unit'], m.span()) for m in matches], Measurement, 'ambiguous_geometry')
        if group == 'round_pipe':
            walls=[b.measure(m['number'],'мм',m.span()) for m in re.finditer(
                r'\bстенк\w*\s*(?P<number>'+_N+r')\s*(?:мм)?(?![\w.,])',b.masked(),re.I)]
            if walls:
                b.data['geometry']['wall_thickness']=b.choose(walls,Measurement,'ambiguous_geometry')
        if group == 'sheet' and not crosses:
            # Only a leading thickness, after an explicit rolling-method token.
            match = re.match(rf'\s*(?P<number>{_N})(?:\s*(?P<unit>{_DIM_UNIT})(?!\w))?(?![\w.,])',
                             b.masked()[product_end:], re.I)
            if match:
                span = (product_end + match.start('number'), product_end + match.end())
                if not _LONG_QUANTITY.match(b.view, product_end + match.start('number')) or match['unit'] in {'мм', 'mm', 'см', 'cm'}:
                    b.data['geometry']['thickness'] = b.claim(*b.measure(match['number'], match['unit'], span))
                    b.consume(span)
        if not b.data['geometry']:
            b.issues.append('missing_geometry')

    @staticmethod
    def _length(b):
        candidates = []
        for m in _EXPLICIT_LENGTH_RE.finditer(b.masked()):
            alternative = re.match(rf'\s*(?:[-–/]|или\b|до\b)\s*(?P<number>{_N})'
                                   rf'(?:\s*(?P<unit>{_DIM_UNIT})(?!\w))?(?![\w.,])', b.view[m.end():], re.I)
            if alternative:
                b.issues.append('unresolved_length')
                span = (m.start(), m.end() + alternative.end())
                values = [b.measure(m[1], m[2], span)[0],
                          b.measure(alternative['number'], alternative['unit'] or m[2], span)[0]]
                b.data['geometry']['length'] = Attribute[Measurement](state='ambiguous', alternatives=values,
                    raw_value=b.text[slice(*span)], provenance=[b.evidence(span, 'explicit-length-alternatives')],
                    reason='Диапазон или альтернативная длина: единственное значение не выбрано.')
                b.consume(span)
                return
            candidates.append(b.measure(m[1], m[2], m.span()))
        if candidates:
            length = b.choose(candidates, Measurement, 'ambiguous_length')
            existing = b.data['geometry'].get('length')
            if existing is not None:
                old = Attribute[Measurement].model_validate(existing)
                if not old.has_same_known_value(length):
                    b.issues.append('conflicting_length')
                    length = Attribute[Measurement](state='conflict',
                        alternatives=[old.value, length.value] if old.is_resolved and length.is_resolved else [
                            *(old.alternatives or ([old.value] if old.is_resolved else [])),
                            *(length.alternatives or ([length.value] if length.is_resolved else []))],
                        provenance=old.provenance + length.provenance, reason='Длина в размерах и отдельном требовании различается.')
            b.data['geometry']['length'] = length

    @staticmethod
    def _quantity(b):
        remaining = b.masked()
        from quantity import LITERAL_QUANTITY_RE
        # Existing pieces = theoretical tonnes annotation is not a second input
        # amount. Use its existing grammar without changing original offsets.
        annotation_free = without_mass_annotation(remaining)
        if annotation_free != remaining:
            for m in re.finditer(r'\s*=\s*' + _N + r'\s*(?:тн|тонн(?:а|ы)?|т)(?!\w)', remaining, re.I):
                if re.search(r'шт\.?\s*$', remaining[:m.start()], re.I):
                    remaining = remaining[:m.start()] + ' ' * (m.end() - m.start()) + remaining[m.end():]
                    b.consume(m.span())
        by_start = {}
        for pattern in (LITERAL_QUANTITY_RE,):
            for m in pattern.finditer(remaining):
                if m.start() not in by_start or m.end() > by_start[m.start()].end():
                    by_start[m.start()] = m
        candidates = []
        for m in by_start.values():
            amount = _decimal(m[1])
            if amount <= 0 or re.search(r'[+−]\s*$|-$|(?:^|[=:])\s*-\s*$', b.text[:m.start()]):
                b.issues.append('invalid_quantity')
                continue
            raw_unit = re.sub(r'\s+', ' ', m[2].lower())
            unit = _UNIT_SPELLINGS.get(raw_unit, raw_unit)
            if unit == 'пог. м':
                unit = 'м'
            candidates.append((Measurement(amount=amount, unit=unit), m.span(), None, 'quantity-unit-spelling'))
        b.data['quantity'] = b.choose(candidates, Measurement, 'ambiguous_quantity')


def recognize_records(records, context=None, policy=None, *, reference_reader=None, reference_context=None) -> RecognitionBatch:
    return RecognitionService(reference_reader, reference_context).recognize_records(records, context, policy)


def recognize_record(record, context=None, policy=None, *, reference_reader=None, reference_context=None) -> RecognitionResult:
    return RecognitionService(reference_reader, reference_context).recognize_record(record, context, policy)
