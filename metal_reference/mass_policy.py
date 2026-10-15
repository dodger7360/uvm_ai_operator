"""Documented calculation assumption, not a normative reference-property record.

See docs/METAL_REFERENCE.md#расчётное-допущение-для-листа-ст3.
No density from another grade or unverified website default is reused here.
"""
from decimal import Decimal

SHEET_ST3_DENSITY_KG_M3 = Decimal('7850')
SHEET_ST3_ASSUMPTION = 'sheet_st3_rectangular_density_7850_v1'
SHEET_ST3_BASIS = 'docs/METAL_REFERENCE.md#расчётное-допущение-для-листа-ст3'


# Reviewed source: official edition of GOST 5781-82 (Standartinform, 2006),
# table 1, printed p.1 / PDF p.2. Not a fixture, supplier default or global alias.
# This deliberately narrow coefficient does not assert applicability to a batch.
REBAR_A240_12_KG_M = Decimal('0.888')
REBAR_A240_12_BASIS = ('ГОСТ 5781-82, таблица 1: номинальная теоретическая масса Ø12 — '
    '0,888 кг/м. Справочный расчёт; применимость стандарта к поставке не подтверждена. '
    'https://files.stroyinf.ru/Data2/1/4294852/4294852762.pdf#page=2')
