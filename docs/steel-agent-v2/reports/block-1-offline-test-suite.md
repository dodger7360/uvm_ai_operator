# Блок 1 — изолированный тестовый запуск Telegram Steel Agent v2

21.10.2026. База GitHub main проверена через API:
`efbb67f6c1a096f57192a2076d6d4f4e2829eede`.
Ветка: `fix/v2-offline-test-suite`. Основная реализация выполнена GPT-6.1 Sol.

## Изменения

- Явный inventory `current` / `legacy` / `research` / `all`, collection gate
  до импорта, проверка явных node IDs и полноты классификации модулей.
- Default execution — current через `scripts/v2_offline_tests.py`.
  Прямой pytest без launcher допускает только collection; выполнение даёт
  понятную ошибку с командой запуска.
- Исправлен `test_release_boot_with_isolated_sqlite_preserves_schema`:
  настоящий run_bot → runtime connect → queue → engine/repository → outbox,
  подменены внешние transport/API. Проверяются обе схемы, сохранённые старые
  непрозрачные записи/права, исходник, Decimal/единицы, один план, версия,
  turn, cursor, delivered receipt/message_id/payload и integrity_check.
- По прямому указанию удалён только
  `test_grade_variants_are_not_inferred_for_other_listed_diameters`.
  Остальные четыре 23MET-сценария сохранены в research.
- Общие проверки доступа, TypingIndicator, quantity и dialog_history выделены
  в модули без импортов старых тестов/AIService/TelegramService.
  В historical modules исходные assertions сохранены; `all` поэтому содержит
  перекрытие с выделенными общими сценариями, его число не означает уникальность.
- Старый runtime-код из Git заменён в активной проверке миграции на небольшой
  synthetic SQL fixture четырёх core-таблиц, старых JSON и истории.
  Сравнение метаданных до/после, чтение старого snapshot и rollback DDL сохранены.
- Исторические byte-comparison/B6/old-repository проверки вынесены в research.
  Для текущего исполнения сохранности добавлены четыре синтетических корпуса,
  всего 97 позиций: исходник, records, originals/items, пять технических фактов,
  два норматива, группировка, точные количества/суммы, версия и restart.
- Per-run SQLite confinement, audit сетевых обращений/открытия БД, ограничения
  URI/UNC/dbapi2/ATTACH, наследуемый дочерними Python guard, очистка credentials
  и внешних настроек pytest. Небезопасный basetemp отклоняется до очистки pytest.

Production source, бизнес-правила, BR-007 (максимум две ссылки), модель бота,
runtime, schema migration implementation и пакет approved knowledge не менялись.

## Проверки и числа

До изменений выполнена collection прежним launcher в отдельном checkout:
2 228 выбранных случаев. Старый pytest.ini исключал ещё четыре `legacy_23met`;
при этом оставшийся удаляемый 23MET-тест попадал в default run.
Полный исходный смешанный набор здесь не исполнялся; число 2 228 не означает passed.

| Состав после изменений | Модули | Определения test_* | Случаи |
|---|---:|---:|---:|
| current | 35 | 523 | 1 085 |
| legacy | 40 | 707 | 1 185 |
| research | 2 | 7 | 10 |
| all (только collection) | 77 | 1 237 | 2 280 |

Из 1 085 current: 1 016 в v2-модулях, 24 Windows operations, один release-smoke,
17 shared access, 20 shared primitives, семь isolation meta-cases.
В прежних 29 v2-модулях собиралось 1 017 случаев; пять Git-зависимых случаев
вынесены, четыре синтетических добавлены: 1 017 − 5 + 4 = 1 016.
Уменьшение default collection объясняется границей приёмки, а не массовым
удалением исторических тестов. Общий all больше прежней collection также потому,
что он включает ранее исключавшиеся четыре research-теста и выделенные проверки.

Адресный запуск после всех изменений кода:

```powershell
python -B scripts/v2_offline_tests.py tests/test_release_startup.py tests/test_shared_primitives.py tests/test_shared_access.py tests/test_steel_agent_v2_corpora.py tests/test_v2_suite_isolation.py tests/test_steel_agent_v2_forward_only.py tests/test_steel_agent_v2_windows.py tests/test_steel_agent_v2_queue.py --tb=short
```

**171 passed in 39.60s**.

Полный актуальный запуск:

```powershell
python -B scripts/v2_offline_tests.py --tb=short
```

Выполнен в отдельном экспорте 277 source-файлов без `.git`, `.env`, SQLite и
локального research HTML; дополнительно запрещён поиск Git-репозитория выше
корня копии. **1 085 passed in 120.25s**, exit 0, без skip/xfail.
Внешние API подменены, сетевые эффекты запрещены guard.

Дополнительно:

- `--suite all --collect-only`: **2 280 collected**, exit 0.
- `python -B scripts/build_v2_knowledge.py --check`: exit 0,
  `sha256:9a4bc0b78ede7364744706d86d1ac4d0cb907dd3a790b614d22817b2a24f4262`.
- `git diff --check`: без ошибок.
- Успешность всего legacy/research не заявляется: выполнена только collection.

Существующие v2 business assertions не ослаблялись ради зелёного набора.
Исправлялись тестовый запуск, dead startup и зависимости fixtures.

## Исключённые из текущей приёмки сценарии

1. Старые AIService/TelegramService, classifier, Low/Medium retry, old pending,
   formatter/normalizer, расчётный диалог и Workbench — legacy.
2. Matching, reference/import и прежний inventory — legacy/research; полезные
   общие зависимости текущего бота имеют отдельные активные тесты.
3. Четыре 23MET HTML-снимка — research; один указанный владельцем тест удалён.
4. Побайтовое равенство четырёх scripts старому Git SHA — research.
5. Исполнение старого repository для проверки его отказа — research.
6. Исходный B6 corpus через Git objects — research; в current есть синтетическая
   проверка тех же структурных обязанностей, но это не предметная эквивалентность
   исходному корпусу и не доказательство распознавания GPT.

Не исключены: реальные crash/lock/DPAPI/PowerShell tests, queue/offset/outbox,
права и гонки отзыва, additive migration/DDL rollback, Decimal, история,
idempotence/unknown outcomes, approved CASE, HTML и BR-007.

## Риски и ограничения

- При добавлении модуля разработчик обязан выбрать группу в inventory;
  иначе collection завершается ошибкой. Это намеренное изменение test interface.
- Прямое `pytest`-исполнение заменено явной командой launcher; документация
  обновлена. Existing/arbitrary basetemp больше не поддерживается.
- Guard рассчитан на доверенный код тестов и обычные наследующие окружение
  Python-потомки. Это не OS sandbox против произвольного нативного или
  намеренно обходящего изоляцию кода. Operational tests запускают только свои
  временные процессы; production process/Tasks не запускались и не менялись.
- Legacy/research сохраняют исторические prerequisites и могут иметь прежние
  падения. Они не считаются готовностью текущего Telegram.
- Офлайн-тесты не подтверждают предметное качество модели, исправление конкретного
  live-подтверждения или время ответа бота до 30 секунд. Эти работы вне блока 1.

Merge/deployment и реальные API-вызовы в этом блоке не выполнялись.
