"""B6 diagnostic harness. Test assets only; never used by the application."""
from decimal import Decimal
from difflib import unified_diff
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import hashlib
import json
import re
import logging

from ai import AIService
from database import Database
from formatter import format_normalized_request, request_consolidation
from repositories.telegram_repository import TelegramRepository
from schemas import PurchaseRequestExtraction
from telegram_service import TelegramService

FIXTURES = Path(__file__).parent / 'fixtures' / 'b6'
CASES = ('angles', 'profiles', 'sheets', 'beams')


def load_case(case):
    source = (FIXTURES / f'{case}_source.txt').read_text(encoding='utf-8')
    expected = (FIXTURES / f'{case}_expected.txt').read_text(encoding='utf-8')
    manifest = json.loads((FIXTURES / 'manifest.json').read_text(encoding='utf-8'))[case]
    transport = json.loads((FIXTURES / f'{case}_transport.json').read_text(encoding='utf-8'))
    return source, expected, manifest, transport


def spelling(value):
    # Audit comparison only: preserve all letters, numbers and suffixes.
    return re.sub(r'[\s.]', '', value or '').casefold()


def amount_sum(lines):
    return sum((Decimal(re.search(r'(\d+(?:[.,]\d+)?)\s*т\s*$', line)[1].replace(',', '.'))
                for line in lines if line.strip()), Decimal(0))


def check_transport(case):
    source, _, manifest, transport = load_case(case)
    # Git may check text out with CRLF on Windows; logical characters stay exact.
    assert hashlib.sha256(source.encode('utf-8')).hexdigest() == manifest['source_text_sha256']
    extraction = PurchaseRequestExtraction.model_validate(transport)
    assert len(extraction.items) == manifest['source_count']
    for item, row in zip(extraction.items, manifest['rows']):
        assert item.source_text == source[row['source_start']:row['source_end']]
        assert item.grade == row['raw_grade']
        assert item.quantity == Decimal(row['quantity']) and item.unit == 'т'
        assert item.gost_tu == ', '.join(row['standards'])
        assert item.product_group == row['group'] and item.size_designation == row['size']
        assert row['size'] in item.source_text
        assert spelling(item.grade) in spelling(item.source_text.replace('"', ''))
        fields = {'angle': ('side_a_mm', 'side_b_mm', 'thickness_mm'),
                  'profile_pipe': ('width_mm', 'height_mm', 'wall_thickness_mm'),
                  'sheet': ('thickness_mm', 'width_mm', 'length_mm')}.get(row['group'], ())
        for field, number in zip(fields, row['size'].split('х')):
            assert Decimal(str(getattr(item, field))) == Decimal(number.replace(',', '.'))
        # Independent terminal-amount check against the literal, unmodified row.
        assert amount_sum([item.source_text]) == item.quantity
    AIService._validate_source_fragments(extraction, source)
    return extraction


def evaluate(case, directory):
    source, expected, manifest, transport = load_case(case)
    check_transport(case)
    ai = AIService.__new__(AIService)
    ai._invoke = Mock(side_effect=lambda *args, **kwargs: SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(refusal=None, parsed=PurchaseRequestExtraction.model_validate(transport)))]))
    repo = TelegramRepository(Database(Path(directory) / f'{case}.sqlite3'))
    service = TelegramService(ai, repo, reliable=True, commercial_view=True)
    events = []
    class Events(logging.Handler):
        def emit(self, record):
            events.append(json.loads(record.getMessage()))
    logger = logging.getLogger('operator_diagnostics')
    handler, old_level = Events(), logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        messages = service.process(600001, source)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
    current = repo.load(600001)
    assert current is not None
    actual = format_normalized_request(current)
    groups = request_consolidation(current)
    by_source = {item.source_text: item for item in current.items}
    preserved, damaged, missing = [], [], []
    for row, raw in zip(manifest['rows'], transport['items']):
        literal = source[row['source_start']:row['source_end']]
        item = by_source.get(literal)
        if item is None:
            missing.append(row['position'])
            continue
        differences = {}
        if item.product_group != row['group']:
            differences['product_group'] = {'source': row['group'], 'actual': item.product_group}
        size_key = lambda value: (value or '').casefold().replace('×', 'х').replace('x', 'х').replace(',', '.')
        if size_key(item.size_designation) != size_key(row['size']):
            differences['size'] = {'source': row['size'], 'actual': item.size_designation}
        if item.original.source_text != literal:
            differences['original_source'] = {'source': literal, 'actual': item.original.source_text}
        if spelling(item.grade) != spelling(row['raw_grade']):
            differences['grade'] = {'source': row['raw_grade'], 'actual': item.grade}
        if item.quantity != Decimal(row['quantity']) or item.unit != 'т':
            differences['quantity_unit'] = {'source': row['quantity'] + ' т', 'actual': str(item.quantity) + ' ' + str(item.unit)}
        actual_documents = set(item.gost_tu.split(', ')) if item.gost_tu else set()
        if actual_documents != set(row['standards']):
            differences['documents'] = {'source': row['standards'], 'actual': sorted(actual_documents)}
        for field in ('form', 'processing', 'thickness_mm', 'width_mm', 'height_mm',
                      'length_mm', 'side_a_mm', 'side_b_mm', 'wall_thickness_mm'):
            if field in raw:
                actual_value = getattr(item, field)
                same = (Decimal(str(actual_value)) == Decimal(raw[field]) if field.endswith('_mm') and actual_value is not None
                        else actual_value == raw[field])
                if not same:
                    differences[field] = {'source': raw[field], 'actual': actual_value}
        if differences:
            damaged.append({'position': row['position'], 'differences': differences})
        else:
            preserved.append(row['position'])
    before_mass = sum((Decimal(row['quantity']) for row in manifest['rows']), Decimal(0))
    after_mass = sum((item.quantity for item in current.items if item.quantity is not None and item.unit == 'т'), Decimal(0))
    grouped_mass = sum((group.item.quantity for group in groups if group.item.quantity is not None and group.item.unit == 'т'), Decimal(0))
    positions_by_id = {by_source[source[r['source_start']:r['source_end']]].item_id: r['position']
                       for r in manifest['rows'] if source[r['source_start']:r['source_end']] in by_source}
    links = [{'line': n, 'source_positions': [positions_by_id.get(key) for key in group.source_item_ids],
              'quantity': str(group.item.quantity), 'grade': group.item.grade}
             for n, group in enumerate(groups, 1)]
    invalid_positions = {int(match[1]) for warning in current.warnings
                         if (match := re.match(r'Позиция (\d+):', warning))}
    invalid_positions.update(positions_by_id[current.items[int(match[1])].item_id]
        for field in current.missing_fields if (match := re.match(r'items\[(\d+)\]', field))
        and current.items[int(match[1])].item_id in positions_by_id)
    return {'case': case, 'source_count': manifest['source_count'], 'transport_count': len(transport['items']),
            'preserved_count': len(preserved), 'fully_valid_count': len(set(preserved) - invalid_positions),
            'damaged': damaged, 'missing_positions': missing, 'events': events,
            'output_lines': len(groups), 'expected_lines': len([l for l in expected.splitlines() if l.strip()]),
            'source_mass_t': str(before_mass), 'saved_mass_t': str(after_mass), 'grouped_mass_t': str(grouped_mass),
            'expected_mass_t': str(amount_sum(expected.splitlines())),
            'fallback_status': current.fallback_status, 'is_valid': current.is_valid,
            'missing_fields': current.missing_fields, 'warnings': current.warnings,
            'unprocessed_source': current.unprocessed_source, 'links': links,
            'source_text_preserved': current.source_text == source,
            'saved_items': [item.model_dump(mode='json', exclude_none=True,
                exclude={'original', 'mass_calculation', 'item_id', 'interpreted_fields', 'source_conditions'})
                for item in current.items],
            'model_calls': ai._invoke.call_count, 'arithmetic_groups': manifest['procurement_arithmetic_groups'],
            'exact_text_match': actual.strip() == expected.strip(),
            'diff': list(unified_diff(expected.splitlines(), actual.splitlines(), fromfile='expected', tofile='actual', lineterm='')),
            'actual_text': actual, 'user_messages': list(map(str, messages))}


if __name__ == '__main__':
    import tempfile
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='uvm-b6-') as directory:
        results = [evaluate(case, directory) for case in CASES]
    args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    for result in results:
        print({key: result[key] for key in ('case', 'source_count', 'preserved_count', 'missing_positions', 'damaged',
                                            'output_lines', 'source_mass_t', 'saved_mass_t', 'fallback_status', 'model_calls', 'exact_text_match')})
