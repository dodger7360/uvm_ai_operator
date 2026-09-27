from ai import AIService
from normalizer import normalize_request
from validator import validate_request
from formatter import format_supplier_request


request_text = (
    "балка 25К1 ст255-2,013 тн\n\n"
    "Швеллер 12П ст3- 1,357 тн\n\n"
    "Швеллер 14П ст 3-1,378 тн\n\n"
    "Уголок 90*90*6 ст3-0,310 тн\n\n"
    "труба 40*40*4,0 - 0,162 тн\n\n"
    "труба 80*40*4,0-0,988 тн\n\n"
    "труба 60*60*4,0- 0,150 тн\n\n"
    "труба 80*80*5,0 - 0,762 тн\n\n"
    "труба 100*100*5,0-0,794 тн\n\n"
    "труба 120*120*6,0- 1,121 тн\n\n"
    "труба 160*120*6-1,545 тн\n\n"
    "труба 30*30*2,0-0,182 тн\n\n"
    "труба 40*40*2,0-0,090 тн\n\n"
    "лист 6 1500*6000 ст3-2 шт\n\n"
    "Лист 8 1500*1250 ст3-0,120 тн\n\n"
    "Лист 10 1500*4000 ст3 - 1 шт\n\n"
    "Лист 20 1500*1000 ст09г2с -1 шт\n\n"
    "лист 30 1500*1500 ст09г2с - 1 шт\n\n"
    "лист пвл 506-0,526 тн\n\n"
    "арматура а1 20-0,115 тн"
)


result = AIService().parse_request(request_text)
normalized_result = normalize_request(result)
validated_result = validate_request(normalized_result)


print()
print("=" * 70)
print("ПРОВЕРКА ЗАЯВКИ")
print("=" * 70)

if not validated_result.is_valid:
    print()
    print("Заявка требует уточнения:")
    print()

    for question in validated_result.clarification_questions:
        print(f"- {question}")

else:
    print()
    print("Заявка прошла проверку.")
    print()
    print("=" * 70)
    print("ТЕКСТ ДЛЯ ПОСТАВЩИКА")
    print("=" * 70)
    print()
    print(format_supplier_request(validated_result))

if validated_result.warnings:
    print()
    print("Предупреждения:")

    for warning in validated_result.warnings:
        print(f"- {warning}")
