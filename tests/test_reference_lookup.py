"""Synthetic reference rows only; no production catalogs or network."""
import hashlib
import json
from pathlib import Path
import sqlite3
from unittest.mock import patch

import pytest

from metal_reference.reader import SQLiteMetalReferenceReader
from metal_reference.schema import SCHEMA
from recognition import RecognitionRecord, RecognitionService, recognize_record, recognize_records
from reference_lookup import ReferenceCandidates, ReferenceLookupContext


def record(text, identity='row-1'):
    return RecognitionRecord(record_id=identity, source=dict(source_id='test-input', source_type='text'),
                             original_description=text)


@pytest.fixture
def catalog(tmp_path):
    path = tmp_path / 'synthetic-reference.sqlite3'
    with sqlite3.connect(path) as c:
        c.executescript(SCHEMA)
        c.executemany('INSERT INTO reference_source(id, source_type, name) VALUES (?, ?, ?)',
                      [(1, 'supplier_catalog', 'Synthetic supplier A'), (2, 'supplier_catalog', 'Synthetic supplier B')])
        c.executemany('INSERT INTO reference_family(id, code, name) VALUES (?, ?, ?)',
                      [(1, 'channel', 'Швеллер'), (2, 'sheet', 'Лист'), (3, 'pipe', 'Труба')])
        c.executemany('INSERT INTO reference_category(id, family_id, code, name) VALUES (?, ?, ?, ?)',
                      [(1, 1, 'channel', 'Швеллер'), (2, 2, 'sheet', 'Лист'),
                       (3, 3, 'rectangular_pipe', 'Труба прямоугольная'), (4, 3, 'round_pipe', 'Труба круглая')])
        c.executemany('INSERT INTO reference_item(id, category_id, normalized_key, designation, source_id, external_id) VALUES (?, ?, ?, ?, ?, ?)',
                      [(1, 1, '20у', '20У', 1, 'supplier-A-20'), (2, 1, '10п', '10П', 2, 'supplier-B-10'),
                       (3, 2, '20у', '20У', None, None), (4, 3, '80x40x3', '80×40×3', None, None),
                       (5, 4, '80x40x3', '80×40×3', None, None)])
        c.execute("INSERT INTO reference_standard(id, designation, document_type, year, source_id, source_locator) VALUES (1, 'ГОСТ 8240-97', 'ГОСТ', 1997, 1, 'synthetic/standard/1')")
        c.executemany('INSERT INTO reference_variant(id, reference_item_id, grade_class, execution, standard_id, source_id, source_locator) VALUES (?, ?, ?, ?, ?, ?, ?)',
                      [(1, 1, 'С345-6', 'test execution', 1, 1, 'synthetic/variant/1'),
                       (2, 1, '09Г2С', None, None, 2, 'synthetic/variant/2'),
                       (3, 2, None, None, None, None, None)])
        c.execute("INSERT INTO reference_dimension(reference_item_id, parameter_type, numeric_value, unit, raw_value, source_id) VALUES (1, 'diagram_dimension', 200, 'мм', '200 мм', 1)")
        c.execute("INSERT INTO reference_property(reference_variant_id, property_type, numeric_value, unit, raw_value, source_id, context) VALUES (1, 'mass_per_length', 0.888, 'кг/м', 'synthetic value', 1, 'test fixture, not normative data')")
        aliases = [
            (1, None, None, 'Канал двадцать', 'Канал двадцать', 'alternative_spelling', None),
            (1, None, None, 'Шв-20', 'Шв-20', 'abbreviation', None),
            (1, None, None, 'Двойной alias', 'Двойной alias', 'alternative_spelling', None),
            (2, None, None, 'Двойной alias', 'Двойной alias', 'alternative_spelling', None),
            (1, None, None, 'Профиль-А', 'Профиль-А', 'supplier_designation', 1),
            (2, None, None, 'Профиль-А', 'Профиль-А', 'supplier_designation', 2),
            (1, None, None, 'Коммерческая подпись', 'Коммерческая подпись', 'commercial_label', 1),
            (None, 1, None, 'SKU-20-6', 'SKU-20-6', 'source_identifier', 1),
            (None, None, 1, 'Швеллер сокращённо', 'Швеллер сокращённо', 'abbreviation', None),
        ]
        c.executemany('INSERT INTO reference_alias(reference_item_id, reference_variant_id, reference_category_id, raw_value, normalized_value, alias_type, source_id) VALUES (?, ?, ?, ?, ?, ?, ?)', aliases)
    return path


def lookup(path, text, context=None):
    return recognize_record(record(text), reference_reader=SQLiteMetalReferenceReader(path),
                            reference_context=context)


@pytest.mark.parametrize('text', ['Канал двадцать', 'канал   ДВАДЦАТЬ', 'Шв-20'])
def test_alias_and_alternative_spelling_return_all_variants(catalog, text):
    result = lookup(catalog, text)
    assert result.reference.status == 'candidates'
    assert {c.item.row_id for c in result.reference.candidates} == {1}
    assert {c.variant.row_id for c in result.reference.candidates} == {1, 2}
    assert all(c.evidence[0].kind == 'alias' for c in result.reference.candidates)
    assert result.spec.category.state == 'unknown'  # No alias promoted to recognition rule.


def test_ambiguous_alias_never_selects_first(catalog):
    found = lookup(catalog, 'Двойной alias').reference
    assert {(c.item.row_id, c.variant.row_id) for c in found.candidates} == {(1, 1), (1, 2), (2, 3)}
    assert all(c.technical_assessment == 'not_assessed' for c in found.candidates)
    assert not hasattr(found, 'selected_candidate')


@pytest.mark.parametrize('source,expected', [(None, set()), (1, {1}), (2, {2}), (9, set())])
def test_supplier_alias_is_scoped_and_not_a_global_rule(catalog, source, expected):
    found = lookup(catalog, 'Профиль-А', ReferenceLookupContext(reference_source_id=source)).reference
    assert {c.item.row_id for c in found.candidates} == expected
    assert all(e.alias_role == 'supplier_designation' for c in found.candidates for e in c.evidence)


def test_source_identifier_variant_target_and_commercial_label_stay_contextual(catalog):
    assert lookup(catalog, 'SKU-20-6').reference.status == 'no_candidates'
    scoped = lookup(catalog, 'SKU-20-6', ReferenceLookupContext(reference_source_id=1)).reference
    assert len(scoped.candidates) == 1 and scoped.candidates[0].variant.row_id == 1
    assert scoped.candidates[0].evidence[0].alias_role == 'source_identifier'
    assert lookup(catalog, 'Коммерческая подпись').reference.status == 'no_candidates'
    commercial = lookup(catalog, 'Коммерческая подпись', ReferenceLookupContext(reference_source_id=1))
    assert commercial.spec.material.designation.state == 'missing'
    assert all(e.alias_role == 'commercial_label' for c in commercial.reference.candidates for e in c.evidence)


def test_external_id_requires_same_reference_source(catalog):
    assert lookup(catalog, 'supplier-A-20').reference.status == 'no_candidates'
    scoped = lookup(catalog, 'supplier-A-20', ReferenceLookupContext(reference_source_id=1)).reference
    assert {c.item.row_id for c in scoped.candidates} == {1}
    assert all(c.evidence[0].kind == 'external_id' for c in scoped.candidates)


def test_category_and_type_limit_reference_candidates(catalog):
    channels = lookup(catalog, 'Швеллер 20У').reference
    assert {c.item.row_id for c in channels.candidates} == {1}  # Neither sheet 20У nor channel 10П.
    pipes = lookup(catalog, 'Труба профильная 80х40х3').reference
    assert {c.item.row_id for c in pipes.candidates} == {4}  # Not the round pipe category.
    limited = lookup(catalog, 'Канал двадцать', ReferenceLookupContext(category_codes=['sheet'])).reference
    assert not limited.candidates


def test_category_alias_is_broad_evidence_not_an_assortment_assertion(catalog):
    found = lookup(catalog, 'Швеллер сокращённо').reference
    assert {c.item.row_id for c in found.candidates} == {1, 2}
    assert all('только категории' in e.explanation for c in found.candidates for e in c.evidence)


def test_candidates_do_not_supply_missing_grade_standard_geometry_or_quantity(catalog):
    plain = recognize_record(record('Швеллер 20У'))
    enriched = lookup(catalog, 'Швеллер 20У')
    assert enriched.spec.model_dump_json() == plain.spec.model_dump_json()
    assert enriched.spec.material.designation.state == 'missing'
    assert enriched.spec.normative_documents.state == 'missing'
    assert enriched.spec.quantity.state == 'missing'
    assert enriched.spec.geometry == {}
    assert any(c.variant.fields['grade_class'] == 'С345-6' and c.standard is not None for c in enriched.reference.candidates)


def test_conflicting_reference_is_a_hypothesis_not_a_technical_replacement(catalog):
    text = 'Швеллер 20У 09Г2С ГОСТ 27772-2015 — 0,519 т'
    plain = recognize_record(record(text))
    result = lookup(catalog, text)
    assert result.spec == plain.spec
    assert result.spec.material.designation.value == '09Г2С'
    assert {c.variant.fields['grade_class'] for c in result.reference.candidates} == {'09Г2С', 'С345-6'}
    assert all(c.technical_assessment == 'not_assessed' for c in result.reference.candidates)
    assert any(c.standard.fields['designation'] == 'ГОСТ 8240-97' for c in result.reference.candidates if c.standard)


def test_reference_provenance_variants_properties_and_serialization(catalog):
    result = lookup(catalog, 'Канал двадцать')
    candidate = next(c for c in result.reference.candidates if c.variant.row_id == 1)
    assert candidate.evidence[0].provenance.locator.startswith('reference_alias/')
    assert candidate.item.provenance.record_id == 'reference_item:1'
    assert candidate.variant.provenance.locator == 'synthetic/variant/1'
    assert candidate.standard.provenance.locator == 'synthetic/standard/1'
    assert candidate.dimensions[0].fields['parameter_type'] == 'diagram_dimension'
    assert candidate.properties[0].fields['numeric_value'] == '0.888'  # Stored REAL representation, not recalculated mass.
    assert candidate.properties[0].fields['context'] == 'test fixture, not normative data'
    assert {s.row_id for s in candidate.sources} == {1}
    assert ReferenceCandidates.model_validate_json(result.reference.model_dump_json()) == result.reference


def test_missing_reference_attribute_stays_missing(catalog):
    found = lookup(catalog, 'Швеллер 10П').reference.candidates[0]
    assert found.variant.fields['grade_class'] is None
    assert found.standard is None and found.properties == []


def test_empty_unavailable_and_partial_reference_do_not_break_recognition(tmp_path):
    empty = tmp_path / 'empty.sqlite3'
    with sqlite3.connect(empty) as c:
        c.executescript(SCHEMA)
    assert lookup(empty, 'Швеллер 20У').reference.status == 'empty_reference'
    missing = tmp_path / 'never-create.sqlite3'
    unavailable = lookup(missing, 'Швеллер 20У')
    assert unavailable.reference.status == 'unavailable' and not missing.exists()
    assert unavailable.spec.profile.designation.value == '20У'
    incomplete = tmp_path / 'incomplete.sqlite3'
    with sqlite3.connect(incomplete) as c:
        c.execute('CREATE TABLE reference_source(id INTEGER PRIMARY KEY)')
    assert lookup(incomplete, 'Швеллер 20У').reference.status == 'unavailable'


def test_optional_tables_can_be_missing_without_migration(catalog):
    with sqlite3.connect(catalog) as c:
        c.execute('DROP TABLE reference_alias')
        c.execute('DROP TABLE reference_property')
    result = lookup(catalog, 'Швеллер 20У')
    assert result.reference.candidates
    assert 'missing_table:reference_alias' in result.reference.warnings
    assert 'missing_table:reference_property' in result.reference.warnings


def test_unknown_alias_type_is_not_silently_promoted(catalog):
    with sqlite3.connect(catalog) as c:
        c.execute("INSERT INTO reference_alias(reference_item_id, raw_value, normalized_value, alias_type, source_id) VALUES (1, 'Unknown scope', 'Unknown scope', 'legacy_vendor_tag', 1)")
    assert not lookup(catalog, 'Unknown scope').reference.candidates
    scoped = lookup(catalog, 'Unknown scope', ReferenceLookupContext(reference_source_id=1)).reference
    assert 'unclassified_alias_type' in scoped.warnings
    assert all(e.alias_role == 'unclassified' for c in scoped.candidates for e in c.evidence)


def test_truncation_is_explicit_and_does_not_select_a_candidate(catalog):
    result = lookup(catalog, 'Двойной alias', ReferenceLookupContext(max_candidates=1)).reference
    assert result.truncated and 'candidate_limit_reached' in result.warnings
    assert len(result.candidates) == 1
    assert result.candidates[0].technical_assessment == 'not_assessed'


def test_read_only_access_leaves_file_data_and_schema_unchanged(catalog):
    before = hashlib.sha256(catalog.read_bytes()).hexdigest()
    calls = []
    original = sqlite3.connect
    def observe(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)
    with patch('metal_reference.reader.sqlite3.connect', side_effect=observe):
        lookup(catalog, 'Швеллер 20У')
    assert len(calls) == 1 and calls[0][1]['uri']
    assert calls[0][0][0].endswith('?mode=ro')
    assert hashlib.sha256(catalog.read_bytes()).hexdigest() == before


def test_one_reference_read_for_batch_and_none_when_disabled(catalog):
    records = [record('Швеллер 20У', f'row-{i}') for i in range(20)]
    reader = SQLiteMetalReferenceReader(catalog)
    with patch.object(reader, '_read_snapshot', wraps=reader._read_snapshot) as reads:
        batch = recognize_records(records, reference_reader=reader)
        assert reads.call_count == 1
    assert [r.record_id for r in batch.results] == [r.record_id for r in records]
    with patch('metal_reference.reader.sqlite3.connect', side_effect=AssertionError('SQLite must remain optional')):
        plain = recognize_records(records)
        assert all(r.reference is None for r in plain.results)
        assert recognize_records([], reference_reader=reader).results == []
        blank = recognize_record(record(''), reference_reader=reader)
        assert blank.spec is None and blank.reference is None


def test_test_adapter_is_independent_and_cannot_mutate_technical_snapshot():
    class Adapter:
        def find_candidates(self, specs, context):
            for spec in specs:
                spec.unparsed_features.append('attempted mutation')
                spec.geometry.clear()
            return [ReferenceCandidates(record_id=s.record_id, status='empty_reference') for s in specs]
    plain = recognize_record(record('Уголок 50х50х5 Ст3'))
    result = recognize_record(record('Уголок 50х50х5 Ст3'), reference_reader=Adapter())
    assert result.spec == plain.spec and result.spec.geometry


def test_reader_must_preserve_record_correspondence():
    class BadAdapter:
        def find_candidates(self, specs, context):
            return [ReferenceCandidates(record_id='wrong-record', status='empty_reference')]
    with pytest.raises(ValueError, match='record_id'):
        RecognitionService(BadAdapter()).recognize_record(record('Швеллер 20У'))


def test_supplier_context_also_receives_normalization_reference():
    from nomenclature import NormalizationContextRef
    context = NormalizationContextRef(context_id='test-context', request_id='test-request', normalization_version='v1')
    seen = []
    class Adapter:
        def find_candidates(self, specs, lookup_context):
            seen.append(lookup_context)
            return [ReferenceCandidates(record_id=s.record_id, status='empty_reference') for s in specs]
    configured = ReferenceLookupContext(reference_source_id=1)
    service = RecognitionService(Adapter(), configured)
    service.recognize_record(record('Швеллер 20У'), context=context)
    assert seen[0].reference_source_id == 1 and seen[0].normalization == context
    assert configured.normalization is None  # No mutation of the shared lookup context.


def test_six_stage_102_examples_are_unchanged_with_reference(catalog):
    examples = json.loads((Path(__file__).parent / 'fixtures' / 'nomenclature_contract_v1.json').read_text(encoding='utf-8'))
    rows = [RecognitionRecord(**{k: x['spec'][k] for k in ('record_id', 'source', 'original_description')}) for x in examples]
    plain = recognize_records(rows)
    enriched = recognize_records(rows, reference_reader=SQLiteMetalReferenceReader(catalog))
    assert [r.spec.model_dump_json() for r in plain.results] == [r.spec.model_dump_json() for r in enriched.results]
    assert all(r.status == 'recognized' for r in enriched.results)
