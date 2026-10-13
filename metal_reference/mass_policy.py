"""Documented calculation assumption, not a normative reference-property record.

See docs/METAL_REFERENCE.md#расчётное-допущение-для-листа-ст3.
No density from another grade or unverified website default is reused here.
"""
from decimal import Decimal

SHEET_ST3_DENSITY_KG_M3 = Decimal('7850')
SHEET_ST3_ASSUMPTION = 'sheet_st3_rectangular_density_7850_v1'
SHEET_ST3_BASIS = 'docs/METAL_REFERENCE.md#расчётное-допущение-для-листа-ст3'
