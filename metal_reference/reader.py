"""Optional read-only adapter to existing MetalReference, no schema setup."""
from collections import defaultdict
from contextlib import closing
from pathlib import Path
import re
import sqlite3
import unicodedata

from nomenclature import Provenance
from normalizer import _detect_product_group, _normalize_dimensions
from rules import PRODUCT_GROUPS
from reference_lookup import (ReferenceCandidate, ReferenceCandidates, ReferenceEntry,
                              ReferenceEvidence, ReferenceLookupContext)


_TABLES = ('reference_source', 'reference_family', 'reference_category', 'reference_item',
           'reference_variant', 'reference_standard', 'reference_dimension',
           'reference_property', 'reference_alias')
_CORE = {'reference_family', 'reference_category', 'reference_item'}
_ALIAS_ROLES = {'alternative_spelling': 'alternative_spelling', 'spelling': 'alternative_spelling',
                'abbreviation': 'abbreviation', 'supplier_designation': 'supplier_designation',
                'source_identifier': 'source_identifier', 'commercial_label': 'commercial_label'}
_SCOPED = {'supplier_designation', 'source_identifier', 'commercial_label'}


def _writing(value):
    """Search-label spelling only. Never an identity or commercial key."""
    value = unicodedata.normalize('NFC', str(value or ''))
    value = re.sub(r'\d+(?:[.,]\d+)?(?:\s*[xхХX×*]\s*\d+(?:[.,]\d+)?){1,2}',
                   lambda m: _normalize_dimensions(m[0]).replace(',', '.'), value)
    return re.sub(r'\s+', '', value.casefold())


def _terms(spec):
    terms = [spec.original_description]
    for attr in (spec.profile.designation, spec.category, spec.product_type):
        if attr.is_resolved:
            terms += [attr.raw_value or '', attr.normalized_text or '']
            if attr is spec.profile.designation:
                terms.append(attr.value)
    if spec.geometry.get('diameter') and spec.geometry['diameter'].is_resolved:
        # A literal diameter is a search term, not proof of compatible class.
        terms.append(format(spec.geometry['diameter'].value.amount, 'f'))
    return list(dict.fromkeys(t for t in terms if t.strip()))


class SQLiteMetalReferenceReader:
    def __init__(self, path, *, reference_id='metal-reference'):
        # No default Database path: callers must explicitly supply a catalog.
        self.path = Path(path)
        self.reference_id = reference_id

    def find_candidates(self, specs, context=None):
        context = ReferenceLookupContext.model_validate(context or ReferenceLookupContext())
        if not specs:
            return []
        try:
            snapshot, warnings = self._read_snapshot()
        except (sqlite3.Error, OSError):
            return [ReferenceCandidates(record_id=s.record_id, status='unavailable',
                                        warnings=['reference_unavailable']) for s in specs]
        if not _CORE.issubset(snapshot):
            return [ReferenceCandidates(record_id=s.record_id, status='unavailable',
                                        warnings=warnings + ['reference_core_missing']) for s in specs]
        return _CatalogSnapshot(snapshot, self.reference_id, warnings).find_candidates(specs, context)

    def _read_snapshot(self):
        # One coherent read transaction per batch, query only reference tables.
        with closing(sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA query_only=ON')
            connection.execute('BEGIN')
            available = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'reference_%'")}
            rows = {name: [dict(row) for row in connection.execute(f'SELECT * FROM {name} ORDER BY id')]
                    for name in _TABLES if name in available}
            return rows, ['missing_table:' + name for name in _TABLES if name not in available]


class _CatalogSnapshot:
    """Ephemeral indexes over existing rows, not a second reference database."""
    def __init__(self, tables, reference_id, warnings):
        self.tables, self.reference_id, self.warnings = tables, reference_id, warnings
        self.by_id = {table: {row['id']: row for row in rows} for table, rows in tables.items()}
        self.labels = defaultdict(list)
        self.variants = defaultdict(list)
        self.details = {table: defaultdict(list) for table in ('reference_dimension', 'reference_property')}
        for item in tables['reference_item']:
            if item.get('status', 'active') != 'active':
                continue
            for label in (item['designation'], item['normalized_key']):
                self.labels[_writing(label)].append(('designation', item['id'], None, label))
            if item.get('external_id'):
                self.labels[_writing(item['external_id'])].append(('external_id', item['id'], None, item['external_id']))
        for category in tables['reference_category']:
            self.labels[_writing(category['name'])].append(('category_name', category['id'], None, category['name']))
        for alias in tables.get('reference_alias', []):
            for value in (alias['raw_value'], alias['normalized_value']):
                self.labels[_writing(value)].append(('alias', alias['id'], alias, value))
        for variant in tables.get('reference_variant', []):
            self.variants[variant['reference_item_id']].append(variant)
        for table in self.details:
            for row in tables.get(table, []):
                owner = ('item', row['reference_item_id']) if row['reference_item_id'] is not None else ('variant', row['reference_variant_id'])
                self.details[table][owner].append(row)

    def entry(self, table, row):
        fields = {k: str(v) if isinstance(v, float) else v for k, v in row.items() if k != 'id'}
        return ReferenceEntry(table=table, row_id=row['id'], fields=fields,
            provenance=Provenance(source_id=self.reference_id, record_id=f'{table}:{row["id"]}',
                                  locator=row.get('source_locator') or f'{table}/{row["id"]}',
                                  method='reference'))

    def allowed_category(self, category, spec, context):
        if category.get('status', 'active') != 'active':
            return False
        if context.category_codes and category['code'] not in context.category_codes:
            return False
        family = self.by_id['reference_family'].get(category['family_id'])
        if family is None:
            return False
        if not spec.category.is_resolved:
            return True
        group = spec.category.value
        detected = _detect_product_group(category['name'])
        if group == 'profile_pipe':
            return family['code'] == group or (family['code'] == 'pipe' or detected == 'pipe') and bool(re.search(
                r'проф|прямоуг|квадр', category['name'], re.I))
        if family['code'] in PRODUCT_GROUPS or family['code'] == 'pipe':
            return family['code'] == group
        return family['code'] == group or category['code'] == group or detected == group

    def alias_allowed(self, alias, context):
        role = _ALIAS_ROLES.get(alias['alias_type'], 'unclassified')
        source = alias.get('source_id')
        if role in _SCOPED:
            return source is not None and source == context.reference_source_id, role
        if role == 'unclassified' and source is not None:
            return source == context.reference_source_id, role
        return True, role

    def find_candidates(self, specs, context):
        results = []
        items = self.by_id['reference_item']
        categories = self.by_id['reference_category']
        variants = self.by_id.get('reference_variant', {})
        for spec in specs:
            hits = defaultdict(list)
            warnings = list(self.warnings)
            if not items:
                results.append(ReferenceCandidates(record_id=spec.record_id, status='empty_reference', warnings=warnings))
                continue
            for term in _terms(spec):
                for kind, target_id, alias, label in self.labels.get(_writing(term), []):
                    role = None
                    if alias:
                        allowed, role = self.alias_allowed(alias, context)
                        if not allowed:
                            continue
                        if role == 'unclassified':
                            warnings.append('unclassified_alias_type')
                        if alias['reference_variant_id'] is not None:
                            variant = variants.get(alias['reference_variant_id'])
                            targets = [(variant['reference_item_id'], variant['id'])] if variant else []
                        elif alias['reference_item_id'] is not None:
                            targets = [(alias['reference_item_id'], None)]
                        else:
                            targets = [(i['id'], None) for i in items.values() if i['category_id'] == alias['reference_category_id']]
                    elif kind == 'category_name':
                        targets = [(i['id'], None) for i in items.values() if i['category_id'] == target_id]
                    else:
                        targets = [(target_id, None)]
                    # A category label alone must not drown a stated profile in
                    # all assortment rows. This is a spelling search constraint,
                    # not a technical comparison or an identity key.
                    if (kind == 'category_name' or alias and alias['reference_category_id'] is not None) and spec.profile.designation.is_resolved:
                        profile = {_writing(spec.profile.designation.value), _writing(spec.profile.designation.normalized_text)}
                        targets = [(i, v) for i, v in targets if i in items and any(
                            _writing(items[i][field]) in profile for field in ('designation', 'normalized_key'))]
                    for item_id, variant_id in targets:
                        item = items.get(item_id)
                        category = categories.get(item['category_id']) if item else None
                        if not item or item.get('status', 'active') != 'active' or not category or not self.allowed_category(category, spec, context):
                            continue
                        if kind == 'external_id' and (item.get('source_id') is None or item['source_id'] != context.reference_source_id):
                            continue
                        explanation = ('Совпадение написания alias; технические характеристики не проверены.' if alias else
                                       'Совпадение справочного обозначения; технические характеристики не проверены.')
                        if kind == 'category_name' or alias and alias['reference_category_id'] is not None:
                            explanation = 'Совпадение только категории; конкретный типоразмер не подтверждён.'
                        evidence_row = alias if alias else category if kind == 'category_name' else item
                        evidence_table = 'reference_alias' if alias else 'reference_category' if kind == 'category_name' else 'reference_item'
                        evidence = ReferenceEvidence(kind=kind, query=term, explanation=explanation, alias_role=role,
                            alias=self.entry('reference_alias', alias) if alias else None,
                            provenance=self.entry(evidence_table, evidence_row).provenance)
                        if evidence not in hits[(item_id, variant_id)]:
                            hits[(item_id, variant_id)].append(evidence)
            expanded = defaultdict(list)
            for (item_id, variant_id), evidence in hits.items():
                choices = [variants[variant_id]] if variant_id is not None else self.variants.get(item_id) or [None]
                for variant in choices:
                    key = (item_id, variant['id'] if variant else None)
                    for reason in evidence:
                        if reason not in expanded[key]:
                            expanded[key].append(reason)
            candidates = []
            for (item_id, variant_id), evidence in sorted(expanded.items(), key=lambda pair: (pair[0][0], pair[0][1] or 0)):
                item = items[item_id]
                category = categories[item['category_id']]
                family = self.by_id['reference_family'][category['family_id']]
                variant = variants.get(variant_id)
                standard = self.by_id.get('reference_standard', {}).get(variant.get('standard_id')) if variant else None
                owners = [('item', item_id)] + ([('variant', variant_id)] if variant else [])
                details = {table: [row for owner in owners for row in self.details[table][owner]] for table in self.details}
                source_ids = {row.get('source_id') for row in [item, category, variant or {}, standard or {},
                              *details['reference_dimension'], *details['reference_property'],
                              *(e.alias.fields if e.alias else {} for e in evidence)]}
                sources = [self.entry('reference_source', self.by_id['reference_source'][sid]) for sid in sorted(
                    s for s in source_ids if s is not None and s in self.by_id.get('reference_source', {}))]
                candidates.append(ReferenceCandidate(item=self.entry('reference_item', item),
                    category=self.entry('reference_category', category), family=self.entry('reference_family', family),
                    variant=self.entry('reference_variant', variant) if variant else None,
                    standard=self.entry('reference_standard', standard) if standard else None,
                    dimensions=[self.entry('reference_dimension', row) for row in details['reference_dimension']],
                    properties=[self.entry('reference_property', row) for row in details['reference_property']],
                    sources=sources, evidence=evidence))
            truncated = len(candidates) > context.max_candidates
            if truncated:
                warnings.append('candidate_limit_reached')
            results.append(ReferenceCandidates(record_id=spec.record_id,
                status='candidates' if candidates else 'no_candidates',
                candidates=candidates[:context.max_candidates], warnings=list(dict.fromkeys(warnings)), truncated=truncated))
        return results
