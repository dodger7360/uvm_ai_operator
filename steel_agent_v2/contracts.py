"""Small GPT wire contracts. No steel equivalence rules or legacy recognizer."""
from typing import Literal
from pydantic import Field, model_validator
from nomenclature import ContractModel, ExactDecimal, NonEmpty


class SourceRecord(ContractModel):
    id: NonEmpty
    kind: Literal['product', 'header', 'note', 'unresolved']
    start: int = Field(ge=0)
    end: int = Field(gt=0)


class Characteristic(ContractModel):
    name: NonEmpty
    value: NonEmpty | None = None
    raw: str | None = None
    source_ids: list[str] = Field(default_factory=list)


class Item(ContractModel):
    id: NonEmpty
    source_id: NonEmpty
    label: NonEmpty
    characteristics: list[Characteristic] = Field(default_factory=list)
    quantity: ExactDecimal | None = None
    unit: NonEmpty | None = None
    quantity_text: str | None = None
    unit_text: str | None = None

    @model_validator(mode='after')
    def finite_nonnegative(self):
        if self.quantity is not None and self.quantity < 0:
            raise ValueError('quantity must be nonnegative')
        return self


class CommercialGroup(ContractModel):
    id: NonEmpty
    label: NonEmpty
    item_ids: list[str]
    rationale: NonEmpty
    differences: list[str] = Field(default_factory=list)


class Extraction(ContractModel):
    records: list[SourceRecord]
    items: list[Item]
    groups: list[CommercialGroup]
    questions: list[str] = Field(default_factory=list)


class Operation(ContractModel):
    kind: Literal['set', 'regroup', 'undo']
    target_id: str | None = None
    field: Literal['quantity', 'unit', 'label', 'characteristic'] | None = None
    name: str | None = None
    value: str | None = None
    evidence: str | None = None
    groups: list[CommercialGroup] = Field(default_factory=list)


class AgentPlan(ContractModel):
    intent: Literal['create_request', 'edit_request', 'show_request', 'answer_question',
                    'web_search', 'free_chat', 'clarify']
    expected_request_id: str | None = None
    expected_revision: int | None = None
    extraction: Extraction | None = None
    operations: list[Operation] = Field(default_factory=list)
    answer: str = ''
    view: Literal['normalized', 'original', 'totals', 'explanation',
                  'forward', 'custom', 'search_status'] = 'normalized'
    search_query: str | None = None
    remaining_questions: list[str] | None = None

    @model_validator(mode='after')
    def only_intended_mutation(self):
        if self.view in {'forward','custom','search_status'}:
            if self.intent not in {'show_request','answer_question'}:
                raise ValueError('readonly presentation requires show_request or answer_question')
            if self.view=='custom' and not self.answer.strip():
                raise ValueError('custom presentation requires answer')
        if self.intent == 'create_request':
            if self.extraction is None or self.operations:
                raise ValueError('create_request requires extraction and no operations')
        elif self.extraction is not None:
            raise ValueError('only create_request may carry extraction')
        if self.intent == 'edit_request':
            if not self.operations or self.expected_request_id is None or self.expected_revision is None:
                raise ValueError('edit requires operations and exact request revision')
        elif self.operations:
            raise ValueError('discussion/search/show cannot carry operations')
        if self.intent != 'edit_request' and self.remaining_questions is not None:
            raise ValueError('only edits may update request questions')
        if self.intent == 'web_search':
            if not self.search_query or not self.search_query.strip():
                raise ValueError('web_search requires a query')
        elif self.search_query is not None:
            raise ValueError('search_query belongs only to web_search')
        return self


class Change(ContractModel):
    item_id: str
    field: str
    before: str | None
    after: str | None
    evidence: str
    message: str


class RequestSnapshot(ContractModel):
    id: str
    revision: int = Field(ge=1)
    original_text: str
    records: list[SourceRecord]
    originals: list[Item]
    items: list[Item]
    groups: list[CommercialGroup]
    questions: list[str] = Field(default_factory=list)
    changes: list[Change] = Field(default_factory=list)
    undo_stack: list[int] = Field(default_factory=list)


class SourceLink(ContractModel):
    url: str
    title: str


class Reply(ContractModel):
    text: str
    status: Literal['ok', 'partial', 'failed', 'busy']
    intent: str
    request_id: str | None = None
    revision: int | None = None
    sources: list[SourceLink] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)
    calls: int = 0
