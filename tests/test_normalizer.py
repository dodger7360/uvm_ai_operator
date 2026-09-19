import unittest

from normalizer import normalize_request
from schemas import PurchaseRequest, RequestItem


def normalize_item(**kwargs):
    request = PurchaseRequest(items=[RequestItem(**kwargs)])
    return normalize_request(request).items[0]


class NormalizerTests(unittest.TestCase):
    def test_normalizes_profile_pipe_dimensions_and_tonnes(self):
        item = normalize_item(
            product="труба",
            size_designation="40*40*4,0",
            quantity=0.162,
            unit="тн",
            source_text="труба 40*40*4,0 - 0,162 тн",
        )

        self.assertEqual(item.product_group, "profile_pipe")
        self.assertEqual(item.size_designation, "40×40×4")
        self.assertEqual((item.width_mm, item.height_mm, item.wall_thickness_mm), (40, 40, 4))
        self.assertIsNone(item.diameter_mm)
        self.assertEqual(item.unit, "т")
        self.assertEqual(item.source_unit, "тн")

    def test_normalizes_standard_designations_without_inventing_data(self):
        beam = normalize_item(product="балка", size_designation="25к1", grade="ст255")
        channel = normalize_item(product="швеллер", size_designation="12п")
        expanded_metal = normalize_item(product="лист пвл", size_designation="506")

        self.assertEqual((beam.product_group, beam.size_designation, beam.grade), ("beam", "25К1", "Ст255"))
        self.assertEqual((channel.product_group, channel.size_designation), ("channel", "12П"))
        self.assertEqual((expanded_metal.product_group, expanded_metal.size_designation), ("expanded_metal", "506"))
        self.assertIsNone(beam.gost_tu)
        self.assertIsNone(beam.length_mm)

    def test_normalizes_sheet_angle_and_rebar_dimensions(self):
        sheet = normalize_item(product="лист", size_designation="6 1500*6000", grade="ст3")
        angle = normalize_item(product="уголок", size_designation="90х90х6")
        rebar = normalize_item(product="арматура", grade="а1", size_designation="20")

        self.assertEqual((sheet.thickness_mm, sheet.width_mm, sheet.length_mm), (6, 1500, 6000))
        self.assertEqual(sheet.size_designation, "6×1500×6000")
        self.assertEqual(sheet.grade, "Ст3")
        self.assertEqual((angle.side_a_mm, angle.side_b_mm, angle.thickness_mm), (90, 90, 6))
        self.assertEqual(angle.size_designation, "90×90×6")
        self.assertEqual(rebar.diameter_mm, 20)
        self.assertEqual(rebar.grade, "А1")
