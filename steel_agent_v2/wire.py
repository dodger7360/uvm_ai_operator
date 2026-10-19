"""Responses schema adapter; all Python validation remains in AgentPlan.

Non-null Pydantic defaults survive the SDK strict-schema conversion. They are
not part of our Structured Outputs profile. Quantities travel as decimal strings
instead of JSON numbers, which could become floats before ExactDecimal sees them.
"""
from typing import Literal
from pydantic import Field
from nomenclature import ContractModel
from .contracts import AgentPlan, Extraction, DisplayLayout, MassSpec


def _without_defaults(node):
    if isinstance(node, dict):
        node.pop('default', None)
        for child in node.values():
            _without_defaults(child)
    elif isinstance(node, list):
        for child in node:
            _without_defaults(child)


class WireAgentPlan(AgentPlan):
    """Same fields and validators, with an API-specific schema representation."""

    @classmethod
    def model_json_schema(cls, *args, **kwargs):
        schema=super().model_json_schema(*args, **kwargs)
        _without_defaults(schema)
        quantity=schema['$defs']['Item']['properties']['quantity']
        # Do not expose Pydantic's numeric branch or its Decimal lookahead regex.
        # Finite/nonnegative/precision checks still run in the inherited model.
        quantity.clear()
        quantity.update(anyOf=[{'type':'string'}, {'type':'null'}],
                        description='Exact decimal quantity as a string; null if unknown.')
        return schema

    def to_internal(self):
        # Rebuild the original contract explicitly, retaining Decimal objects.
        return AgentPlan.model_validate(self.model_dump())


class WireCreatePlan(ContractModel):
    """Only the five fields used by creation, not empty edit/search machinery."""
    intent: Literal['create_request']
    extraction: Extraction
    pending_event_id: str | None = None
    display_layout: DisplayLayout | None = None
    mass_specs: list[MassSpec] = Field(default_factory=list)

    def to_internal(self):
        return AgentPlan.model_validate(self.model_dump())


class WireActionPlan(WireAgentPlan):
    intent: Literal['append_request','close_request','edit_request','show_request',
                    'answer_question','web_search','free_chat','clarify']


class WireCoordinatorPlan(ContractModel):
    # Nested anyOf, object root: one response by the same coordinator, no router
    # or second call. Internal snapshots and the full mutation validator stay put.
    plan: WireCreatePlan | WireActionPlan

    @classmethod
    def model_json_schema(cls,*args,**kwargs):
        schema=super().model_json_schema(*args,**kwargs)
        _without_defaults(schema)
        quantity=schema['$defs']['Item']['properties']['quantity']
        quantity.clear()
        quantity.update(anyOf=[{'type':'string'},{'type':'null'}],
                        description='Exact decimal quantity as a string; null if unknown.')
        return schema

    def to_internal(self):
        return self.plan.to_internal()
