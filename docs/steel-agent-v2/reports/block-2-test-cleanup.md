# Блок 2 — удаление фиктивных проверок

21.10.2026. PR #27 проверен на неизменном HEAD
`11913ee934892540efcb51ff7d23adf34e964191` и слит по разрешению владельца.
GitHub CI/check-runs отсутствуют; успешные 1085 current и 171 адресный тест
зафиксированы в отчёте этого HEAD. Новый main/local main/origin/main:
`0abee11945d6b33dda3d2cf1fbce01286cfa6465`.
Ветка блока 2: `test/v2-remove-vacuous-checks`.

## Удалено и сохранено

Удалены ровно семь функций: 19 параметризованных случаев (13 current,
два legacy, четыре research). Проверки не заменялись skip/xfail.

| Модуль / удалённая проверка | Оставшееся полезное покрытие |
|---|---|
| forward_only / `test_supported_runtime_has_no_external_guard_dependency`: поиск слов в десяти файлах | В том же модуле реальный PowerShell start без elevation/external files, supervisor launch, отказ legacy-конфигурации; Windows operations и release-smoke проверяют запуск/locks. Комментарий с запрещённым словом не свидетельствует о зависимости. |
| wire_schema / `test_original_sdk_schema_exposes_concrete_defaults`: точная форма дефекта приватной SDK-схемы | `test_final_http_schema_supported_profile` проверяет фактически отправляемую strict-схему; nullable quantity, Decimal, SDK → агент → SQLite/edit/undo остаются. |
| mass_review / `test_all_six_knowledge_documents_remain_approved_with_25_original_positions`: счёт строк, Markdown-заголовки и ревизия | knowledge: воспроизводимая approved-only сборка и все CASE через агент/Repository; mass_review сохраняет точные расчёты, provenance и адресацию. Арифметическое утверждение о сумме констант удалено. |
| knowledge_bindings / `test_original_nine_bindings_keep_the_existing_artifact_and_version`: SHA-префикс и дублирующие счётчики | Сравнение artifact/build уже есть в knowledge и knowledge_evidence; проверка хешей вычисляется из источников. Сохраняются отрицательные проверки URL/длины и точное Decimal-сопоставление длин. |
| nomenclature_contract / `test_existing_request_item_contract_remains_independent`: создание несвязанного объекта не доказывает интеграцию | Сохранены контрактные проверки исходников/нормативов/provenance/Decimal; `test_schemas.py::RequestItemSchemaTests.test_legacy_data_can_be_created_and_loaded` проверяет совместимость старых данных. Ложная гарантия независимости не заменялась другой фикцией. |
| normalization_context_contract / `test_context_deep_copy_keeps_independent_snapshots`: библиотечный model_copy | Сохранены scope/context/record ограничения, roundtrip before/after с версиями и provenance, новый рабочий контекст без старых коррекций. |
| research/historical_git_contracts / `test_existing_launch_infrastructure_is_exactly_production`: четыре побайтовых сравнения с aa11380 | Действующие PowerShell/locks/supervisor/release-smoke проверяют поведение; historical repository и B6 research остаются. Равенство старому коду больше не требуется. |

Не изменены BR-007/две ссылки, Decimal assertions, queue, SQLite, права,
Telegram, бизнес-правила, approved CASE, коэффициенты, inventory и launcher.
Из Python изменены только семь перечисленных тестовых файлов.

## Проверки

- Адресные current: **250 passed in 20.07s**.
- Затронутые legacy-контракты и RequestItem schemas:
  **76 passed, 11 subtests passed in 0.28s**.
- Research: два оставшихся historical Git-сценария успешно собраны;
  их исполнение с историческими prerequisites не заявляется.
- Knowledge `--check`: прежний
  `sha256:9a4bc0b78ede7364744706d86d1ac4d0cb907dd3a790b614d22817b2a24f4262`.

- Один полный current-прогон: **1071 passed, 1 failed in 113.15s**.
  `test_release_boot_with_isolated_sqlite_preserves_schema`, строка 90:
  `json.loads(outbox['payload'])['text']` → `KeyError: 'text'`.
- Контрольный адресный запуск того же smoke на НЕИЗМЕНЁННОМ HEAD PR #27
  `11913ee934892540efcb51ff7d23adf34e964191`: **1 failed in 1.04s**,
  тот же KeyError. Release-smoke в этом PR не редактируется и не отключается.
  Полный набор не объявляется успешным; причина формы выбранного outbox payload
  и исправление smoke требуют отдельного рассмотрения, вне списка удалений.
- Ранние попытки адресного запуска не дошли до тестов из-за Windows sandbox
  PermissionError при создании OS temp. Повтор через штатный launcher вне
  ограничения sandbox прошёл; сетевые/SQLite guard оставались включены.
- `git diff --check`: без ошибок. Удаление current-случаев объясняет
  1085 − 13 = 1072 выбранных теста; остальные не исключались.

**Итог:** очистка подготовлена; merge этого PR до разбора красного release-smoke
не рекомендуется. Зелёный результат исторического отчёта блока 1 не выдаётся
за текущий фактический результат.

## Границы

Это очистка качества проверок, не исправление живой нормализации.
Отсутствие запрещённых слов, стабильность внутренней SDK-схемы и побайтовое
равенство старых scripts больше не считаются гарантиями продукта.
Субагенты, платные API и deployment не использовались. Production HEAD при
контрольном чтении: `efbb67f6c1a096f57192a2076d6d4f4e2829eede`, checkout/БД/процессы
не изменялись. Блок 2 публикуется отдельным PR без merge.
