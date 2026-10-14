"""Contracts for future contextual experience; no memory store or rule executor."""
from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from nomenclature import (Attribute, ContractModel, NomenclatureSpec, NonEmpty,
                          NormalizationContextRef, SourceRef)


class KnowledgeScope(ContractModel):
    kind: Literal['request', 'product_type', 'general']
    request_id: NonEmpty | None = None
    product_type: NonEmpty | None = None
    conditions: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode='after')
    def explicit_scope(self):
        if self.kind == 'request' and (self.request_id is None or self.product_type is not None):
            raise ValueError('Request scope needs only its request ID.')
        if self.kind == 'product_type' and (self.product_type is None or self.request_id is not None):
            raise ValueError('Product scope needs an explicit product type, not a request ID.')
        if self.kind == 'general' and (self.request_id is not None or self.product_type is not None):
            raise ValueError('General scope must be explicitly selected without a hidden narrower scope.')
        return self


class RuleVersion(ContractModel):
    rule_id: NonEmpty
    version: NonEmpty


class UserConfirmation(ContractModel):
    status: Literal['pending', 'confirmed', 'rejected'] = 'pending'
    actor_id: NonEmpty | None = None
    confirmed_at: datetime | None = None
    evidence: str | None = None

    @model_validator(mode='after')
    def confirmed_has_receipt(self):
        if self.status == 'confirmed':
            if self.actor_id is None or self.confirmed_at is None or not self.evidence or not self.evidence.strip():
                raise ValueError('Confirmation requires an actor, timestamp and evidence.')
            if self.confirmed_at.utcoffset() is None:
                raise ValueError('Confirmation time must include a timezone.')
        return self


InterpretationLayer = Literal['technical_requirement', 'recognized_attribute',
                              'normalized_spelling', 'commercial_group', 'analog_candidate']


class CorrectionRecord(ContractModel):
    correction_id: NonEmpty
    context_id: NonEmpty
    request_id: NonEmpty | None = None
    before: NomenclatureSpec
    after: NomenclatureSpec
    original_error: NonEmpty
    reason: NonEmpty
    changed_attributes: list[NonEmpty] = Field(min_length=1)
    layer: InterpretationLayer
    scope: KnowledgeScope
    knowledge_source: SourceRef
    confirmation: UserConfirmation = Field(default_factory=UserConfirmation)
    rule: RuleVersion

    @model_validator(mode='after')
    def snapshots_belong_to_context(self):
        if self.before.record_id != self.after.record_id:
            raise ValueError('A correction cannot silently target another source record.')
        for snapshot in (self.before, self.after):
            if snapshot.context and (snapshot.context.context_id != self.context_id
                                     or snapshot.context.request_id != self.request_id):
                raise ValueError('Correction and snapshot contexts differ.')
        if self.scope.kind == 'request' and self.scope.request_id != self.request_id:
            raise ValueError('A request correction cannot apply to another request.')
        return self


class ConfirmedExperience(ContractModel):
    """Explicit knowledge reference, not automatically inherited correction state."""
    correction_id: NonEmpty
    scope: KnowledgeScope
    layer: InterpretationLayer
    knowledge_source: SourceRef
    rule: RuleVersion
    confirmation: UserConfirmation

    @model_validator(mode='after')
    def only_confirmed(self):
        if self.confirmation.status != 'confirmed':
            raise ValueError('Unconfirmed/rejected experience cannot be reused as confirmed knowledge.')
        return self


class WorkingContext(NormalizationContextRef):
    corrections: list[CorrectionRecord] = Field(default_factory=list)
    confirmed_experience: list[ConfirmedExperience] = Field(default_factory=list)

    @model_validator(mode='after')
    def isolate_requests(self):
        for correction in self.corrections:
            if correction.context_id != self.context_id or correction.request_id != self.request_id:
                raise ValueError('A new working context cannot inherit another request\'s corrections.')
        for experience in self.confirmed_experience:
            if experience.scope.kind == 'request' and experience.scope.request_id != self.request_id:
                raise ValueError('Request-scoped knowledge cannot be silently reused for another request.')
        return self


class ContextualInterpretation(ContractModel):
    """Display grouping / candidate analog is separate from technical facts."""
    source_record_id: NonEmpty
    layer: Literal['commercial_group', 'analog_candidate']
    label: Attribute[str]
    scope: KnowledgeScope
    reason: NonEmpty
    # These two layers alone cannot confer technical interchangeability.
    technical_assessment: Literal['not_assessed'] = 'not_assessed'
