"""Explainable technical comparison; no identity keys or substitution engine."""
from decimal import Decimal
import re
from typing import Literal

from pydantic import Field, model_validator

from nomenclature import (Attribute, ContractModel, Measurement, NomenclatureSpec,
                          NonEmpty, Provenance)
from normalization_context import KnowledgeScope, RuleVersion, UserConfirmation
from normalizer import _normalize_grade
from recognition import _CHANNEL, _CROSS, _REBAR_CLASS, _spelling_view
from rules import REQUIRED_FIELDS
from source_attributes import (has_standard_evidence, parse_carbon_designation,
                               source_grade_matches, source_standard_matches)
from reference_lookup import ReferenceCandidate


MATCH_RULE_VERSION = 'technical-matching.v1'
TechnicalStatus = Literal['exact_match', 'equivalent_notation', 'incompatible', 'insufficient_data']
_SUPPORTED = {'angle', 'channel', 'rebar', 'sheet', 'profile_pipe'}
_LEGACY_PATHS = {'size_designation': 'profile.designation', 'grade': 'material.designation',
                 'diameter_mm': 'geometry.diameter', 'thickness_mm': 'geometry.thickness',
                 'width_mm': 'geometry.width', 'length_mm': 'geometry.length',
                 'height_mm': 'geometry.height', 'side_a_mm': 'geometry.leg_a',
                 'side_b_mm': 'geometry.leg_b', 'wall_thickness_mm': 'geometry.wall_thickness'}
_SIZE = re.compile(r'\d+(?:[.,]\d+)?(?:\s*[xхХX×*]\s*\d+(?:[.,]\d+)?){1,2}')


class CategorySufficiencyRule(ContractModel):
    """Caller-approved sufficiency, not an invented normative default."""
    category: NonEmpty
    required_paths: list[NonEmpty] = Field(min_length=1)
    standard_roles: dict[str, Literal['required', 'informational']] = Field(default_factory=dict)
    scope: KnowledgeScope
    rule: RuleVersion
    reason: NonEmpty
    provenance: list[Provenance] = Field(min_length=1)
    confirmation: UserConfirmation

    @model_validator(mode='after')
    def confirmed_nontrivial_rule(self):
        if self.confirmation.status != 'confirmed':
            raise ValueError('Sufficiency must be explicitly confirmed for its scope.')
        minimum = {'category', 'product_type', 'material.designation', 'execution.coating'}
        minimum.update(_LEGACY_PATHS[p] for p in REQUIRED_FIELDS.get(self.category, ()) if p in _LEGACY_PATHS)
        if not minimum.issubset(self.required_paths) or 'quantity' in self.required_paths:
            raise ValueError('A sufficiency rule must cover technical minimums; quantity is not identity.')
        return self


class ContextConstraint(ContractModel):
    scope: KnowledgeScope
    required_paths: list[NonEmpty] = Field(min_length=1)
    reason: NonEmpty
    provenance: list[Provenance] = Field(min_length=1)
    request_record_id: NonEmpty | None = None


class AlternativeBasis(ContractModel):
    """Permission to consider these snapshots, never approval of substitution."""
    request: NomenclatureSpec
    offer: NomenclatureSpec
    review_paths: list[NonEmpty] = Field(min_length=1)
    scope: KnowledgeScope
    reason: NonEmpty
    provenance: list[Provenance] = Field(min_length=1)
    confirmation: UserConfirmation = Field(default_factory=UserConfirmation)


class MatchingPolicy(ContractModel):
    sufficiency_rules: list[CategorySufficiencyRule] = Field(default_factory=list)
    constraints: list[ContextConstraint] = Field(default_factory=list)
    alternatives: list[AlternativeBasis] = Field(default_factory=list)


class AttributeFinding(ContractModel):
    path: NonEmpty
    outcome: Literal['matched', 'equivalent', 'different', 'missing', 'conflict', 'unassessed']
    request_state: str
    offer_state: str
    request_value: str | None = None
    offer_value: str | None = None
    significant: bool = True
    explanation: NonEmpty
    provenance: list[Provenance] = Field(default_factory=list)


class MatchResult(ContractModel):
    request: NomenclatureSpec
    offer: NomenclatureSpec
    status: Literal['exact_match', 'equivalent_notation', 'possible_alternative', 'incompatible', 'insufficient_data']
    technical_status: TechnicalStatus
    matching: list[AttributeFinding] = Field(default_factory=list)
    differing: list[AttributeFinding] = Field(default_factory=list)
    missing: list[AttributeFinding] = Field(default_factory=list)
    explanation: NonEmpty
    provenance: list[Provenance] = Field(default_factory=list)
    rule_version: Literal['technical-matching.v1'] = MATCH_RULE_VERSION
    applied_rules: list[RuleVersion] = Field(default_factory=list)
    alternative: AlternativeBasis | None = None
    reference_candidate: ReferenceCandidate | None = None
    automatic_substitution_allowed: Literal[False] = False


class MatchBatch(ContractModel):
    results: list[MatchResult] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    truncated: bool = False
    # Results retain input order including repeated records. No automatic winner.
    automatic_substitution_allowed: Literal[False] = False


def _scope_applies(scope, spec):
    if scope.kind == 'request':
        return spec.context is not None and scope.request_id == spec.context.request_id
    if scope.kind == 'product_type':
        return scope.product_type in [spec.category.value, spec.product_type.value]
    return True


def _scope_conditions(scope, spec):
    # Conditions are metadata in stage 101, not an executable language. An
    # unevaluated condition must not silently widen a rule's applicability.
    return not scope.conditions and _scope_applies(scope, spec)


def _attributes(spec):
    result = {'category': spec.category, 'product_type': spec.product_type}
    for section in ('profile', 'material', 'execution'):
        model = getattr(spec, section)
        result.update({f'{section}.{key}': getattr(model, key) for key in type(model).model_fields})
    for section in ('geometry', 'extensions'):
        result.update({f'{section}.{key}': value for key, value in getattr(spec, section).items()})
    return result


def _measurement_key(value):
    shift = {'mm': 0, 'мм': 0, 'cm': 1, 'см': 1, 'm': 3, 'м': 3}.get((value.unit or '').casefold())
    if shift is None or value.amount <= 0:
        return None
    sign, digits, exponent = value.amount.as_tuple()
    return Decimal((sign, digits, exponent + shift))


def _material_key(value):
    value = _spelling_view(value.strip())
    carbon = parse_carbon_designation(value)
    if carbon:
        return carbon.canonical().replace('B', 'В').casefold()
    if _REBAR_CLASS.fullmatch(value):
        return re.sub(r'\s+', '', value.upper()).replace('A', 'А').replace('C', 'С').casefold()
    matches = source_grade_matches(value)
    if len(matches) == 1 and matches[0].span() == (0, len(value)):
        compact = _normalize_grade(value)
        if re.fullmatch(r'[СC]\d{3}(?:-\d+)?', compact, re.I):
            compact = compact.upper().replace('C', 'С')
        return compact.casefold()
    return value  # Unknown vocabulary permits literal equality only.


def _profile_key(value, spec):
    value = value.strip()
    if spec.category.value == 'channel' and _CHANNEL.fullmatch(value):
        m = _CHANNEL.fullmatch(value)
        return Decimal(m['number'].replace(',', '.')), m['series'].upper().replace('U', 'У').replace('P', 'П')
    if _SIZE.fullmatch(value):
        # The existing recognizer has already proved expansion of equal legs.
        fields = {'angle': ['leg_a', 'leg_b', 'thickness'], 'profile_pipe': ['width', 'height', 'wall_thickness'],
                  'sheet': ['thickness', 'width', 'length']}.get(spec.category.value, [])
        values = [spec.geometry.get(key) for key in fields]
        if fields and all(attr and attr.is_resolved and isinstance(attr.value, Measurement)
                          and _measurement_key(attr.value) is not None for attr in values):
            return tuple(_measurement_key(attr.value) for attr in values)
        return None  # A textual profile cannot override unresolved geometry.
    return value  # No alias or commercial label can invent a profile match.


def _key(path, value, spec):
    if isinstance(value, Measurement):
        return _measurement_key(value)
    if path in {'material.designation', 'material.steel_grade', 'material.strength_class', 'material.rebar_class'}:
        return _material_key(value)
    if path == 'profile.designation':
        return _profile_key(value, spec)
    if path == 'profile.series' and re.fullmatch('[УПЭЛСUP]', value, re.I):
        return value.upper().replace('U', 'У').replace('P', 'П')
    if path.startswith('extensions.') or path == 'execution.supply_form':
        return value  # Opaque supplier/technical labels have no approved alias rule.
    return value.casefold() if isinstance(value, str) else value


def _display(attr):
    if not attr.is_resolved:
        return None
    if isinstance(attr.value, Measurement):
        return f'{attr.value.amount} {attr.value.unit or "(единица неизвестна)"}'
    return str(attr.value)


def _finding(path, a, b, outcome, reason, significant=True):
    return AttributeFinding(path=path, outcome=outcome, request_state=a.state, offer_state=b.state,
                            request_value=_display(a), offer_value=_display(b), significant=significant,
                            explanation=reason, provenance=a.provenance + b.provenance)


def _internal_findings(spec, side):
    findings = []
    full = spec.material.designation
    if full.is_resolved and full.raw_value:
        raw = _spelling_view(full.raw_value.strip())
        # Only the existing complete material grammar can prove that a source
        # requirement was shortened. Arbitrary prose is not reinterpreted.
        parsed_raw = parse_carbon_designation(raw)
        raw_matches = source_grade_matches(raw)
        if (parsed_raw or _REBAR_CLASS.fullmatch(raw) or len(raw_matches) == 1 and raw_matches[0].span() == (0, len(raw))) and _material_key(full.value) != _material_key(raw):
            findings.append(AttributeFinding(path='material.designation', outcome='conflict',
                request_state=full.state if side == 'request' else 'missing', offer_state=full.state if side == 'offer' else 'missing',
                explanation=f'{side}.material.designation: значение {full.value} не сохраняет полное исходное обозначение {full.raw_value}.',
                provenance=full.provenance))
    carbon = parse_carbon_designation(_spelling_view(full.value)) if full.is_resolved else None
    checks = []
    if carbon:
        expected = {'steel_grade': 'Ст' + carbon.steel_number, 'deoxidation': carbon.deoxidation,
                    'supply_group': carbon.supply_group.replace('B', 'В'),
                    'category': carbon.category or carbon.suffix.lstrip('-')}
        checks += [(f'material.{name}', getattr(spec.material, name), value, full)
                   for name, value in expected.items() if value]
    profile = spec.profile.designation
    if profile.is_resolved and spec.category.value == 'channel':
        match = _CHANNEL.fullmatch(profile.value.strip())
        if match:
            checks.append(('profile.series', spec.profile.series, match['series'].upper().replace('U', 'У').replace('P', 'П'), profile))
    if profile.is_resolved:
        match = _CROSS.fullmatch((profile.raw_value or profile.value).strip())
        fields = {'angle': ['leg_a', 'leg_b', 'thickness'], 'profile_pipe': ['width', 'height', 'wall_thickness'],
                  'sheet': ['thickness', 'width', 'length']}.get(spec.category.value, [])
        if match and fields:
            tokens = re.findall(r'\d+(?:[.,]\d+)?', match['size'])
            if len(tokens) == 2 and spec.category.value == 'angle' and spec.profile.cross_section.value == 'equal_leg':
                tokens = [tokens[0], tokens[0], tokens[1]]
            if len(tokens) == len(fields):
                # Bare profile sizes use the same versioned mm convention as
                # RecognitionService, rather than inventing a tolerance.
                unit = match['unit'] or 'mm'
                for name, token in zip(fields, tokens):
                    value = Measurement(amount=token.replace(',', '.'), unit=unit)
                    checks.append((f'geometry.{name}', spec.geometry.get(name, Attribute[Measurement]()), value, profile))
    for path, attr, expected, basis in checks:
        if attr.is_resolved and _key(path, attr.value, spec) is not None and _key(path, attr.value, spec) != _key(path, expected, spec):
            findings.append(AttributeFinding(path=path, outcome='conflict', request_state=attr.state if side == 'request' else 'missing',
                offer_state=attr.state if side == 'offer' else 'missing', request_value=_display(attr) if side == 'request' else None,
                offer_value=_display(attr) if side == 'offer' else None,
                explanation=f'{side}.{path}: структурированное значение {_display(attr)} противоречит полному исходному обозначению ({expected}).',
                provenance=attr.provenance + basis.provenance))
    return findings


class TechnicalMatchingService:
    def compare(self, request_spec, offer_spec, policy=None) -> MatchResult:
        request = NomenclatureSpec.model_validate(request_spec).model_copy(deep=True)
        offer = NomenclatureSpec.model_validate(offer_spec).model_copy(deep=True)
        policy = MatchingPolicy.model_validate(policy or MatchingPolicy())
        a, b = _attributes(request), _attributes(offer)
        matching, differing, missing = [], [], []
        differing += _internal_findings(request, 'request') + _internal_findings(offer, 'offer')
        rules = [rule for rule in policy.sufficiency_rules if request.category.is_resolved
                 and rule.category == request.category.value and _scope_conditions(rule.scope, request)]
        rule = rules[0] if len(rules) == 1 else None
        required = set(rule.required_paths) if rule else {'category', 'product_type', 'material.designation'}
        policy_evidence = list(rule.provenance) if rule else []
        constraint_reasons = {}
        for constraint in policy.constraints:
            if _scope_conditions(constraint.scope, request) and (constraint.request_record_id is None or constraint.request_record_id == request.record_id):
                required.update(constraint.required_paths)
                policy_evidence += constraint.provenance
                for path in constraint.required_paths:
                    constraint_reasons.setdefault(path, []).append(constraint.reason)
        blocking_missing = False
        for path in sorted(set(a) | set(b) | required):
            av, bv = a.get(path, Attribute[str]()), b.get(path, Attribute[str]())
            if av.state == 'conflict' or bv.state == 'conflict':
                differing.append(_finding(path, av, bv, 'conflict', f'{path}: противоречащие значения внутри исходной позиции.'))
                continue
            if not av.is_resolved or not bv.is_resolved:
                if path in required or av.state != 'missing' or bv.state != 'missing':
                    missing.append(_finding(path, av, bv, 'missing',
                        f'{path}: заявка — {av.state}, предложение — {bv.state}; совпадение не подтверждено.'
                        + (' Контекст: ' + '; '.join(constraint_reasons[path]) if path in constraint_reasons else '')))
                if (path in required or av.is_resolved != bv.is_resolved or av.state == 'ambiguous' or bv.state == 'ambiguous'
                    or path != 'material.category_kind' and (av.state == 'unknown' or bv.state == 'unknown')):
                    blocking_missing = True
                continue
            ak, bk = _key(path, av.value, request), _key(path, bv.value, offer)
            if ak is None or bk is None:
                missing.append(_finding(path, av, bv, 'unassessed', f'{path}: неизвестная единица или недопустимый размер.'))
                blocking_missing = True
            elif ak != bk:
                differing.append(_finding(path, av, bv, 'different',
                    f'{path}: {_display(av)} ≠ {_display(bv)}; техническое различие, без автоматической замены.'))
            else:
                equivalent = av.value != bv.value or av.raw_value != bv.raw_value
                matching.append(_finding(path, av, bv, 'equivalent' if equivalent else 'matched',
                    f'{path}: одно значение подтверждено проверяемой грамматикой написания/единиц.' if equivalent else f'{path}: явно известные значения совпадают.'))
        normative_block = self._standards(request, offer, rule, matching, differing, missing)
        for label, spec in [('request', request), ('offer', offer)]:
            if spec.unparsed_features:
                missing.append(AttributeFinding(path=f'{label}.unparsed_features', outcome='unassessed',
                    request_state='unknown' if label == 'request' else 'missing', offer_state='unknown' if label == 'offer' else 'missing',
                    explanation='Неразобранные исходные признаки могут быть технически значимыми.'))
                blocking_missing = True
        if any(f.significant for f in differing):
            technical = 'incompatible'
        elif (request.category.value not in _SUPPORTED or offer.category.value not in _SUPPORTED
              or rule is None or blocking_missing or normative_block):
            technical = 'insufficient_data'
        else:
            technical = 'equivalent_notation' if any(f.outcome == 'equivalent' for f in matching) else 'exact_match'
        reason = {'incompatible': 'Обнаружено значимое техническое противоречие.',
                  'insufficient_data': 'Недостаточно подтверждённых данных или применимого утверждённого правила достаточности.',
                  'equivalent_notation': 'Данных достаточно; различается только подтверждённое написание или единицы.',
                  'exact_match': 'Данных достаточно по явно подтверждённому правилу; технические значения совпадают.'}[technical]
        if not rules:
            reason += ' Утверждённое правило достаточности для категории/контекста не передано.'
        elif len(rules) > 1:
            reason += ' Несколько применимых правил достаточности; первое не выбирается автоматически.'
        if rule:
            reason += ' Основание достаточности: ' + rule.reason
        basis = None
        significant_paths = {f.path for f in differing if f.significant}
        has_internal_conflict = any(f.outcome == 'conflict' for f in differing)
        for proposed in policy.alternatives:
            if (technical in {'incompatible', 'insufficient_data'} and not has_internal_conflict
                and proposed.confirmation.status != 'rejected'
                and proposed.request == request and proposed.offer == offer and _scope_conditions(proposed.scope, request)
                and significant_paths.issubset(proposed.review_paths)
                and (significant_paths or any(f.path in proposed.review_paths for f in missing))):
                basis = proposed
                break
        if basis:
            reason += ' Есть явное основание рассмотреть альтернативу: ' + basis.reason + ' Принятие замены не разрешено.'
        evidence = [p for f in matching + differing + missing for p in f.provenance] + policy_evidence
        if basis:
            evidence += basis.provenance
        return MatchResult(request=request, offer=offer, status='possible_alternative' if basis else technical,
            technical_status=technical, matching=matching, differing=differing, missing=missing,
            explanation=reason, provenance=evidence, applied_rules=[rule.rule] if rule else [], alternative=basis)

    @staticmethod
    def _standards(request, offer, rule, matching, differing, missing):
        attrs = [request.normative_documents, offer.normative_documents]
        role_policy = rule.standard_roles if rule else {}
        groups = [{}, {}]
        blocked = False
        for side, attr in enumerate(attrs):
            if attr.state in {'ambiguous', 'conflict', 'unknown'}:
                blocked = True
                missing.append(_finding('normative_documents', attrs[0], attrs[1], 'unassessed', 'Нормативные требования не разрешены однозначно.'))
            for index, doc in enumerate(attr.value or []):
                if source_standard_matches(doc.raw_designation) and doc.edition.is_resolved:
                    claim = f'{doc.document_type} {doc.number}-{doc.edition.value}'
                    if not has_standard_evidence(claim, doc.raw_designation):
                        differing.append(AttributeFinding(path=f'normative_documents.{"request" if side == 0 else "offer"}.{index}',
                            outcome='conflict', request_state=attrs[0].state, offer_state=attrs[1].state,
                            explanation=f'Структурированное обозначение {claim} противоречит исходному {doc.raw_designation}.',
                            provenance=doc.provenance + doc.edition.provenance))
                        blocked = True
                        continue
                role = doc.role.value if doc.role.is_resolved else None
                if role is None or role not in role_policy or not doc.edition.is_resolved:
                    blocked = True
                    missing.append(AttributeFinding(path=f'normative_documents.{"request" if side == 0 else "offer"}.{index}',
                        outcome='unassessed', request_state=attrs[0].state, offer_state=attrs[1].state,
                        explanation='Роль, значимость или редакция документа не подтверждены; эквивалентность ГОСТ не выводится.',
                        provenance=doc.provenance + doc.role.provenance + doc.edition.provenance))
                    continue
                groups[side].setdefault(role, []).append(doc)
        for role in sorted(set(groups[0]) | set(groups[1]) | set(role_policy)):
            significant = role_policy.get(role) == 'required'
            av, bv = groups[0].get(role, []), groups[1].get(role, [])
            if not av or not bv:
                if significant:
                    blocked = True
                missing.append(_finding('normative_documents.' + role, attrs[0], attrs[1], 'missing',
                    f'Не подтверждены документы роли {role} на обеих сторонах.', significant=significant))
                continue
            keys = lambda docs: {(re.sub(r'\s+', ' ', d.document_type.strip()).casefold(), d.number.strip(), d.edition.value) for d in docs}
            evidence = [p for doc in av + bv for p in doc.provenance + doc.role.provenance + doc.edition.provenance]
            same = keys(av) == keys(bv)
            target = matching if same else differing
            target.append(AttributeFinding(path='normative_documents.' + role, outcome='matched' if same else 'different',
                request_state='present', offer_state='present', request_value=str(sorted(keys(av))), offer_value=str(sorted(keys(bv))),
                significant=significant, explanation=f'Документы роли {role}: ' + ('обозначения и редакции совпадают.' if same else 'различаются; нормативная эквивалентность не установлена.'),
                provenance=evidence))
        return blocked

    def compare_many(self, request_spec, offer_specs, policy=None) -> MatchBatch:
        return MatchBatch(results=[self.compare(request_spec, offer, policy) for offer in offer_specs])

    def compare_reference(self, request_spec, candidates, policy=None) -> MatchBatch:
        from reference_matching import ReferenceTechnicalAdapter
        from reference_lookup import ReferenceCandidates
        candidates = ReferenceCandidates.model_validate(candidates)
        request = NomenclatureSpec.model_validate(request_spec)
        if candidates.record_id != request.record_id:
            raise ValueError('ReferenceCandidates belongs to another source record.')
        adapter = ReferenceTechnicalAdapter()
        results = []
        for candidate in candidates.candidates:
            adapted = adapter.to_spec(candidate)
            result = self.compare(request, adapted, policy)
            results.append(result.model_copy(update={'reference_candidate': candidate.model_copy(deep=True)}))
        return MatchBatch(results=results, warnings=candidates.warnings + [
            'Reference aliases are search evidence, not technical identity.',
            f'reference_status:{candidates.status}'], truncated=candidates.truncated)


def compare(request_spec, offer_spec, policy=None) -> MatchResult:
    return TechnicalMatchingService().compare(request_spec, offer_spec, policy)
