"""B6 acceptance: red tests retain actual unmet user expectations."""
import pytest
from decimal import Decimal
import re
from tests.b6_acceptance import CASES, FIXTURES, check_transport, evaluate, load_case


@pytest.fixture(params=CASES)
def result(request, tmp_path):
    return evaluate(request.param, tmp_path)


@pytest.mark.parametrize('case', CASES)
def test_controlled_transport_has_literal_fragments_and_amounts(case):
    check_transport(case)


def test_source_and_mass_are_preserved(result):
    assert result['source_text_preserved']
    assert result['source_mass_t'] == result['saved_mass_t'] == result['grouped_mass_t']
    assert not result['missing_positions']
    positions = [position for group in result['links'] for position in group['source_positions']]
    assert sorted(positions) == list(range(1, result['source_count'] + 1))


def test_all_technical_attributes_survive(result):
    assert not result['damaged'], result['damaged']
    assert result['preserved_count'] == result['source_count']
    assert result['fully_valid_count'] == result['source_count'], result['missing_fields']


def test_procurement_text_matches_available_expectation(result):
    assert result['exact_text_match'], '\n'.join(result['diff'])


def test_issue_code_blocks_are_copied_literally():
    body = (FIXTURES / 'issue11.md').read_text(encoding='utf-8')
    blocks = re.findall(r'~~~text\n([\s\S]*?)~~~', body)
    files = ['profiles_source', 'profiles_expected', 'sheets_source', 'sheets_expected', 'beams_source', 'beams_expected']
    assert len(blocks) == len(files)
    for name, block in zip(files, blocks):
        assert (FIXTURES / f'{name}.txt').read_text(encoding='utf-8') == block
    assert (FIXTURES / 'angles_source.txt').read_bytes() == (FIXTURES.parent / 'p0_angles_channels_38.txt').read_bytes()
    assert (FIXTURES / 'angles_expected.txt').read_bytes() == (FIXTURES.parent / 'p0_angles_channels_28_expected.txt').read_bytes()


@pytest.mark.parametrize('case', CASES)
def test_each_requested_group_has_exact_decimal_arithmetic(case):
    _, _, manifest, _ = load_case(case)
    positions = []
    for group in manifest['procurement_arithmetic_groups']:
        positions.extend(group['source_positions'])
        total = sum((Decimal(manifest['rows'][p-1]['quantity']) for p in group['source_positions']), Decimal(0))
        assert total == Decimal(group['sum_t']) == Decimal(group['expected_t'])
    assert sorted(positions) == list(range(1, manifest['source_count'] + 1))


def test_profile_dimensions_have_sequence_evidence_but_fail_standalone_number_guard():
    from schemas import PurchaseRequestExtraction
    from validator import _has_dimension_sequence_evidence, _has_number_source_evidence
    _, _, _, transport = load_case('profiles')
    item = PurchaseRequestExtraction.model_validate(transport).items[0]
    assert _has_dimension_sequence_evidence(item, item.source_text)
    assert not _has_number_source_evidence(item.width_mm, item.source_text)
