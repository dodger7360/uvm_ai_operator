# Codex №19 — forward-only deployment и durable queue

2026-10-18. Кандидат для review; deployment в этом задании не выполняется.

> Steel Agent v2 развивается по стратегии forward-only. Откат на устаревший код не является поддерживаемым штатным сценарием. Сохранность данных, очереди и восстановление после аварий остаются обязательными.

## 1. Исходное состояние и границы

Отдельный worktree `steel-agent-v2-forward-only`, ветка
`feature/steel-agent-v2-forward-only`, исходный HEAD
`3c0d28fe1ad6fec341987ada3fc05a1260f89412`. До правок дерево чистое;
remote persistent-guard совпал с этим SHA. Production отдельно проверен только
чтением: `aa11380dfe1ed646fec2355b22880fdb03b0d706`, detached, чистое дерево.

Действующая задача UVM_SteelBot: Ready, RunLevel Limited. Action — прежний
production `.venv\Scripts\pythonw.exe` с аргументом `windows_supervisor.py` и
рабочим каталогом production. Изменений задачи нет. Фактические native PID:
supervisor 7424, worker 9500; 28700/4588 — venv redirectors, не второй polling.
Проверка текущего токена Windows: IsInRole(Administrator)=False.

Production interpreter и отдельное тестовое окружение: Python 3.13.15,
openai 3.19.2, pydantic 2.13.5, httpx2 2.13.1, pytest 9.1.1.
Ни production Python, ни зависимости, .env, реальные SQLite и процессы не менялись.
Реальные /claim credentials, переписка и заявки не читались для тестов.

## 2. Что удалено и что сохранено

Удалены рабочие установщики/bundle `ops/windows_guard/*`,
`scripts/v2_guard_proposal.py`, `scripts/v2_queue_epoch.py`.
Нет ProgramData, административных ACL/RunAs, manifest, epoch activation,
retirement или смены Task Scheduler Action. Исторический проект
PERSISTENT_STARTUP_GUARD.md сохранён с пометкой о решении №19;
отчёты №15–18 не изменены. Guard не был установлен в production.

`start-telegram.ps1` восстановлен байт-в-байт (с нормализацией Git line endings)
из production aa11380. `stop-telegram.ps1`, windows_supervisor.py и
windows_runtime.py остались идентичны production. Один bot token, один poller,
прежние OS locks, supervisor и кооперативный maintenance. Обычная учётная запись
запускает прежний supervisor; дополнительных системных компонентов нет.

После ручной остановки задания владелец уточнил: единственный мозг Telegram —
Steel Agent v2 / GPT-6 Luna High. Сохранённый diff повторно проверен без reset.
Из действующего telegram_bot удалены импорты/создание AIService, TelegramService,
WorkAssistantService и прежний синхронный intent dispatch/polling loop.
run_bot собирает только v2 и запускает только durable run_queue. selection()
принимает только v2, без параметра обхода. Missing/legacy config — отказ до
Telegram API, exit 7. Старый ИИ не может включиться через handler/config.

Старые handle_update/send_operator_message сохранены в legacy_telegram_adapter.py
для отдельного сравнения; действующий entrypoint и v2 их не импортируют.
Полезные парсеры, прежние ИИ-модули, библиотеки и SQLite сохранены, их предметная
логика не менялась. TelegramRepository остаётся инфраструктурой общего offset и
прав; старый нормализатор/intent router из него не вызываются.

Read-only queue CLI сохранён; --check-forward проверяет данные/схему/направление
курсоров, а не право downgrade. Операторское --abandon сохраняет maintenance,
оба OS locks, точное подтверждение, историю и запрет replay/send. Старый импорт
prepare_rollback теперь всегда отказывает до любых файловых операций; CLI
retirement/DROP больше нет. drained() — только состояние очереди, не startup gate.

## 3. Инварианты данных без внешнего guard

Durable inbox/attempt/outbox/meta schema=1 остаются в той же SQLite v2, прежние
core-таблицы неизменны. QueueConfig: 3 workers, 100 pending, 20 на пользователя,
8388608 байт входящего payload. Ни денежных, ни токенных ограничений не добавлено.

Проверки перенесены в обычный data layer: версия/полный набор таблиц,
columns и primary keys, integer nonnegative metadata, cursor выше всех inbox IDs,
отсутствие orphan attempts/outbox, quick_check. Repository проверяет существующую
queue до любых CREATE, частичную core/schema не «чинит» созданием недостающих таблиц.
Повреждение/неизвестная версия — отказ без очистки. Оставшийся исторический
ключ guard_epoch не требуется и не удаляется: значения данных не зависят от manifest.

Schema/cursor validation Repository и read-only status используют один read
transaction. Управляемый WAL-тест доказывает, что concurrent admission между
чтением metadata и inbox не создаёт ложного повреждения из смешанных снимков.
Этот read lock короткий, до GPT; write transaction во время GPT не удерживается.

Порядок write-before-ack неизменен: inbox+durable cursor commit, затем legacy offset.
Lag mirror восстанавливается вперёд; ahead после initialization запрещён. Значения
update_id не считаются непрерывными. Дедупликация, stage CAS, atomic snapshot/
version/history/claim/outbox, FIFO, fairness, отдельные SDK clients и диагностические
контексты сохранены. queue_runtime и transport не менялись.

## 4. Конкурентность, аварии и авторизация

Event/barrier тесты доказывают перекрытие трёх fake GPT, ожидание четвёртого,
независимость медленного A и B, FIFO A1→A2 и изоляцию ответов/контекста/версий.
Это реальные ожидания внутри fake вызовов, не только создание потоков.

Сохранены subprocess os._exit crash tests №17 на границах inbox/offset,
claimed/API-start/result/transaction commit, commit/delivery-start/receipt и
между частями ответа. Нет потерянных принятых исходников и повторных мутаций.
Unknown GPT и неизвестная доставка не повторяются. Только queued/proven unstarted
и точно не начатые части committed outbox продолжаются автоматически.

Дополнительно Backup API + reopen проверены на каждой стадии очереди, включая
queued, claimed, api_started, result_received, committed, delivery_started,
оба unknown и delivered. Все core hash/count сохраняются. Отказ аддитивной
миграции до commit откатывает весь DDL, не трогая историю. Backup не является
разрешением удалить текущую очередь или вернуть старый код.

AccessRepository не менялся. Прежний legacy BEGIN IMMEDIATE permission gate
линейризует v2 commit/delivery-start: revoke, повторное approve и смена revision
не оживляют старое задание. Durable administrative callback не исполняется дважды.
Уже начатый внешний вызов после revoke нельзя гарантированно отменить; его
результат не получает право на мутацию/новую доставку автоматически.

Maintenance прекращает admission/scheduling, выполняющиеся операции кооперативно
завершаются, queued остаются на диске. Timeout 180 секунд не приводит к kill.
Новый процесс не запускается, пока OS lock прежнего удерживается.

## 5. Нормализация и поиск

Agent/engine/contracts/wire/transport не менялись. Четыре буквальные балки из
фикстуры №15 сохраняют 4 позиции, Decimal количества и итог **381,164 т**.
Исправление №16 удаляет только доказанно нессылаемые service whitespace records;
после него остаётся полная проверка источников и количества. Product/unresolved,
реальные пропуски, duplicate IDs и спорные диапазоны не замалчиваются.

«Добавь» не превращается в create/append и не применяет отклонённый план:
сохраняется понятное clarify без schema:value_error пользователю. История/undo,
Structured Outputs и Web Search входят в общий целевой набор. gpt-6-luna/high
проверяется по настоящему SDK payload через mock HTTP. Качество живой GPT и
успех живого поиска этим этапом не подтверждаются.

## 6. Проверки и изменение критериев

46 тестов отменённой инфраструктуры guard заменены 55 проверками действующего
forward-only поведения; число общих проверок не снижено. Четыре selector-теста
№17 остаются четырьмя проверками queued/unknown × missing/legacy config на реальном
current run_bot. Требование о внешней защите после checkout старого SHA отменено
владельцем; прежние отчёты сохраняют исторический результат без переписывания.
Остальные crash/concurrency/access/Decimal/история проверки не отключены.

Прежние unit-тесты синхронного poller и legacy reasoning перенесены в tests/legacy/:
они загружают фиксированные исторические bot+selector из aa11380, без подмены
current runtime. Все старые assertions сохранены. Регрессии старого адаптера
проверяются отдельно через явно legacy_telegram_adapter, не через текущий worker.
Это исторические сравнения, не доказательство готовности нового ИИ-пути.
В активном create/edit/replay тесте старые AIService, TelegramService.process и
WorkAssistantService.answer запрещены sentinel mocks. Missing/legacy config
отклоняются также без очереди; актуальный entrypoint не содержит их сборки.
Нет skip/xfail, не удалялись проверки действующих data invariants.

Целевой набор (offline launcher, fake OpenAI/Telegram, только временные SQLite):

```text
tests/test_steel_agent_v2.py
tests/test_steel_agent_v2_prelaunch.py
tests/test_steel_agent_v2_windows.py
tests/test_steel_agent_v2_wire_schema.py
tests/test_steel_agent_v2_api_diagnostics.py
tests/test_steel_agent_v2_ux_search.py
tests/test_steel_agent_v2_search_runtime.py
tests/test_windows_operations.py
tests/test_access.py
tests/test_steel_agent_v2_015_reproduction.py
tests/test_steel_agent_v2_queue.py
tests/test_steel_agent_v2_017_acceptance.py
tests/test_steel_agent_v2_forward_only.py
```

После первоначального прогона 468 passed требования уточнены владельцем;
это число не является финальной приёмкой уточнённого v2-only entrypoint.
Отдельный исторический прогон после уточнения: **132 passed, 20 subtests passed**
(tests/legacy/test_sequential_poller.py, tests/legacy/test_reasoning_high.py,
tests/test_telegram_operator.py, tests/test_telegram.py, tests/test_typing.py,
tests/test_normalizer_stabilization.py). Ни один из них не включает старый ИИ
в действующий entrypoint. Финальная приёмка актуального v2-only контура: **406 passed за 54,98 с**.
Отдельная историческая регрессия: **132 passed, 20 subtests passed за 5,11 с**.
Итого два непересекающихся набора: 538 passed и 20 subtests passed. Нет падений,
skip/xfail; full pytest не запускался. pip check: No broken requirements found.
git diff --check проходит; предупреждения Git касаются только LF/CRLF.
Рабочий процесс проверки не имел административных прав; live API отключён launcher.

## 7. Установка только после отдельного согласования

Полная пошаговая процедура и команды — WINDOWS_DEPLOYMENT.md:

1. Проверить точный опубликованный полный SHA из этого release commit и ancestry
   от production; staging offline suite; текущие PID/конфиг/Task Action/диск.
2. Штатный stop, дождаться supervisor/worker, проверить оба locks.
3. Новые согласованные Backup API копии всех используемых SQLite вне Git;
   integrity/reopen/hash/count/history, прежний SHA и handler config.
4. Проверить старые inflight без inbox/attempt; неизвестные не удалять.
5. Safe detached checkout approved SHA с --no-overwrite-ignore, проверить imports/
   pip check, неизменность .env/Task/config.
6. Аддитивно инициализировать очередь, schema/cursor check, сравнить core hash/count;
   recover только по документированным стадиям. Никакого удаления pending/unknown.
7. Прежний start от обычного account; один polling, три execution threads,
   gpt-6-luna/high, права, SQLite и история; наблюдать минимум 3 минуты.
8. Живой параллельный тест выполняют пользователи только после отдельного разрешения.
9. Сбой — maintenance, свежие backup/диагностика и следующий совместимый fix-forward.
   Старый SHA/handler и старые SQLite поверх новых событий не запускать/не восстанавливать.

## 8. Ограничения

Exactly-once внешних GPT/Telegram вызовов не заявляется. Unknown требует ручного
разбора/точного offline подтверждения. Пустая очередь не разрешает downgrade.
Произвольный ручной запуск старого checkout теперь вне поддерживаемой модели;
постоянного внешнего барьера против такого действия нет по решению владельца.

Backpressure одного пользователя может остановить общий admission на первом
непринятом update; уже принятые чужие jobs исполняются. Oversized одиночный update
требует вмешательства, не скрытого advance offset. Pending bytes — лимит входящих
payload, не лимит полного SQLite файла, outbox, истории или стоимости API.
Многобазовый backup согласован только после остановки writers. /claim DPAPI требует
того же Windows account. Новая схема будущей версии требует явной миграции вперёд.

В №19 не было deployment, остановки/перезапуска, реального polling или платных
API-вызовов. READY FOR DEPLOYMENT APPROVAL не является разрешением установки.

## 9. Перечень изменений и результат

Код текущего контура: telegram_bot.py (единственный v2 маршрут),
steel_agent_v2/runtime.py (v2-only selector), steel_agent_v2/queue.py
(локальная fail-closed валидация, drained), steel_agent_v2/repository.py
(один snapshot проверки), scripts/v2_queue_status.py (forward health, без DROP),
start-telegram.ps1 (прежний прямой supervisor).

Сохранённый старый адаптер: legacy_telegram_adapter.py. Тесты: новый
test_steel_agent_v2_forward_only.py, обновлённые prelaunch/queue/017;
старые импорты адаптера явно перенаправлены в test_access, test_telegram_operator,
test_telegram, test_typing, test_normalizer_stabilization. Синхронные poller tests
выделены из test_windows_operations в tests/legacy/test_sequential_poller.py;
reasoning legacy tests перенесены в tests/legacy/test_reasoning_high.py.
reference.py фиксирует historical bot+selector из aa11380, не меняя текущий модуль.
Отменённый test_steel_agent_v2_guard.py заменён forward-only data/startup проверками,
а не скрыт skip/xfail. Исторические отчёты сохранены без изменения.

Документация: ARCHITECTURE.md, CONCURRENT_QUEUE_DESIGN.md, TESTING.md,
WINDOWS_DEPLOYMENT.md, историческая пометка PERSISTENT_STARTUP_GUARD.md и этот отчёт.
Удалённые guard компоненты перечислены в разделе 2.

Перед публикацией повторно подтверждены чистый production aa11380, прежние Task
Action и PID 7424/9500. Проверяются только свои исходники/фикстуры: секреты,
пользовательские SQLite, backup, .env, логи, venv и runtime config в коммит не входят.
Новый коммит и ветка публикуются только как кандидат, полный SHA приводится в
итоговом сообщении и проверяется против GitHub HEAD. main не изменяется.

**READY FOR DEPLOYMENT APPROVAL.** Готовность подтверждена офлайн, не является
разрешением установки. Production продолжает работать на aa11380. Следующий шаг —
отдельное согласование опубликованного полного SHA и установка по runbook.
