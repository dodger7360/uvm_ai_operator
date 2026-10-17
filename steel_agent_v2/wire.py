"""Responses schema adapter; all Python validation remains in AgentPlan.

Non-null Pydantic defaults survive the SDK strict-schema conversion. They are
not part of our Structured Outputs profile. Quantities travel as decimal strings
instead of JSON numbers, which could become floats before ExactDecimal sees them.
"""
from .contracts import AgentPlan


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
