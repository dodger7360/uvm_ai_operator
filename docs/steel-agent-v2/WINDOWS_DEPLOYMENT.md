# Контролируемый выпуск на Windows

**Для версии с очередью №16 обязательно применить раздел «Уточнение №16» в конце
этого файла. Прежнюю процедуру возврата на legacy нельзя использовать для обхода
queue rollback gate. Этот этап разработки не разрешает deployment.**

**Инструкция для будущего разрешённого этапа. В №2/№3 rollout не выполнялся.**
Базовый HEAD 6b16c40 не содержит реализацию №1/№2; она включается в публикацию №3.
SHA публикации №3: `e8f3a04f2164998bff09c40f7dac477e6111d382`.
Перед каждым выпуском сверять его с origin и фактическим production, а не
предполагать наличие PR9/10. Переключение требует отдельного разрешения.

## 1. Входные условия и доставка

Политика revoke/reapprove утверждена в №3: история и заявки сохраняются, повторное
одобрение того же Telegram ID возвращает контекст. Отдельную систему отзыва не строить.
После публикации feature-ветки отдельно проверить совместимость с фактическим
production, сохранив имеющиеся права, offset, locks и историю; проверить diff/тесты,
согласовать commit и публикацию совместимого кандидата. /version и бюджетная
защита сохраняются, если присутствуют в фактическом production; не переносить
их из других веток автоматически. V2 не получает денежных/токенных квот.
При rollback сохранять утверждённую политику исходной установки, не подменять её.

№4 (2026-10-17): фактический production `19c9657`, является предком `e8f3a04`.
Supervisor, windows_runtime, start/stop и requirements.txt не отличаются.
PR9/10, /version и постоянные квоты отсутствуют в этой установке: возвращать их
не требуется. Исправление Windows SQLite URI в offline launcher подготовлено
локально в `fix/steel-agent-v2-windows-stage4`, не закоммичено/не опубликовано.
Это проверочный кандидат, а не готовый одобренный релиз для установки.

№5: Windows-кандидат публикуется отдельно в `fix/steel-agent-v2-windows-stage4`.
Одобренный полный SHA взять из итогового сообщения №5 и сверить через ls-remote;
не устанавливать просто текущий HEAD изменяемой feature-ветки. №4 остаётся
историческим отчётом. Тесты №5: отдельный venv 182 passed / 20 subtests;
production interpreter на staging-тестах 185 passed / 20 subtests (включая
три legacy reasoning-регрессии). Предупреждения Git о 51 каталоге воспроизведены
только sandbox-пользователем: у обычного пользователя status чистый без warning.
51 каталог с защищённой DACL находится в корне production вне .git; не удалять.

V2 runtime.connect явно передаёт model=gpt-6-luna, reasoning=high в фабрику
транспорта. Значения .env low/medium больше не понижают выбранный v2 handler.
Legacy и переменные окружения не изменяются; startup-лог v2 берёт фактические
параметры транспорта. Прямой CLI from_environment() без override сохраняет своё
прежнее поведение; для начальной Telegram-приёмки используется штатный runtime.

На Windows сначала установить фактические каталог, interpreter, способ запуска,
ветку/SHA, чистоту Git, пути OPERATOR_DB/бюджета и состояние supervisor/worker.
Не печатать .env, ключи, тела заявок, полные command lines с потенциальными секретами.
Ожидаемый каталог C:\Users\usoshin\uvm_ai_operator — только гипотеза до проверки.
Проверить другие каталоги/компьютеры на polling с этим токеном без запуска getUpdates.
Locks защищают только один каталог, не все компьютеры с одинаковым токеном.

PowerShell, после проверки $Project и присвоения одобренного полного SHA $Target:

```powershell
Set-Location -LiteralPath $Project
$Before = git rev-parse HEAD
git status --short
git fetch origin
if ($LASTEXITCODE -ne 0) { throw 'Fetch failed' }
# $Target — заранее одобренный полный SHA из отчёта №5, не main/HEAD.
if ((git rev-parse origin/fix/steel-agent-v2-windows-stage4) -ne $Target) {
    throw 'Published candidate changed; review required'
}
git cat-file -e "$Target^{commit}"
if ($LASTEXITCODE -ne 0) { throw 'Release commit is unavailable' }
git merge-base --is-ancestor $Before $Target
if ($LASTEXITCODE -ne 0) { throw 'Target omits current production; integrate and review first' }
git diff --name-status $Before $Target
```

При локальных изменениях/другом обслуживании остановиться. Сохранить $Before и
runtime manifest вне Git. В №4 ancestry проходит для проверенной пары SHA;
если будущий production отличается, повторить проверку и остановиться при
несовместимости. Нужен опубликованный одобренный релиз; документ его не создаёт.

## 2. Проверка до остановки

Доставить $Target в отдельный *проверочный* worktree (`git worktree add --detach
$Stage $Target`), не копировать туда .env, БД, token, runtime config. Это не место
запуска бота. Создать там отдельный venv совместимой версии Python, установить requirements-dev.txt,
проверить pip check и офлайн-команды TESTING.md. Ни main.py, ни Telegram CLI не запускать.
Убедиться, что B6 Git-объект доступен; если нет — не выдавать skip за приёмку.
Проверить `scripts/v2_llm_check.py --help`, не --live.

Сверить версии production .venv с требованиями: в №3 проверен Python 3.12.14,
на Windows №4 проверен Python 3.13.15; openai 3.19.2,
pydantic 2.13.5 и остальные pins requirements*.txt. Не обновлять рабочий venv
наугад. При несовпадении — остановиться и подготовить отдельный план зависимостей
с проверенным возвратом; текущий путь rollout предполагает совместимый runtime.

Подтвердить права NTFS на OPERATOR_DB, будущую v2 SQLite и backup: только текущий
оператор/администратор. Не менять токен, endpoint, сеть, прокси, Task Scheduler.
Проверить наличие нужных env по именам без вывода значений. В staging допустим
только fake transport и временные SQLite. Не переносить BETA_ROLLOUT.md/квоты из
PR9, если их нет в фактической исходной установке и пользователь их не разрешил.

## 3. Остановка и окончательный backup

Пока проверяется staging, production продолжает работу. Перед переключением:

```powershell
.\stop-telegram.ps1 -TimeoutSeconds 900
```

Не продолжать до выхода прежнего worker/supervisor и освобождения обеих OS locks.
Maintenance marker сохранять. При таймауте не убивать процесс и не начинать второй.
Старые PID/ready-файл не являются доказательством текущего состояния.
Снять baseline: SHA, branch, права, числа active/history/users, offset, существующий
выбор handler и пути БД. Не копировать живые WAL/SHM обычным Copy-Item.

Для каждой фактической SQLite ($OperatorDb, $BudgetDb если существует, $V2Db если
уже существует), использовать проверенный helper из $Stage; output пути — новые,
в отдельном backup-каталоге с временем/уникальным суффиксом:

```powershell
& "$Stage\.venv\Scripts\python.exe" "$Stage\scripts\v2_sqlite_snapshot.py" --source $OperatorDb --destination "$Backup\operator.sqlite3" --manifest "$Backup\operator.json"
if ($LASTEXITCODE -ne 0) { throw 'Backup failed; keep maintenance' }
```

Повторить с отдельными именами для бюджета и v2. Helper открывает источник read-only,
применяет Connection.backup, проверяет integrity_check, сохраняет только числа строк,
schema/row hashes и пути в manifest. Старые копии не перезаписывает. Копия содержит
персональные данные: не отправлять её в Git/чат. Сохранить предыдущий локальный
telegram-handler.json отдельно, если он был; .env не копировать. Не обнулять бюджет.
Проверить manifest и повторное открытие backup. При любой ошибке — maintenance и STOP.

## 4. Установка кода и выбор handler

Оставаясь в прежнем production каталоге, с чистым Git и обоими остановленными
процессами, установить одобренный commit без изменения main ref:

```powershell
git switch --no-overwrite-ignore --detach $Target
if ($LASTEXITCODE -ne 0) { throw 'Checkout failed; keep maintenance' }
if ((git rev-parse HEAD) -ne $Target) { throw 'Unexpected HEAD' }
```

Так сохраняется исходная ветка для возврата. При будущем разрешённом выпуске
можно отдельно согласовать fast-forward основной ветки. Не применять reset/clean/stash.
Для проверенной пары 19c9657 → кандидат №5 история не расходится: production
является предком. Разные имена веток не означают несовместимую историю.
При ином фактическом HEAD и провале ancestry не применять этот checkout:
сохранить maintenance, вернуться к review отдельного staging worktree от
проверенного SHA и подготовить отдельный совместимый кандидат. Не копировать
staging поверх production и не запускать бота из второго каталога.
--no-overwrite-ignore дополнительно запрещает перезаписать игнорируемые локальные
файлы, совпавшие с путями нового кода. Флаг проверен на установленном Windows Git
в одноразовом репозитории; production checkout в №5 не выполнялся.
Повторить импорты/оффлайн-тесты в production interpreter; launcher не читает user DB.
Существующие Windows startup scripts/main.py/supervisor/Task Scheduler не меняются.
В релизе меняются telegram_bot.py и добавляется пакет v2; runtime выбор задаётся
отдельным локальным файлом, без правок .env и кода при каждом переключении.

Проверить, что новый путь v2 не совпадает с legacy, budget или их aliases; не
подменять старую SQLite. Новый v2 стартует без legacy active: участнику нужно вновь
передать исходник; архив/права остаются в прежней БД. Если найден старый v2-файл,
сначала проверить его схему/целостность и inflight, не создавать поверх него пустой.
Для свежей БД допустимо имя `steel-agent-v2.sqlite3` рядом с legacy. Следующий
конфиг включать только после разрешения переключения:

```powershell
$Pending = Join-Path $Project 'telegram-handler.pending'
$Config = Join-Path $Project 'telegram-handler.json'
[IO.File]::WriteAllText($Pending, '{"handler":"v2","database":"steel-agent-v2.sqlite3"}', [Text.UTF8Encoding]::new($false))
if (Test-Path -LiteralPath $Config) {
    [IO.File]::Replace($Pending, $Config, $null)
} else {
    [IO.File]::Move($Pending, $Config)
}
```

Путь в JSON относителен к production root, не к staging. До первого запуска
подтвердить metadata-only `selection(Path(...))`, импорты и явный `handler=v2`;
они не запускают API. Неправильная конфигурация должна блокировать запуск.

## 5. Один запуск и приёмка

```powershell
.\start-telegram.ps1
```

Скрипт снимает maintenance и запускает один supervisor; worker использует прежние
OS locks и offset. Не запускать параллельно main.py или staging checkout.
Проверить живые PID: один supervisor и один worker, совпадение ready с worker;
в логе telegram_handler=v2, polling_connected, без новых 409/auth/import/SQLite
ошибок не менее 3 минут. Показать только безопасные строки диагностики.

Сверить сохранённые права/active/history legacy по hash/count; telegram_state
меняется только ожидаемым offset, его нельзя сравнивать как полностью неизменный.
Бюджетный журнал legacy не сбрасывается. V2 schema/PRAGMA integrity_check=ok.
Если фактическая установка поддерживает /version, сохранить её при сборке
совместимого релиза. У production №4 команды нет; идентификация выполняется
полным Git SHA и telegram_handler=v2, новый номер не выдумывать.

Только при отдельном разрешении платных вызовов/приёмки — один согласованный
участник проверяет сценарии TESTING.md через существующий UVM_Steel. Иначе
остановиться до живой приёмки и явно сообщить её отсутствие. Проверка 3 минут
без сообщений подтверждает работу polling, но не качество GPT.

## 6. Откат без потери новых данных

При сбое сохранить maintenance, код выхода, времена/PID и безопасные логи.
Если процесс ещё жив — штатно stop, без kill. Сделать новые диагностические backup
ВСЕХ текущих БД тем же helper; не восстанавливать старую operator.sqlite3 поверх
свежей: это потеряет offset, права и историю после переключения.

При остановленных процессах и чистом Git:

```powershell
git switch --no-overwrite-ignore --detach $Before
if ($LASTEXITCODE -ne 0) { throw 'Rollback checkout failed; keep maintenance' }
```

Вернуть прежний handler config из сохранённого файла (или убрать созданный config,
если раньше его не было), сохранив неудачный config для диагностики. Если предыдущий
код содержит selector и нужен legacy, явный JSON `{"handler":"legacy"}` допустим
только после проверки прежней конфигурации. .env/SQLite/бюджет не восстанавливать
автоматически. Старый runtime должен иметь прежние зависимости и утверждённые
настройки legacy AI. Для исходного 19c9657 квоты не добавлять.

V2 active/version/turn сохраняются отдельно, старый код их не читает. В legacy
снова доступна старая активная заявка; изменения v2 туда автоматически не переносятся.
Общий offset не откручивать назад: уже обработанные v2 update иначе попадут в legacy
без его идемпотентности. Перенос результата пользователем — отдельное явное действие.
Запустить один штатный supervisor, повторить наблюдение и сверку данных.

При повреждении legacy DB, неясном inflight или несовместимости старого runtime
не запускать ни один handler: BLOCKED, данные сохранены для разбора. `abandon`
допустим только для точного claim после доказанной смерти worker и сохранения
исходного сообщения в защищённом локальном отчёте; не снимать все claims массово.

## 7. Итог Windows-оператора

SUCCESS / ROLLBACK / BLOCKED; фактические SHA до/после, Python/deps, выбранный handler,
пути backup/manifest/отчёта, integrity/hash/count без содержимого, offset до/после,
права и история, PID/число polling, длительность наблюдения, выполненные живые
сценарии или явное «не выполнялись». Не объявлять качество GPT по mock-проверкам.
# Уточнение №16: очередь и обязательный gate отката

Этот раздел имеет приоритет над прежней процедурой rollback ниже, если устанавливается
версия с durable queue. Разработка №16 не означает разрешения deployment.

Перед будущей установкой: штатно остановить supervisor/worker; удерживать maintenance;
сделать verified SQLite backups; убедиться в отсутствии старого unresolved inflight.
Не очищать его автоматически. Проверить целевой полный опубликованный SHA и
staging-тесты. Сохранить прежний handler=v2 и отдельные пути БД, .env и общий offset.
Схема queue version=1 создаётся аддитивно в v2; legacy schema не меняется.

Начальная локальная конфигурация `telegram-queue.json` необязательна: без неё
workers=3, max_pending=100, max_pending_per_user=20, max_pending_bytes=8388608.
Это admission limits, не API-квоты. Файл не публиковать. Изменения применяются при
следующем согласованном запуске; production-конфигурация в №16 не создавалась.
Проверить DPAPI под действительным Windows account запуска, если используется
существующая bootstrap-команда /claim. Нет новых ключей/пакетов/переменных .env.

Остановка остаётся прежней. Maintenance прекращает admission и планирование,
даёт active завершиться, queued сохраняет. Две plan-попытки и search с timeout=180
могут идти дольше stop timeout=180. При timeout не убивать процессы, не выполнять
checkout и не запускать новый worker. Дождаться выхода обоих процессов/locks.

## Диагностика и unknown

Read-only, из установленной версии, с явно установленным фактическим `$db`:

```powershell
& .\.venv\Scripts\python.exe scripts\v2_queue_status.py --database $db
& .\.venv\Scripts\python.exe scripts\v2_queue_status.py --database $db --guard-rollback
```

Вывод содержит состояния/счётчики/update IDs/opaque confirmation, без переписки,
user IDs и ключей. guard возвращает 2, если есть незакрытые jobs/claims/outbox.
В логах queue update_id связан с operation ID, далее с безопасной API-диагностикой.
Не выгружать тела сообщений/ответов для диагностики.

Unknown не означает отсутствие внешнего эффекта. Сверить canonical snapshot/version,
callback audit/access status и известные receipt. Не повторять GPT/send/approval
автоматически. Для **закрытия без повторов**, после штатной остановки и backups:

```powershell
& .\.venv\Scripts\python.exe scripts\v2_queue_status.py --database $db --root $root --abandon $updateId --confirm $exactConfirmation
```

`$root` — фактический production root, `$exactConfirmation` — точное значение для
этого unknown из read-only вывода. CLI требует maintenance, свободных supervisor
и worker locks, совпадения handler=v2/database. Активные данные не откатываются;
обычный исходный текст сохраняется в history перед снятием точного claim.
Никаких отправок и платных повторов этот инструмент не делает. После закрытия
queued можно продолжить только согласованным запуском совместимого кода.

## Разрешённый rollback на предыдущий aa11380

1. Не выполнять checkout старого SHA, пока queue gate запрещает возврат.
2. Дождаться обработки queued совместимым runtime. Если это невозможно, BLOCKED:
   нужен recovery/fix-forward, а не ручное удаление заданий или изменение offset.
3. Штатно остановить оба процесса, сохранить maintenance и свежие backups всех БД.
4. Unknown разрешить явно после сверки результата. Права после неоднозначного
   approval не отзывать и не выдавать повторно автоматически.
5. Только если guard успешен, архивировать всю v2 SQLite в **новый защищённый путь
   вне checkout**, передав текущий rollback_confirmation:

```powershell
& .\.venv\Scripts\python.exe scripts\v2_queue_status.py --database $db --root $root --prepare-rollback --archive $newArchivePath --confirm $rollbackConfirmation
```

Инструмент использует SQLite Backup API, integrity_check, hash/count manifest,
повторное открытие и сравнение с source под write lock. Архивы не перезаписываются.
После проверки атомарно удаляются **только** четыре operational queue таблицы;
их история полностью остаётся в архиве. Active/version/turn/inflight и общий legacy
offset не заменяются. При ошибке backup таблицы не удаляются.

6. Установить согласованный прежний полный SHA безопасным detached checkout
   `--no-overwrite-ignore`. Сохранить handler=v2 и прежние database paths. Не восстанавливать
   старые SQLite и не откручивать offset. Проверить импорты и штатно запустить один
   supervisor; провести согласованное наблюдение и сверку данных.

Старый опубликованный v2 Repository отвергает дополнительные таблицы, поэтому без
этого downgrade gate он не начнёт polling. **Нельзя** обходить защиту переключением
handler на legacy, удалением config или ручным DROP. Такой переход требует отдельной
процедуры и решения пользователя. Исходную процедуру rollback на legacy ниже
нельзя автоматически применять к версии с очередью.

Полный проект и границы гарантий: CONCURRENT_QUEUE_DESIGN.md. Абсолютное exactly-once
внешних API не гарантируется; unknown означает ручное решение без автоматических повторов.
