"""Shared source-row accounting before any derived consolidation."""
import re
from diagnostics import phase, SemanticExtractionError
from quantity import QUANTITY_RE, without_mass_annotation


PRODUCT_ROW = re.compile(r'\b(балк\w*|двутавр\w*|лист\w*|труб\w*|угол\w*|швеллер\w*|арматур\w*|круг\w*|полос\w*|проволок\w*|лент\w*|сетк\w*|прут\w*|отвод\w*|фланец\w*|муфт\w*|тройник\w*)\b',re.I)


def product_row_key(row, city):
    """Ignore a request opener and the explicitly extracted trailing city."""
    from ai import _compact_source_text
    row = without_mass_annotation(row)
    row = re.sub(r'^\s*(?:нужна|нужны|нужно|нужен|требуется|требуются)\s+', '', row, flags=re.I)
    if city and city.strip():
        row = re.sub(r'(?:[,;]\s*|\s+)' + re.escape(city.strip()) + r'[.!?]*\s*$', '', row, flags=re.I)
    return _compact_source_text(row)[0]



def validate_source_coverage(draft, parse_text, allow_heading_rows=False):
    from ai import _source_fragment_occurrences
    # Every physical row containing a product must be represented. Quoted Excel
    # continuations remain within the item's literal source fragment.
    product = PRODUCT_ROW
    literal_rows = [line.strip().strip('"')
        for line in parse_text.splitlines()
        if product.search(line) and re.search(r'\d', line)]
    if allow_heading_rows and not literal_rows:
        return
    phase('validation',source_count=len(literal_rows),extracted_count=len(draft.items))
    for item in draft.items:
        physical = [line for line in (item.source_text or '').splitlines() if product.search(line) and QUANTITY_RE.search(line)]
        if len(physical) > 1:
            raise SemanticExtractionError('merged_positions','Несколько исходных строк объединены в одну позицию. Повторите заявку.')
    # Consume each physical row once, so a fragment cannot cover two rows
    # and duplicate rows require separate extracted items.
    unmatched_rows = list(literal_rows)
    remaining_positions = list(range(1, len(literal_rows) + 1))
    source_positions = []
    unmatched_items = []
    for item in draft.items:
        fragment = item.source_text or ''
        if not _source_fragment_occurrences(parse_text, fragment):
            raise SemanticExtractionError('source_fragment_mismatch','Исходный фрагмент позиции не найден в заявке. Повторите заявку.')
        literal = next((row for row in unmatched_rows
            if _source_fragment_occurrences(row, fragment)
            and product_row_key(row, draft.city) == product_row_key(fragment, draft.city)), None)
        if literal is not None:
            item.source_text = literal
        else:
            # Preserve quoted Excel continuations within a literal excerpt.
            literal = next((row for row in unmatched_rows
                if _source_fragment_occurrences(fragment, row)), None)
        if literal is not None:
            if len(list(product.finditer(fragment))) > len(list(product.finditer(literal))):
                raise SemanticExtractionError('merged_positions','Несколько исходных строк объединены в одну позицию. Повторите заявку.')
            matched_index = unmatched_rows.index(literal)
            source_positions.append(remaining_positions.pop(matched_index))
            unmatched_rows.pop(matched_index)
        else:
            unmatched_items.append(item)
    if unmatched_rows:
        raise SemanticExtractionError('missing_positions','Не все исходные позиции распознаны. Повторите заявку; предыдущая сохранена.',source_count=len(literal_rows),extracted_count=len(draft.items))
    if unmatched_items:
        raise SemanticExtractionError('additional_positions','Извлечены лишние позиции без отдельной исходной строки. Повторите заявку.',source_count=len(literal_rows),extracted_count=len(draft.items))
    return source_positions
