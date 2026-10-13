"""Offline data-preservation checks on existing real request artifacts.
These are not live AI extraction tests; live checks are recorded separately.
"""
import json
from pathlib import Path
import re
import unittest
from decimal import Decimal
from schemas import PurchaseRequest, RequestItem
from telegram_service import exact_source_attributes
from normalizer import normalize_request
from formatter import sort_items, format_normalized_request


class RealRequestFixtureTests(unittest.TestCase):
    def check_fixture(self, name, count):
        data = json.loads((Path(__file__).parent/'fixtures'/f'{name}.json').read_text(encoding='utf-8'))
        items = []
        for row in data['items']:
            description = row['description']
            if name == 'beams_10':
                size,grade = re.search(r'([0-9]+[БКШ][0-9]+) / (\S+)', description).groups()
                group,product='i_beam','Двутавр'
            else:
                group = 'expanded_metal' if 'ПВЛ' in description else 'sheet'
                size = '506' if group=='expanded_metal' else re.search(r'\d+(?:[.,]\d+)?(?:×\d+){0,2}',description)[0]
                grade = re.search(r'(12Х18Н10Т|Ст\S+|09Г2С(?:-12)?)',description)[0]
                product = 'Лист ПВЛ' if group=='expanded_metal' else ('Лист Х/К' if 'Х/К' in description else 'Лист Г/К')
            item = RequestItem(product=product,product_group=group,size_designation=size,grade=grade,
                quantity=row['quantity'],unit=row['unit'],source_text=row['source_text'])
            exact_source_attributes(item)
            self.assertEqual(item.quantity,Decimal(row['quantity']))
            self.assertEqual(item.grade,grade)
            for token in re.findall(r'(?:ГОСТ(?: Р)?|ТУ|СТО АСЧМ)\s+[0-9.\-]+',description):
                self.assertIn(token,item.gost_tu)
            items.append(item)
        request=normalize_request(PurchaseRequest(items=items))
        request.items=sort_items(request.items)
        self.assertEqual(len(request.items),count)
        self.assertEqual(len(format_normalized_request(request).splitlines()),count)
        self.assertEqual(sorted(i.quantity for i in request.items),sorted(Decimal(r['quantity']) for r in data['items']))
        return request

    def test_real_beams_10(self):
        request=self.check_fixture('beams_10',10)
        self.assertEqual(sum(i.grade=='С345-6' for i in request.items),6)

    def test_real_sheets_27(self):
        request=self.check_fixture('sheets_27',27)
        sheets=[i for i in request.items if i.product_group=='sheet']
        self.assertEqual([i.thickness_mm for i in sheets],sorted(i.thickness_mm for i in sheets))
        self.assertEqual(sum(i.product_group=='expanded_metal' for i in request.items),2)
