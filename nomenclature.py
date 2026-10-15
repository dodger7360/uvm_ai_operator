"""Source-independent nomenclature contracts; no recognition or identity engine.

Raw descriptions and evidence survive spelling normalization. Commercial
groups and candidate analogs belong to normalization_context, not these facts.
"""
from decimal import Decimal
from typing import Annotated, Generic, Literal, TypeVar

from pydantic import (BaseModel, BeforeValidator, ConfigDict, Field,
                      StringConstraints, model_validator)


NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


def _exact_decimal(value):
    if isinstance(value, (float, bool)):
        raise ValueError('Use Decimal, an integer, or a decimal string; floats lose source precision.')
    return value


ExactDecimal = Annotated[Decimal, BeforeValidator(_exact_decimal), Field(allow_inf_nan=False)]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, validate_default=True)


class SourceRef(ContractModel):
    source_id: NonEmpty
    source_type: NonEmpty
    locator: str | None = None
    source_version: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


class Provenance(ContractModel):
    source_id: NonEmpty
    record_id: NonEmpty | None = None
    locator: str | None = None
    fragment: str | None = None
    method: Literal['source', 'context', 'rule', 'reference', 'user', 'fixture']
    rule_id: NonEmpty | None = None
    rule_version: NonEmpty | None = None

    @model_validator(mode='after')
    def rule_has_version(self):
        if self.method == 'rule' and (self.rule_id is None or self.rule_version is None):
            raise ValueError('A derived attribute requires its rule ID and version.')
        return self


class Measurement(ContractModel):
    amount: ExactDecimal
    # No implicit millimetres, density, tonnage or piece count in this contract.
    unit: NonEmpty | None = None


T = TypeVar('T')


class Attribute(ContractModel, Generic[T]):
    state: Literal['missing', 'present', 'unknown', 'ambiguous', 'conflict'] = 'missing'
    value: T | None = None
    raw_value: str | None = None
    normalized_text: str | None = None
    alternatives: list[T] = Field(default_factory=list)
    provenance: list[Provenance] = Field(default_factory=list)
    reason: str | None = None

    @model_validator(mode='after')
    def consistent_state(self):
        if self.state == 'present':
            if self.value is None or isinstance(self.value, str) and not self.value.strip():
                raise ValueError('A present attribute requires a nonempty value.')
            if not self.provenance:
                raise ValueError('A present attribute requires provenance.')
            if self.alternatives:
                raise ValueError('A resolved value cannot conceal unresolved alternatives.')
        elif self.value is not None or self.normalized_text is not None:
            raise ValueError('An unresolved attribute cannot claim a resolved value or spelling.')
        if self.state in {'ambiguous', 'conflict'}:
            if len(self.alternatives) < 2 or not self.provenance:
                raise ValueError('Ambiguity/conflict requires alternatives and provenance.')
        elif self.alternatives:
            raise ValueError('Alternatives require an ambiguous or conflicting state.')
        if self.state == 'missing' and self.raw_value is not None:
            raise ValueError('Explicit source text belongs to unknown/present, not missing.')
        return self

    @property
    def is_resolved(self):
        return self.state == 'present' and self.value is not None

    def has_same_known_value(self, other):
        """Value-level assertion only, never product identity or interchangeability.

        BaseModel equality is structural (including unknowns). Consumers must
        not use it as technical matching. Unknown units also prevent assertion.
        Normalized spelling and commercial labels do not replace technical value.
        """
        if not isinstance(other, Attribute) or not self.is_resolved or not other.is_resolved:
            return False
        if type(self.value) is not type(other.value):
            return False
        if isinstance(self.value, Measurement) and (self.value.unit is None or other.value.unit is None):
            return False
        return self.value == other.value


class ProfileSpec(ContractModel):
    designation: Attribute[str] = Field(default_factory=Attribute[str])
    series: Attribute[str] = Field(default_factory=Attribute[str])
    cross_section: Attribute[str] = Field(default_factory=Attribute[str])


class MaterialSpec(ContractModel):
    designation: Attribute[str] = Field(default_factory=Attribute[str])
    steel_grade: Attribute[str] = Field(default_factory=Attribute[str])
    strength_class: Attribute[str] = Field(default_factory=Attribute[str])
    rebar_class: Attribute[str] = Field(default_factory=Attribute[str])
    deoxidation: Attribute[str] = Field(default_factory=Attribute[str])
    supply_group: Attribute[str] = Field(default_factory=Attribute[str])
    category: Attribute[str] = Field(default_factory=Attribute[str])
    # A literal suffix is preserved even when its normative meaning is unknown.
    category_kind: Attribute[str] = Field(default_factory=Attribute[str])


class NormativeDocument(ContractModel):
    raw_designation: NonEmpty
    document_type: NonEmpty
    number: NonEmpty
    edition: Attribute[str] = Field(default_factory=Attribute[str])
    role: Attribute[str] = Field(default_factory=Attribute[str])
    provenance: list[Provenance] = Field(min_length=1)


class ExecutionSpec(ContractModel):
    rolling_method: Attribute[str] = Field(default_factory=Attribute[str])
    manufacturing_method: Attribute[str] = Field(default_factory=Attribute[str])
    coating: Attribute[str] = Field(default_factory=Attribute[str])
    supply_form: Attribute[str] = Field(default_factory=Attribute[str])


class NormalizationContextRef(ContractModel):
    context_id: NonEmpty
    request_id: NonEmpty | None = None
    normalization_version: NonEmpty
    rule_set_version: NonEmpty | None = None


class NomenclatureSpec(ContractModel):
    schema_version: Literal['nomenclature.v1'] = 'nomenclature.v1'
    record_id: NonEmpty
    source: SourceRef
    original_description: str = Field(min_length=1)
    context: NormalizationContextRef | None = None
    category: Attribute[str] = Field(default_factory=Attribute[str])
    product_type: Attribute[str] = Field(default_factory=Attribute[str])
    profile: ProfileSpec = Field(default_factory=ProfileSpec)
    geometry: dict[str, Attribute[Measurement]] = Field(default_factory=dict)
    material: MaterialSpec = Field(default_factory=MaterialSpec)
    normative_documents: Attribute[list[NormativeDocument]] = Field(default_factory=lambda: Attribute[list[NormativeDocument]]())
    execution: ExecutionSpec = Field(default_factory=ExecutionSpec)
    # Quantity is a separate observation, not an identity field/key.
    quantity: Attribute[Measurement] = Field(default_factory=Attribute[Measurement])
    extensions: dict[str, Attribute[str]] = Field(default_factory=dict)
    unparsed_features: list[str] = Field(default_factory=list)

    @model_validator(mode='after')
    def nonblank_description(self):
        if not self.original_description.strip():
            raise ValueError('The original description cannot be blank.')
        return self
