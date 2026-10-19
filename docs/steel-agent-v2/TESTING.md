## Этап 6: расчётные аннотации м/шт

## Этап 7 — пакет approved знаний

`tests/test_steel_agent_v2_knowledge.py` проверяет воспроизводимость artifact,
хеши/редакции/approved-only, текущие итоги всех шести CASE (25 позиций), отсутствие
исторической ошибочной массы, релевантность, независимость контекстов, отказ при
повреждении пакета и изменении статуса источника. Все CASE проходят SteelAgent и
Repository без инъекции mass_catalog: текст, ID/порядок, исходные характеристики,
raw/source_ids, точные массы, ссылки расчётов, версии, edit/undo/restart и event replay.
Есть настоящий SDK с mock HTTP: пакет в фактическом input, прежняя модель/high,
один вызов. Двойные исходные количества CASE-005 не учитываются дважды; неизвестный
товар не блокируется, scopes массы не расширяются на другой вид/марку/длину/резку/ГОСТ. Отдельно
проверяются конфликтующая стенка, пропущенные/лишние/перепутанные размерные роли:
сам профиль не позволяет обойти числовую область утверждённого коэффициента.

Перед публикацией: `python -B scripts/build_v2_knowledge.py --check` и полный
офлайн-набор v2/Windows/access через scripts/v2_offline_tests.py. Сеть и доступ к
пользовательским SQLite этим launcher запрещены. Старые прямые render-проверки
сохраняют прежний режим; нормализованный ответ агента использует компактный режим.
Эти тесты доказывают обработку заданного валидного плана, не способность настоящего
GPT классифицировать товар, точно следовать знаниям или противостоять всем prompt
injection. Живые испытания требуют отдельного разрешения, в этапе 7 не выполнялись.
[Итоги/ограничения](reports/stage-7-approved-knowledge.md).

tests/test_steel_agent_v2_theoretical_mass.py: 64 адресных сценария на разрешённом
эталоне и синтетике. Проверяется точная арифметика 28,6034375 т, отсутствие подмены
исходного количества, альтернативная марка, длина штуки/default/резка, применимость
профиля/норматива/марки, кг/шт и длина, ошибки коэффициента/quote/единиц, источники,
conflicting/unknown, edit/undo/restart, ID-карта, полная атомарность подтверждения,
изолированные пользователи, immutable multipart HTML, delivery_unknown и отсутствие
повторного поискового API после неизвестного исхода. Старый тест supported_edit_operations
дополнен calculate_mass; его прочие assertions сохранены. Skip/xfail не добавлены.
Mock-планы и search не доказывают живую классификацию GPT, качество интернет-фактов
или техническую взаимозаменяемость. Итоги — [отчёт](reports/stage-6-theoretical-mass.md).

Повторный review: tests/test_steel_agent_v2_mass_review.py добавляет 58 сценариев;
совместно **122 passed**, полный набор **792 passed**. Два способа расчёта штук при резке, справочная длина
кг/шт в catalog/applicable/calculate/refresh/research/confirm/validate, default12м
только балки, сохранённый повреждённый JSON после restart, отдельная фиксированная
масса штуки, объяснение/HTML/две ссылки и коммерческая политика только балок.
CASE-002/003/006: точные коэффициенты из явных fixtures, компактный текст,
edit номера 3/undo/история/версии/restart. Остальные CASE проверяют сохранность
исходников/количеств/provenance, без обещания живой нормализации всех 25 позиций.
Явно несовместимое исполнение не обходится через indicative; поиск должен
подтвердить его цитатой. Длина кг/шт из соседней строки не принимается.
Нет массового импорта знаний или чтения production; runtime seed по-прежнему три.
Изменены только assertions разрешённого оформления и прежний синтетический
штучный пример переведён на явный fixed_unit_mass; проверки фактов не удалялись.

## Исторический этап 5: компактные и полные сохранённые источники

tests/test_steel_agent_v2_compact_sources.py проверяет BR-007 через SteelAgent,
Repository, настоящий SDK с mock HTTP и прежний durable outbox. Синтетика:
1/2/10 источников, приоритет цитирований, дедупликация с сохранением разных titles,
Markdown/HTML/bare URL, фактические anchors, API markers/диапазоны, отсутствие URL,
unsafe targets, provider-only без выдуманного сайта, отсутствие нового search.
Полные списки читаются после restart и после выхода за bounded history;
проверены пользователь/request/revision isolation, барьер неудачного поиска,
длинные Unicode/HTML labels, multipart, повтор event_id, delivery_unknown,
неизменность snapshot/version/display и прежней SQL-схемы, старые receipts.
Естественные команды представлены mock-планами существующего координатора;
это не доказательство качества живого распознавания намерений или сайтов.
Три прежних UX-assertions обновлены для коротких ссылок и скрытого provider;
проверки provenance/readonly не удалены, skip/xfail не добавлены.
Результаты — [отчёт этапа 5](reports/stage-5-compact-sources.md).

## Исторический этап 4: нативный HTML и прежний durable outbox

tests/test_steel_agent_v2_telegram_html.py проверяет безопасные нативные payload,
неизменность буквальных технических символов, утверждённые семь листов, смешанные
товары, Markdown/HTML/URL, entities без повторного разбора, Unicode и длинные ссылки,
несколько частей, атомарный отказ, restart/dedup/unknown, старый plain outbox и
отсутствие глобального parse_mode. Все Telegram/OpenAI эффекты подменены.
Прежний тест прямого адаптера ожидает новый HTML-режим только у sendMessage;
проверки авторизации, отсутствия polling и повторного события сохранены.
Результаты/ограничения — [отчёт этапа 4](reports/stage-4-native-telegram-html.md).

## Исторический этап 3: группировка и числовая сортировка

tests/test_steel_agent_v2_display_sorting.py проверяет единую последовательность
строк/ID, шесть смешанных видов, полные марки/нормативы без слияния, числовые
толщины и вторичные размеры, неизвестные/несравнимые ключи, правку новой третьей
строки после пересортировки, undo/reopen/show/forward, историю и durable receipt.
Эталон tests/fixtures/steel_v2_stage3_approved_sheets.txt явно предоставлен и
разрешён владельцем; другие данные синтетические. Единица из общей шапки также
проходит реальный RequestEngine, а не подставляется в снимок в обход проверки.
SDK проверяет HTTP-схему через mock transport; сеть/платные API заблокированы
scripts/v2_offline_tests.py. Эти тесты не доказывают живое извлечение/классификацию.
Финальные результаты — [отчёт этапа 3](reports/stage-3-display-sorting.md).

## Исторический этап 2: семантическое редактирование, кандидат 19.10.2026

tests/test_steel_agent_v2_semantic_edits.py проверяет BR-010/011 через SteelAgent,
Repository, временные SQLite и fake Telegram/Responses. Сценарии: групповые марки,
отдельная толщина и компонент составного размера, прибавление/абсолютное количество,
длина в мм, неоднозначность с сохранением операции, карта после show/edit/undo/reopen,
неизвестная доставка, атомарный отказ и изоляция пользователей.
Wire проверяется настоящим SDK с mock HTTP. Новые инструкции не являются
доказательством семантической точности живой GPT.
Результат финальной регрессии и ограничения — [отчёт](reports/stage-2-semantic-editing.md).
Итог кандидата: 558 passed актуального контура, отдельно 132 passed/20 subtests
исторических проверок; новая группа включает 38 сценариев с параметризацией.
Число тестов прежних этапов ниже относится к их датам.

## Историческая проверка: №21–23

Патч №21 установлен в №22 на `4f7babf3e9aebee76e582737c2fe31680a1fa528`.
Последняя доступная регрессия №21/22 — **422 passed** актуального контура,
отдельно **132 passed, 20 subtests passed** исторических проверок. Наборы частично
пересекаются; не суммировать их как уникальные проверки. №23 меняет только Markdown,
проверяет ссылки/согласованность/факты/diff; Python-регрессия заново не запускалась.

Состояние подтверждений — [PROJECT_STATE.md](PROJECT_STATE.md). Качество извлечения
не доказывается количеством pytest. Предметная матрица —
[NORMALIZATION_CASES.md](NORMALIZATION_CASES.md): новые случаи помечены как
неисполненные, fake-планы не выдаются за живое поведение GPT. Целевые требования
[NORMALIZATION.md](NORMALIZATION.md) не описывают уже полностью готовый formatter.

## Патч №21: офлайн-доказательства

Новые проверки: tests/test_steel_agent_v2_021_ux.py и tests/test_typing.py.
Активный queue route теперь оборачивает GPT обработку TypingIndicator;
fake API обязаны отличать ephemeral sendChatAction от durable sendMessage.
Проверяются независимость пользователей, повторы индикатора, остановка/ошибка,
неизменность очереди и нормальный отказ без ложного append. Explanation repetition
пока диагностируется, формат не меняется. По решению владельца используются исключительно вымышленные строки из
steel_v2_021_synthetic_angles_channels.txt. Внедрённый в fake GPT дубликат grade
воспроизводит duplicate_characteristic:i1; это проверка строгого отказа Python,
не воспроизведение самостоятельного поведения живой модели. Положительный случай
сохраняет шесть позиций и Decimal 0,240726 т. Production тексты не копируются.
Полные результаты и границы доказательств: reports/021-live-test-and-typing.md.

Финал №19: **406 passed** актуального контура; отдельно **132 passed и 20 subtests
passed** исторических регрессий. Ни skip, ни xfail. Команды запуска — offline
launcher с явным списком файлов из reports/019-forward-only-deployment.md;
историческая группа перечислена там отдельно и не используется для обоснования
готовности нового ИИ-маршрута. pip check и diff --check проходят.

# Приёмка №19 — сохранённая база текущего набора

`tests/test_steel_agent_v2_forward_only.py` заменяет тесты установки guard №18:
штатный PowerShell start с перехватом только Start-Process; неизменность прежних
supervisor/stop/ProcessLock; отсутствие ProgramData/admin зависимости; все стадии
очереди, backup/reopen, миграция из aa11380, повреждение схемы/metadata/PK/orphans,
cursor/offset, запрет удаления очереди. Четыре selector-теста №17 проверяют текущий
реальный run_bot с missing/legacy config × queued/unknown: отказ до внешних вызовов.
Возврат старого SHA больше не поддерживается, поэтому внешний барьер через checkout
не является текущим критерием. Это изменение требований владельца, не skip/xfail.

Старые тесты синхронного poller и legacy reasoning перенесены в tests/legacy/.
Они читают фиксированный исторический telegram_bot и selector из Git aa11380,
не подменяют действующий v2 selector и не включают legacy в новом entrypoint.
Сохранены все assertions; этот набор запускается отдельно как исторический.
Старые тесты адаптера (test_telegram_operator, test_telegram, test_typing,
test_normalizer_stabilization) используют явно legacy_telegram_adapter и также
проверяются отдельно. AccessRepository — общий компонент, его набор остаётся
в приёмке v2. Активный create/edit/offset тест запрещает вызов старых AIService,
TelegramService.process и WorkAssistantService.answer. Отдельно проверяются AST
entrypoint и отказ missing/legacy конфигурации даже без существующей очереди.

Все прежние fault injection, overlap/FIFO/доступ/Decimal/история тесты остаются.
Список полного целевого набора и фактические результаты — reports/019-forward-only-deployment.md.
Исторические отчёты №15–18 не изменяются. Ни один API вызов не разрешён тестами.

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

Forward-only тест загружает Repository из aa11380 для проверки аддитивной
миграции настоящей прежней core schema. Hash/count всех core-таблиц неизменны;
полный Backup API архив сохраняет и queue-таблицы. DROP/downgrade не выполняются.

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

## Документационный №26

Проверяются ссылки, только Markdown в diff, статусы/источники/границы утверждений.
Новых предметных CASE и Python-изменений нет: pytest и платные API не запускаются;
исторические числа выше не выдаются за новый прогон. Владелец в задании №26
подтвердил typing в Telegram. Будущий пакет знаний проверять на утверждённых и
пограничных кейсах, с сохранением полноты/Decimal/истории; успех Python-тестов
не заменяет предметной приёмки. [Порядок обучения](knowledge/README.md).

## №27 — профессиональное представление

Новый файл tests/test_steel_agent_v2_027_presentation.py: 27 проверок синтетических
снимков, неизменности originals/items/groups/версий, create/show/edit/undo,
неизвестных полей и единиц, точного Decimal, нескольких нормативов, города/вопросов,
120 позиций через durable outbox и UTF-16/CRLF/сверхдлинной строки. Старые assertions
про версию/групповой итог обновлены под явное решение №27: проверки полноты,
partial и подробного предупреждения не удалены; skip/xfail не добавлены.

Актуальный набор из №19 плюс №21 и №27: **444 passed за 56,63 с**. Отдельные
исторические проверки: **132 passed, 20 subtests passed за 5,38 с**. Только offline
launcher с сетевым запретом, fake API, временные SQLite. Python 3.13.15,
OpenAI 3.19.2, Pydantic 2.13.5; pip check проходит. Списки команд и ограничения —
[отчёт №27](reports/027-professional-presentation.md). Качество живой нормализации
и видимость нового текста в Telegram ещё не проверены.

## №28 — P0 partial/recovery/corrections

Новый tests/test_steel_agent_v2_028_recovery.py проходит через реальные agent,
RequestEngine, Repository, Executor и установленный SDK c httpx2.MockTransport.
Сценарии: неполная заявка, поштучная независимая валидация после двух сбоев,
покрытие всего исходника, unknown/ambiguous/несогласованные характеристики,
duplicate_characteristic/плохие ranges/quantity/source/группы, pending после reopen
и за пределами history window, explicit resume, source identity, set по номеру,
массовая замена, составной размер, resolve+set/undo, атомарный отказ, идемпотентность,
изоляция, 200 позиций и durable части. HTTP timeout/incomplete/refusal/400 дают один
вызов без repair/replay; commentary игнорируется как structured plan.

Старые тесты оставляют прямой assert отказа RequestEngine на повреждённом плане и
дополнительно проверяют реконструированный partial. Skip/xfail не добавлены. Миграционная
фикстура создаётся опубликованным старым Agent+Repository, затем проверяется новым
кодом: смешивать новый API pending с историческим Repository неверно. Новых DDL нет.
Команды, точные результаты и пределы доказательств — [№28](reports/028-partial-normalization-and-corrections.md).

Финал №28: **488 passed за 66,65 с**, отдельно **132 passed / 20 subtests за 5,02 с**;
44 новых проверки. Live GPT не выполнялся.
