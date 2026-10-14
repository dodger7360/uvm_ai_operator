"""Conservative projection of reference rows, without SQLite or alias inference."""
from decimal import Decimal
import re

from nomenclature import Attribute, Measurement, NomenclatureSpec, Provenance
from normalizer import DISPLAY_PRODUCTS
from recognition import (RecognitionRecord, RecognitionService, _COATING, _MANUFACTURING,
                         _PRODUCT, _ROLLING)
from reference_lookup import ReferenceCandidate


_DIMENSIONS = {'thickness': 'thickness', 'width': 'width', 'height': 'height', 'length': 'length',
               'wall_thickness': 'wall_thickness', 'nominal_diameter': 'diameter', 'diameter': 'diameter',
               'side_a': 'leg_a', 'side_b': 'leg_b', 'leg_a': 'leg_a', 'leg_b': 'leg_b'}


def _reference_proof(entry, field):
    return entry.provenance.model_copy(update={'locator': (entry.provenance.locator or f'{entry.table}/{entry.row_id}') + '#' + field,
                                               'fragment': str(entry.fields.get(field) or '')})


class ReferenceTechnicalAdapter:
    """Only explicitly available rows; missing values are never backfilled."""
    def to_spec(self, candidate) -> NomenclatureSpec:
        candidate = ReferenceCandidate.model_validate(candidate)
        family = candidate.family.fields.get('code')
        name = str(candidate.category.fields.get('name') or '')
        group = family
        if family == 'pipe' and re.search(r'проф|прямоуг|квадр', name, re.I):
            group = 'profile_pipe'
        label = name if _PRODUCT.search(name) else ' '.join(v for v in [DISPLAY_PRODUCTS.get(group, ''), name] if v)
        variant = candidate.variant
        values = [label, str(candidate.item.fields.get('designation') or '')]
        if variant:
            values += [str(variant.fields.get(k) or '') for k in ('grade_class', 'execution')]
        if candidate.standard:
            values.append(str(candidate.standard.fields.get('designation') or ''))
        # This description is an explicit row projection, not the request text.
        identity = (candidate.item.provenance.record_id or f'{candidate.item.table}:{candidate.item.row_id}') + (f'/variant:{variant.row_id}' if variant else '')
        source_id = candidate.item.provenance.source_id
        result = RecognitionService().recognize_record(RecognitionRecord(record_id=identity,
            source=dict(source_id=source_id, source_type='metal_reference_projection',
                        locator=candidate.item.provenance.locator), original_description=' '.join(v for v in values if v)))
        spec = result.spec
        if spec is None:
            return NomenclatureSpec(record_id=identity,
                source=dict(source_id=source_id, source_type='metal_reference_projection'),
                original_description=identity, unparsed_features=['reference_projection_unavailable'])
        data = spec.model_dump()
        # The reference schema has no stock/request quantity column. A number
        # embedded in an execution label cannot manufacture a supply quantity.
        data['quantity'] = Attribute[Measurement]().model_dump()
        # Replace synthetic string-location evidence by the actual catalog rows.
        for section in ('profile', 'geometry', 'material', 'execution'):
            attrs = data[section]
            for key, attr in attrs.items():
                if attr['state'] == 'missing':
                    continue
                entry = variant if section in {'material', 'execution'} and variant else candidate.item
                field = 'grade_class' if section == 'material' else 'execution' if section == 'execution' else 'designation'
                if section == 'execution':
                    pattern = {'rolling_method': _ROLLING, 'manufacturing_method': _MANUFACTURING, 'coating': _COATING}.get(key)
                    if pattern and pattern.search(name) and not (variant and pattern.search(str(variant.fields.get('execution') or ''))):
                        entry, field = candidate.category, 'name'
                old_rules = [p for p in attr['provenance'] if p['method'] == 'rule']
                proof = _reference_proof(entry, field)
                attr['provenance'] = [proof.model_dump()] + [dict(p, source_id=source_id, record_id=identity,
                    locator=proof.locator, fragment=proof.fragment) for p in old_rules]
                if section == 'geometry' and any(p['rule_id'] == 'equal-leg-abbreviation' for p in old_rules):
                    attr['provenance'].append(_reference_proof(candidate.category, 'name').model_dump())
        for key in ('category', 'product_type'):
            if data[key]['state'] == 'present':
                data[key]['provenance'] = [candidate.family.provenance.model_dump(), candidate.category.provenance.model_dump(),
                    Provenance(source_id=source_id, record_id=identity, method='rule', rule_id='reference-category-projection',
                               rule_version='reference-projection.v1', locator=candidate.category.provenance.locator).model_dump()]
        if variant and variant.fields.get('form'):
            data['execution']['supply_form'] = Attribute[str](state='present', value=variant.fields['form'],
                provenance=[_reference_proof(variant, 'form')]).model_dump()
        if group == 'profile_pipe':
            explicit = 'square' if re.search(r'квадратн', name, re.I) else 'rectangular' if re.search(r'прямоугольн', name, re.I) else None
            cross = Attribute[str].model_validate(data['profile']['cross_section'])
            if explicit and cross.is_resolved and explicit != cross.value:
                data['profile']['cross_section'] = Attribute[str](state='conflict', alternatives=[explicit, cross.value],
                    provenance=[_reference_proof(candidate.category, 'name')] + cross.provenance,
                    reason='Тип трубы в категории противоречит размерам изделия.').model_dump()
            # Remove only a verified, independently structured category trait.
            if not explicit or cross.is_resolved and explicit == cross.value:
                data['unparsed_features'] = [feature for feature in data['unparsed_features'] if not re.fullmatch(
                    r'(?:профильн|прямоугольн|квадратн)(?:ая|ый|ое|ые)', feature.strip(), re.I)]
        if candidate.standard and spec.normative_documents.is_resolved:
            for doc in data['normative_documents']['value']:
                doc['provenance'] = [candidate.standard.provenance.model_dump()]
                if doc['edition']['state'] == 'present':
                    doc['edition']['provenance'] = [_reference_proof(candidate.standard, 'designation').model_dump()]
                role = candidate.standard.fields.get('role')
                if role:
                    doc['role'] = Attribute[str](state='present', value=role,
                        provenance=[_reference_proof(candidate.standard, 'role')]).model_dump()
            data['normative_documents']['provenance'] = [candidate.standard.provenance.model_dump()]
        for dimension in candidate.dimensions:
            field = _DIMENSIONS.get(dimension.fields.get('parameter_type'))
            if field is None:
                data['unparsed_features'].append(f'{dimension.table}:{dimension.row_id}:unmapped_dimension')
                continue  # A diagram dimension cannot be guessed into a named axis.
            raw = str(dimension.fields.get('raw_value') or '').strip()
            match = re.fullmatch(r'(\d+(?:[.,]\d+)?)\s*(мм|mm|см|cm|м|m)?', raw, re.I)
            unit = (match[2] if match else None) or dimension.fields.get('unit')
            shift = {'мм': 0, 'mm': 0, 'см': 1, 'cm': 1, 'м': 3, 'm': 3}.get(str(unit).lower())
            if not match or shift is None:
                data['geometry'][field] = Attribute[Measurement](state='unknown', raw_value=raw or None,
                    provenance=[dimension.provenance], reason='REAL без проверяемого исходного десятичного значения не подтверждает точный размер.').model_dump()
                continue
            amount = Decimal(match[1].replace(',', '.'))
            sign, digits, exponent = amount.as_tuple()
            value = Measurement(amount=Decimal((sign, digits, exponent + shift)), unit='mm')
            proof = [_reference_proof(dimension, 'raw_value'), _reference_proof(dimension, 'unit')]
            old = Attribute[Measurement].model_validate(data['geometry'].get(field, {}))
            if old.is_resolved and old.value != value:
                attr = Attribute[Measurement](state='conflict', alternatives=[old.value, value],
                    provenance=old.provenance + proof, reason='Размер в обозначении и структурированном справочном поле противоречат друг другу.')
            else:
                attr = Attribute[Measurement](state='present', value=value, raw_value=raw, provenance=proof)
            data['geometry'][field] = attr.model_dump()
        for prop in candidate.properties:
            # Properties in the research schema have no general technical-role
            # contract. Never silently omit a possibly significant requirement.
            data['unparsed_features'].append(f'{prop.table}:{prop.row_id}:unmapped_property')
        return NomenclatureSpec.model_validate(data)
