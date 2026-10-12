import unittest

from normalizer import normalize_request
from schemas import PurchaseRequest, RequestItem


def normalize_item(**kwargs):
    request = PurchaseRequest(items=[RequestItem(**kwargs)])
    return normalize_request(request).items[0]


class NormalizerTests(unittest.TestCase):
    def test_preserves_three_problem_positions_and_is_idempotent(self):
        request = PurchaseRequest(items=[
            RequestItem(
                product="Арматура", product_group="rebar", size_designation="12",
                grade="А500С", length_requirement="11,7", quantity=20, unit="т",
                source_text="Арматура 12 А500С 11,7 — 20 т",
            ),
            RequestItem(
                product="Труба профильная", product_group="profile_pipe",
                size_designation="40×40", width_mm=40, height_mm=40,
                wall_thickness_requirement="стенка минимально возможная",
                quantity=100, unit="м",
                source_text="Труба профильная 40×40, стенка минимально возможная — 100 м",
            ),
            RequestItem(
                product="Труба", product_group="round_pipe",
                size_designation="76×3,5 НД", length_type="unmeasured",
                length_requirement="НД", quantity=4, unit="т",
                source_text="Труба 76×3,5 НД — 4 т",
            ),
        ])
        original_sources = [item.source_text for item in request.items]
        normalize_request(request)
        rebar, profile, pipe = request.items
        self.assertEqual(rebar.length_requirement, "11,7")
        self.assertIsNone(rebar.length_mm)
        self.assertIsNone(rebar.length_type)
        self.assertEqual(rebar.grade, "А500С")
        self.assertEqual(profile.wall_thickness_requirement, "стенка минимально возможная")
        self.assertIsNone(profile.wall_thickness_mm)
        self.assertEqual((profile.width_mm, profile.height_mm), (40, 40))
        self.assertEqual(pipe.size_designation, "76×3,5 НД")
        self.assertEqual((pipe.diameter_mm, pipe.wall_thickness_mm), (76, 3.5))
        self.assertEqual((pipe.length_type, pipe.length_requirement), ("unmeasured", "НД"))
        self.assertEqual([item.quantity for item in request.items], [20, 100, 4])
        self.assertEqual([item.unit for item in request.items], ["т", "м", "т"])
        self.assertEqual([item.source_text for item in request.items], original_sources)
        for item in request.items:
            self.assertIsNone(item.gost_tu)
            self.assertIsNone(item.form)
            self.assertIsNone(item.processing)
            self.assertIsNone(item.length_mm)
        self.assertIsNone(pipe.grade)
        self.assertIsNone(profile.grade)
        snapshot = request.model_dump()
        self.assertEqual(normalize_request(request).model_dump(), snapshot)

    def test_round_pipe_keeps_text_suffix_and_its_numbers_out_of_dimensions(self):
        for suffix in ("НД", "немерная", "мерная длина 6 м", "холоднодеформированная"):
            with self.subTest(suffix=suffix):
                source = f"Труба 57х3,0 {suffix} — 2 т"
                item = normalize_item(
                    product_group="round_pipe", size_designation=f"57х3,0 {suffix}",
                    source_text=source, quantity=2, unit="т",
                )
                self.assertEqual(item.size_designation, f"57×3 {suffix}")
                self.assertEqual((item.diameter_mm, item.wall_thickness_mm), (57, 3))
                snapshot = item.model_dump()
                normalize_request(PurchaseRequest(items=[item]))
                self.assertEqual(item.model_dump(), snapshot)

    def test_explicit_length_with_unit_converts_without_inferring_type(self):
        for text, expected in (("11,7 м", 11700), ("6000 мм", 6000), ("600 см", 6000)):
            with self.subTest(text=text):
                source = f"Арматура 12 А500С длина {text} — 20 т"
                item = normalize_item(
                    product_group="rebar", size_designation="12", grade="А500С",
                    length_requirement=f"длина {text}", source_text=source,
                    quantity=20, unit="т",
                )
                self.assertEqual(item.length_mm, expected)
                self.assertIsNone(item.length_type)
                self.assertEqual(item.length_requirement, f"длина {text}")
                snapshot = item.model_dump()
                normalize_request(PurchaseRequest(items=[item]))
                self.assertEqual(item.model_dump(), snapshot)

    def test_length_suffix_does_not_turn_round_pipe_into_profile_pipe(self):
        item = normalize_item(
            product="труба", size_designation="57х3,0 длина 6 м",
            source_text="Труба 57х3,0 длина 6 м — 2 т",
            quantity=2, unit="т",
        )
        self.assertEqual(item.product_group, "round_pipe")
        self.assertEqual(item.size_designation, "57×3 длина 6 м")
        self.assertEqual((item.diameter_mm, item.wall_thickness_mm), (57, 3))
        self.assertEqual(item.length_mm, 6000)

    def test_does_not_infer_length_from_quantity_or_ambiguous_source(self):
        for source in (
            "Арматура 12 А500С 11,7 — 20 т",
            "Труба 57×3 — 100 м",
            "Труба длина 11,7 — 100 м",
            "Труба длина 6–12 м",
            "Труба длина 6 м или 12 м",
            "Труба длина 6 м — 12 м",
            "Труба длина 6 м, длина 12 м",
            "Труба длина -6 м",
        ):
            with self.subTest(source=source):
                item = normalize_item(product_group="round_pipe", source_text=source)
                self.assertIsNone(item.length_mm)
                self.assertIsNone(item.length_type)
        item = normalize_item(
            product_group="rebar", length_requirement="длина 11,7 м",
            source_text="Арматура 12 — 20 т",
        )
        self.assertIsNone(item.length_mm)

    def test_preserves_existing_length_values_and_requirements(self):
        for length_type in ("measured", "unmeasured", "other"):
            with self.subTest(length_type=length_type):
                item = normalize_item(
                    product_group="round_pipe", length_mm=6000,
                    length_type=length_type, length_requirement="исходное требование",
                    wall_thickness_requirement="стенка минимально возможная",
                    source_text="Труба длина 6 м, исходное требование, стенка минимально возможная",
                )
                self.assertEqual(item.length_mm, 6000)
                self.assertEqual(item.length_type, length_type)
                self.assertEqual(item.length_requirement, "исходное требование")
                self.assertEqual(item.wall_thickness_requirement, "стенка минимально возможная")

    def test_sheet_length_does_not_imply_length_type(self):
        item = normalize_item(
            product_group="sheet", size_designation="16 1500×6000",
            source_text="Лист 16 1500×6000, длина 6 м — 10 т",
        )
        self.assertEqual(item.length_mm, 6000)
        self.assertIsNone(item.length_type)
        self.assertIsNone(item.length_requirement)

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

    def test_normalizes_linear_meter_aliases_and_preserves_source_unit(self):
        for source_unit in (
            "м.п.", "мп", "пог.м", "погонный метр", "погонные метры",
        ):
            with self.subTest(source_unit=source_unit):
                item = normalize_item(
                    product="труба профильная", product_group="profile_pipe",
                    size_designation="40х40х2", quantity=60,
                    unit=source_unit,
                    source_text=f"40х40х2 — 60 {source_unit}",
                )
                self.assertEqual(item.unit, "м")
                self.assertEqual(item.source_unit, source_unit)
                self.assertEqual(item.quantity, 60)
