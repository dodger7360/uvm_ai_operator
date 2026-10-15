"""Real smoke defects reproduced offline; no production or paid API calls."""
from datetime import date
from decimal import Decimal
import json
from unittest.mock import Mock
import pytest
from database import Database
from repositories.telegram_repository import TelegramRepository
from schemas import PurchaseRequest, RequestItem
from source_fidelity import source_product_rows
from telegram_service import TelegramService
from telegram_operator import _is_question
from work_assistant import WorkAssistantService, needs_search
from tests.test_telegram_operator import response, assistant_client

ROWS = ['Арматура А500С 12 мм — 3 т.', 'Лист горячекатаный Ст3 5 мм — 2 т.']

@pytest.mark.parametrize('separator', [' ', '; ', '\n'])
def test_two_products_real_service(tmp_path, separator):
    rows = [ROWS[0].rstrip('.') if separator.startswith(';') else ROWS[0], ROWS[1]]
    source = separator.join(rows)
    ai = Mock()
    ai.parse_request.side_effect = lambda *a, **k: PurchaseRequest(items=[
        RequestItem(product='Арматура', product_group='rebar', diameter_mm=12, grade='А500С', size_designation='12', source_text=rows[0]),
        RequestItem(product='Лист', product_group='sheet', thickness_mm=5, grade='Ст3', size_designation='5', source_text=rows[1])])
    repo = TelegramRepository(Database(tmp_path / 'test.sqlite3'))
    service = TelegramService(ai, repo, reliable=True)
    service.process(1, source)
    saved = repo.load(1)
    assert saved.fallback_status is None
    assert len(saved.items) == 2
    assert sorted(i.quantity for i in saved.items) == [Decimal('2'), Decimal('3')]
    assert saved.source_text == source
    assert {i.grade for i in saved.items} == {'А500С', 'Ст3'}
    ai.parse_request.assert_called_once()

@pytest.mark.parametrize('text,count', [
    ('Лист 1.5х1500х6000 Ст3 — 1 т. Труба 80х40х2,5 — 2 т. Уголок 50х50х5 — 3 т.', 3),
    ('Уголок 50х50х5 В Ст.3 сп.5 ГОСТ8509-93; ГОСТ535-05 — 0,193 т.', 1),
    ('Лист 1.5 мм С345-6 ГОСТ 19903-2015 — 2 т.', 1),
    ('Труба 80х40 — 2 т. Лист 5 мм — 3 т.', 2),
])
def test_logical_boundaries_preserve_technical_tokens(text, count):
    rows = source_product_rows(text)
    assert len(rows) == count
    assert ''.join(''.join(rows).split()).replace(';', '') == ''.join(text.split()).replace(';', '')

QUESTIONS = ['Найди в интернете ГОСТ для швеллера', 'Проверь в интернете ГОСТ для швеллера',
    'Проверь в интернете применимый ГОСТ', 'Уточни актуальность ГОСТ',
    'Посмотри, действует ли этот стандарт', 'Какие нормативные документы относятся к этой позиции?',
    'Найди действующие стандарты для швеллера 20У', 'Проверь эту позицию по нормативам',
    'ГОСТ 19281-2025 уже действует?', 'Посмотри, какой ГОСТ применяется',
    'Уточни, какой стандарт нужен', 'Проверь актуальность ГОСТ']
@pytest.mark.parametrize('question', QUESTIONS)
@pytest.mark.parametrize('active', [False, True])
def test_normative_question_preserves_context(tmp_path, question, active):
    repo = TelegramRepository(Database(tmp_path / 'test.sqlite3'))
    ai, assistant = Mock(), Mock()
    assistant.answer.return_value.text = 'Ответ'
    service = TelegramService(ai, repo, reliable=True, assistant=assistant)
    if active:
        ai.parse_request.side_effect = RuntimeError()
        service.process(1, 'Труба профильная 80х40 Ст3 — 2 т.')
    before = repo.load(1).model_dump_json() if active else None
    with repo.database.connection() as connection:
        history_before = list(connection.execute('SELECT * FROM telegram_history'))
    assert _is_question(question)
    assert needs_search(question)
    assert service.process(1, question) == ['Ответ']
    after = repo.load(1)
    assert (after.model_dump_json() if after else None) == before
    assistant.answer.assert_called_once()
    ai.interpret_message.assert_not_called()
    with repo.database.connection() as connection:
        assert list(connection.execute('SELECT * FROM telegram_history')) == history_before

@pytest.mark.parametrize('text', ['Добавь 2 тонны', 'Измени количество на 7 тонн', 'Замени марку стали на 09Г2С'])
def test_edits_are_not_questions(text):
    assert not _is_question(text)

@pytest.mark.parametrize('omit', [False, True])
def test_three_inline_products_preserve_verified_part_and_original(tmp_path, omit):
    rows = ['Уголок 50х50х5 Ст3 — 2 т.', 'Труба профильная 80х40 Ст3 — 3 т.', 'Лист 1.5 мм Ст3 — 1 т.']
    source = ' '.join(rows)
    items = [RequestItem(product='Уголок', product_group='angle', size_designation='50х50х5', source_text=rows[0]),
             RequestItem(product='Труба профильная', product_group='profile_pipe', size_designation='80х40', source_text=rows[1]),
             RequestItem(product='Лист', product_group='sheet', thickness_mm=1.5, source_text=rows[2])]
    ai = Mock()
    ai.parse_request.side_effect = lambda *a, **k: PurchaseRequest(items=items[:1] if omit else items)
    repo = TelegramRepository(Database(tmp_path / 'test.sqlite3'))
    messages = TelegramService(ai, repo, reliable=True).process(1, source)
    saved = repo.load(1)
    assert saved.source_text == source
    if omit:
        assert saved.fallback_status in {'partial', 'original'}
        assert source in '\n'.join(messages)
        assert ai.parse_request.call_count == 2
    else:
        assert saved.fallback_status is None
        assert len(saved.items) == 3
        assert sum(i.quantity for i in saved.items) == Decimal('6')
        assert any('толщин' in m.casefold() for m in messages)
        assert {i.source_position for i in saved.items} == {1, 2, 3}
        ai.parse_request.assert_called_once()

def test_known_concept_does_not_require_search():
    assert not needs_search('Чем отличается швеллер У от П?')
    assert not _is_question('Арматура А500С 12 мм — 3 т. Лист Ст3 5 мм — 2 т.')


@pytest.mark.parametrize('standard', ['ГОСТ 19903-2015', ''])
def test_user_standard_is_preserved_and_missing_standard_is_not_blocking(tmp_path, standard):
    source = f'Лист 10 мм Ст3 {standard} — 5 т'
    ai, assistant = Mock(), Mock()
    ai.parse_request.return_value = PurchaseRequest(items=[RequestItem(
        product='Лист', product_group='sheet', thickness_mm=10, grade='Ст3',
        gost_tu=standard or None, source_text=source)])
    repo = TelegramRepository(Database(tmp_path / 'test.sqlite3'))
    service = TelegramService(ai, repo, reliable=True, assistant=assistant)
    messages = service.process(1, source)
    saved = repo.load(1)
    assert saved.fallback_status is None and len(saved.items) == 1
    assert saved.items[0].gost_tu == (standard or None)
    assert saved.source_text == source
    assert saved.items[0].quantity == Decimal('5')
    assistant.answer.assert_not_called()
    assert not needs_search(source)
    before = saved.model_dump_json()
    assistant.answer.return_value.text = 'В источнике найден ГОСТ другой редакции; заявка не изменена.'
    service.process(1, 'Проверь в интернете ГОСТ для этой позиции')
    assert repo.load(1).model_dump_json() == before


def test_normative_answer_is_plain_useful_text_without_mandatory_json():
    text = 'Для швеллера 20У рассмотрите ГОСТ 8240-97; условия поставки нужно уточнить.'
    client = assistant_client(response(text, search=True))
    answer = WorkAssistantService(client).answer('Какой ГОСТ применяется к швеллеру 20У?', user_id=1)
    assert text in answer.text and answer.searched and answer.sources
    payload = client.responses.create.call_args.kwargs
    assert 'standards' not in payload.get('text', {})
    assert 'Верни JSON' not in payload['instructions']


def test_unconfirmed_status_can_be_reported_without_normative_expertise():
    text = 'Не удалось достоверно подтвердить актуальность по найденным источникам'
    client = assistant_client(response(text, search=True))
    answer = WorkAssistantService(client, today=lambda: date(2026,10,15)).answer('Этот ГОСТ сейчас действует?', user_id=1)
    assert text in answer.text
    assert json.loads(client.responses.create.call_args.kwargs['input'])['reference_date'] == '2026-10-15'
    assert 'Не выдумывай подтверждённый статус' in client.responses.create.call_args.kwargs['instructions']


@pytest.mark.parametrize('question', ['Чем отличается швеллер У от П?', 'Какая марка стали обычно используется?'])
def test_general_work_question_without_search(question):
    assert _is_question(question)
    assert not needs_search(question)


def test_comments_between_products_are_preserved(tmp_path):
    source = ROWS[0] + '\nКомментарий: доставка завтра.\n' + ROWS[1]
    ai = Mock()
    ai.parse_request.return_value = PurchaseRequest(items=[
        RequestItem(product='Арматура', product_group='rebar', diameter_mm=12, grade='А500С', source_text=ROWS[0]),
        RequestItem(product='Лист', product_group='sheet', thickness_mm=5, grade='Ст3', source_text=ROWS[1])])
    repo = TelegramRepository(Database(tmp_path / 'test.sqlite3'))
    messages = TelegramService(ai, repo, reliable=True).process(1, source)
    assert len(source_product_rows(source)) == 2
    assert len(repo.load(1).items) == 2
    assert source in '\n'.join(messages)
    assert repo.load(1).source_text == source
