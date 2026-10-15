"""Narrow business approvals, applied only to a derived procurement view."""
from pydantic import BaseModel, ConfigDict, Field, model_validator
from rules import PRODUCT_GROUPS
from product_identity import consolidation_key


class GradeRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')
    rule_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    product_group: str
    size_designation: str = Field(min_length=1)
    gost_tu: str | None  # None means exactly no standard, never a wildcard.
    from_grade: str = Field(min_length=1)
    to_grade: str = Field(min_length=1)
    basis: str = Field(min_length=1)
    form: str | None = None
    processing: str | None = None
    length_mm: float | None = None
    length_type: str | None = None
    length_requirement: str | None = None
    wall_thickness_requirement: str | None = None

    @model_validator(mode='after')
    def check_scope(self):
        if self.product_group not in PRODUCT_GROUPS or self.from_grade == self.to_grade:
            raise ValueError('Правило требует известную категорию и разные марки.')
        return self

    def matches(self, item):
        return all(getattr(item, name) == getattr(self, name) for name in
                   ('product_group', 'size_designation', 'gost_tu', 'form', 'processing',
                    'length_mm', 'length_type', 'length_requirement', 'wall_thickness_requirement')) and item.grade == self.from_grade


def project_item(item, rules=(), original=None):
    """Only caller-supplied approved rules; no implicit steel equivalences/chains."""
    projected = item.model_copy(deep=True)
    if not rules or consolidation_key(item, original=original) is None:
        return projected, ()
    matches = [rule for rule in rules if rule.matches(item)]
    if len({rule.to_grade for rule in matches}) > 1:
        raise ValueError('Конфликт утверждённых правил; требуется решение администратора.')
    if matches:
        projected.grade = matches[0].to_grade
    return projected, tuple(sorted(f'{rule.rule_id}@{rule.version}' for rule in matches))


def grade_merge_candidates(items, approved_rules=()):
    """Suggestions only: every protected characteristic except grade must agree."""
    from quantity import normalize_quantity
    buckets = {}
    for index, item in enumerate(items, 1):
        original = getattr(item, 'original', None) or item
        projected, _ = project_item(item, approved_rules, original=original)
        if not projected.grade or consolidation_key(projected, original=original) is None:
            continue
        try:
            _, unit = normalize_quantity(projected.quantity, projected.unit)
        except ValueError:
            continue
        key = tuple((field, value) for field, value in consolidation_key(projected, original=original)
                    if field != 'grade') + (('unit', unit),)
        buckets.setdefault(key, []).append((getattr(item, 'source_position', None) or index, projected.grade))
    return [tuple(rows) for rows in buckets.values() if len({grade for _, grade in rows}) > 1]
