# Постоянная граница запуска Windows — проект №18

Статус: **REQUIRES DESIGN APPROVAL**. Реализация и изолированные тесты подготовлены;
в production ничего не установлено. Нужны отдельное разрешение на смену Task Scheduler
action и утверждение единой постоянной ручной точки запуска. Документ не разрешает
применять команды к работающему UVM_Steel.

## Граница и модель угроз

Нынешний Task Scheduler запускает checkout `.venv\Scripts\pythonw.exe` с
`windows_supervisor.py` из checkout. Production/старый ручной start делает то же. Старый selector при
legacy/отсутствующем config не открывает v2 DB, поэтому whitelist старого Repository
не является общей защитой. После checkout старого SHA новая проверка исчезает.

Предложение: административно установленный комплект в прямом дочернем каталоге
ProgramData, например `C:\ProgramData\UVM_Steel-Guard-v1`. В нём:

- `guard.py`, `runtime.json` — policy и защищённый manifest;
- точные зафиксированные копии `windows_supervisor.py` и `windows_runtime.py`;
- `Start-UVM.ps1`, `Test-GuardInstall.ps1`.

Guard и frozen supervisor не импортируют checkout до проверки. Supervisor использует
прежние process locks, restart policy, ready/maintenance и никогда не kill. Каждый
новый child проходит guard ещё раз до импорта main.py. Один процесс supervisor,
один worker/poller, прежние execution threads внутри worker. Нового Windows-сервиса нет.
В command line сохранены проверяемые labels полных путей main.py/windows_supervisor.py:
это позволяет прежнему stop script обнаружить новые процессы, хотя запускается
guard.py. Worker без удерживаемого supervisor lock запрещён.

Не обещается защита от администратора или произвольного самостоятельного запуска
старого `python main.py`/старого start script вне утверждённого пути. Для этого нужна
отдельная ОС-политика выполнения, а не ещё одна проверка Python. **Если старый start
из любого возвращённого SHA должен остаться штатным ручным API, задача не закрыта.**
Нельзя молча назвать этот путь защищённым. Предлагается отдельно утвердить постоянный
Start-UVM и перенести все штатные shortcuts/runbooks на него. До такого решения
статус не READY. Старый checkout и legacy-ветки не удаляются.

В кандидате №18 `start-telegram.ps1` уже является только facade постоянного
ProgramData Start-UVM; при отсутствии/отказе guard он не снимает maintenance и
не запускает checkout supervisor. StartupAttempts передаётся с прежней проверкой
диапазона. Это дополнительная защита новой версии, но не замена постоянной точки
после checkout старого SHA. Scheduler restart policy и maintenance поведения сохранены.

Guard interpreter и его стандартная библиотека также должны быть административно
защищены. Не считать пользовательское venv единственным доверенным барьером.
Использовать явно выбранный проверенный Python, `-I -S` для guard host; worker
использует прежнее production venv. Установка нового Python этим этапом не разрешена.

## Проверки и протокол

Manifest version=1, пять хешей bundle, явные канонические root/legacy/v2/interpreter/git
paths, approvals полного SHA + queue protocol + handlers, phase, epoch, offset_floor,
необязательный rollback receipt. Launch/update закреплены за `runtime.json` именно
рядом с установленным guard: передача другого manifest через CLI не отключает policy.
Внутренний check proposal не является разрешением запуска. Секретов/токенов/переписки нет. Неизвестные поля,
duplicate JSON keys, неизвестные protocol/schema/state, missing/corrupt DB или manifest
дают отказ с постоянным безопасным reason, без текста исключения/SQL/сообщения.
Файлы БД только mode=ro; guard не создаёт их, не меняет offset и не мигрирует схему.

При active нужны одновременно:

1. Чистый checkout без незавершённых Git-операций, одобренный полный SHA.
2. Queue protocol=1 в approval и **tracked descriptor из того же HEAD**,
   равный протоколу, который guard умеет проверять. Подложенный untracked JSON не подходит.
3. Схема queue=1, полный набор core/queue таблиц, известные состояния, совпавший
   целочисленный `guard_epoch` в SQLite metadata и manifest.
4. handler=v2 с тем же фактическим файлом SQLite. Legacy и отсутствие config запрещены
   при active независимо от полноты обработки и наличия SHA в approvals.
5. offset_floor ≤ legacy offset ≤ durable cursor. Mirror lag разрешён **только для
   совместимого recovery**, уже доказанного №16/17; offset ahead запрещён.

Descriptor содержит schema versions, write-before-ack, snapshot/history/outbox commit,
quarantine-unknown и имя epoch key. Это явный ABI/семантический договор, а не попытка
доказать корректность произвольного Python по названию файла. Approval требует review
и регрессии конкретного SHA; один список SHA или одна декларация недостаточны.

Pending/unknown допускают продолжение совместимым runtime с его безопасным recovery,
но запрещают retirement/несовместимый запуск. Пустая очередь при active тоже не
разрешает старый код. Guard не устраняет unknown и не повторяет внешние вызовы.
Read-only открытие БД с hot journal может потребовать ручного SQLite recovery:
guard в таком состоянии отказывает, не делает записывающий recovery за оператора.

## FSM и атомарность

`baseline → armed → active → armed → retired`.

- Baseline: до миграции, нет queue таблиц, старый protocol=0. Код с descriptor новой
  очереди не допускается, чтобы он не создал её до arming. Initial epoch=0.
- Armed: всегда отказ запуска. До первоначального checkout/DDL сначала публикуется
  эта фаза. Новый epoch=предыдущий+1; при подготовке retirement active epoch сохраняется.
  Под обоими process locks фиксируется точный cutover cursor/floor и сверяется mirror.
- Active: schema/epoch/совместимость проверяются на каждом старте/restart.
- Retired: только после архива, закрытых jobs/outbox/inflight, равного offset/cursor,
  проверки архива и совпадения core hashes после удаления operational таблиц.
  Старый protocol=0 может работать, в том числе legacy/без config, **если это явно
  одобрено**. Архив/receipt должны сохраняться. Offset не может стать ниже retirement floor.

`scripts/v2_guard_proposal.py` создаёт только новый proposal, ничего не устанавливает.
`guard.py update` отдельно требует SHA256 именно прочитанных proposal bytes, maintenance,
оба свободных locks и допустимый переход. Пути/установка неизменны. Manifest записывается
через temp → flush/fsync → Administrative owner → os.replace. Ошибка до replace оставляет
старый manifest; после replace виден целый новый. Удалённый manifest не означает baseline.

`scripts/v2_queue_epoch.py` — отдельная **явная offline activation операция**, а не guard.
После штатной миграции/align она связывает metadata с уже armed manifest. Требует оба
locks/maintenance/точное подтверждение, закрытое исходное состояние и равенство offset.
Не меняет core/offset и не выполняет миграцию. Одного epoch недостаточно: active manifest
публикуется последним. До этого ни scheduler, ни ручной guard не запускает worker.

## Retirement

До rollback сначала stop + согласованный backup, затем manifest armed с текущим cursor.
`v2_queue_status.py --prepare-rollback` для epoch-bound DB требует дополнительно
`--startup-manifest` и `--startup-guard`; проверяет armed/epoch/path/cursor до backup
и перед DROP. Прежнее offset==cursor и проверки jobs/outbox/inflight сохранены.

После полного Backup API архива и явного DROP только operational таблиц (существующая
offline процедура) proposal retire связывает epoch/cursor, полный SHA256 архивного
файла и хеши четырёх core tables. Guard проверяет archive integrity, schema/epoch,
закрытость, core hashes живой БД и равенство mirror. Устаревший архив до новых pending
jobs не подходит: arming уже закрепил более новый cursor. Guard сам таблицы не удаляет.

Только после атомарной публикации retired разрешён checkout явно одобренного старого
полного SHA. Нельзя восстанавливать старую SQLite поверх актуальной или уменьшать offset.
Публикация receipt проверяет данные, а не требует, чтобы ещё установленный queue-код
мог запуститься в retired. Он как раз остаётся запрещённым до checkout protocol=0.
После старта старого приложения core может легитимно изменяться, поэтому равенство core
проверяется при retirement; последующие starts проверяют неизменность архива/receipt,
отсутствие queue и monotonic floor, не требуют заморозить новую историю.

## Права, обновление и восстановление guard

Installer работает только elevated, из чистого явно одобренного source SHA и только
в новом прямом дочернем каталоге ProgramData. Не перезаписывает старые installations.
Directory DACL protected; Administrators/SYSTEM FullControl, runtime account RX.
Owner всех файлов/manifest — Administrators, иначе владелец мог бы поменять DACL.
Основание: [Microsoft: owner/WRITE_DAC](https://learn.microsoft.com/en-us/windows/win32/secauthz/owner-of-a-new-object)
и [SetNamedSecurityInfoW](https://learn.microsoft.com/en-us/windows/win32/api/aclapi/nf-aclapi-setnamedsecurityinfow).
Назначение owner требует соответствующих прав/SID в elevated token; ошибка Win32
не игнорируется и происходит до replace, сохраняя прежний manifest.
Test-GuardInstall проверяет owner/write permissions/reparse points и protected root.
Worker не имеет прав менять manifest. Тесты используют mocked install authority;
реальная административная установка и cross-account DACL этим этапом не выполнялись.

Обновление bundle: stop, backup DB/manifest/task XML, maintenance, новый versioned
ProgramData каталог, проверенные hashes/ACL/guard host, offline проверка прежней
policy новым guard и только затем отдельно разрешённая смена task/manual entrypoint.
Не заменять guard.py/frozen vendor поверх работающей установки и не сбрасывать epoch.
Обычный `update` специально запрещает менять bundle hashes/пути. Для новой версии
нужно отдельно согласованное admin re-provisioning: сначала old manifest armed,
проверенная копия того же armed epoch/cursor/approvals в новый каталог с новыми
проверенными bundle hashes. Записать её атомарно через temp/fsync/Administrative owner/
replace (тот же `atomic_write`), сверить все неизменяемые поля с архивной копией.
Не создавать baseline над активной очередью. Новый guard должен отказывать в armed;
только после проверки прежнего epoch/DB новый `update` active и разрешённая смена
точки запуска. Старая установка остаётся armed; при неопределённости обе остановлены.
Это процедура отдельного административного обновления, не автоматический fallback.

Повреждение manifest/bundle: maintenance, проверить оба locks, сохранить диагностику
без содержимого заявок, восстановить точную проверенную защищённую копию manifest/bundle
с прежним epoch/paths/receipt, проверить ACL/проверку guard. Если доказать соответствие
epoch/cursor/архива нельзя — BLOCKED. Не создавать baseline над существующей очередью,
не отключать guard, не удалять очередь. Повреждение SQLite разбирается отдельно offline.

## Будущая установка — только после отдельного решения

1. Проверить актуальный production SHA/чистоту/процессы/DB/config, опубликованный
   полный одобренный SHA нового релиза и staging regression. Не использовать ветку как цель.
2. Штатный stop, дождаться supervisor/worker/redirectors/locks. Timeout=180 не означает kill.
3. Согласованные Backup API всех БД + reopen/integrity/hash/count manifests,
   configs/прежний SHA/task XML. Проверить unmanaged old inflight; при наличии остановиться.
4. Из отдельного staging **опубликованного полного SHA** выполнить elevated installer:

```powershell
& "$staging\ops\windows_guard\Install-Guard.ps1" -Destination $guardDir -RuntimeAccount $runtimeAccount -ApprovedSourceSha $ApprovedSha
```

`$guardDir` — новый прямой child ProgramData; `$guardCode=$guardDir\guard.py`,
`$manifest=$guardDir\runtime.json`. Все дальнейшие proposals создавать в защищённом
admin staging; `$guardPython` — проверенный protected interpreter, `$workerPython` —
существующий production venv, `$git` — проверенный полный путь к git.exe.

5. Создать baseline proposal с явными root/DB/interpreters/git, **фактическим старым SHA**,
   protocol=0 и точным handler, затем elevated атомарно опубликовать:

```powershell
& $guardPython -I -S -B "$staging\scripts\v2_guard_proposal.py" baseline --guard $guardCode --manifest $manifest --out $baselineProposal --root $root --legacy-database $legacyDb --v2-database $v2Db --guard-python $guardPython --worker-python $workerPython --git $git --sha $PreviousSha --protocol 0 --handlers v2
& $guardPython -I -S -B $guardCode update --manifest $manifest --proposal $baselineProposal --confirm $ReviewedProposalSha256
& $guardPython -I -S -B $guardCode check --manifest $manifest
```

Proposal hash следует из конкретного вывода builder и review; не подставлять пример.
Check не снимает maintenance и не запускает Telegram.

6. **Только при отдельном разрешении на точку запуска**, сохраняя все Principal/Settings/
   Triggers задачи, заменить её Action. Пример, не выполнявшийся в №18:

```powershell
$arguments = '-I -S -B "{0}" supervise --manifest "{1}" --supervisor-script "{2}"' -f $guardCode,$manifest,(Join-Path $root 'windows_supervisor.py')
$action = New-ScheduledTaskAction -Execute $guardPython -Argument $arguments -WorkingDirectory $root
Set-ScheduledTask -TaskName 'UVM_SteelBot' -Action $action
```

Проверить фактический Action/ACL/guard отказами и перенести все ручные точки на
`$guardDir\Start-UVM.ps1`. Не активировать очередь, пока исторический in-checkout
start остаётся разрешённым штатным обходом. Это отдельное решение пользователя.

7. Создать arm proposal, добавив approval **полного** $ApprovedSha с protocol=1/handler=v2,
   атомарно update; проверить отказ запуска и сохранение maintenance. Только после этого
   безопасный detached checkout `--no-overwrite-ignore $ApprovedSha`, без reset/force/merge.

```powershell
& $guardPython -I -S -B "$staging\scripts\v2_guard_proposal.py" arm --guard $guardCode --manifest $manifest --out $armProposal --sha $ApprovedSha --protocol 1 --handlers v2
& $guardPython -I -S -B $guardCode update --manifest $manifest --proposal $armProposal --confirm $ReviewedArmSha256
```

8. Проверить импорты/handler=v2/model=Luna high/отдельные DB; обычная явная миграция queue=1,
   начальный align к прежнему offset и сверка core hashes. Guard DDL не выполняет.
   Выполнить `v2_queue_epoch.py --manifest ... --guard ... --confirm activate:<epoch>`.
   Проверить, что guard всё ещё отказывает в armed. Затем proposal activate + atomic update.

```powershell
& $workerPython -B "$staging\scripts\v2_queue_epoch.py" --manifest $manifest --guard $guardCode --confirm $ExactActivationConfirmation
& $guardPython -I -S -B "$staging\scripts\v2_guard_proposal.py" activate --guard $guardCode --manifest $manifest --out $activeProposal
& $guardPython -I -S -B $guardCode update --manifest $manifest --proposal $activeProposal --confirm $ReviewedActiveSha256
```

9. Check/ACL снова, штатный постоянный Start-UVM. Проверить один poller/worker/supervisor,
   до трёх execution threads, handler v2/Luna high, отсутствие 409/auth/import/SQLite,
   историю/права/offset, наблюдение ≥3 минут. Пользовательские параллельные тесты —
   отдельно согласованные ручные сообщения, без автоматического replay.
10. Мониторинг — read-only queue_status; unknown не повторять. При аварии maintenance/stop,
    свежие согласованные backups, разбор unknown по receipt/history и exact confirmation.
    Если stop timeout/locks неизвестны, не запускать другой worker.
11. Для rollback: закрыть queued/unknown/inflight/outbox, равный offset/cursor, stop/backups;
    arm proposal закрепляет текущий cursor. Нельзя бросить pending ради возврата кода.

Перед prepare-rollback выполнить builder `arm` без нового approval и atomic update,
как выше; он сохраняет active epoch и фиксирует текущий cursor. После архива:

```powershell
& $guardPython -I -S -B "$staging\scripts\v2_guard_proposal.py" retire --guard $guardCode --manifest $manifest --out $retiredProposal --archive $newArchive
& $guardPython -I -S -B $guardCode update --manifest $manifest --proposal $retiredProposal --confirm $ReviewedRetiredSha256
```

Эти две команды выполняются **после** успешного prepare-rollback ниже, а не до него.

```powershell
& $workerPython "$staging\scripts\v2_queue_status.py" --database $v2Db --legacy-database $legacyDb --root $root --prepare-rollback --startup-manifest $manifest --startup-guard $guardCode --archive $newArchive --confirm $ExactRollbackConfirmation
```

Проверить архив. Proposal retire + elevated atomic update проверит его и неизменность
core. При ошибке останется armed, и старый код не стартует. Затем только одобренный
старый полный SHA/handler, guard check, постоянный start. Старые SQLite поверх новых
не восстанавливать; offset не откручивать; новую историю хранить.

Во всех шагах `.env`, Telegram token, модель и предметная логика не меняются.
Новые версии guard/manifest/артефакты не являются разрешением выполнить этот runbook.
