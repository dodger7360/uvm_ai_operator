# Проверки и критерии приёмки

## №16: queue / crash / authorization

Добавить к целевому набору `tests/test_steel_agent_v2_queue.py` и
`tests/test_steel_agent_v2_015_reproduction.py`. Всегда использовать offline launcher,
временную SQLite и fake Telegram; установленный OpenAI SDK работает через httpx2
MockTransport. Живые API-вызовы не разрешены этим набором.

Event-тесты доказывают: три GPT-операции действительно перекрываются; четвёртая
ждёт; медленный A не блокирует B; A1/A2 FIFO; новые пользователи не вытесняют
ожидающих. Фоновое планирование не требует возврата getUpdates.
Проверяются crash до admission commit/после commit до offset, claimed/API-start,
потерянный результат, атомарный rollback snapshot+history+outbox, восстановление
committed, неизвестная/многочастная доставка, bounded admission, SQLite busy,
maintenance/drain, replay и изоляция.

Гонка прав проверяется разными экземплярами AccessRepository и реальными locks
двух SQLite: revoke до commit блокирует мутацию; revoke во время permission gate
ожидает окончания v2 commit; revoke/reapprove не оживляет старую операцию.
Проверяется настоящий callback, fail-closed approval notification и авария после
access commit без повторной transition. DPAPI round-trip использует синтетический
binding credential на Windows; это не обращение к API/production секретам.

Rollback тест загружает Repository из Git-объекта опубликованного aa11380:
queue schema блокирует старый runtime, queued/unknown запрещают downgrade;
verified archive и разрешённое удаление только operational таблиц сохраняют
хеши/количества всех core-таблиц. Offset не изменяется.

Фикстура четырёх балок остаётся буквальной, итог 381,164 т. Новые отрицательные
тесты исключают удаление product/unresolved/referenced/overlapping whitespace,
дубликатов IDs и недопустимых границ. Проверены CRLF, Unicode, шапка и примечание.
Отдельно проверены сообщения без внутренних кодов и unsupported append без мутации.

Mock подтверждает Python и интеграцию, но не качество решений живой модели,
реальное ограничение API-аккаунта или устойчивость Windows при отключении питания.
Результаты текущего запуска: reports/016-concurrent-queue-implementation.md.

## Безопасный офлайн-запуск

Mac: `.venv/bin/python scripts/v2_offline_tests.py tests/test_steel_agent_v2.py tests/test_steel_agent_v2_prelaunch.py`

Windows: `.\.venv\Scripts\python.exe scripts\v2_offline_tests.py tests/test_steel_agent_v2.py tests/test_steel_agent_v2_prelaunch.py`

Launcher очищает API/Telegram-переменные в дочернем процессе, отключает dotenv,
сеть и плагины pytest. SQLite допустима только внутри worktree и системного tmp;
тесты используют временные БД. Наличие mock API не означает разрешения реального
polling: запрещённый сетевой вызов завершится ошибкой. Operational-тесты запускают
только отдельные тестовые процессы блокировок, не main.py/бота.

Интеграция/регрессии: добавить tests/test_windows_operations.py tests/test_access.py
tests/test_telegram.py. Полный набор — без аргументов. Выводы №2 хранятся локально
в tmp/steel-v2/prelaunch-targeted.txt и prelaunch-full.txt, исключены из Git.
Итоговые числа текущего этапа — reports/002-prelaunch.md.

## Что доказывают офлайн-тесты

Проверяются: исходный текст и literal provenance, partition и Decimal; полная
атомарность серии правок; адресные изменения/undo/stale revision; текущий snapshot
в диалоге; отсутствие изменений от chat/search; повтор user/update/hash и
конкуренция; повторное открытие SQLite; SQL rollback и inflight при отказе записи;
невалидный JSON, ссылки и отказ GPT; отзыв прав и plain delivery; SDK parse/search
через HTTP MockTransport; отсутствие поиска без подтверждённых источников.

№2 добавляет fault injection формирования ответа и интеграцию настоящего
run_bot с fake Telegram/API: legacy active не загружается как v2, права и прежняя
история не меняются, offset продолжается, повтор update не вызывает модель.
Отдельно проверены fail-closed конфигурация, отсутствие legacy AI в ветке v2,
конфигурируемый reasoning и SQLite backup/reopen без перезаписи копии.
Тест reapproval подтверждает утверждённую в №3 политику: отзыв блокирует доступ,
данные сохраняются, повторное одобрение того же Telegram ID возвращает контекст.

## Корпуса B6

Только read-only Git-объект 510be4e332ea1f8811dc63e9cf04b3996e62fbe9:
tests/fixtures/b6/manifest.json и *_source.txt. Проверяются SHA256 исходников,
38 / 205.355 т, 22 / 121.900 т, 27 / 442.296 т, 10 / 476.313 т.
Код B6 и старые expected-группы не импортируются. GPT-предложения подменены.
Если объект отсутствует, тест сообщает skip; это не успешная приёмка корпуса.
На Mac в №1/№2 все четыре объекта доступны, пропусков этих корпусов нет.

## Исходные семь падений общего pytest

До №1: 1204 passed, 7 failed, 4 deselected, 86 subtests passed.
После №1: 1273 passed, те же 7 failed, 4 deselected, 86 subtests passed.

1. tests/test_armatura_price_ingestion.py::ArmaturaPriceIngestionTests::test_grade_variants_are_not_inferred_for_other_listed_diameters — нет tmp/23met_research/armatura_a1_12.html.
2. tests/test_source_coverage_2b.py::test_38_source_positions_attributes_and_retry_are_wrap_independent[retry-real-omission-wrapped38].
3. Тот же тест [retry-real-omission-flat38].
4. tests/test_source_text_fidelity.py::test_audit_failures_preserve_existing_request_and_retry_once[extra].
5. Тот же тест [city].
6. Тот же тест [merged].
7. tests/test_stabilization.py::StabilizationTests::test_medium_once_after_completeness_failure_and_one_save.

Во 2–7 ожидается medium, получен high: legacy startup мутирует os.environ, а
исходный startup-тест не восстанавливает все поля. Изолированный запуск этих
сценариев в №1 давал 8 passed. Эти тесты/ожидания/fixtures в №2 не исправлялись,
skip/xfail для сокрытия ошибок не добавлялись.

## Живые GPT-испытания — только после отдельного разрешения

`python scripts/v2_llm_check.py --live --b6-ref 510be4e332ea1f8811dc63e9cf04b3996e62fbe9 --output tmp/steel-v2/live.json`

Ключи передаются через защищённое окружение; runner не читает .env, не запускает
Telegram и использует временную БД. Можно задать собственные --input файлы.
Фиксируются model/reasoning, время, число вызовов/HTTP-попыток, tokens/reasoning,
исходные позиции, суммы, группы и snapshot. Снимки содержат заявки — хранить локально
вне Git. При недоступности корпуса runner завершается до создания GPT-клиента.

Человек сравнивает исходник и результат: полноту каждой позиции/характеристики,
количества, сохранённые отличия и обоснованность коммерческих групп. Точное
совпадение со старым текстом/числом групп не является критерием. Mock не оценивает
качество модели. Отдельно нужны живые последовательные правки/отмена, новый запрос
после старого, вопрос без заявки, обсуждение без мутации, search с источниками и
отказом, попытка веб-инструкции изменить заявку в последующем диалоге.

После разрешённого переключения в существующем UVM_Steel: один согласованный
участник проверяет те же сценарии, повтор update проверяется локально без спама.
Наблюдение не менее 3 минут, без 409/auth/import/SQLite ошибок, с контролем прав,
старых данных и одного polling. Операционный порядок — WINDOWS_DEPLOYMENT.md.

## Сверка API

Сверено 2026-10-17: openai==3.19.2 / pydantic==2.13.5, установленный Responses
SDK и подменённый HTTP. По [модели Luna](https://developers.openai.com/api/docs/models/gpt-6-luna)
поддерживаются Responses, structured outputs и web_search; используемый high допустим.
Схема [structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
и [web_search/citations](https://developers.openai.com/api/docs/guides/tools-web-search)
сверены с реализацией. Это не доказывает доступность инструмента конкретному аккаунту.
