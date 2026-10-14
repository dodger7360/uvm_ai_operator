"""Curated expectations test the contract, not an unimplemented recognizer."""
import json
from decimal import Decimal, localcontext
from pathlib import Path

import pytest
from pydantic import ValidationError

from nomenclature import (Attribute, Measurement, NomenclatureSpec,
                          NormativeDocument, Provenance)
from schemas import RequestItem


EXAMPLES = json.loads((Path(__file__).parent / 'fixtures' /
                       'nomenclature_contract_v1.json').read_text(encoding='utf-8'))
ORIGINALS = [
    'Уголок 50х50х5 Ст3сп5 ГОСТ 8509-93',
    'Уг. равнопол. 50*5 ст3',
    'Швеллер 20У 09Г2С',
    'Труба профильная 80х40х3',
    'Лист г/к 10 мм С345',
    'Арматура А500С ф12',
]
EVIDENCE = Provenance(source_id='test-input', record_id='test-record',
                      method='source', fragment='explicit test input')


def known(value, raw=None, **extra):
    return Attribute[type(value)](state='present', value=value,
                                  raw_value=raw, provenance=[EVIDENCE], **extra)


@pytest.mark.parametrize('example,original', list(zip(EXAMPLES, ORIGINALS)),
                         ids=[entry['case'] for entry in EXAMPLES])
def test_curated_examples_roundtrip_preserve_source_and_exact_geometry(example, original):
    spec = NomenclatureSpec.model_validate(example['spec'])
    assert spec.original_description == original
    assert spec.record_id == example['case']
    assert spec.source.source_type == 'contract_fixture'
    assert spec.category.is_resolved and spec.product_type.is_resolved
    assert spec.quantity.state == 'missing'  # None of these inputs states a quantity.
    restored = NomenclatureSpec.model_validate_json(spec.model_dump_json())
    assert restored == spec
    wire = json.loads(spec.model_dump_json())
    for name, attr in spec.geometry.items():
        assert isinstance(attr.value.amount, Decimal)
        assert isinstance(wire['geometry'][name]['value']['amount'], str)
        assert attr.provenance[0].record_id == spec.record_id
        assert attr.provenance[0].method == 'fixture'
        assert attr.provenance[0].fragment in original
    # A bare dimension has no asserted unit until an approved rule supplies one.
    if example['case'] != 'sheet':
        assert all(attr.value.unit is None for attr in spec.geometry.values())


@pytest.mark.parametrize('case,path,expected', [
    ('angle_full', 'material.designation', 'Ст3сп5'),
    ('angle_full', 'material.steel_grade', 'Ст3'),
    ('angle_full', 'material.deoxidation', 'сп'),
    ('angle_full', 'material.category', '5'),
    ('angle_abbreviation', 'profile.cross_section', 'equal_leg'),
    ('channel', 'profile.series', 'У'),
    ('channel', 'material.steel_grade', '09Г2С'),
    ('sheet', 'material.strength_class', 'С345'),
    ('sheet', 'execution.rolling_method', 'hot_rolled'),
    ('rebar', 'material.rebar_class', 'А500С'),
])
def test_explicit_characteristics_are_distinct_fields(case, path, expected):
    spec = NomenclatureSpec.model_validate(next(x['spec'] for x in EXAMPLES if x['case'] == case))
    value = spec
    for name in path.split('.'):
        value = getattr(value, name)
    assert value.value == expected and value.provenance


def test_unknown_role_suffix_meaning_and_absent_fields_are_not_invented():
    specs = {x['case']: NomenclatureSpec.model_validate(x['spec']) for x in EXAMPLES}
    angle = specs['angle_full']
    assert angle.material.category_kind.state == 'unknown'
    document = angle.normative_documents.value[0]
    assert document.number == '8509' and document.edition.value == '93'
    assert document.role.state == 'unknown'  # No automatic reference lookup.
    assert document.raw_designation == 'ГОСТ 8509-93'
    assert specs['channel'].geometry == {}  # 20У does not assert a height in mm.
    assert specs['profile_pipe'].material.steel_grade.state == 'missing'
    assert specs['profile_pipe'].execution.manufacturing_method.state == 'missing'
    assert specs['sheet'].material.steel_grade.state == 'missing'
    assert set(specs['sheet'].geometry) == {'thickness'}
    assert specs['sheet'].geometry['thickness'].value == Measurement(amount='10', unit='mm')
    assert specs['rebar'].material.steel_grade.state == 'missing'


@pytest.mark.parametrize('left,right', [
    ('10У', '10П'), ('С345', 'С355'), ('hot_rolled', 'cold_rolled'),
    ('09Г2С', 'С345'),
])
def test_different_technical_values_are_not_the_same_known_value(left, right):
    assert not known(left).has_same_known_value(known(right))


def test_missing_coating_is_distinct_from_explicitly_uncoated():
    absent = Attribute[str]()
    explicit = known('none', 'без покрытия')
    assert explicit.is_resolved and not absent.is_resolved
    assert not absent.has_same_known_value(explicit)
    assert Attribute[str].model_validate_json(explicit.model_dump_json()) == explicit


@pytest.mark.parametrize('state', ['missing', 'unknown', 'ambiguous', 'conflict'])
def test_unresolved_attributes_do_not_confirm_matches_even_with_same_structure(state):
    extra = {}
    if state in {'ambiguous', 'conflict'}:
        extra = dict(alternatives=['С345', 'С355'], provenance=[EVIDENCE])
    a = Attribute[str](state=state, **extra)
    b = Attribute[str].model_validate_json(a.model_dump_json())
    assert a == b  # Serialization equality is explicitly not technical matching.
    assert not a.is_resolved and not a.has_same_known_value(b)


def test_normalized_spelling_does_not_replace_technical_value():
    a = known('В Ст.3 сп.5', 'В Ст.3 сп.5', normalized_text='ВСт3сп5')
    b = known('Ст3', normalized_text='ВСт3сп5')
    assert a.raw_value == 'В Ст.3 сп.5'
    assert not a.has_same_known_value(b)  # No new alias/identity engine.
    assert known('С345').has_same_known_value(known('С345'))


@pytest.mark.parametrize('unit', [None, 'm'])
def test_unknown_or_different_units_prevent_confirmed_measurement_equality(unit):
    a = known(Measurement(amount='10', unit=unit))
    b = known(Measurement(amount='10', unit='mm'))
    assert not a.has_same_known_value(b)
    if unit is None:
        assert not a.has_same_known_value(a)


@pytest.mark.parametrize('invalid', [
    dict(state='present', value='Ст3'),
    dict(state='present', value=None, provenance=[EVIDENCE]),
    dict(state='present', value=' ', provenance=[EVIDENCE]),
    dict(state='unknown', value='Ст3'),
    dict(state='unknown', normalized_text='Ст3'),
    dict(state='ambiguous', alternatives=['Ст3'], provenance=[EVIDENCE]),
    dict(state='conflict', alternatives=['Ст3', 'С255']),
    dict(state='missing', raw_value='неизвестно'),
    dict(state='present', value='Ст3', alternatives=['С255'], provenance=[EVIDENCE]),
])
def test_invalid_attribute_claims_are_rejected(invalid):
    with pytest.raises(ValidationError):
        Attribute[str](**invalid)


@pytest.mark.parametrize('value', [0.1, True, 'NaN', 'Infinity', '-Infinity'])
def test_measurement_rejects_floats_booleans_and_nonfinite_values(value):
    with pytest.raises(ValidationError):
        Measurement(amount=value, unit='mm')


def test_decimal_roundtrip_retains_full_precision_without_arithmetic_or_rounding():
    amount = '12345678901234567890.123456789012345678900'
    with localcontext() as ctx:
        ctx.prec = 6
        measure = Measurement(amount=amount, unit='t')
        restored = Measurement.model_validate_json(measure.model_dump_json())
        assert restored.amount.as_tuple() == Decimal(amount).as_tuple()
        assert json.loads(measure.model_dump_json())['amount'] == amount
    assert Measurement(amount=0, unit='шт').amount == Decimal(0)
    assert Measurement(amount=5).amount == Decimal(5)


def test_quantity_is_separate_and_extensions_and_unparsed_evidence_survive_roundtrip():
    data = EXAMPLES[0]['spec'].copy()
    data.update(quantity=known(Measurement(amount='4', unit='шт')).model_dump(),
                extensions={'tolerance': known('Б', 'Б').model_dump()},
                unparsed_features=['спец. требование заказчика'])
    spec = NomenclatureSpec.model_validate(data)
    restored = NomenclatureSpec.model_validate_json(spec.model_dump_json())
    assert restored == spec
    assert restored.quantity.value.amount == Decimal('4')
    assert restored.material.designation.value == 'Ст3сп5'
    assert restored.extensions['tolerance'].provenance == [EVIDENCE]
    assert restored.unparsed_features == ['спец. требование заказчика']
    assert 'identity_key' not in NomenclatureSpec.model_fields


def test_rule_evidence_requires_id_and_version():
    with pytest.raises(ValidationError):
        Provenance(source_id='rules', method='rule', rule_id='spelling')
    p = Provenance(source_id='rules', method='rule', rule_id='spelling', rule_version='v1')
    assert Provenance.model_validate_json(p.model_dump_json()) == p


def test_original_description_is_exact_and_schema_is_versioned():
    data = dict(record_id='test', source={'source_id': 'text', 'source_type': 'text'},
                original_description='  В Ст.3 сп.5\nГОСТ8509-93  ')
    spec = NomenclatureSpec(**data)
    assert spec.original_description == data['original_description']
    for update in [dict(original_description='   '), dict(schema_version='nomenclature.v2'),
                   dict(unapproved_field='Ст3')]:
        with pytest.raises(ValidationError):
            NomenclatureSpec(**(data | update))


def test_multiple_standards_with_separate_editions_and_provenance_survive():
    docs = [NormativeDocument(raw_designation=raw, document_type='ГОСТ', number=number,
                              edition=known(edition, raw), provenance=[EVIDENCE])
            for raw, number, edition in [('ГОСТ8509-93', '8509', '93'),
                                          ('ГОСТ 535-05', '535', '05')]]
    claim = Attribute[list[NormativeDocument]](state='present', value=docs, provenance=[EVIDENCE])
    restored = Attribute[list[NormativeDocument]].model_validate_json(claim.model_dump_json())
    assert [doc.number for doc in restored.value] == ['8509', '535']
    assert [doc.edition.value for doc in restored.value] == ['93', '05']
    assert restored == claim


def test_existing_request_item_contract_remains_independent():
    item = RequestItem(product='Уголок', grade='ВСт3сп5', quantity='0,193', unit='т',
                       gost_tu='ГОСТ 8509-93, ГОСТ 535-05', source_text='unchanged test source')
    before = item.model_dump_json()
    NomenclatureSpec.model_validate(EXAMPLES[0]['spec'])
    assert item.model_dump_json() == before
    assert RequestItem.model_validate_json(before) == item
    assert item.quantity == Decimal('0.193')
    assert 'nomenclature' not in RequestItem.model_fields
