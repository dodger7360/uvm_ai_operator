from decimal import Decimal
from unittest.mock import Mock

import pytest

from ai import AIService
from database import Database
from diagnostics import SemanticExtractionError
from repositories.telegram_repository import TelegramRepository
from schemas import PurchaseRequest, PurchaseRequestExtraction, RequestItem
from telegram_service import TelegramService

TEXT = 'Нужна арматура А500С 12 мм, 5 тонн, Екатеринбург'
FRAGMENT = 'арматура А500С 12 мм, 5 тонн'


def rebar(fragment=FRAGMENT, diameter=12, quantity='5'):
    return RequestItem(product='арматура', product_group='rebar', grade='А500С',
                       diameter_mm=diameter, size_designation=str(diameter),
                       quantity=quantity, unit='т', source_text=fragment)


@pytest.fixture
def context(tmp_path):
    repo = TelegramRepository(Database(tmp_path / 'isolated.sqlite3'))
    ai = Mock()
    return TelegramService(ai, repo), ai, repo


def extraction(ai, items, city='Екатеринбург'):
    draft = PurchaseRequest(city=city, items=items)
    ai.parse_request.side_effect = lambda *args, **kwargs: draft.model_copy(deep=True)
    return draft


@pytest.mark.parametrize('active', [False, True])
@pytest.mark.parametrize('fragment', [FRAGMENT, TEXT, TEXT.removeprefix('Нужна '), 'Нужна '+FRAGMENT])
def test_conversational_rebar_request(context, active, fragment):
    service, ai, repo = context
    if active:
        extraction(ai, [rebar(TEXT)])
        service.process(1, TEXT)
    ai.reset_mock()
    draft = extraction(ai, [rebar(fragment)])
    AIService._validate_source_fragments(PurchaseRequestExtraction(**draft.model_dump()), TEXT)
    service.process(1, TEXT)
    saved = repo.load(1)
    assert len(saved.items) == 1
    assert saved.items[0].quantity == Decimal('5')
    assert saved.items[0].grade == 'А500С'
    assert saved.items[0].diameter_mm == 12
    assert saved.items[0].source_text == TEXT
    assert saved.initial_extraction.items[0].source_text == fragment
    assert saved.city == 'Екатеринбург'
    ai.parse_request.assert_called_once()
    ai.interpret_message.assert_not_called()


@pytest.mark.parametrize('opener', ['Нужны', 'Нужно', 'Нужен', 'Требуется', 'Требуются'])
def test_other_request_openers(context, opener):
    service, ai, repo = context
    extraction(ai, [rebar()])
    service.process(1, opener+' '+FRAGMENT+', Екатеринбург')
    assert repo.load(1).items[0].quantity == Decimal('5')


@pytest.mark.parametrize('rows', [
    [TEXT, 'Требуется арматура А500С 16 мм, 2 тонны, Екатеринбург'],
    [TEXT, TEXT],
    [TEXT, 'арматура А500С 12 мм, 5 тонн, Екатеринбург'],
])
def test_multiline_rows_and_duplicates_keep_separate_positions(context, rows):
    service, ai, repo = context
    different = '16 мм' in rows[1]
    second = rebar('арматура А500С 16 мм, 2 тонны', 16, '2') if different else rebar()
    extraction(ai, [rebar(), second])
    service.process(1, '\n'.join(rows))
    saved = repo.load(1)
    assert len(saved.items) == 2
    assert {entry.source_text for entry in saved.items} == set(rows)
    assert len({entry.item_id for entry in saved.items}) == 2
    assert sorted(entry.quantity for entry in saved.items) == ([Decimal('2'), Decimal('5')] if different else [Decimal('5'), Decimal('5')])


@pytest.mark.parametrize('fragment', [
    'арматура', 'арматура А500С', 'арматура А500С 12 мм',
    'арматура А500С 12 мм, 5 тонн, Москва',
])
def test_incomplete_or_nonliteral_fragment_is_rejected(context, fragment):
    service, ai, repo = context
    extraction(ai, [rebar(fragment)])
    with pytest.raises(SemanticExtractionError) as error:
        service.process(1, TEXT)
    assert error.value.code in {'missing_positions', 'source_fragment_mismatch'}
    assert repo.load(1) is None


@pytest.mark.parametrize('text', [
    TEXT+'\n'+TEXT,
    TEXT+'\nарматура А500С 16 мм, 2 тонны, Екатеринбург',
    TEXT+'\nарматура А500С 12 мм',
    TEXT.replace('12 мм', '12 мм ГОСТ 34028'),
    TEXT.replace('12 мм', 'Ст20 12 мм'),
    TEXT.replace('5 тонн', '5 тонн, длина 11,7 м'),
])
def test_missing_positions_or_attributes_preserve_previous_request(context, text):
    service, ai, repo = context
    extraction(ai, [rebar(TEXT)])
    service.process(1, TEXT)
    before = repo.load(1).model_dump()
    ai.reset_mock()
    extraction(ai, [rebar()])
    with pytest.raises(SemanticExtractionError) as error:
        service.process(1, text)
    assert error.value.code in {'missing_positions', 'source_fragment_mismatch'}
    assert repo.load(1).model_dump() == before
    expected_calls = 1 if text.endswith('\nарматура А500С 12 мм') else 2
    assert ai.parse_request.call_count == expected_calls


@pytest.mark.parametrize('city', [None, 'Москва'])
def test_city_omission_requires_matching_extracted_city(context, city):
    service, ai, repo = context
    extraction(ai, [rebar()], city=city)
    with pytest.raises(SemanticExtractionError):
        service.process(1, TEXT)
    assert repo.load(1) is None


@pytest.mark.parametrize('separator', ['\n', ', '])
def test_merged_rows_are_rejected(context, separator):
    service, ai, repo = context
    second = 'арматура А500С 16 мм, 2 тонны'
    extraction(ai, [rebar(FRAGMENT+separator+second)], city=None)
    with pytest.raises(SemanticExtractionError) as error:
        service.process(1, FRAGMENT+'\n'+second)
    assert error.value.code in {'merged_positions', 'missing_positions'}
    assert repo.load(1) is None


@pytest.mark.parametrize('extra', [rebar(), rebar('арматура А500С 16 мм, 2 тонны', 16, '2'), rebar('арматура А500С')])
def test_additional_invented_duplicate_or_partial_item_rejected(context, extra):
    service, ai, repo = context
    extraction(ai, [rebar(), extra])
    with pytest.raises(SemanticExtractionError):
        service.process(1, TEXT)
    assert repo.load(1) is None


@pytest.mark.parametrize('city', ['Москва', 'Санкт-Петербург'])
def test_full_source_does_not_allow_city_substitution(context, city):
    service, ai, repo = context
    extraction(ai, [rebar(TEXT)], city=city)
    with pytest.raises(SemanticExtractionError):
        service.process(1, TEXT)
    assert repo.load(1) is None


@pytest.mark.parametrize('city,fragment', [
    ('5 тонн', 'арматура А500С 12 мм'),
    ('т', 'арматура А500С 12 мм, 5'),
    ('ГОСТ 34028', FRAGMENT),
])
def test_city_cannot_hide_quantity_unit_or_standard(context, city, fragment):
    service, ai, repo = context
    text = 'Нужна '+FRAGMENT if city == '5 тонн' else 'Нужна арматура А500С 12 мм, 5 т' if city == 'т' else 'Нужна '+FRAGMENT+', ГОСТ 34028'
    extraction(ai, [rebar(fragment)], city=city)
    with pytest.raises(SemanticExtractionError):
        service.process(1, text)
    assert repo.load(1) is None


def test_flattened_merged_excerpt_with_another_item_is_rejected(context):
    service, ai, repo = context
    second = 'арматура А500С 16 мм, 2 тонны'
    extraction(ai, [rebar(FRAGMENT+', '+second), rebar(second, 16, '2')], city=None)
    with pytest.raises(SemanticExtractionError):
        service.process(1, FRAGMENT+'\n'+second)
    assert repo.load(1) is None


def test_real_oct13_2109_all_21_positions_and_source_attributes(context):
    """Exact request copied from the linked chat, including nonbreaking spaces."""
    from collections import Counter
    from pathlib import Path
    from telegram_service import prepare_sheet_heading

    fixtures = Path(__file__).parent / 'fixtures'
    text = (fixtures / 'mixed_21_20261013_2109.txt').read_text(encoding='utf-8').rstrip('\n')
    draft = PurchaseRequest.model_validate_json((fixtures / 'mixed_21_extraction.json').read_text(encoding='utf-8'))
    prepared, records = prepare_sheet_heading(text)
    assert len(records) == 9
    assert len(prepared.splitlines()) == 21
    AIService._validate_source_fragments(PurchaseRequestExtraction(**draft.model_dump()), prepared)
    service, ai, repo = context
    ai.parse_request.side_effect = lambda *args, **kwargs: draft.model_copy(deep=True)
    replies = service.process(1, text)
    saved = repo.load(1)
    assert saved.is_valid
    assert saved.source_text == text
    assert len(saved.items) == len('\n'.join(replies).splitlines()) == 21
    assert '\n'.join(replies) == (fixtures / 'mixed_21_expected.txt').read_text(encoding='utf-8').strip()
    expected = [
        ('round_pipe', '530×10', 'Ст3 (или Ст20)', 'ГОСТ 10704-91', '71.380'),
        ('round_pipe', '219×6', 'Ст3 (или Ст20)', 'ГОСТ 10704-91', '2.951'),
        ('round_pipe', '159×6', 'Ст3 (или Ст20)', 'ГОСТ 10704-91', '14.143'),
        ('round_pipe', '159×8', 'Ст3 (или Ст20)', 'ГОСТ 10704-91', '33.6'),
        ('round_pipe', '25×2', 'Ст3 (или Ст20)', 'ГОСТ 10704-91', '0.004'),
        ('round_bar', '12', 'С245', 'ГОСТ 2590', '0.014'),
        ('round_bar', '18', 'С245', 'ГОСТ 2590', '0.691'),
        ('beam', '30К1', 'С245', 'ГОСТ 57837', '3.493'),
        ('angle', '25×3', 'С245', 'ГОСТ 8509', '0.965'),
        ('sheet', '3×1500×6000', 'С245', 'ГОСТ 19903', '0.461'),
        ('sheet', '4×1500×6000', 'С245', 'ГОСТ 19903', '4.625'),
        ('sheet', '10×1500×6000', 'С245', 'ГОСТ 19903', '5.785'),
        ('sheet', '16×1500×6000', 'С245', 'ГОСТ 19903', '22.780'),
        ('sheet', '12×1500×6000', 'Ст09Г2С', 'ГОСТ 19903', '4.594'),
        ('sheet', '16×1500×6000', 'Ст09Г2С', 'ГОСТ 19903', '1.994'),
        ('sheet', '30×1500×6000', 'Ст09Г2С', 'ГОСТ 19903', '9.182'),
        ('sheet', '50×1500×6000', 'Ст09Г2С', 'ГОСТ 19903', '1.312'),
        ('sheet', '80×1500×6000', 'Ст09Г2С', 'ГОСТ 19903', '2.413'),
        ('expanded_metal', '508×1000', 'С245', 'ТУ 36.26.11-5-89', '17.149'),
        ('expanded_metal', '508×718', 'С245', 'ТУ 36.26.11-5-89', '0.157'),
        ('expanded_metal', '508×450', 'С245', 'ТУ 36.26.11-5-89', '0.102'),
    ]
    assert Counter((i.product_group, i.size_designation, i.grade, i.gost_tu, i.quantity.as_tuple()) for i in saved.items) == Counter((*row[:4], Decimal(row[4]).as_tuple()) for row in expected)
    assert all(i.unit == 'т' for i in saved.items)
    assert len({i.item_id for i in saved.items}) == 21
    for i in saved.items:
        if i.product_group == 'sheet':
            assert (i.width_mm, i.length_mm) == (1500, 6000)
            assert i.thickness_mm == float(i.size_designation.split('×')[0])
            assert (i.source_text, i.source_context) in [(row, heading) for _, row, heading in records]
            assert i.original.source_text == i.source_text
            assert ('09г2с' in i.source_context) == (i.grade == 'Ст09Г2С')
        elif i.product_group == 'round_pipe':
            assert (i.diameter_mm, i.wall_thickness_mm) == tuple(map(float, i.size_designation.split('×')))
        elif i.product_group == 'round_bar':
            assert i.diameter_mm == float(i.size_designation)
    ai.parse_request.assert_called_once_with(prepared)


@pytest.mark.parametrize('quantity', ['0.001500', '71.380', '0.000000123456789', '123456789.123456789'])
def test_literal_decimal_quantity_restored_without_rounding(context, quantity):
    service, ai, repo = context
    fragment = FRAGMENT.replace('5 тонн', quantity.replace('.', ',')+' тонн')
    extraction(ai, [rebar(fragment, quantity='9')])
    service.process(1, 'Нужна '+fragment+', Екатеринбург')
    saved = repo.load(1).items[0]
    assert saved.quantity.as_tuple() == Decimal(quantity).as_tuple()
    assert saved.original.quantity.as_tuple() == Decimal(quantity).as_tuple()


@pytest.mark.parametrize('standard', ['ГОСТ 19903', 'ТУ 14-1-5580-2010', 'ГОСТ 19903, ТУ 14-1-5580-2010'])
def test_literal_grade_size_and_all_standards_restored(context, standard):
    service, ai, repo = context
    fragment = 'Лист ГК 12×1500×6000 Ст09Г2С '+standard+' — 0,001500 т'
    data = RequestItem(product='Лист ГК', product_group='sheet', size_designation='12×1500×6000',
                       grade=None, gost_tu=None, quantity='9', unit='т', source_text=fragment)
    extraction(ai, [data])
    service.process(1, 'Нужен '+fragment+', Екатеринбург')
    saved = repo.load(1).items[0]
    assert saved.grade == 'Ст09Г2С'
    assert saved.gost_tu == standard
    assert (saved.thickness_mm, saved.width_mm, saved.length_mm) == (12, 1500, 6000)
    assert saved.quantity.as_tuple() == Decimal('0.001500').as_tuple()


def test_nonliteral_extension_to_valid_source_row_is_rejected(context):
    service, ai, repo = context
    extraction(ai, [rebar(TEXT+' ГОСТ 34028')])
    with pytest.raises(SemanticExtractionError) as error:
        service.process(1, TEXT)
    assert error.value.code == 'source_fragment_mismatch'
    assert repo.load(1) is None


@pytest.mark.parametrize('failure', ['extra', 'city', 'merged'])
def test_audit_failures_preserve_existing_request_and_retry_once(context, failure):
    service, ai, repo = context
    extraction(ai, [rebar(TEXT)])
    service.process(1, TEXT)
    before = repo.load(1).model_dump()
    ai.reset_mock()
    if failure == 'extra':
        text = TEXT
        extraction(ai, [rebar(), rebar()])
    elif failure == 'city':
        text = TEXT
        extraction(ai, [rebar(TEXT)], city='Москва')
    else:
        second = 'арматура А500С 16 мм, 2 тонны'
        text = FRAGMENT+'\n'+second
        extraction(ai, [rebar(FRAGMENT+', '+second), rebar(second, 16, '2')], city=None)
    with pytest.raises(SemanticExtractionError):
        service.process(1, text)
    assert repo.load(1).model_dump() == before
    assert ai.parse_request.call_count == 2
    assert ai.parse_request.call_args.kwargs['reasoning_effort'] == 'medium'


@pytest.mark.parametrize('failure', ['extra', 'city'])
def test_medium_can_recover_after_audit_failure_with_one_save(context, failure):
    service, ai, repo = context
    bad = PurchaseRequest(items=[rebar(), rebar()], city='Екатеринбург') if failure == 'extra' else PurchaseRequest(items=[rebar(TEXT)], city='Москва')
    ai.parse_request.side_effect = [bad, PurchaseRequest(items=[rebar()], city='Екатеринбург')]
    service.process(1, TEXT)
    assert len(repo.load(1).items) == 1
    assert repo.load(1).city == 'Екатеринбург'
    assert ai.parse_request.call_count == 2
    with repo.database.connection() as connection:
        assert connection.execute('SELECT count(*) FROM telegram_history').fetchone()[0] == 1
