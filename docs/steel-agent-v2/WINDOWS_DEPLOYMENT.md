# Windows deployment: forward-only, начиная с №19

> Steel Agent v2 развивается по стратегии forward-only. Откат на устаревший код не является поддерживаемым штатным сценарием. Сохранность данных, очереди и восстановление после аварий остаются обязательными.

Это инструкция будущего отдельно разрешённого переключения. В №19 production не
меняется. Устанавливать полный опубликованный SHA из отчёта №19 после review;
`3c0d28f` — только исходный кандидат №18, не целевой релиз. Имя ветки не является
целью установки. Исторические процедуры №15–18 (guard/retirement/rollback) отменены.

## Сохраняем существующую инфраструктуру

Один UVM_Steel, прежний токен/.env, текущая задача Windows с прежним Action
`.venv\Scripts\pythonw.exe windows_supervisor.py`, обычная рабочая учётная запись.
Ручной start-telegram.ps1 запускает тот же supervisor; stop-telegram.ps1 создаёт
maintenance и кооперативно ждёт. Supervisor/worker имеют прежние два ProcessLock.
Нет ProgramData, внешнего guard/manifest, ACL-установки, RunAs, службы или нового
polling. Task Scheduler Action и права задачи не менять.

Живой entrypoint требует явный telegram-handler.json с handler=v2 и прежним
отдельным путём SQLite v2. Missing/legacy config — startup exit 7, без fallback.
Не создавать новый конфиг поверх имеющегося и не менять .env.
Обычная учётная запись должна иметь доступ к каталогу checkout, lock/maintenance,
SQLite и каталогу backup. DPAPI /claim остаётся привязан к тому же Windows account;
это существующая защита credential, не требование admin.

## 1. Проверки до остановки

В production только чтением: HEAD должен совпасть с согласованным исходным,
чистый Git, отсутствие merge/rebase/cherry-pick, точные PID и пути supervisor/worker,
один polling, handler=v2, gpt-6-luna/high, все фактические SQLite и свободное место.
Сравнить Task Scheduler Action с исходным. Не выводить CommandLine с секретами,
.env, пользовательские IDs, сообщения и payload SQLite.

В PowerShell использовать явно проверенные переменные `$projectRoot`, `$approvedSha`
(ровно 40 hex, получен из опубликованного отчёта после review) и `$releaseBranch`.

```powershell
git -C $projectRoot fetch origin
git -C $projectRoot rev-parse HEAD
git -C $projectRoot status --porcelain
git -C $projectRoot rev-parse "origin/$releaseBranch"
git -C $projectRoot merge-base --is-ancestor HEAD $approvedSha
git -C $projectRoot diff --stat HEAD $approvedSha
```

Remote SHA должен точно равняться approvedSha. При расхождении — остановиться.
Проверить пересечения tracked target файлов с ignored/untracked; не перезаписывать
их. Никакого reset/clean/force/merge. Production aa11380 является предком кандидата.
Отдельный staging worktree на точном SHA, без .env/реальных БД, запускает тесты из
TESTING.md, pip check, импорты и mock wire payload. API/polling из staging запрещены.

## 2. Штатная остановка

Зафиксировать SHA/config и агрегатные контрольные показатели прав, offset, истории,
версий и активных заявок. Выполнить существующий stop-telegram.ps1. Убедиться,
что оба прежних процесса завершены и оба ProcessLock свободны. Наличие файлов
.lock само по себе не доказывает владение OS lock. Maintenance оставить до запуска.

Timeout 180 секунд не означает право kill. Два plan и search могут занимать дольше;
ждать и разбирать безопасную диагностику. Пока прежний worker жив, не checkout,
не мигрировать, не запускать следующий. Task Scheduler не перенастраивать.

## 3. Согласованный backup после остановки

Создать НОВЫЙ отдельный каталог вне Git, не заменяя прошлые backup. Для каждой
фактически используемой SQLite (legacy operator, v2, дополнительные при наличии)
применить существующий SQLite Backup API инструмент:

```powershell
& $python scripts/v2_sqlite_snapshot.py --source $source --destination $copy --manifest $manifest
```

`$python` — проверенный production interpreter; все пути задаются явно.
Сохранить прежний handler config и полный production SHA отдельными локальными
файлами в backup, не в Git. .env не менять и не публиковать.
Каждая копия: повторное открытие, integrity_check=ok; manifest таблиц содержит
количества и row/schema SHA256. Сравнить с metadata исходника при остановленном
worker. Дополнительно сохранить файловый SHA256 каждой копии (Get-FileHash).
Не использовать копирование WAL/SHM вместо Backup API. При живом worker несколько
SQLite не образуют общую атомарную копию; поэтому установочный backup только после
остановки всех writers. Ошибка проверки — installation прекращается.

## 4. Старые inflight и схема

До первой queue-миграции в копии и затем в остановленной v2 проверить
`SELECT count(*) FROM steel_v2_inflight`. Если есть записи без durable attempt/inbox
(старые операции aa11380), автоматическая очистка запрещена. Оставить maintenance,
архивировать, разобрать технические идентификаторы/логи; платный запрос мог состояться.
Без отдельного подтверждённого решения не удалять их и не повторять GPT.

На новой версии неизвестная/частичная схема, некорректные ключи/metadata, orphan
attempt/outbox, повреждение SQLite или offset ahead вызывают отказ. Не лечить
ручным DROP, подменой базы, понижением версии или удалением pending/unknown.

## 5. Установка и аддитивная инициализация

Повторно проверить чистоту Git, отсутствие процессов и свободные locks.

```powershell
git -C $projectRoot checkout --detach --no-overwrite-ignore $approvedSha
git -C $projectRoot rev-parse HEAD
```

При отказе checkout не форсировать. Проверить Python/imports/pip check, неизменность
.env/config/Task Action. Не обновлять зависимости без необходимости. Приостановленную
новую версию чинить следующим совместимым релизом, не запуском старого обработчика.

Из production-каталога запустить ниже только после backup и inflight check, с
проверенными абсолютными `$v2Database` и `$legacyOffset` (целое неотрицательное
значение из legacy TelegramRepository). Команда не вызывает API и не запускает polling:

```powershell
& $python -B -c 'import sys; from database import Database; from steel_agent_v2.repository import Repository; from steel_agent_v2.queue import DurableQueue; d=Database(sys.argv[1]); Repository(d); q=DurableQueue(d); q.align_offset(int(sys.argv[2])); q.recover(); print("queue_schema=1 cursor="+str(q.cursor()))' $v2Database $legacyOffset
& $python -B scripts/v2_queue_status.py --database $v2Database --legacy-database $legacyDatabase --check-forward
```

Создаются только 4 дополнительные queue-таблицы schema=1. Core, оригиналы, active,
versions/history не преобразуются и не заменяются. Сверить их hash/count с backup.
Повторная инициализация идемпотентна. Будущая schema>1 требует явной аддитивной
миграции следующего релиза; текущий код не делает downgrade или auto-repair.

При первой миграции adopt существующий legacy offset допустим только для пустой,
ещё не initialized очереди. Для существующей queue legacy>cursor запрещён. Отставший
legacy mirror допустим как известное окно inbox-commit→offset; run_queue продвинет
его только вперёд перед getUpdates. Нельзя вручную откручивать любой cursor назад.

## 6. Запуск и контроль

Оставить прежнюю конфигурацию handler=v2/database и .env. QueueConfig по умолчанию:
workers=3,max_pending=100,max_pending_per_user=20,max_pending_bytes=8388608.
Необязательный локальный telegram-queue.json меняет только эти параметры очереди,
не модель/стоимость API. Исполнять прежний start-telegram.ps1 обычным аккаунтом.

Подтвердить один supervisor, один Telegram-worker, один getUpdates, gpt-6-luna/high,
права доступа и общий offset. Три execution worker — потоки одного процесса,
не три polling и не три Python-бота; они создают независимые SDK/API clients.
Проверить queue schema/cursor/counts, отсутствие 409/auth/import/SQLite ошибок и
сохранность active/history/versions. Наблюдать минимум 3 минуты.
Раздельно фиксировать native Python и venv redirector PID, не считать их вторым ботом.

После отдельного разрешения живых испытаний 2–3 пользователя вручную отправляют
независимые запросы. Проверить перекрытие, FIFO одного пользователя, адресность
ответов и версии. Mock не подтверждает качество живой GPT/поиска. Самостоятельно
не отправлять Telegram-сообщения и не инициировать платные запросы.

## 7. Crash, unknown и fix-forward

Read-only `v2_queue_status.py --database ... --legacy-database ...` показывает только
технические агрегаты/идентификаторы. Не выводить payload, user IDs и переписку.
queued продолжается; proven claimed без API возвращается в очередь; api_started /
result_received без commit → execution_unknown; committed → только не начатая
доставка; delivery_started без receipt → delivery_unknown. Остальные пользователи
продолжают работать. GPT repair допускается только при известном исходе прошлого
вызова; внешнее exactly-once не обещается.

Операторское закрытие unknown: штатная остановка, backup, оба свободных locks,
maintenance, явное подтверждение `update_id:state:input_hash` из локальной диагностики:

```powershell
& $python scripts/v2_queue_status.py --database $v2Database --legacy-database $legacyDatabase --root $projectRoot --abandon $updateId --confirm $confirmation
```

Это только отказ от продолжения неопределённой операции. Нет GPT replay, resend,
отмены сохранённой мутации или удаления истории. Credentials в историю не попадают.
Не выдавать обычному пользователю технический confirmation.

При критическом сбое: штатно остановить/оставить maintenance, создать свежие Backup
API snapshots текущих БД, сохранить безопасную диагностику, подготовить следующий
совместимый SHA, проверить на копии, отдельно согласовать его установку. Не запускать
legacy/старый код, не восстанавливать старую SQLite поверх новых событий, не двигать
offset назад. Даже пустая очередь не разрешает downgrade. Отказ миграции оставляет
систему остановленной до fix-forward; SQL transaction rollback не является возвратом
версии приложения. Удаление queue schema/retirement больше не поддерживается.

## Ограничения

Backpressure не подтверждает непринятый update. Переполненный первый пользователь
может задержать общий admission, но уже принятые чужие jobs исполняются независимо.
Слишком большое одиночное событие также требует вмешательства: не пропускать его
изменением offset. Unknown блокирует только FIFO своего пользователя. Already-started
внешний вызов невозможно гарантированно отменить после revoke; разрешение на commit
и delivery-start проверяется поколением прав под legacy→v2 gate. Сетевые вызовы GPT
не держат SQLite write transaction. Поддерживаемого возврата старого SHA нет; защита
от намеренного ручного обхода через старый checkout не заявляется.
