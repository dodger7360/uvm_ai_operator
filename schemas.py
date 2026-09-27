from typing import List, Optional
from pydantic import BaseModel, Field


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

    quantity: Optional[float] = None
    unit: Optional[str] = None
    source_unit: Optional[str] = None

    form: Optional[str] = None
    gost_tu: Optional[str] = None
    processing: Optional[str] = None

    source_text: Optional[str] = None
    interpreted_fields: List[str] = Field(default_factory=list)


class PurchaseRequestExtraction(BaseModel):
    """Facts extracted from the user's text; no Python validation output."""

    items: List[RequestItem] = Field(default_factory=list)

    city: Optional[str] = None
    delivery: Optional[str] = None
    deadline: Optional[str] = None

    customer: Optional[str] = None
    comment: Optional[str] = None


class PurchaseRequest(PurchaseRequestExtraction):
    """Extracted request enriched with Python-owned validation results."""

    missing_fields: List[str] = Field(default_factory=list)
    clarification_questions: List[str] = Field(default_factory=list)

    warnings: List[str] = Field(default_factory=list)
    is_valid: bool = False
