import unittest

from formatter import format_item
from normalizer import normalize_request
from schemas import PurchaseRequest, RequestItem


def format_normalized_item(**kwargs):
    item = normalize_request(PurchaseRequest(items=[RequestItem(**kwargs)])).items[0]
    return format_item(item, 1)


class FormatterTests(unittest.TestCase):
    def test_formats_real_supplier_positions_without_invented_details(self):
        self.assertEqual(format_normalized_item(
            product="труба", size_designation="40*40*4,0", quantity=0.162, unit="тн",
        ), "1. Труба 40×40×4 — 0,162 т")

        self.assertEqual(format_normalized_item(
            product="балка", size_designation="25К1", grade="ст255", quantity=2.013, unit="тн",
        ), "1. Балка 25К1 Ст255 — 2,013 т")

        self.assertEqual(format_normalized_item(
            product="лист", size_designation="6 1500*6000", grade="ст3", quantity=2, unit="шт",
        ), "1. Лист 6×1500×6000 Ст3 — 2 шт")

    def test_formats_round_pipe_and_rebar_with_diameter_symbol(self):
        self.assertEqual(format_normalized_item(
            product="труба", size_designation="57×3,5", quantity=1.2, unit="т",
        ), "1. Труба Ø57×3,5 — 1,2 т")

        self.assertEqual(format_normalized_item(
            product="арматура", grade="А1", size_designation="20", quantity=0.115, unit="т",
        ), "1. Арматура А1 Ø20 — 0,115 т")
