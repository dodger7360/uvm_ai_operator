"""Synthetic explicit policies are fixtures, not production sufficiency rules."""
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import json
from pathlib import Path
import subprocess
import sys

import pytest
from pydantic import ValidationError

from nomenclature import Attribute, Measurement, NomenclatureSpec, NormativeDocument, Provenance
from normalization_context import KnowledgeScope
from recognition import RecognitionRecord, recognize_record
from reference_lookup import (ReferenceCandidate, ReferenceCandidates, ReferenceEntry,
                              ReferenceEvidence)
from reference_matching import ReferenceTechnicalAdapter
from technical_matching import (AlternativeBasis, CategorySufficiencyRule, ContextConstraint,
                                MatchResult, MatchingPolicy, TechnicalMatchingService, compare)


PROOF = Provenance(source_id='synthetic-test', method='fixture', fragment='explicit fixture assertion')
RECEIPT = dict(status='confirmed', actor_id='test-reviewer',
               confirmed_at=datetime(2026, 10, 15, tzinfo=timezone.utc), evidence='Test policy confirmation')
GENERAL = dict(kind='general')
EXAMPLES = {
    'angle': 'Уголок 50х50х5 Ст3 без покрытия',
    'channel': 'Швеллер 10У С345-6 без покрытия',
    'rebar': 'Арматура А500С ф12 без покрытия',
    'sheet': 'Лист г/к 6х1500х6000 Ст3 без покрытия',
    'profile_pipe': 'Труба профильная 80х40х3 электросварная Ст3 без покрытия',
}
BASE = ['category', 'product_type', 'material.designation', 'execution.coating']
SHAPES = {'angle': ['geometry.leg_a', 'geometry.leg_b', 'geometry.thickness'],
          'channel': ['profile.designation', 'profile.series'],
          'rebar': ['geometry.diameter', 'material.rebar_class'],
          'sheet': ['geometry.thickness', 'geometry.width', 'geometry.length', 'execution.rolling_method'],
          'profile_pipe': ['geometry.width', 'geometry.height', 'geometry.wall_thickness', 'execution.manufacturing_method']}


def spec(text, identity='request', request_id=None):
    context = dict(context_id='context-' + request_id, request_id=request_id, normalization_version='v1') if request_id else None
    result = recognize_record(RecognitionRecord(record_id=identity, source=dict(source_id='fixture-input', source_type='text'),
                                                original_description=text), context=context)
    assert result.spec is not None
    return result.spec


def policy(category, roles=None, scope=None):
    rule = CategorySufficiencyRule(category=category, required_paths=BASE + SHAPES[category],
        standard_roles=roles or {}, scope=scope or GENERAL,
        rule=dict(rule_id='test-only-' + category, version='v1'), reason='Synthetic test scope, not a normative standard',
        provenance=[PROOF], confirmation=RECEIPT)
    return MatchingPolicy(sufficiency_rules=[rule])


def update_attribute(value, path, attribute):
    data = value.model_dump()
    section, name = path.split('.')
    data[section][name] = attribute.model_dump()
    return NomenclatureSpec.model_validate(data)


def known(value):
    return Attribute[type(value)](state='present', value=value, provenance=[PROOF])


@pytest.mark.parametrize('category', list(EXAMPLES))
def test_full_technical_equality_requires_confirmed_category_sufficiency(category):
    request, offer = spec(EXAMPLES[category]), spec(EXAMPLES[category], 'offer')
    plain = compare(request, offer)
    assert plain.status == 'insufficient_data'
    assert 'правило' in plain.explanation
    result = compare(request, offer, policy(category))
    assert result.status == result.technical_status == 'exact_match'
    assert result.matching and not result.differing
    assert all(f.request_state == f.offer_state == 'present' for f in result.matching)
    assert result.provenance and result.applied_rules[0].rule_id == 'test-only-' + category
    assert not result.automatic_substitution_allowed
    assert MatchResult.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize('left,right,category', [
    ('Уголок 50х50х5 В Ст.3 сп.5 без покрытия', 'Уг. равнопол. 50*5 ВСт3сп5 без покрытия', 'angle'),
    ('Уголок 50х50х5 Ст3сп5 без покрытия', 'Уголок 50X50X5 Ст.3 сп.5 без покрытия', 'angle'),
    ('Швеллер 10У C345-6 без покрытия', 'Швеллер 10U С345-6 без покрытия', 'channel'),
    ('Арматура A500C ф12 без покрытия', 'Арматура А500С Ø12 без покрытия', 'rebar'),
    ('Лист г/к 6х1500х6000 Ст3 без покрытия', 'Лист ГК 6x1500x6000 Ст3 без покрытия', 'sheet'),
])
def test_equivalent_notation_is_proved_without_commercial_aliases(left, right, category):
    request, offer = spec(left), spec(right, 'offer')
    result = compare(request, offer, policy(category))
    assert result.status == 'equivalent_notation'
    assert any(f.outcome == 'equivalent' for f in result.matching)
    assert all(f.provenance and f.explanation for f in result.matching)
    assert request.original_description == left and offer.original_description == right


@pytest.mark.parametrize('left,right,path', [
    ('Уголок 50х50х5 Ст3', 'Уголок 50х50х6 Ст3', 'geometry.thickness'),
    ('Швеллер 10У С345', 'Швеллер 10П С345', 'profile.series'),
    ('Швеллер 10У С345', 'Швеллер 10У С355', 'material.designation'),
    ('Швеллер 20У 09Г2С', 'Швеллер 20У С345-6', 'material.designation'),
    ('Уголок 50х50х5 ВСт3сп5', 'Уголок 50х50х5 Ст3', 'material.designation'),
    ('Лист г/к 10 мм Ст3', 'Лист х/к 10 мм Ст3', 'execution.rolling_method'),
    ('Труба профильная 80х40х3 бесшовная', 'Труба профильная 80х40х3 электросварная', 'execution.manufacturing_method'),
    ('Уголок 50х50х5 Ст3 без покрытия', 'Уголок 50х50х5 Ст3 оцинкованный', 'execution.coating'),
])
def test_known_technical_contradictions_are_explained_even_without_sufficiency(left, right, path):
    result = compare(spec(left), spec(right, 'offer'))
    assert result.status == result.technical_status == 'incompatible'
    finding = next(f for f in result.differing if f.path == path)
    assert finding.request_value != finding.offer_value and finding.explanation and finding.provenance
    assert not result.automatic_substitution_allowed


def test_unknown_coating_does_not_equal_explicit_uncoated():
    result = compare(spec('Уголок 50х50х5 Ст3'), spec(EXAMPLES['angle'], 'offer'), policy('angle'))
    assert result.status == 'insufficient_data'
    f = next(f for f in result.missing if f.path == 'execution.coating')
    assert f.request_state == 'missing' and f.offer_state == 'present'
    assert 'не подтверждено' in f.explanation


def test_quantity_does_not_participate_in_technical_identity():
    request = spec(EXAMPLES['angle'])
    offer = spec(EXAMPLES['angle'] + ' — 100 т', 'offer')
    result = compare(request, offer, policy('angle'))
    assert result.status == 'exact_match'
    assert request.quantity.state == 'missing' and result.offer.quantity.value.amount == Decimal('100')
    assert not any('quantity' in f.path for f in result.matching + result.differing + result.missing)


def documents(position, docs):
    data = position.model_dump()
    data['normative_documents'] = Attribute[list[NormativeDocument]](state='present', value=docs, provenance=[PROOF]).model_dump()
    return NomenclatureSpec.model_validate(data)


def document(number, edition, role=None):
    return NormativeDocument(raw_designation=f'ГОСТ {number}-{edition}', document_type='ГОСТ', number=number,
                             edition=known(edition), role=known(role) if role else Attribute[str](state='unknown'),
                             provenance=[PROOF])


def test_multiple_gost_roles_and_editions_are_checked_independently():
    request = documents(spec(EXAMPLES['angle']), [document('8509', '93', 'assortment'), document('535', '05', 'technical_requirements')])
    same = documents(spec(EXAMPLES['angle'], 'offer'), [document('535', '05', 'technical_requirements'), document('8509', '93', 'assortment')])
    rule = policy('angle', roles={'assortment': 'required', 'technical_requirements': 'required'})
    result = compare(request, same, rule)
    assert result.status == 'exact_match'
    assert {f.path for f in result.matching if f.path.startswith('normative_documents')} == {
        'normative_documents.assortment', 'normative_documents.technical_requirements'}
    different = documents(same, [document('8509', '2022', 'assortment'), document('535', '05', 'technical_requirements')])
    result = compare(request, different, rule)
    assert result.status == 'incompatible'
    assert any(f.path == 'normative_documents.assortment' and 'эквивалентность не установлена' in f.explanation for f in result.differing)


def test_unconfirmed_role_or_significance_never_confirms_normative_match():
    request = documents(spec(EXAMPLES['angle']), [document('8509', '93')])
    offer = documents(spec(EXAMPLES['angle'], 'offer'), [document('8509', '93')])
    result = compare(request, offer, policy('angle', roles={'assortment': 'required'}))
    assert result.status == 'insufficient_data'
    assert any('Роль' in f.explanation for f in result.missing)
    request = documents(request, [document('8509', '93', 'assortment')])
    offer = documents(offer, [document('8509', '93', 'assortment')])
    assert compare(request, offer, policy('angle')).status == 'insufficient_data'


def test_standard_structured_identifier_cannot_silently_replace_original():
    doc = document('8509', '93', 'assortment')
    bad = NormativeDocument.model_validate(doc.model_dump() | dict(number='27772'))
    request = documents(spec(EXAMPLES['angle']), [bad])
    result = compare(request, request, policy('angle', roles={'assortment': 'required'}))
    assert result.status == 'incompatible'
    assert any(f.outcome == 'conflict' and 'исходному' in f.explanation for f in result.differing)


def test_informational_document_difference_is_not_a_technical_conflict_when_explicitly_scoped():
    request = documents(spec(EXAMPLES['angle']), [document('1', '2020', 'informational')])
    offer = documents(spec(EXAMPLES['angle'], 'offer'), [document('2', '2021', 'informational')])
    result = compare(request, offer, policy('angle', roles={'informational': 'informational'}))
    assert result.status == 'exact_match'
    assert len(result.differing) == 1 and not result.differing[0].significant


def test_missing_same_attributes_never_prove_exact_match():
    result = compare(spec('Лист г/к 10 мм'), spec('Лист г/к 10 мм', 'offer'), policy('sheet'))
    assert result.status == 'insufficient_data'
    assert {'geometry.width', 'geometry.length', 'material.designation', 'execution.coating'}.issubset({f.path for f in result.missing})
    assert not any(f.path in {'geometry.width', 'geometry.length'} for f in result.matching)


@pytest.mark.parametrize('state', ['unknown', 'ambiguous', 'conflict'])
def test_unresolved_and_internal_conflicting_values_are_not_matches(state):
    request = spec(EXAMPLES['angle'])
    field = Attribute[Measurement](state=state, alternatives=[Measurement(amount='5', unit='mm'), Measurement(amount='6', unit='mm')], provenance=[PROOF]) if state != 'unknown' else Attribute[Measurement](state='unknown')
    offer = update_attribute(spec(EXAMPLES['angle'], 'offer'), 'geometry.thickness', field)
    result = compare(request, offer, policy('angle'))
    assert result.status == ('incompatible' if state == 'conflict' else 'insufficient_data')
    assert any(f.path == 'geometry.thickness' for f in result.differing + result.missing)


def test_profile_or_full_material_contradicts_structured_fields():
    request = spec('Уголок 50х50х5 ВСт3сп5 без покрытия')
    offer = update_attribute(request, 'geometry.thickness', known(Measurement(amount='6', unit='mm')))
    result = compare(offer, offer, policy('angle'))
    assert result.status == 'incompatible'
    assert any(f.outcome == 'conflict' and 'исходному обозначению' in f.explanation for f in result.differing)
    offer = update_attribute(request, 'material.supply_group', known('А'))
    assert compare(offer, offer, policy('angle')).status == 'incompatible'


def test_equivalent_units_and_decimal_comparison_have_no_tolerance_or_rounding():
    request = spec(EXAMPLES['angle'])
    offer = update_attribute(spec(EXAMPLES['angle'], 'offer'), 'geometry.thickness', known(Measurement(amount='0.5', unit='cm')))
    assert compare(request, offer, policy('angle')).status == 'equivalent_notation'
    precise = '5.0000000000000000000000000001'
    with localcontext() as ctx:
        ctx.prec = 4
        result = compare(request, spec(f'Уголок 50х50х{precise} Ст3 без покрытия', 'offer'), policy('angle'))
    assert result.status == 'incompatible'
    assert any(f.path == 'geometry.thickness' for f in result.differing)


def test_fake_normalized_text_or_commercial_group_is_not_identity_evidence():
    request = spec('Уголок 50х50х5 ВСт3сп5 без покрытия')
    offer = update_attribute(spec(EXAMPLES['angle'], 'offer'), 'material.designation',
                             Attribute[str](state='present', value='Ст3', normalized_text='ВСт3сп5', provenance=[PROOF]))
    assert compare(request, offer, policy('angle')).status == 'incompatible'


def test_shortened_source_material_is_not_self_consistent_even_on_both_sides():
    request = spec('Уголок 50х50х5 ВСт3сп5 без покрытия')
    bad = update_attribute(request, 'material.designation', Attribute[str](state='present', value='Ст3',
                          raw_value='ВСт3сп5', provenance=[PROOF]))
    result = compare(bad, bad, policy('angle'))
    assert result.status == 'incompatible'
    assert any('не сохраняет полное' in f.explanation for f in result.differing)


def test_possible_alternative_keeps_technical_difference_and_never_accepts_substitution():
    request = spec('Швеллер 20У 09Г2С без покрытия', request_id='request-a')
    offer = spec('Швеллер 20У С345-6 без покрытия', 'offer')
    basis = AlternativeBasis(request=request, offer=offer, review_paths=['material.designation'],
        scope=dict(kind='request', request_id='request-a'), reason='Отдельно рассмотреть коммерческий вариант для этой заявки',
        provenance=[PROOF], confirmation=RECEIPT)
    result = compare(request, offer, MatchingPolicy(alternatives=[basis]))
    assert result.status == 'possible_alternative' and result.technical_status == 'incompatible'
    assert result.alternative == basis and 'рассмотреть альтернативу' in result.explanation
    assert result.differing and not result.automatic_substitution_allowed
    assert compare(request, offer).status == 'incompatible'  # No learned global alias.
    changed = spec('Швеллер 20У 09Г2С без покрытия — 1 т', request_id='request-a')
    assert compare(changed, offer, MatchingPolicy(alternatives=[basis])).status == 'incompatible'
    another = spec('Швеллер 20У 09Г2С без покрытия', request_id='request-b')
    assert compare(another, offer, MatchingPolicy(alternatives=[basis])).status == 'incompatible'


def test_explicit_unconfirmed_analogy_can_be_proposed_but_rejection_is_respected():
    request = spec('Швеллер 10У С345-6 без покрытия')
    offer = spec('Швеллер 10У С355-6 без покрытия', 'offer')
    basis = AlternativeBasis(request=request, offer=offer,
        review_paths=['material.designation', 'material.strength_class'], scope=GENERAL,
        reason='Явное предложение поставщика для отдельной проверки', provenance=[PROOF])
    result = compare(request, offer, MatchingPolicy(alternatives=[basis]))
    assert basis.confirmation.status == 'pending'
    assert result.status == 'possible_alternative' and result.technical_status == 'incompatible'
    assert not result.automatic_substitution_allowed
    rejected = AlternativeBasis.model_validate(basis.model_dump() | dict(confirmation=dict(status='rejected')))
    assert compare(request, offer, MatchingPolicy(alternatives=[rejected])).status == 'incompatible'


def test_context_constraint_is_request_scoped_not_global():
    a = spec(EXAMPLES['angle'], request_id='a')
    b = spec(EXAMPLES['angle'], 'offer')
    rule = policy('angle')
    constraint = ContextConstraint(scope=dict(kind='request', request_id='a'), required_paths=['material.supply_group'],
                                   reason='Здесь группа поставки должна быть явно указана', provenance=[PROOF])
    scoped = rule.model_copy(update={'constraints': [constraint]})
    result = compare(a, b, scoped)
    assert result.status == 'insufficient_data'
    assert any('группа поставки' in f.explanation for f in result.missing)
    c = spec(EXAMPLES['angle'], request_id='b')
    assert compare(c, b, scoped).status == 'exact_match'


def test_unsupported_scope_conditions_and_multiple_sufficiency_rules_do_not_choose_first():
    request, offer = spec(EXAMPLES['angle']), spec(EXAMPLES['angle'], 'offer')
    rule = policy('angle').sufficiency_rules[0]
    data = rule.model_dump()
    data['scope'] = dict(kind='general', conditions={'supplier': 'unimplemented condition'})
    assert compare(request, offer, MatchingPolicy(sufficiency_rules=[CategorySufficiencyRule(**data)])).status == 'insufficient_data'
    assert compare(request, offer, MatchingPolicy(sufficiency_rules=[rule, rule])).status == 'insufficient_data'


def test_unapproved_or_trivial_sufficiency_is_rejected():
    data = policy('angle').sufficiency_rules[0].model_dump()
    for patch in [dict(required_paths=['category']), dict(confirmation=dict(status='pending'))]:
        with pytest.raises(ValidationError):
            CategorySufficiencyRule(**(data | patch))


def test_batch_retains_input_order_and_duplicate_records_without_automatic_winner():
    request = spec(EXAMPLES['angle'])
    offers = [spec(EXAMPLES['angle'], 'same-id'), spec('Уголок 50х50х6 Ст3 без покрытия', 'other'), spec(EXAMPLES['angle'], 'same-id')]
    batch = TechnicalMatchingService().compare_many(request, offers, policy('angle'))
    assert len(batch.results) == 3
    assert [r.offer.record_id for r in batch.results] == ['same-id', 'other', 'same-id']
    assert [r.status for r in batch.results] == ['exact_match', 'incompatible', 'exact_match']
    assert not batch.automatic_substitution_allowed


def entry(table, identity, **fields):
    return ReferenceEntry(table=table, row_id=identity, fields=fields,
        provenance=Provenance(source_id='synthetic-catalog', record_id=f'{table}:{identity}', locator=f'{table}/{identity}', method='reference'))


def candidate(identity=1, grade='С345-6'):
    item = entry('reference_item', identity, designation='10У', normalized_key='10у')
    return ReferenceCandidate(item=item, category=entry('reference_category', 1, name='Швеллер', code='channel'),
        family=entry('reference_family', 1, name='Швеллер', code='channel'),
        variant=entry('reference_variant', identity, grade_class=grade, execution='без покрытия', form=None),
        evidence=[ReferenceEvidence(kind='alias', query='ambiguous alias', explanation='Synthetic ambiguous alias',
                                   alias_role='alternative_spelling', provenance=item.provenance)])


def test_structured_reference_candidate_is_compared_without_using_alias_as_identity():
    request = spec(EXAMPLES['channel'])
    candidates = ReferenceCandidates(record_id=request.record_id, status='candidates', candidates=[candidate(), candidate(2, 'С355-6')])
    before = candidates.model_dump_json()
    batch = TechnicalMatchingService().compare_reference(request, candidates, policy('channel'))
    assert [r.status for r in batch.results] == ['exact_match', 'incompatible']
    assert [r.reference_candidate.item.row_id for r in batch.results] == [1, 2]
    assert candidates.model_dump_json() == before
    assert not batch.automatic_substitution_allowed
    assert any(p.method == 'reference' for p in batch.results[0].provenance)


def test_partial_reference_and_ambiguous_alias_never_fill_missing_traits():
    request = spec(EXAMPLES['channel'])
    raw = candidate().model_dump()
    raw['variant'] = None
    incomplete = ReferenceCandidate.model_validate(raw)
    candidates = ReferenceCandidates(record_id=request.record_id, status='candidates', candidates=[incomplete, incomplete], truncated=True)
    batch = TechnicalMatchingService().compare_reference(request, candidates, policy('channel'))
    assert len(batch.results) == 2 and all(r.status == 'insufficient_data' for r in batch.results)
    assert all(r.offer.material.designation.state == 'missing' for r in batch.results)
    assert batch.truncated
    with pytest.raises(ValueError):
        TechnicalMatchingService().compare_reference(request, candidates.model_copy(update={'record_id': 'wrong'}))


def test_reference_real_only_dimension_is_unknown_not_exact_decimal_evidence():
    raw = candidate().model_dump()
    raw['dimensions'] = [entry('reference_dimension', 1, parameter_type='length', numeric_value='6000.0', unit='мм', raw_value=None).model_dump()]
    adapted = ReferenceTechnicalAdapter().to_spec(raw)
    assert adapted.geometry['length'].state == 'unknown'
    assert 'REAL' in adapted.geometry['length'].reason


def test_reference_standard_unknown_role_does_not_confirm_identity():
    raw = candidate().model_dump()
    raw['standard'] = entry('reference_standard', 1, designation='ГОСТ 8240-97', document_type='ГОСТ', year=1997).model_dump()
    request = spec(EXAMPLES['channel'])
    adapted = ReferenceTechnicalAdapter().to_spec(raw)
    assert adapted.normative_documents.value[0].role.state == 'unknown'
    assert compare(request, adapted, policy('channel')).status == 'insufficient_data'


def test_reference_category_explicit_rolling_method_is_preserved():
    raw = candidate().model_dump()
    raw['family'] = entry('reference_family', 2, name='Лист', code='sheet').model_dump()
    raw['category'] = entry('reference_category', 2, name='Лист х/к', code='sheet_cold_rolled').model_dump()
    raw['item'] = entry('reference_item', 2, designation='6х1500х6000', normalized_key='6x1500x6000').model_dump()
    raw['variant'] = entry('reference_variant', 2, grade_class='Ст3', execution='без покрытия').model_dump()
    adapted = ReferenceTechnicalAdapter().to_spec(raw)
    assert adapted.execution.rolling_method.value == 'cold_rolled'
    assert any(p.locator.endswith('#name') for p in adapted.execution.rolling_method.provenance)
    assert compare(spec(EXAMPLES['sheet']), adapted, policy('sheet')).status == 'incompatible'


def test_reference_equal_leg_category_is_not_lost_in_projection():
    raw = candidate().model_dump()
    raw['family'] = entry('reference_family', 2, name='Уголок', code='angle').model_dump()
    raw['category'] = entry('reference_category', 2, name='Уголок равнополочный', code='angle_equal').model_dump()
    raw['item'] = entry('reference_item', 2, designation='50*5', normalized_key='50*5').model_dump()
    raw['variant'] = entry('reference_variant', 2, grade_class='Ст3', execution='без покрытия').model_dump()
    adapted = ReferenceTechnicalAdapter().to_spec(raw)
    assert adapted.geometry['leg_b'].value.amount == Decimal('50')
    assert any(p.rule_id == 'profile-dimension-unit-convention' for p in adapted.geometry['leg_b'].provenance)
    assert compare(spec(EXAMPLES['angle']), adapted, policy('angle')).status == 'equivalent_notation'


def test_unmapped_reference_properties_and_dimensions_are_not_silently_ignored():
    raw = candidate().model_dump()
    raw['properties'] = [entry('reference_property', 1, property_type='unknown_requirement', numeric_value='1.2', unit='мм').model_dump()]
    raw['dimensions'] = [entry('reference_dimension', 1, parameter_type='diagram_dimension', numeric_value='200', unit='мм').model_dump()]
    adapted = ReferenceTechnicalAdapter().to_spec(raw)
    assert len(adapted.unparsed_features) == 2
    result = compare(spec(EXAMPLES['channel']), adapted, policy('channel'))
    assert result.status == 'insufficient_data'
    assert any(f.path == 'offer.unparsed_features' for f in result.missing)


def test_reference_empty_fields_and_absent_locators_do_not_drop_candidate():
    raw = candidate().model_dump()
    for key in ('item', 'category', 'family'):
        raw[key]['fields'] = {}
        raw[key]['provenance']['locator'] = None
        raw[key]['provenance']['record_id'] = None
    raw['variant'] = None
    request = spec(EXAMPLES['channel'])
    batch = TechnicalMatchingService().compare_reference(request,
        ReferenceCandidates(record_id=request.record_id, status='candidates', candidates=[ReferenceCandidate.model_validate(raw)]), policy('channel'))
    assert len(batch.results) == 1 and batch.results[0].status == 'insufficient_data'


def test_opaque_technical_extension_is_not_normalized_as_supplier_alias():
    request = spec(EXAMPLES['angle'])
    offer = spec(EXAMPLES['angle'], 'offer')
    a, b = request.model_dump(), offer.model_dump()
    a['extensions'] = {'execution_code': known('ABC').model_dump()}
    b['extensions'] = {'execution_code': known('abc').model_dump()}
    result = compare(NomenclatureSpec.model_validate(a), NomenclatureSpec.model_validate(b), policy('angle'))
    assert result.status == 'incompatible'
    assert any(f.path == 'extensions.execution_code' for f in result.differing)


def test_comparison_does_not_mutate_inputs_or_share_nested_snapshots():
    request, offer = spec(EXAMPLES['angle']), spec(EXAMPLES['angle'], 'offer')
    before = request.model_dump_json(), offer.model_dump_json()
    result = compare(request, offer, policy('angle'))
    result.request.geometry.clear()
    assert (request.model_dump_json(), offer.model_dump_json()) == before


def test_matching_is_local_without_ai_database_or_telegram_imports():
    script = '''
import builtins
old = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.split('.')[0] in {'ai', 'sqlite3', 'database', 'openai', 'telegram', 'telegram_service', 'dotenv'}:
        raise AssertionError(name)
    return old(name, *args, **kwargs)
builtins.__import__ = guarded
from technical_matching import compare
from recognition import recognize_record
def spec(t, rid):
    return recognize_record(dict(record_id=rid, source=dict(source_id='test', source_type='text'), original_description=t)).spec
assert compare(spec('Швеллер 10У', 'a'), spec('Швеллер 10П', 'b')).status == 'incompatible'
print('local matching passed')
'''
    result = subprocess.run([sys.executable, '-B', '-X', 'utf8', '-c', script], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'local matching passed'
