from decimal import Decimal
from typing import Annotated, List, Literal, Optional
from pydantic import BaseModel, Field, WithJsonSchema, BeforeValidator


class RequestItem(BaseModel):
    product: Optional[str] = None
    product_group: Optional[str] = None
    size_designation: Optional[str] = None
    grade: Optional[str] = None

    diameter_mm: Optional[float] = None
    thickness_mm: Optional[float] = None
    width_mm: Optional[float] = None
    length_mm: Optional[float] = None
    height_mm: Optional[float] = None
    side_a_mm: Optional[float] = None
    side_b_mm: Optional[float] = None
    wall_thickness_mm: Optional[float] = None

    length_type: Optional[Literal["measured", "unmeasured", "other"]] = None
    length_requirement: Optional[str] = None
    wall_thickness_requirement: Optional[str] = None

    quantity: Optional[Annotated[Decimal, BeforeValidator(lambda value: value.replace(",", ".") if isinstance(value, str) else value), WithJsonSchema({"type": "string"}, mode="validation")]] = None
    unit: Optional[str] = None
    source_unit: Optional[str] = None

    form: Optional[str] = None
    gost_tu: Optional[str] = None
    processing: Optional[str] = None

    source_text: Optional[str] = None
    interpreted_fields: List[str] = Field(default_factory=list)


class SharedAttributeEvidence(BaseModel):
    """A request-level attribute and its explicit target positions."""

    attribute: Literal["grade"]
    value: str
    source_text: str
    applies_to: List[int] = Field(default_factory=list)


class PurchaseRequestExtraction(BaseModel):
    """Facts extracted from the user's text; no Python validation output."""

    items: List[RequestItem] = Field(default_factory=list)

    city: Optional[str] = None
    delivery: Optional[str] = None
    deadline: Optional[str] = None

    customer: Optional[str] = None
    comment: Optional[str] = None
    shared_attributes: List[SharedAttributeEvidence] = Field(default_factory=list)


class PurchaseRequest(PurchaseRequestExtraction):
    """Extracted request enriched with Python-owned validation results."""

    missing_fields: List[str] = Field(default_factory=list)
    clarification_questions: List[str] = Field(default_factory=list)

    warnings: List[str] = Field(default_factory=list)
    is_valid: bool = False
    confirmed_shared_attributes: List[SharedAttributeEvidence] = Field(default_factory=list)
