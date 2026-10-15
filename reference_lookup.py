"""Read-side candidate contracts, separate from technical recognition/identity."""
from typing import Literal, Protocol

from pydantic import Field

from nomenclature import ContractModel, NomenclatureSpec, NonEmpty, NormalizationContextRef, Provenance


class ReferenceLookupContext(ContractModel):
    # Explicit MetalReference source ID, not inferred from a supplier name.
    reference_source_id: int | None = Field(default=None, gt=0)
    category_codes: list[NonEmpty] = Field(default_factory=list)
    normalization: NormalizationContextRef | None = None
    max_candidates: int = Field(default=100, ge=1, le=1000)


class ReferenceEntry(ContractModel):
    """Existing row and provenance, not another product/identity model.

    REAL values are rendered as strings, preserving their stored representation
    rather than claiming restored Decimal precision or normative authority.
    """
    table: NonEmpty
    row_id: int
    fields: dict[str, str | int | None]
    provenance: Provenance


class ReferenceEvidence(ContractModel):
    kind: Literal['designation', 'alias', 'category_name', 'external_id']
    query: str
    explanation: str
    alias_role: Literal['alternative_spelling', 'abbreviation', 'supplier_designation',
                        'source_identifier', 'commercial_label', 'unclassified'] | None = None
    alias: ReferenceEntry | None = None
    provenance: Provenance


class ReferenceCandidate(ContractModel):
    item: ReferenceEntry
    category: ReferenceEntry
    family: ReferenceEntry
    variant: ReferenceEntry | None = None
    standard: ReferenceEntry | None = None
    dimensions: list[ReferenceEntry] = Field(default_factory=list)
    properties: list[ReferenceEntry] = Field(default_factory=list)
    sources: list[ReferenceEntry] = Field(default_factory=list)
    evidence: list[ReferenceEvidence] = Field(min_length=1)
    knowledge_layer: Literal['metal_reference'] = 'metal_reference'
    technical_assessment: Literal['not_assessed'] = 'not_assessed'


class ReferenceCandidates(ContractModel):
    record_id: NonEmpty
    status: Literal['candidates', 'no_candidates', 'empty_reference', 'unavailable']
    candidates: list[ReferenceCandidate] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    truncated: bool = False


class ReferenceReader(Protocol):
    def find_candidates(self, specs: list[NomenclatureSpec],
                        context: ReferenceLookupContext | None = None) -> list[ReferenceCandidates]:
        """One result per source record. Never modifies specs or applies knowledge."""
        ...
