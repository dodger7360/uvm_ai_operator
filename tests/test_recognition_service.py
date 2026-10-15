"""Local extraction tests, independent of GPT and production request storage."""
import json
from decimal import Decimal, localcontext
from pathlib import Path
import subprocess
import sys
from time import perf_counter

import pytest
from pydantic import ValidationError

from nomenclature import Attribute, Measurement, NomenclatureSpec, NormalizationContextRef, Provenance
from normalization_context import WorkingContext
from recognition import (RecognitionBatch, RecognitionPolicy, RecognitionRecord,
                         RecognitionService, recognize_record, recognize_records)


EXAMPLES = json.loads((Path(__file__).parent / 'fixtures' /
                       'nomenclature_contract_v1.json').read_text(encoding='utf-8'))


def record(text, record_id='sample', source_type='text'):
    return RecognitionRecord(record_id=record_id, original_description=text,
                             source=dict(source_id='recognition-tests', source_type=source_type,
                                         locator=f'row/{record_id}', source_version='v1'))


def extract(text, **kwargs):
    return recognize_record(record(text), **kwargs)


def assert_literal_provenance(spec):
    def walk(value):
        if isinstance(value, dict):
            if 'method' in value and 'fragment' in value:
                start, end = map(int, value['locator'].split('#chars=')[1].split(':'))
                assert value['fragment'] == spec.original_description[start:end]
                assert value['record_id'] == spec.record_id
                assert value['source_id'] == spec.source.source_id
                if value['method'] == 'rule':
                    assert value['rule_id'] and value['rule_version'] == 'local-recognition.v1'
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)
    walk(spec.model_dump())


@pytest.mark.parametrize('example', EXAMPLES, ids=[x['case'] for x in EXAMPLES])
def test_six_stage_101_fixtures_are_actually_recognized_locally(example):
    expected = NomenclatureSpec.model_validate(example['spec'])
    source = RecognitionRecord(**{k: example['spec'][k] for k in ('record_id', 'source', 'original_description')})
    result = recognize_record(source)
    spec = result.spec
    assert result.status == 'recognized' and not result.issues
    assert result.record == source and result.record_id == spec.record_id
    assert spec.original_description == expected.original_description
    assert spec.category.value == expected.category.value
    assert spec.product_type.value == expected.product_type.value
    assert spec.material.designation.value == expected.material.designation.value
    assert spec.quantity.state == 'missing'
    for name, attr in expected.geometry.items():
        assert spec.geometry[name].value.amount == attr.value.amount
        assert spec.geometry[name].value.unit == 'mm'
    for name in ('steel_grade', 'strength_class', 'rebar_class', 'deoxidation', 'category'):
        wanted = getattr(expected.material, name)
        if wanted.is_resolved:
            assert getattr(spec.material, name).value == wanted.value
    if example['case'] == 'angle_abbreviation':
        assert spec.geometry['leg_b'].value == Measurement(amount='50', unit='mm')
        assert any(p.fragment == 'равнопол.' for p in spec.geometry['leg_b'].provenance)
    if example['case'] == 'sheet':
        assert spec.execution.rolling_method.value == 'hot_rolled'
        assert set(spec.geometry) == {'thickness'}  # Width/length not requested.
    if example['case'] == 'rebar':
        assert spec.geometry['diameter'].raw_value == 'ф12'
    if expected.normative_documents.is_resolved:
        assert spec.normative_documents.value[0].number == '8509'
        assert spec.normative_documents.value[0].edition.value == '93'
    assert spec.execution.manufacturing_method.state == 'missing'
    assert spec.execution.coating.state == 'missing'
    assert NomenclatureSpec.model_validate_json(spec.model_dump_json()) == spec
    assert_literal_provenance(spec)


@pytest.mark.parametrize('delimiter', ['х', 'Х', 'x', 'X', '*', '×', ' x ', ' × '])
def test_dimension_delimiters_do_not_change_exact_geometry(delimiter):
    result = extract('Уголок ' + delimiter.join(['50.25', '50,25', '5.125']) + ' Ст3')
    assert result.status == 'recognized'
    assert [result.spec.geometry[key].value.amount for key in ('leg_a', 'leg_b', 'thickness')] == [
        Decimal('50.25'), Decimal('50.25'), Decimal('5.125')]
    assert result.spec.profile.cross_section.value == 'equal_leg'
    assert_literal_provenance(result.spec)


@pytest.mark.parametrize('text,group', [
    ('Уг. равнопол. 50*5 ст3', 'angle'),
    ('Шв. 10У С345', 'channel'), ('Швел. 10П С355', 'channel'),
    ('Арм. А500С Ø12', 'rebar'),
    ('Труба проф. 80x40x3', 'profile_pipe'), ('Проф. труба 80x40x3', 'profile_pipe'),
    ('Лист х/к 1,2 мм Ст3', 'sheet'),
])
def test_product_abbreviations(text, group):
    result = extract(text)
    assert result.status == 'recognized'
    assert result.spec.category.value == group


@pytest.mark.parametrize('diameter', ['ф12', 'Ф12', 'Ø12', 'ø12', '⌀12', 'd12', 'диаметр 12 мм', '12 мм'])
def test_rebar_diameter_notation_has_literal_evidence(diameter):
    text = f'Арматура А500С {diameter}' if diameter != '12 мм' else 'Арматура 12 мм А500С'
    result = extract(text)
    assert result.status == 'recognized'
    assert result.spec.geometry['diameter'].value == Measurement(amount='12', unit='mm')
    assert_literal_provenance(result.spec)


@pytest.mark.parametrize('raw,normalized', [
    ('В Ст.3 сп.5', 'ВСт3сп5'), ('Ст3пс5', 'Ст3пс5'), ('Ст3сп5', 'Ст3сп5'),
    ('Ст.3 сп.5', 'Ст3сп5'), ('B Ст.3 сп.5', 'ВСт3сп5'),
    ('09Г2С', '09Г2С'), ('09G2C', '09Г2С'), ('С255-4', 'С255-4'),
    ('С345-6', 'С345-6'), ('C345-6', 'С345-6'), ('С355', 'С355'),
])
def test_full_material_designation_survives_normalized_spelling(raw, normalized):
    result = extract(f'Уголок 50х50х5 {raw}')
    designation = result.spec.material.designation
    assert result.status == 'recognized'
    assert designation.value == designation.raw_value == raw
    assert designation.normalized_text == normalized
    assert_literal_provenance(result.spec)
    if normalized == 'ВСт3сп5':
        assert result.spec.material.supply_group.value == 'В'
        assert result.spec.material.steel_grade.value == 'Ст3'
        assert result.spec.material.deoxidation.value == 'сп'
        assert result.spec.material.category.value == '5'
        assert result.spec.material.category_kind.state == 'unknown'
    if normalized == 'С345-6':
        assert result.spec.material.steel_grade.state == 'missing'
        assert result.spec.material.strength_class.value == 'С345'
        assert result.spec.material.category.value == '6'


def test_latin_rebar_class_is_spelling_not_material_substitution():
    result = extract('Арматура A500C ф12')
    assert result.status == 'recognized'
    assert result.spec.material.designation.value == 'A500C'
    assert result.spec.material.designation.normalized_text == 'А500С'
    assert result.spec.material.rebar_class.value == 'А500С'
    assert result.spec.material.steel_grade.state == 'missing'


@pytest.mark.parametrize('raw,series', [('10У', 'У'), ('10П', 'П'), ('10U', 'У'), ('10P', 'П')])
def test_channel_series_are_preserved_and_height_is_not_inferred(raw, series):
    result = extract(f'Швеллер {raw} 09Г2С')
    assert result.status == 'recognized'
    assert result.spec.profile.designation.value == raw
    assert result.spec.profile.series.value == series
    assert result.spec.geometry == {}
    other = extract('Швеллер 10П 09Г2С' if series == 'У' else 'Швеллер 10У 09Г2С')
    assert not result.spec.profile.series.has_same_known_value(other.spec.profile.series)


@pytest.mark.parametrize('text', ['Уголок 50*5', 'Уг. 50*5', 'Уголок неравнопол. 50*5'])
def test_two_angle_dimensions_without_equal_leg_grammar_are_unresolved(text):
    result = extract(text)
    assert result.status == 'partial'
    assert 'unresolved_angle_dimensions' in result.issues
    assert all(attr.state == 'unknown' for attr in result.spec.geometry.values())
    assert all(not attr.has_same_known_value(attr) for attr in result.spec.geometry.values())


def test_gost_equal_angle_grammar_is_reused_with_explicit_provenance():
    result = extract('Уголок 50*5 Ст3 ГОСТ8509-93')
    assert result.status == 'recognized'
    assert result.spec.geometry['leg_b'].value.amount == Decimal('50')
    assert any(p.fragment == 'ГОСТ8509-93' for p in result.spec.geometry['leg_b'].provenance)


@pytest.mark.parametrize('text,field,issue', [
    ('Уголок 50х50х5 Ст3 или 09Г2С', 'material.designation', 'ambiguous_material'),
    ('Швеллер 10У или 10П', 'profile.series', 'ambiguous_profile'),
    ('Лист г/к или х/к 10 мм', 'execution.rolling_method', 'ambiguous_rolling_method'),
    ('Труба профильная 80x40x3 бесшовная или электросварная', 'execution.manufacturing_method', 'ambiguous_manufacturing_method'),
    ('Уголок неравнопол. 50х50х5', 'profile.cross_section', 'ambiguous_cross_section'),
])
def test_alternatives_are_preserved_without_selecting_one(text, field, issue):
    result = extract(text)
    assert result.status == 'partial' and issue in result.issues
    attr = result.spec
    for name in field.split('.'):
        attr = getattr(attr, name)
    assert attr.state == 'ambiguous' and attr.value is None
    assert len(attr.alternatives) >= 2 and not attr.has_same_known_value(attr)
    assert_literal_provenance(result.spec)


def test_absent_and_unknown_material_are_distinct():
    absent = extract('Уголок 50х50х5').spec.material.designation
    result = extract('Уголок 50х50х5 сталь Q235')
    assert absent.state == 'missing'
    assert result.spec.material.designation.state == 'unknown'
    assert result.spec.material.designation.raw_value == 'Q235'
    assert 'Q235' in ' '.join(result.spec.unparsed_features)
    assert not absent.has_same_known_value(result.spec.material.designation)


@pytest.mark.parametrize('separator', [' ', '/', '\n', ' " '])
def test_multiple_documents_use_existing_source_grammar(separator):
    text = f'Уголок 50х50х5 ГОСТ8509-93{separator}В Ст.3 сп.5{separator}ГОСТ535-05 — 0,193 т'
    result = extract(text)
    assert result.status == 'recognized'
    docs = result.spec.normative_documents.value
    assert [(d.number, d.edition.value) for d in docs] == [('8509', '93'), ('535', '05')]
    assert [d.raw_designation for d in docs] == ['ГОСТ8509-93', 'ГОСТ535-05']
    assert all(d.role.state == 'unknown' for d in docs)
    assert result.spec.material.designation.value == 'В Ст.3 сп.5'
    assert result.spec.quantity.value == Measurement(amount='0.193', unit='т')
    assert result.spec.original_description == text
    assert_literal_provenance(result.spec)


def test_document_type_number_and_literal_edition_are_not_substituted():
    result = extract('Лист 10 мм GOST19903-2015 ТУ 14-1-1234-75 СТО АСЧМ 7-93')
    docs = result.spec.normative_documents.value
    assert result.status == 'recognized'
    assert [(d.document_type, d.number, d.edition.value) for d in docs] == [
        ('ГОСТ', '19903', '2015'), ('ТУ', '14-1-1234-75', None), ('СТО АСЧМ', '7-93', None)]
    assert docs[0].raw_designation == 'GOST19903-2015'
    assert_literal_provenance(result.spec)


@pytest.mark.parametrize('text,expected', [
    ('Лист 10 мм ГОСТ8509-930', ('8509-930', None)),
    ('Лист 10 мм ГОСТ8509-93ABC', None),
    ('Лист 10 мм ГОСТ8509-93-123', ('8509-93-123', None)),
])
def test_standards_are_not_truncated_to_a_nearby_known_number(text, expected):
    result = extract(text)
    documents = result.spec.normative_documents
    if expected is None:
        assert documents.state == 'missing'
        assert 'ГОСТ8509-93ABC' in ' '.join(result.spec.unparsed_features)
    else:
        assert (documents.value[0].number, documents.value[0].edition.value) == expected


@pytest.mark.parametrize('amount,unit,canonical', [
    ('4', 'шт.', 'шт'), ('4', 'штуки', 'шт'), ('500.125', 'кг', 'кг'),
    ('0,193', 'т', 'т'), ('5', 'тонн', 'т'), ('12', 'м', 'м'),
    ('12', 'пог. м', 'м'), ('12', 'м.п.', 'м'), ('2,12345', 'м²', 'м²'),
])
def test_quantity_is_exact_separate_and_kg_is_not_rewritten_as_tonnes(amount, unit, canonical):
    result = extract(f'Уголок 50х50х5 Ст3 — {amount} {unit}')
    assert result.status == 'recognized'
    assert result.spec.quantity.value == Measurement(amount=amount.replace(',', '.'), unit=canonical)
    assert result.spec.quantity.raw_value == f'{amount} {unit}'
    assert result.spec.geometry['thickness'].value.amount == Decimal('5')


def test_spaced_hyphen_is_quantity_separator_and_mass_annotation_is_not_second_quantity():
    assert extract('Уголок 50х50х5 - 4 шт').spec.quantity.value.amount == Decimal('4')
    result = extract('Лист 6×1500×6000 Ст3 — 4 шт. = 1,696 т')
    assert result.status == 'recognized'
    assert result.spec.quantity.value == Measurement(amount='4', unit='шт')


@pytest.mark.parametrize('quantity', ['0 шт', '-3 шт', '+3 шт', '−3 шт'])
def test_invalid_quantity_is_not_silently_positive(quantity):
    result = extract(f'Уголок 50х50х5 — {quantity}')
    assert result.status == 'partial'
    assert 'invalid_quantity' in result.issues
    assert not result.spec.quantity.is_resolved


def test_multiple_quantities_and_geometries_remain_ambiguous():
    result = extract('Труба профильная 80х40х3 или 100х60х4 — 5 шт или 10 шт')
    assert result.status == 'partial'
    assert result.spec.geometry['wall_thickness'].state == 'ambiguous'
    assert {m.amount for m in result.spec.geometry['wall_thickness'].alternatives} == {Decimal('3'), Decimal('4')}
    assert result.spec.quantity.state == 'ambiguous'
    assert {m.amount for m in result.spec.quantity.alternatives} == {Decimal('5'), Decimal('10')}


def test_absent_coating_does_not_mean_uncoated_and_manufacturing_is_explicit():
    absent = extract('Труба профильная 80х40х3').spec
    present = extract('Труба профильная 80х40х3 электросварная без покрытия').spec
    assert absent.execution.coating.state == 'missing'
    assert absent.execution.manufacturing_method.state == 'missing'
    assert present.execution.manufacturing_method.value == 'electric_welded'
    assert present.execution.coating.value == 'none'
    assert not absent.execution.coating.has_same_known_value(present.execution.coating)


def test_implicit_mm_is_versioned_policy_and_can_be_disabled():
    result = extract('Уголок 50х50х5', policy=RecognitionPolicy(implicit_dimension_unit=None))
    assert all(m.value.unit is None for m in result.spec.geometry.values())
    assert not result.spec.geometry['thickness'].has_same_known_value(result.spec.geometry['thickness'])
    explicit = extract('Уголок 50х50х5 мм', policy=RecognitionPolicy(implicit_dimension_unit=None)).spec
    assert explicit.geometry['thickness'].value.unit == 'mm'


def test_decimal_precision_and_explicit_unit_conversion_ignore_small_context():
    token = '50.123456789012345678901234567890'
    amount = '12345678901234567890.1234567890123456789'
    with localcontext() as ctx:
        ctx.prec = 6
        result = extract(f'Труба профильная {token}x40x3 см — {amount} кг')
        sign, digits, exponent = Decimal(token).as_tuple()
        assert result.spec.geometry['width'].value.amount.as_tuple() == Decimal((sign, digits, exponent + 1)).as_tuple()
        assert result.spec.quantity.value.amount.as_tuple() == Decimal(amount).as_tuple()
        assert NomenclatureSpec.model_validate_json(result.spec.model_dump_json()) == result.spec


def test_explicit_length_is_geometry_not_quantity():
    result = extract('Труба профильная 80х40х3 длина 6 м — 100 м')
    assert result.status == 'recognized'
    assert result.spec.geometry['length'].value == Measurement(amount='6000', unit='mm')
    assert result.spec.quantity.value == Measurement(amount='100', unit='м')
    result = extract('Лист 6х1500х6000 длина 6 м — 4 шт')
    assert result.status == 'recognized' and result.spec.geometry['length'].is_resolved


def test_length_alternatives_are_not_misread_as_quantity():
    result = extract('Труба профильная 80х40х3 длина 6 м или 12 м — 5 шт')
    assert result.status == 'partial'
    assert result.spec.geometry['length'].state == 'ambiguous'
    assert {m.amount for m in result.spec.geometry['length'].alternatives} == {Decimal('6000'), Decimal('12000')}
    assert result.spec.quantity.value == Measurement(amount='5', unit='шт')


@pytest.mark.parametrize('text', ['Уголок 50х50х-5', 'Уголок 50х50х5х7', 'Уголок -50х50х5'])
def test_invalid_cross_section_is_not_read_as_a_valid_prefix(text):
    result = extract(text)
    assert result.status == 'partial'
    assert not result.spec.geometry
    assert result.spec.unparsed_features


def test_empty_unsupported_and_multiple_product_records_are_preserved():
    records = [record('', 'empty'), record(' \n ', 'blank'), record('Шестигранник 20 — 1 т', 'unsupported'),
               record('Круг 20 Ст3 — 1 т', 'round'),
               record('Уголок 50х50х5\nШвеллер 10У', 'multi'), record('Лист 10 мм', 'sheet')]
    batch = recognize_records(records)
    assert [r.record_id for r in batch.results] == [r.record_id for r in records]
    assert [r.status for r in batch.results] == ['empty', 'empty', 'unsupported', 'unsupported', 'ambiguous', 'recognized']
    assert batch.results[0].spec is None and batch.results[1].record.original_description == ' \n '
    assert batch.results[2].spec.original_description == records[2].original_description
    assert batch.results[3].spec.category.value == 'round_bar'
    assert 'multiple_products_in_record' in batch.results[4].issues
    assert RecognitionBatch.model_validate_json(batch.model_dump_json()) == batch


def test_identical_rows_have_distinct_ids_and_duplicate_ids_are_rejected():
    text = 'Уголок 50х50х5 Ст3 — 1 т'
    batch = recognize_records(record(text, str(i)) for i in range(3))
    assert len(batch.results) == 3 and {r.spec.record_id for r in batch.results} == {'0', '1', '2'}
    with pytest.raises(ValueError, match='unique'):
        recognize_records([record(text), record(text)])
    with pytest.raises(ValidationError):
        record(text, '')
    assert recognize_records([]).results == []


def test_existing_context_contract_is_preserved_without_applying_memory():
    context = WorkingContext(context_id='local-context', request_id='test-request',
                             normalization_version='v1', rule_set_version='local-recognition.v1')
    result = RecognitionService().recognize_record(record('Уголок 50х50х5 В Ст.3 сп.5'), context)
    assert result.spec.context == NormalizationContextRef(**{key: context.model_dump()[key] for key in NormalizationContextRef.model_fields})
    assert result.spec.material.designation.value == 'В Ст.3 сп.5'
    assert context.corrections == context.confirmed_experience == []


def test_import_and_recognition_need_no_ai_telegram_database_or_network():
    script = '''
import builtins
original_import = builtins.__import__
forbidden = {'ai', 'openai', 'telegram', 'telegram_service', 'telegram_bot',
             'database', 'sqlite3', 'dotenv', 'httpx', 'requests'}
def guarded(name, *args, **kwargs):
    if name.split('.')[0] in forbidden:
        raise AssertionError('Recognition imported external integration: ' + name)
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded
from recognition import recognize_record
result = recognize_record(dict(record_id='isolated', source=dict(source_id='test', source_type='text'),
                               original_description='Арматура А500С ф12'))
assert result.status == 'recognized'
print('isolated recognition passed')
'''
    result = subprocess.run([sys.executable, '-B', '-X', 'utf8', '-c', script],
                            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'isolated recognition passed'


def test_1000_records_keep_order_source_ids_and_decimal_values():
    texts = [x['spec']['original_description'] for x in EXAMPLES]
    records = [record(texts[i % len(texts)], f'bulk-{i}', 'price_list') for i in range(1000)]
    started = perf_counter()
    batch = recognize_records(records)
    elapsed = perf_counter() - started
    assert len(batch.results) == 1000
    assert [r.record_id for r in batch.results] == [r.record_id for r in records]
    assert all(r.status == 'recognized' and r.spec.original_description == source.original_description
               for r, source in zip(batch.results, records))
    assert all(isinstance(a.value.amount, Decimal) for r in batch.results for a in r.spec.geometry.values())
    # Observational timing only; no unstable machine-specific latency threshold.
    print(f'Local recognition: 1000 rows in {elapsed:.6f} seconds')
