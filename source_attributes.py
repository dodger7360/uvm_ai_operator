"""Literal technical attributes, without commercial or normative equivalence.

The legacy ``grade`` string also carries complete material designations. Parse
their components for comparison; do not interpret a supply group/category as
a new steel grade. Original spelling remains in RequestItem.source_text.
"""
from dataclasses import dataclass
import re


_CARBON_BODY = (
    r'(?P<group>[ВB])?\s*Ст\s*\.?\s*(?P<number>\d+)'
    r'(?:\s*\.?\s*(?P<deoxidation>пс|сп|кп)\s*\.?\s*(?P<category>\d*))?'
    r'(?P<suffix>-\d+)?'
)
_CARBON_DESIGNATION = re.compile(_CARBON_BODY, re.I)
GRADE_RE = re.compile(
    r'(?<![\w])(?:\d{3}-\d+-)?(?:[СC]\s*\d{3}(?:-\d+)?|'
    r'(?:[ВB]\s*)?Ст\s*\.?\s*(?:\d{2}Г[0-9]С|'
    r'\d+(?:\s*\.?\s*(?:пс|сп|кп)\s*\.?\s*\d*)?)(?:-\d+)?|'
    r'\d{2}Г[0-9]С(?:-\d+)?|12Х18Н10Т(?:-\d+)?)(?![\w]|-\d)',
    re.I,
)


@dataclass(frozen=True)
class CarbonSteelDesignation:
    supply_group: str
    steel_number: str
    deoxidation: str
    category: str
    suffix: str

    def canonical(self):
        return (self.supply_group + 'Ст' + self.steel_number
                + self.deoxidation + self.category + self.suffix)


def parse_carbon_designation(value):
    match = _CARBON_DESIGNATION.fullmatch(value.strip())
    if match is None:
        return None
    return CarbonSteelDesignation(
        (match['group'] or '').upper(), match['number'],
        (match['deoxidation'] or '').lower(), match['category'] or '',
        match['suffix'] or '',
    )


def designation_key(value):
    parsed = parse_carbon_designation(value)
    if parsed:
        return parsed.canonical().casefold()
    # Only spaces/case are ignored for other already-supported designations.
    return re.sub(r'\s+', '', value).casefold()


def has_designation_evidence(value, source):
    """Return None for the legacy grades outside the supported token grammar."""
    if GRADE_RE.fullmatch(value.strip()) is None:
        return None
    expected = designation_key(value)
    return any(designation_key(match[0]) == expected for match in source_grade_matches(source))


def _without_quantities(source):
    # Reuse the existing unit grammar. Mask rather than concatenate: a quantity
    # after a dash must not become another segment of a standard number, nor
    # an isolated count become a carbon steel category.
    from quantity import QUANTITY_RE

    def mask(match):
        # An integer attached directly to a document/grade suffix is not a
        # standalone quantity. Never remove a year to confirm a shorter ГОСТ.
        if match[1].isdigit() and re.search(r'\d[.\-]$', source[:match.start()]):
            return match[0]
        return '\0' * len(match[0])

    return QUANTITY_RE.sub(mask, source)


def source_grade_matches(source):
    return list(GRADE_RE.finditer(_without_quantities(source)))


_STANDARD_RE = re.compile(
    r'(?<!\w)(?P<kind>ГОСТ(?:\s*Р)?|ТУ|СТО(?:\s+АСЧМ)?)\s*'
    r'(?P<number>\d+(?:\s*[.\-]\s*\d+)*)'
    r'(?!\w|\s*[.\-]\s*\d)', re.I,
)


def _standard_value(match):
    kind = re.sub(r'\s+', ' ', match['kind'].upper())
    if kind == 'ГОСТР':
        kind = 'ГОСТ Р'
    return kind + ' ' + re.sub(r'\s+', '', match['number'])


def source_standard_matches(source):
    """Literal document spans, using the same grammar as source validation."""
    return list(_STANDARD_RE.finditer(_without_quantities(source)))


def source_standards(source):
    return [_standard_value(match) for match in source_standard_matches(source)]


def has_standard_evidence(value, source):
    """Every complete document identifier must have its own literal evidence.

    Reject unparsed claim text and partial identifiers; a nearby real ГОСТ
    cannot substantiate another document, year, or type of standard.
    """
    matches = list(_STANDARD_RE.finditer(value))
    if not matches:
        return False
    remainder = _STANDARD_RE.sub('', value)
    if re.sub(r'(?:\bи\b|[\s,;/"\'«»])', '', remainder, flags=re.I):
        return False
    actual = set(source_standards(source))
    return all(_standard_value(match) in actual for match in matches)
