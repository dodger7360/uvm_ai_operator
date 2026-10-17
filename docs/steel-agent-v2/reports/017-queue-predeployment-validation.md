# Codex №17 — предпусковая приёмка durable queue

Дата: 2026-10-17. Windows, PowerShell. **REQUIRES DESIGN CHANGES**.
Это отчёт о проверке и одном минимальном исправлении, не разрешение deployment.
Четыре проверки обязательного rollback gate остаются красными, без skip/xfail.

## 1. Исходное состояние и область проверки

- Production HEAD: `aa11380dfe1ed646fec2355b22880fdb03b0d706`, detached, чистое дерево.
- Исходный кандидат: `8ead20a87789489ac02ebb4fb6eed8339b7d5211`.
  Локальная `feature/steel-agent-v2-durable-queue` и GitHub совпали по полному SHA.
  Worktree кандидата чистый, четыре материала №15 сохранены в этом коммите.
- Отдельный worktree приёмки: `steel-agent-v2-predeployment`, ветка
  `feature/steel-agent-v2-predeployment-validation`, основа — точный SHA кандидата.
- Git production сначала давал 51 предупреждение доступа к временным каталогам
  из sandbox. Read-only проверка с правами владельца прошла без предупреждений и
  показала чистоту, отсутствие незавершённых Git-операций. Ничего не удалялось.
- Python production и тестового venv: 3.13.15; OpenAI 3.19.2, Pydantic 2.13.5,
  httpx2 2.13.1. Production `pip check`: No broken requirements found.
  Использовано существующее отдельное тестовое venv, зависимости не обновлялись.
- Worker PID 9500, supervisor PID 7424. Venv redirectors PID 4588/28700 — их
  родители, а не дополнительные poller/supervisor. Ready PID совпал с 9500.
  Владелец всех процессов совпал с Windows account проверки; имена не публикуются.
- Handler config: v2, отдельная `steel-agent-v2.sqlite3`. GPT-ключ присутствует;
  значение не выводилось. Luna/high подтверждены тестами фактического SDK payload
  и runtime factory. Живой API в этом этапе не вызывался.

Изучены queue, queue_runtime, repository, agent, transport, windows_secrets,
AccessRepository, telegram_bot, Windows lifecycle, CLI и нормативные разделы
CONCURRENT_QUEUE_DESIGN/ARCHITECTURE/TESTING/WINDOWS_DEPLOYMENT/отчёта №16.
Исторические разделы документации не заменяют проверку исполняемого кода.

## 2. Онлайн-снимки и миграция копии

Создан новый защищённый каталог вне Git: `stage17_online_20261017_154837`
в существующем локальном backup root. Полный путь передан владельцу отдельно.
ACL нового каталога: только текущий account и SYSTEM, без унаследованных разрешений.

Каждая база открывалась `mode=ro`; использован SQLite Backup API. Checkpoint,
VACUUM и записывающие PRAGMA на production не выполнялись. У каждой копии проверены
integrity_check=ok, повторное открытие, количества строк, хеши строк/схемы и файла.
Отдельные manifests, set-manifest, прежние handler config и полный SHA сохранены
локально. Исходные запечатанные копии повторно проверены по хешам после испытаний.
Секреты, сообщения, Telegram user IDs и копии в Git не попадали.

| Копия | Контрольные показатели |
|---|---|
| operator.sqlite3 | 9 таблиц; active=2, history=97, access_user=3, access_event=5, state=41 |
| steel-agent-v2.sqlite3 | 4 таблицы; active=2, version=5, turn=31, inflight=0 |
| ai-budget.sqlite3 | 1 таблица, 19 записей; сохранён существующий журнал, его использование v2 не утверждается |

Это **отдельные согласованные снимки баз, не общая атомарная копия набора**.
Работающий worker может изменить базы между backup. `cross_database_atomic=false`
зафиксирован в manifest. Общий согласованный backup обязателен после остановки
на будущем этапе установки. Для функциональных тестов использовались синтетические
БД; реальные копии не передавались даже mock-модели.

Из запечатанного v2 backup через Backup API создана `migration-working.sqlite3`.
Только она мигрирована: schema=1, созданы inbox/attempt/outbox/queue_meta, повторное
открытие и повторная инициализация успешны. Хеши, схемы и количества **всех четырёх
старых таблиц полностью совпали** до/после; не только число активных заявок.
Неизвестная/повреждённая схема отклоняется профильными тестами, без авторемонта.

Старых inflight в снимке нет. Перед deployment это нужно проверить заново после
остановки. При обнаружении — BLOCKED: архивировать базу и безопасную диагностику,
сверить конкретную операцию/известный исход API/состояние версии, получить явное
решение владельца. Не чистить inflight и не воспроизводить оплаченный вызов.
Новый recovery также отказывает для unmanaged pre-queue claim.

## 3. Конкурентность, FIFO и backpressure

События синхронизации доказывают реальное перекрытие A/B: B входит в model.plan
после входа A и до освобождения A; `a_start < b_start < a_end`. Пока fake GPT A
ждёт, независимые соединения успешно берут BEGIN IMMEDIATE **в обеих SQLite**.
Write transaction на время GPT не удерживается. Тесты также доказывают одновременный
вход трёх пользователей, ожидание четвёртого, FIFO A1→A2, изоляцию истории/версий
и соответствие chat_id. Не используется доказательство только по числу потоков.

У каждого исполнения собственный SDK клиент, Telegram delivery adapter и контекст
диагностики. SQLite connections короткие и независимые; CAS и SQLite writer gate
защищают общий durable state. Тестируются busy/locked и отсутствие сетевого replay.
Fair scheduling по durable served ticket подтверждено для уже принятых заданий.

Проверены **фактические defaults**: workers=3, max_pending=100,
max_pending_per_user=20, max_pending_bytes=8388608. Дополнительные тесты заполняют
20/100, общий объём bytes и слишком большой отдельный event. Отказ не создаёт inbox
и не продвигает cursor; освобождение места возобновляет admission того же ID.

Отдельный настоящий run_queue с fake poller подтвердил head-of-line blocking:
переполненный пользователь удерживает первый непринятый update; следующий update
другого пользователя не принимается до освобождения места. ID не теряется,
offset не проходит через непринятый event, после освобождения оба попадают в inbox.
Это ограничение доступности, а не потеря данных. При unknown + заполненной очереди
одного пользователя задержка может стать неограниченной до действий оператора.
Для небольшого доверенного круга допустимость нужно явно согласовать; защиты от
flood здесь нет. Пропуск update или ранний ack не являются безопасным исправлением.

## 4. Аварии: отдельный процесс и настоящие SQLite transactions

Новый harness вызывает `os._exit(73)` на изолированных данных: это аварийное
завершение тестового процесса, не исключение, которое приложение успевает обработать.
Сеть в дочернем процессе запрещена; внешние эффекты заменены только счётчиками.

| Граница | После повторного открытия/recovery |
|---|---|
| До inbox | Задания нет, cursor/offset не изменены; Telegram должен доставить заново |
| После inbox, до legacy offset | Inbox сохранён, mirror отстаёт; dedup и движение mirror вперёд |
| После legacy offset | Inbox сохранён, queued, ID не потерян |
| Claimed до API | Возврат в queued при отсутствии attempt |
| API started | execution_unknown, платный вызов автоматически не повторяется |
| Получен результат, до commit | execution_unknown, активная версия не появилась |
| Внутри commit после SQL outbox/history | SQLite rollback всей транзакции; нет частичного snapshot/version/turn/outbox |
| Commit до delivery | Snapshot/history/outbox сохранены, продолжается только доставка |
| Delivery started до вызова транспорта | delivery_unknown: консервативно без повторной отправки |
| Отправка без receipt | delivery_unknown, не повторяется |
| message_id получен, receipt ещё не закоммичен | delivery_unknown, message_id в БД отсутствует, повторной отправки нет |
| Между частями | Receipt первой части сохранён; отправляются только части 2/3 |

Для unknown подтверждены блокировка следующего сообщения только этого пользователя
и возможность claim другого пользователя. Это не отменяет глобальный admission
backpressure, описанный выше. История/версии согласованы, cursor/offset не откручиваются.
Подтверждённая мутация не выполняется второй раз; точно не начатая доставка может
продолжиться. Exactly-once внешних GPT/Telegram эффектов не обещается.

## 5. Unknown, доступ и Windows lifecycle

Read-only CLI показывает только состояния/счётчики/технические ID/opaque confirmation.
На synthetic БД проверено закрытие unknown с точным подтверждением, сохранение
обычного исходника в history, отсутствие API/send/mutation и отказ при неверном confirm.
Новые тесты отдельно удерживают каждый из двух OS process locks: CLI отказывается
менять unknown при работающем worker либо supervisor. После освобождения + maintenance
явное закрытие работает. Записи заявок и версий не удаляются.

Авторизация: проверены revoke до/после admission, перед исполнением, во время GPT,
до commit, после commit до send; reapprove и смена revision не оживляют старое задание.
В новом тесте настоящий durable административный callback отзывает право, пока
employee fake GPT ждёт; commit его заявки запрещён. Callback дедуплицируется.
Независимый AccessRepository writer блокируется SQLite gate на commit, то есть
гарантия основана не на двух последовательных reads прав.

Граница гарантии: revoke после устойчивого API/send-start не может отменить уже
начатый внешний вызов. Новый тест отзывает право внутри fake send после маркера:
одна начатая доставка завершается с receipt, новых повторов нет. Не обещается
отмена принятого провайдером запроса. Между access commit и v2 completion общей
транзакции нет; административная авария карантинируется, не replay.

Maintenance прекращает admission/scheduling, active завершаются, queued сохраняются.
Существующие supervisor/lock тесты прошли. Новый тест копии настоящего stop script
с fake CIM проверил timeout-ветку при TimeoutSeconds=1: maintenance остаётся,
убийства процесса нет. Default=180 проверен по коду; ожидание реальных 180 секунд
и остановка production не выполнялись. GPT/repair/search могут длиться дольше
180 секунд; в этом случае ждать и разбирать, не снимать maintenance/locks.

DPAPI: синтетический секрет расшифрован в **другом процессе того же account**;
plaintext отсутствует в сохранённом blob. Реальные /claim credentials не читались.
Отказ **под другим Windows account не испытан**: подходящего разрешённого сеанса/
credentials нет; новые аккаунты/задачи не создавались. Тест повреждённого blob не
подменяет это требование. До полной приёмки нужна отдельная проверка другого account.

## 6. Дефекты rollback

### Исправлен локально: offset не входил в gate

В исходном `prepare_rollback` проверялись только закрытые queue/inflight/outbox.
Полностью завершённое задание при durable cursor=42 и legacy offset=0 позволяло
архивировать и удалить queue. Старый poller мог получить старые updates снова.
Тест до исправления упал: DID NOT RAISE ValueError. Предыдущий rollback-тест даже
оставлял legacy offset=0; его проверка не доказывала безопасный downgrade.

Минимальное изменение `scripts/v2_queue_status.py`:

- Для готовности нужны известная schema=1, закрытая очередь **и** равенство mirror/cursor.
- Явный `--legacy-database`; чтение только существующей БД через mode=ro, без dotenv,
  догадок о путях или изменения offset. Legacy/v2 должны быть разными файлами.
- `queue_drained` и `offset_verified` разделены; без legacy пути rollback_ready=false.
- Проверка до backup и повторно внутри v2 transaction перед DROP; несовпадение
  оставляет queue в целости. CLI мутации по-прежнему требует оба locks/maintenance.
- Проверены отсутствие пути, mirror lag/ahead, смена mirror между backup и DROP,
  отсутствие создания missing DB. Штатный drain→archive→DROP→старый Repository
  сохраняет все core hashes и offset. Новый guard ничего не исправляет в production.

### Не исправлен: handler/SHA bypass — требуется решение

`runtime.selection` кандидата **и aa11380** при отсутствии config либо явном legacy
возвращает legacy без проверки v2 queue. `telegram_bot.run_bot` уходит в старый
последовательный путь и не открывает v2 Repository. Его whitelist не помогает.
Штатный start script удаляет maintenance и не проверяет очередь.

Четыре обычных красных теста выражают обязательный fail-closed контракт для двух
SHA и двух config-вариантов. Ручная смена SHA **с сохранением handler=v2** действительно
отклоняется прежним Repository; сочетание со сменой/удалением config обходит защиту.
Документальный запрет — полезная процедура, но не требуемый технический gate.

Патч только нового selector не защитит старт после checkout старого SHA. Нужен
согласованный постоянный startup guard вне сменяемого checkout: явный runtime
manifest с путём v2/queue epoch, проверка независимо от handler, проверенный список
совместимых SHA и обязательный guard в поддерживаемом launch/supervisor пути.
Это затрагивает эксплуатационную границу и может потребовать согласованной настройки
точки запуска. Сейчас Task Scheduler не менялся. Защита от произвольного запуска
обходного Python администратором — отдельная модель угроз, не обещание этого проекта.
До выбора и проверки решения статус READY недопустим.

## 7. Тесты и артефакты

До локального исправления полная целевая регрессия №16: **384 passed, 35.78 s**.
Новые process/fault/limits/auth/DPAPI тесты запускаются через `v2_offline_tests.py`,
который очищает credentials/env, запрещает сеть и production SQLite. Дочерние
crash-процессы также блокируют сеть. Тесты не копировали `.env` или токен.

Итог после исправления offset: **412 passed, 4 failed за 52.63 s**. Все 384 прежние
проверки зелёные; новые — 28 passed и 4 failed. Все четыре падения — selector bypass,
обычные FAILED, без skip/xfail. Отдельного счётчика subtests этот вывод не содержит.
Исправление offset и отказ при повторной смене mirror проходят. `pip check` в
тестовом окружении также No broken requirements found; pytest 9.1.1.

Воспроизведение из worktree приёмки, `$testPython` — Python отдельного venv,
`$newTemp` — новый отдельный каталог в системном temp:

```powershell
$tests = @(
  'tests/test_steel_agent_v2.py', 'tests/test_steel_agent_v2_prelaunch.py',
  'tests/test_steel_agent_v2_windows.py', 'tests/test_steel_agent_v2_wire_schema.py',
  'tests/test_steel_agent_v2_api_diagnostics.py', 'tests/test_steel_agent_v2_ux_search.py',
  'tests/test_steel_agent_v2_search_runtime.py', 'tests/test_windows_operations.py',
  'tests/test_access.py', 'tests/test_reasoning_high.py', 'tests/test_telegram_operator.py',
  'tests/test_steel_agent_v2_015_reproduction.py', 'tests/test_steel_agent_v2_queue.py',
  'tests/test_steel_agent_v2_017_acceptance.py'
)
& $testPython -X utf8 -B scripts/v2_offline_tests.py @tests --basetemp=$newTemp --tb=short -ra
```

Нормализация буквального fixture четырёх балок, итог Decimal 381,164 т, адресные
правки, undo, history, Structured Outputs, high и Web Search входят в зелёную
регрессию. Это проверка offline-инвариантов, не качества живой модели.

Первый sandbox запуск не имел прав на temp; повтор выполнен под владельцем.
Начальная гонка в новом callback-тесте исправлена Events: callback поступает после
входа fake GPT. Это изменение harness, не скрытое исправление production-ошибки.

Изменены только `scripts/v2_queue_status.py`, профильный rollback-тест №16,
новый `tests/test_steel_agent_v2_017_acceptance.py`, Windows runbook и этот отчёт.
RequestEngine, контракты, GPT transport/prompt, Decimal, нормализация и runtime
не менялись. Diff/security check обязателен перед отдельным commit/push приёмки.
Публикация частичного исправления с красными acceptance-тестами не означает релиз.
Полный SHA коммита приёмки сообщается отдельно после публикации; основа остаётся
8ead20a. Ветка — `feature/steel-agent-v2-predeployment-validation`; main не менялся.

## 8. План будущей установки — сейчас НЕ выполнять

**Сейчас нет SHA, одобренного этой приёмкой к установке.** Исходный 8ead20a и ветка
приёмки не READY. После решения startup gate и cross-account DPAPI требуется
проверить новый полный SHA и получить отдельное разрешение. Не использовать имя
ветки как цель. Ниже условия последующего контролируемого этапа:

1. Проверить production HEAD/чистоту/незавершённые операции, опубликованный полный
   SHA, ancestry, ignored/untracked collisions, Python/pip check, конфигурацию и
   отдельные DB пути. Создать staging только от одобренного SHA; offline suite.
2. Зафиксировать исходный полный SHA, handler/queue config, контрольные метаданные
   access/history/offset. Проверить доступность постоянного startup gate и того же
   Windows account. Не менять `.env` или модель. При расхождении — BLOCKED.
3. Штатный stop-telegram.ps1; дождаться supervisor, worker и redirectors, обоих OS
   locks. При timeout сохранить maintenance и ждать; ни checkout, ни kill.
4. После остановки Backup API всех используемых SQLite в новый защищённый каталог
   вне Git. Reopen/integrity/count/hash/manifest + прежний config/SHA. Только теперь
   набор можно считать согласованным при отсутствии других writers. Fail — BLOCKED.
5. На копии backup повторить миграцию/сверку core hashes. Проверить реальные старые
   inflight; любой unmanaged claim — остановка и отдельный ручной разбор, не очистка.
6. Повторить чистоту Git. Безопасный detached checkout полного одобренного SHA
   с `--no-overwrite-ignore`, без reset/clean/force/merge. Проверить импорты/config.
   При неудаче не продолжать принудительно. Main не переключать и не переписывать.
7. При удерживаемой остановке применить аддитивную schema=1 в существующей v2 DB;
   повторить init/reopen/integrity/core hashes. Принять старый общий offset только
   как начальную baseline через штатный align_offset; не двигать offset назад.
   Не переносить активные legacy заявки. Luna/high и separate v2 DB сохраняются.
8. Штатный start только после успешных проверок и постоянного startup gate.
   Один supervisor и один native Telegram-worker; внутри него до трёх execution
   threads, не три polling-процесса. Проверить getUpdates/handler/high, queue schema,
   cursor/mirror, отсутствие 409/auth/import/SQLite, hashes старых данных.
9. Наблюдать ≥3 минут. Затем только пользователи вручную запускают согласованный
   параллельный A/B/третьего, проверяют независимые ответы и FIFO одного пользователя.
   Технические operation/attempt метаданные подтверждают перекрытие. Живые API не
   воспроизводить автоматически. Полноту/качество заявок проверить отдельно людьми.
10. При unknown — read-only queue_status с обоими путями, без текста сообщений.
    Сначала сверка сохранённого результата/receipt. Для явного abandon: stop, backup,
    maintenance/свободные locks, точное opaque confirmation; без GPT/send/replay.
11. При критическом сбое остановить штатно и сохранить свежие согласованные backup.
    Pending/unknown/inflight/невыверенный offset запрещают rollback: совместимый
    fix-forward/recovery вместо уничтожения очереди. Для закрытой очереди запустить
    guard с явным legacy путём. Если он запрещает возврат, не обходить config/SHA.
12. Только после успешного gate выполнить prepare-rollback с новым архивом всей v2
    DB и exact confirmation. Проверить архив/core hashes/offset. Удаляются только
    operational queue tables в проверенной процедуре; history/version не заменяются.
    Установить согласованный прежний полный SHA безопасным checkout, сохранить v2
    handler/DB/offset/историю, проверить совместимость и запустить штатно. Не
    восстанавливать старые SQLite поверх новых. Постоянный startup gate обязателен.

## 9. Итог

Сохранность однобазной миграции, write-before-ack, реальное конкурентное исполнение,
основные crash/unknown границы и контролируемая процедура archive подтверждены offline.
Локальный дефект offset gate исправлен. Обход rollback через legacy не устранён;
cross-account DPAPI не доказан; глобальный admission HOL требует явной оценки риска.

Production не переключался, не останавливался и не перезапускался; реальные SQLite,
`.env`, scheduler, история и polling этим заданием не изменялись. Живое качество
GPT не оценивалось. **REQUIRES DESIGN CHANGES — не READY FOR DEPLOYMENT APPROVAL.**
