# Отчёт Codex №16 — надёжная многопользовательская очередь

Статус: **IMPLEMENTED — READY FOR REVIEW**. Это кандидат для review, не разрешение
deployment и не подтверждение качества живого GPT/Telegram. Работа выполнена
на Windows в отдельном worktree, ветка `feature/steel-agent-v2-durable-queue`.
Основа: `aa11380dfe1ed646fec2355b22880fdb03b0d706`.
Четыре незакоммиченных файла №15 сохранены; проект и регрессии актуализированы,
исторический отчёт №15 и буквальная фикстура сохранены.

## 1. Структура и миграция

Один прежний worker process/ProcessLock и один poller. Отдельный scheduler thread
обслуживает bounded ThreadPoolExecutor: workers=3. Каждый execution создаёт свой
SDK client и Telegram adapter; общего mutable пользовательского контекста нет.
Планирование продолжается во время long getUpdates.

В существующую отдельную v2 SQLite аддитивно добавлены:

| Таблица | Назначение |
|---|---|
| steel_v2_inbox | update_id, actor, точный минимизированный payload/hash/bytes, state, admission flags/revision, execution grant, fairness ticket, commit/uncertainty flags |
| steel_v2_attempt | номер каждой plan/search/access попытки и её устойчивый исход |
| steel_v2_outbox | части ответа/administrative API calls, send state, подтверждённый message_id |
| steel_v2_queue_meta | schema=1, durable cursor, initial-baseline marker, счётчик обслуживания |

DDL транзакционна. Неизвестная версия, частичная/неправильная схема и чужая БД
отклоняются без автоматического ремонта. Active/version/turn/inflight не ALTER-ятся.
Не меняются legacy schema, .env, зависимости и пользовательские файлы.

## 2. Admission, offset, порядок и пределы

Структурно допустимое событие сохраняется транзакцией до записи legacy offset.
Нет общей транзакции двух БД: crash в промежутке означает durable dedup при
повторной доставке; при рестарте accepted cursor можно безопасно зеркалировать
в legacy. Непрерывность update_id не предполагается. Повтор принятого ID с другим
нормализованным payload останавливается, а не выполняется заново.

Первоначальный legacy offset принимается как baseline один раз. Последующее
опережение legacy относительно inbox блокирует запуск. Нельзя скрыть рассогласование
после ручного восстановления БД простым max двух cursor.

Один user_id имеет максимум одну активную операцию вместе с доставкой. Head job
с unknown блокирует последующие сообщения только этого пользователя. Claim —
CAS, head определяется update_id. Ticket последнего обслуживания задаёт round-robin;
новые пользователи присоединяются в конец, не вытесняют ожидающих.

Настройки в игнорируемом локальном `telegram-queue.json`; defaults:
workers=3, max_pending=100, max_pending_per_user=20, max_pending_bytes=8388608.
Это пределы принятых незавершённых входящих событий, не API-квоты. Ничего не
отрезается от исходного text. При заполнении update не подтверждается.
Нецелевые/невалидные события с достоверным ID получают устойчивый rejected.
Закрытые payload-копии очищаются, сохраняются 1000 terminal tombstones; более
старые ID ниже cursor не исполняются заново. Canonical history не удаляется.
Обычный исходник denied/закрытого unknown без agent finish сначала сохраняется
в turn. Pending/unknown не удаляются автоматически.

## 3. Атомарность и стадии

В одной транзакции finish: проверенный snapshot, новая версия при изменении,
история, завершение точного execution claim и **все** части outbox. Не отдельная
поздняя запись ответа. При SQL-сбое откатывается весь набор.

Путь: queued → claimed → api_started → result_received → committed →
delivery_pending → delivery_started → delivered. Состояния ограничены CHECK,
ключевые переходы — CAS. denied/rejected/execution_unknown/delivery_unknown
сохраняются отдельно. Платные plan/search попытки и repair учитываются отдельно;
repair только после известного ответа, существующее ограничение попыток сохранено.
SDK retries=0, gpt-6-luna/high, без новых денежных/токенных квот.

При старте marker фиксируется до API. Таймаут/потеря ответа не повторяются.
SearchUnavailable сохраняет outcome_known: сетевой отказ search больше нельзя
ошибочно считать обычной завершённой валидацией из-за наследования ValueError.
Известная HTTP-ошибка также не повторяется, но может закончиться failed turn,
не блокируя следующую явную пользовательскую операцию.

Send-start фиксируется до сети, sendMessage требует настоящий message_id.
Неизвестная доставка не повторяется. Можно продолжить только части, отправка
которых точно не начиналась; предыдущие receipt не теряются. Pre-queue cached Reply
доказывает commit, но не отсутствие прежней отправки — такая доставка карантинируется.
`delivered` — технический статус доставки; бизнес-результат Reply.status остаётся
ok/partial/failed/busy, ошибка не превращается в успешную нормализацию.

## 4. Авторизация и administrative events

`commit_permission` берёт legacy BEGIN IMMEDIATE, проверяет approved/revision,
держит legacy writer gate до окончания короткой v2 транзакции. Все конкурирующие
SQLite writers, включая другой AccessRepository, ждут этот lock. Порядок legacy → v2,
никакой write transaction не удерживается во время GPT. Это линейризация, а не
две последовательные несвязанные проверки прав.

Admission сохраняет generation права; API-start, commit и обычный send-start
проверяют ту же генерацию. Revoke/reapprove не оживляет прежнюю queued/inflight
операцию. После разрешённого API/send-start отзыв не может отменить уже принятый
внешний вызов. До выдачи доступа не происходит ретроактивного GPT-выполнения;
до получения admin-права нельзя заранее поставить исполнимый privileged callback.

Все callbacks/admin commands проходят inbox. Старый access.route вызывается
после устойчивого access marker. Реальные send/answer/edit journaled; постановка
уведомления в outbox не считается доставкой и не выдаёт доступ. Сохраняются
старые роли, revisions, server-side admin checks и fail-closed approval.
Legacy approval сохраняет lock на время реального уведомления; queue proxy
использует socket timeout=3. Это не строгая общая wall-clock граница.

Общей транзакции access commit/v2 completion нет. Crash между ними → unknown,
ручная сверка существующего access audit, без автоматической повторной transition
или отзыва уже предоставленного права. Эта граница явно протестирована.

Bootstrap /claim сохранён. Его credential шифруется Windows DPAPI перед inbox,
не попадает plaintext в SQLite, history и логи; stable input hash вычисляется до
шифрования. Нужен тот же Windows account после рестарта. Новых ролей, ключевых
файлов и зависимостей не добавлено.

## 5. Восстановление, диагностика, остановка и rollback

| Durable состояние после аварии | Действие |
|---|---|
| queued | можно продолжить |
| claimed без attempt/external marker | возврат в queued; снять лишь точно совпадающий claim |
| api_started или result_received без commit | execution_unknown, без GPT replay |
| committed / delivery_pending | только доставка не начатых частей |
| delivery_started без receipt | delivery_unknown, без resend |
| старый unmanaged inflight | блокировка запуска; автоматического abandon нет |

CLI `scripts/v2_queue_status.py` по умолчанию открывает SQLite read-only и
показывает технические счётчики, состояния/update IDs и opaque confirm, без
переписки и user IDs. Queue update_id связан в безопасных логах с operation ID
и существующими SDK-метриками. Каждый execution/resume delivery имеет отдельный
ContextVar; произвольные исключения/тела API в новые логи не попадают.

Операторское закрытие unknown не повторяет GPT/send и не откатывает сохранённые
данные. Нужны точный confirm, maintenance, свободные оба process locks и совпадение
root/handler/database. Полная процедура — WINDOWS_DEPLOYMENT.md.

Maintenance прекращает admission/scheduling, active кооперативно завершаются,
queued остаются на диске. Supervisor/start/stop/ProcessLock не изменены. До двух
plan и search с прежним timeout=180 могут превысить stop timeout=180: при timeout
нельзя убивать worker, менять checkout или запускать второй экземпляр.

Прежний v2 Repository из aa11380 отказывается открывать новые queue-таблицы:
старый runtime не начнёт polling с опережающим offset и незакрытой очередью.
Разрешённый downgrade требует полностью закрытых jobs/claims/outbox. CLI сначала
делает полный SQLite Backup API архив вне checkout с integrity/hash/count manifest,
проверяет source/backup, затем атомарно убирает только четыре operational таблицы.
Core/history/versions/legacy offset не заменяются. Queue audit остаётся в архиве.
Нельзя обходить gate выбором handler=legacy или ручным DROP. При queued backlog,
который нельзя завершить совместимым кодом, rollback остаётся BLOCKED.

## 6. Балки и «Добавь»

Буквальные четыре строки №15 проходят через Telegram adapter/agent/engine/temp DB.
Сохраняются 20Ш2/30Ш2/40Ш2/45Ш2, С355-5, оба исходных стандарта и количества
10,700 / 186,698 / 127,097 / 56,669. Итог **381,164 т**, исходник не изменён.

Удаляются только корректно ограниченные, ни на что не ссылающиеся, непересекающиеся
header/note whitespace-only records. До фильтра проверяются уникальность IDs и
геометрия; после — все существующие инварианты. Product/unresolved, referenced,
overlapping и неверные диапазоны не удаляются. Реальные пропуски/дубли остаются
ошибками. Проверены Unicode/CRLF, длинная шапка, запятые и примечание.
Точный прежний живой GPT-план не сохранён: его источник ошибки не выдумывается.

Новый append не добавлен. GPT получает явные supported_edit_operations и
append_supported=false, инструкция требует clarify вместо create при просьбе
добавить отклонённые позиции. Неуспешный edit/repair не заменяет текущую заявку;
технические коды исключены из пользовательского текста и остаются диагностикой.
Проверки моделируют намерение; живое понимание такого диалога ещё не аттестовано.

## 7. Проверки

Окружение: Windows, Python 3.13.15, OpenAI 3.19.2, Pydantic 2.13.5,
httpx2 2.13.1, pytest 9.1.1; `pip check` — No broken requirements found.
Установка/обновление пакетов не выполнялись.

Итог целевого offline launcher: **384 passed за 34,28 s**. Полный legacy pytest
не запускался. 65 новых параметризованных случаев №16; 10 случаев №15 сохранены
и актуализированы. Отдельного счётчика subtests этот вывод pytest не содержит.

В набор включены v2, prelaunch, Windows, wire schema, API diagnostics, UX/search,
search runtime, Windows operations, access, reasoning high, Telegram operator,
воспроизведения №15 и новая queue матрица. Events доказывают три пересекающихся
вызова и ожидание четвёртого, FIFO и отсутствие блокировки B медленным A; проверены
fairness, отдельные клиенты/context, replay, crash boundaries, SQLite rollback/busy,
отзыв/reapproval, административные уведомления, multipart/unknown, restart/drain,
offset и запрет несовместимого rollback. Тесты используют настоящие repositories,
временные SQLite и установленный SDK с mock HTTP; реальные сети блокирует launcher.

Изменённые тестовые ожидания №15 отражают исправленное поведение, не обходят
инварианты. Начальные ошибки тестового импорта httpx (нужен httpx2) и устаревшие
ожидания синхронного loop/повторной доставки исправлены в тестах. Действующие
Structured Outputs, Decimal, grouping/edit/undo/history проверки остались зелёными.
git diff --check и staged security review выполнены перед коммитом.

## 8. Файлы и доставка

- `.gitignore`: только исключение локального telegram-queue.json.
- `repositories/access_repository.py`: short permission/revision gate.
- `steel_agent_v2/queue.py`, `queue_runtime.py`, `windows_secrets.py`.
- `steel_agent_v2/repository.py`: guard + атомарный finish hook, whitelist queue tables.
- `steel_agent_v2/agent.py`, `engine.py`, `transport.py`: безопасный отказ append,
  service whitespace, сохранение known/unknown исхода search.
- `telegram_bot.py`: новый путь v2 внутри прежнего single-worker lifecycle.
- `scripts/v2_queue_status.py`: read-only/explicit offline close/verified downgrade.
- `tests/test_steel_agent_v2_queue.py`, `test_steel_agent_v2_015_reproduction.py`,
  `test_steel_agent_v2_prelaunch.py`, буквальная fixtures/steel_v2_015_beams.txt.
- ARCHITECTURE.md, TESTING.md, WINDOWS_DEPLOYMENT.md, CONCURRENT_QUEUE_DESIGN.md,
  исторический отчёт 015 и этот отчёт.

Один логический коммит содержит кандидат и этот отчёт. Его полный SHA сообщается
в итоговом ответе после commit/push; локальный и remote HEAD должны совпасть.
Ветка: https://github.com/dodger7360/uvm_ai_operator/tree/feature/steel-agent-v2-durable-queue
main не изменялся, merge/deployment не выполнялись.

## 9. Риски и production

Exactly-once внешних API не обещается; unknown требует решения оператора.
Bounded admission может приостановить общий Telegram приём, если первое ещё не
принятое событие принадлежит переполненной пользовательской очереди. Нельзя
подтвердить последующие IDs, потеряв это событие. Fairness касается принятых jobs;
устойчивость к бесконечному flood одного пользователя не заявляется.
Canonical история сохраняется, её объём не ограничивается новым queue retention.

Для deployment нужны отдельное разрешение, backups и проверки под действительным
Windows account. DPAPI привязан к account; смена account требует ручного решения
по незакрытому bootstrap, а не расшифровки обходным способом. Approval notification
по-прежнему на короткое время занимает legacy writer lock. При queued/unknown
rollback может быть запрещён; это намеренная защита от потери принятой работы.

Живое GPT/Telegram, реальные лимиты API-аккаунта, качество intent-классификации и
нормализации этим offline набором не подтверждены. Production остаётся на aa11380;
checkout, .env, SQLite, работающие процессы и polling не изменялись. Статус живого
бота не аттестовывался запросами. Публикация кандидата не означает его установку.
