"""Shared source-row accounting before any derived consolidation."""
import re
from diagnostics import phase, SemanticExtractionError
from quantity import QUANTITY_RE, without_mass_annotation


PRODUCT_ROW = re.compile(r'\b(балк\w*|двутавр\w*|лист\w*|труб\w*|угол\w*|швеллер\w*|арматур\w*|круг\w*|полос\w*|проволок\w*|лент\w*|сетк\w*|прут\w*|соединительн\w*|отвод\w*|фланец\w*|муфт\w*|тройник\w*|шестигран\w*|квадрат\w*|пвл)\b',re.I)


_CONTEXT_ROW = re.compile(
    r'^(?:примечани\w*|комментари\w*|доставк\w*|адрес|город|срок\w*|'
    r'услови\w*|оплат\w*|итого|всего|заявка|спецификаци\w*|перечень)\b', re.I)
_CONTINUATION_ROW = re.compile(
    r'^(?:ГОСТ(?=\s|\d|$)|ТУ(?=\s|\d|$)|СТО\b|'
    r'равнополоч\w*|неравнополоч\w*|горячекатан\w*|холоднокатан\w*|'
    r'стальн\w*|бесшовн\w*|электросварн\w*|длина|ширина|толщина|'
    r'раскрой|размер\w*|марка|материал|класс|БТ-ПН-О|[\dØø⌀])', re.I)


def _context_row(line):
    return bool(_CONTEXT_ROW.search(line.lstrip('" ')) or line.rstrip().endswith(':')
                or re.search(r'\bдля\s+(?:всех|позиций)\b', line, re.I))


def _continuation_row(line):
    from source_attributes import GRADE_RE
    stripped = line.lstrip('" ')
    return bool(_CONTINUATION_ROW.search(stripped) or GRADE_RE.match(stripped))


def _product_start(line):
    """Known families plus conservative evidence of an unknown specification.

    Unknown names need a separate size/identifier and a quantity, rather than
    arbitrary prose or a standalone amount. These are coverage candidates,
    not new product groups or normative reference records.
    """
    from normalizer import _detect_product_group
    line = re.sub(r'^\s*(?:\d+[.)]\s+|[-•]\s+)', '', line)
    if _context_row(line):
        return False
    if re.match(r'^\s*"?(?:ГОСТ|ТУ|СТО)(?=\s|\d|$)', line, re.I):
        return False
    if PRODUCT_ROW.search(line) or _detect_product_group(line):
        return True
    if _continuation_row(line):
        return False
    quantity = QUANTITY_RE.search(line)
    prefix = line[:quantity.start()] if quantity else ''
    dimension = re.search(r'\d+(?:[.,]\d+)?\s*[хx×*]\s*\d+', line, re.I)
    return bool((quantity and re.search(r'[А-Яа-яA-Za-z]', prefix)
                 and re.search(r'\d', prefix))
                or (dimension and re.search(r'[А-Яа-яA-Za-z]', line[:dimension.start()])))


def logical_source_rows(parse_text):
    """Join physical continuations before counting procurement rows.

    Spreadsheet exports commonly wrap one quoted row over multiple lines,
    especially between a grade and a ГОСТ. Treating each physical line as an
    independent row creates phantom source positions and then rejects a
    complete extraction. Explicit unquoted name/attribute continuations also
    stay with their product; headings and notes retain separate boundaries.
    """
    rows = []
    continuation = []

    for physical in parse_text.splitlines():
        line = physical.strip()
        if not line:
            continue
        if continuation:
            if _product_start(line) and line.count('"') % 2 == 0:
                rows.append('\n'.join(continuation))
                continuation = []
            else:
                continuation.append(line)
                if sum(part.count('"') for part in continuation) % 2 == 0:
                    rows.append('\n'.join(continuation))
                    continuation = []
                continue
        if _product_start(line):
            if line.startswith('"') or line.count('"') % 2:
                continuation = [line]
            else:
                rows.append(line)
            continue
        rows.append(line)
    if continuation:
        rows.append('\n'.join(continuation))
    joined = []
    for row in rows:
        if (joined and _continuation_row(row) and not _context_row(row)
                and not _product_start(row)
                and _product_start(joined[-1])):
            joined[-1] += '\n' + row
        else:
            joined.append(row)
    return joined


def source_product_rows(parse_text):
    """One common source of source-position identities and retry eligibility."""
    return [row for row in logical_source_rows(parse_text)
            if _product_start(row) and re.search(r'\d', row)]


def source_has_complete_quantities(parse_text):
    rows = source_product_rows(parse_text)
    return bool(rows) and all(QUANTITY_RE.search(row) for row in rows)


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
    # Every logical row containing a product must be represented. Quoted
    # continuations remain within one item's literal source fragment.
    # Preserve Workbench's existing heading-only contract: numeric assortment
    # rows under a shared product heading are validated by its field evidence.
    if allow_heading_rows and not any(_product_start(line) and re.search(r'\d', line)
                                     for line in parse_text.splitlines()):
        return
    literal_rows = source_product_rows(parse_text)
    if allow_heading_rows and not literal_rows:
        return
    phase('validation',source_count=len(literal_rows),extracted_count=len(draft.items))
    distinct_rows = {product_row_key(row, draft.city): row for row in literal_rows}
    for item in draft.items:
        fragment = item.source_text or ''
        represented = sum(_source_fragment_occurrences(fragment, row)
                          for row in distinct_rows.values())
        if len(source_product_rows(fragment)) > 1 or represented > 1:
            raise SemanticExtractionError('merged_positions','Несколько исходных строк объединены в одну позицию. Повторите заявку.')
    # Consume each logical row once, so a fragment cannot cover two rows
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
