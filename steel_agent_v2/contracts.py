"""Small GPT wire contracts. No steel equivalence rules or legacy recognizer."""
from typing import Literal, Annotated
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
    unit_source_id: NonEmpty | None = Field(default=None, exclude_if=lambda value: value is None)

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


class Clarification(ContractModel):
    """A proposed value and its candidate addressees, not authorization to edit."""
    target_ids: list[NonEmpty] = Field(min_length=1)
    field: Literal['quantity', 'unit', 'characteristic']
    name: str | None = None
    value: NonEmpty
    evidence: NonEmpty
    alternatives: list[NonEmpty] = Field(min_length=1)


class SelectionPredicate(ContractModel):
    field: Literal['label', 'characteristic']
    name: str | None = None
    value: NonEmpty
    match: Literal['equals', 'token', 'component']
    separator: str | None = None
    component: Annotated[int, Field(strict=True, ge=1)] | None = None

    @model_validator(mode='after')
    def named_characteristic(self):
        if (self.field=='characteristic')!=bool(self.name):
            raise ValueError('name belongs only to a characteristic predicate')
        if self.match=='component':
            if self.field!='characteristic' or not self.separator or self.component is None:
                raise ValueError('component requires a characteristic, separator and positive component')
        elif self.separator is not None or self.component is not None:
            raise ValueError('component parameters belong only to component matching')
        if self.match=='token' and (self.field!='label' or not any(c.isalpha() for c in self.value)):
            raise ValueError('token matching is for product labels; dimensions require exact fields or components')
        return self


class Selection(ContractModel):
    mode: Literal['display', 'where', 'all']
    positions: list[Annotated[int, Field(strict=True, ge=1)]] = Field(default_factory=list)
    predicates: list[SelectionPredicate] = Field(default_factory=list)
    cardinality: Literal['one', 'all']
    evidence: NonEmpty

    @model_validator(mode='after')
    def shape(self):
        if self.mode=='display':
            if not self.positions or self.predicates or any(n<=0 for n in self.positions):
                raise ValueError('display selection requires positive positions only')
            if len(set(self.positions))!=len(self.positions):
                raise ValueError('duplicate display position')
        elif self.positions or (self.mode=='where')!=bool(self.predicates):
            raise ValueError('where requires predicates; all carries neither predicates nor positions')
        return self


class Operation(ContractModel):
    kind: Literal['set', 'regroup', 'undo', 'resolve', 'convert_mass',
                  'set_quantity', 'add_quantity', 'set_length', 'calculate_mass']
    target_id: str | None = None
    selection: Selection | None = None
    field: Literal['quantity', 'unit', 'label', 'characteristic'] | None = None
    name: str | None = None
    value: str | None = None
    evidence: str | None = None
    groups: list[CommercialGroup] = Field(default_factory=list)
    resolution: Extraction | None = None
    replace_from: str | None = None
    unit: str | None = None
    unit_evidence: str | None = None

    @model_validator(mode='after')
    def resolution_only(self):
        if self.selection is not None and (self.target_id is not None or self.kind in {'undo','regroup','resolve'}):
            raise ValueError('selection replaces target_id only for item operations')
        if self.kind not in {'set_quantity','add_quantity','set_length'} and (self.unit is not None or self.unit_evidence is not None):
            raise ValueError('unit fields belong only to measured operations')
        if self.replace_from is not None and (self.kind != 'set' or self.field != 'characteristic'):
            raise ValueError('literal replacement belongs only to a characteristic set')
        if (self.kind == 'resolve') != (self.resolution is not None):
            raise ValueError('resolve requires resolution; other operations cannot carry it')
        return self


class PendingClarification(ContractModel):
    event_id: str
    request_id: str
    revision: int
    proposal: Clarification
    source_message: str
    question: str
    operation: Operation | None = Field(default=None, exclude_if=lambda value: value is None)


class DisplayDimension(ContractModel):
    """A semantic role supplied by GPT, pointing at a current fact, not a new fact."""
    role: NonEmpty
    name: NonEmpty
    unit: Literal['мм', 'см', 'м'] | None = None
    separator: str | None = None
    component: Annotated[int, Field(strict=True, ge=1)] | None = None

    @model_validator(mode='after')
    def component_reference(self):
        if bool(self.separator) != (self.component is not None):
            raise ValueError('dimension component requires a nonempty separator and index')
        return self


class DisplayItem(ContractModel):
    item_id: NonEmpty
    kind: NonEmpty | None = None
    grade_name: NonEmpty | None = None
    dimensions: list[DisplayDimension] = Field(default_factory=list)


class DisplayGradeOrder(ContractModel):
    kind: NonEmpty
    grades: list[NonEmpty]


class DisplayLayout(ContractModel):
    """Presentation hints, separate from commercial groups and saved product facts."""
    items: list[DisplayItem]
    grade_order: list[DisplayGradeOrder] = Field(default_factory=list)


class DisplayOrder(ContractModel):
    request_id: str
    revision: int
    item_ids: list[str]
    layout: DisplayLayout | None = Field(default=None, exclude_if=lambda value: value is None)


class MassSpec(ContractModel):
    """Semantic references to current facts; never numeric arithmetic from GPT."""
    item_id: NonEmpty
    kind: Literal['solid_carbon_sheet','round_welded_pipe','ibeam','linear','fixed_unit','other']
    family: NonEmpty | None = None
    dimensions: list[DisplayDimension] = Field(default_factory=list)
    profile_name: str | None = None
    standard_name: str | None = None
    grade_name: str | None = None
    grade_policy: Literal['uvm_beam_commercial'] | None = None
    execution_name: str | None = None
    cutting: Literal['none','specified','unknown','confirmed_piece_length']
    evidence: NonEmpty


class MassCandidate(ContractModel):
    item_id: NonEmpty
    value: NonEmpty
    unit: Literal['kg_per_m','kg_per_piece']
    source_url: NonEmpty
    quote: NonEmpty
    profile_evidence: NonEmpty
    standard_evidence: str | None = None
    piece_length_evidence: str | None = None
    execution_evidence: str | None = None
    suitability: Literal['applicable','uncertain','conflicting']


class AgentPlan(ContractModel):
    intent: Literal['create_request', 'append_request', 'close_request', 'edit_request', 'show_request', 'answer_question',
                    'web_search', 'free_chat', 'clarify']
    expected_request_id: str | None = None
    expected_revision: int | None = None
    extraction: Extraction | None = None
    pending_event_id: str | None = None
    awaiting_new_request: bool = False
    operations: list[Operation] = Field(default_factory=list)
    answer: str = ''
    view: Literal['normalized', 'original', 'totals', 'explanation',
                  'forward', 'custom', 'search_status', 'sources'] = 'normalized'
    search_query: str | None = None
    remaining_questions: list[str] | None = None
    clarification: Clarification | None = None
    clarification_event_id: str | None = None
    display_layout: DisplayLayout | None = None
    mass_specs: list[MassSpec] = Field(default_factory=list)
    mass_candidates: list[MassCandidate] = Field(default_factory=list)
    mass_confirmation_event_id: str | None = None
    mass_approval_ids: list[str] = Field(default_factory=list)

    @model_validator(mode='after')
    def only_intended_mutation(self):
        if self.awaiting_new_request and self.intent!='clarify':
            raise ValueError('new source confirmation belongs only to clarify')
        if self.clarification is not None and self.intent != 'clarify':
            raise ValueError('clarification proposal belongs only to clarify')
        if self.clarification_event_id is not None and self.intent != 'edit_request':
            raise ValueError('clarification reference belongs only to edit_request')
        if self.pending_event_id is not None and self.intent != 'create_request':
            raise ValueError('pending source belongs only to explicit create_request recovery')
        if self.view in {'forward','custom','search_status','sources'}:
            if self.intent not in {'show_request','answer_question'}:
                raise ValueError('readonly presentation requires show_request or answer_question')
            if self.view=='custom' and not self.answer.strip():
                raise ValueError('custom presentation requires answer')
        if self.intent in {'create_request','append_request'}:
            if self.extraction is None or self.operations:
                raise ValueError('create/append requires extraction and no operations')
        elif self.extraction is not None:
            raise ValueError('only create/append may carry extraction')
        if self.intent=='append_request' and (self.expected_request_id is None or self.expected_revision is None):
            raise ValueError('append requires exact request revision')
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
        elif self.search_query is not None and not (self.intent=='edit_request' and self.operations
                and all(op.kind=='calculate_mass' for op in self.operations)):
            raise ValueError('search_query belongs only to web_search')
        if self.mass_specs and self.intent not in {'create_request','append_request','edit_request'}:
            raise ValueError('mass specs belong to request mutations')
        if self.mass_candidates and self.intent!='answer_question':
            raise ValueError('mass research cannot carry mutations')
        if bool(self.mass_confirmation_event_id)!=bool(self.mass_approval_ids):
            raise ValueError('mass confirmation requires event and chosen candidate IDs')
        if self.mass_confirmation_event_id and (self.intent!='edit_request' or self.search_query
                or any(op.kind!='calculate_mass' for op in self.operations)):
            raise ValueError('mass confirmation is a separate mass-only edit')
        if self.operations and all(op.kind=='calculate_mass' for op in self.operations) and self.remaining_questions is not None:
            raise ValueError('mass-only edit cannot clear product questions')
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
    theoretical_mass: list['TheoreticalMass'] = Field(default_factory=list,exclude_if=lambda value:not value)


class SourceCitation(ContractModel):
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    text: str


class SourceLink(ContractModel):
    url: str
    title: str
    origins: list[Literal['search','open_page','find_in_page','url_citation']] = Field(default_factory=list,exclude_if=lambda value:not value)
    titles: list[str] = Field(default_factory=list,exclude_if=lambda value:not value)
    citations: list[SourceCitation] = Field(default_factory=list,exclude_if=lambda value:not value)


class MassReference(ContractModel):
    id: NonEmpty
    revision: NonEmpty
    kind: NonEmpty
    family: NonEmpty | None = None
    profile: str | None = None
    piece_length_mm: str | None = None
    dimensions_mm: list[str] = Field(default_factory=list)
    dimension_roles: list[str] = Field(default_factory=list)
    standards: list[str] = Field(default_factory=list)
    unspecified_standard_only: bool = False
    grades: list[str] = Field(default_factory=list)
    value: NonEmpty
    unit: Literal['kg_per_m','kg_per_piece','kg_per_m3']
    status: Literal['reviewed','owner_confirmed','owner_indicative','observed']
    approval_scope: str | None = None
    execution: str | None = None
    method: NonEmpty
    sources: list[SourceLink] = Field(min_length=1)
    checked_at: NonEmpty
    limitation: NonEmpty


class TheoreticalMass(ContractModel):
    spec: MassSpec
    classification: NonEmpty
    status: Literal['calculated','unavailable','conflicting']
    tonnes: ExactDecimal | None = None
    reference: MassReference | None = None
    dimensions_mm: list[str] = Field(default_factory=list)
    method: NonEmpty
    reason: str | None = None
    piece_length_mm: ExactDecimal | None = Field(default=None,exclude_if=lambda value:value is None)
    length_origin: Literal['user','uvm_default'] | None = Field(default=None,exclude_if=lambda value:value is None)
    source_grade: str | None = Field(default=None,exclude_if=lambda value:value is None)
    grade_policy: Literal['uvm_beam_commercial'] | None = Field(default=None,exclude_if=lambda value:value is None)


class PendingMass(ContractModel):
    event_id: NonEmpty
    request_id: NonEmpty
    revision: int
    candidates: list[MassReference]
    specs: list[MassSpec]
    item_ids: list[str]
    classifications: list[str]
    requested_specs: list[MassSpec] = Field(default_factory=list)


RequestSnapshot.model_rebuild()


class PresentationRun(ContractModel):
    """Application-owned visible text and allowlisted marks, never raw HTML."""
    text: str
    marks: list[Literal['b', 'i', 'code', 'pre']] = Field(default_factory=list)
    url: str | None = None


class KnowledgeContext(ContractModel):
    """Knowledge supplied to GPT, not a claim that every rule was applied."""
    version: NonEmpty
    provided_ids: list[str]
    sources: dict[str,str]


class Reply(ContractModel):
    text: str
    status: Literal['ok', 'partial', 'failed', 'busy']
    intent: str
    request_id: str | None = None
    revision: int | None = None
    sources: list[SourceLink] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)
    calls: int = 0
    pending_snapshot: RequestSnapshot | None = Field(default=None, exclude_if=lambda value: value is None)
    clarification: PendingClarification | None = Field(default=None, exclude_if=lambda value: value is None)
    display: DisplayOrder | None = Field(default=None, exclude_if=lambda value: value is None)
    presentation: list[PresentationRun] | None = Field(default=None, exclude_if=lambda value: value is None)
    search_query: str | None = Field(default=None, exclude_if=lambda value: value is None)
    search_text: str | None = Field(default=None, exclude_if=lambda value: value is None)
    source_event_id: str | None = Field(default=None, exclude_if=lambda value: value is None)
    search_providers: list[str] = Field(default_factory=list,exclude_if=lambda value:not value)
    pending_mass: PendingMass | None = Field(default=None,exclude_if=lambda value:value is None)
    knowledge: KnowledgeContext | None = Field(default=None,exclude_if=lambda value:value is None)
