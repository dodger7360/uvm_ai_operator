# Контролируемый выпуск на Windows

**Инструкция для будущего разрешённого этапа. В №2/№3 rollout не выполнялся.**
Базовый HEAD 6b16c40 не содержит реализацию №1/№2; она включается в публикацию №3.
SHA публикации указан в итоговом отчёте №3 и определяется через `git log -1`
опубликованной feature-ветки. У feature-ветки отсутствуют слитые PR9/10; непосредственное
переключение Windows на эту ветку сейчас запрещено данным runbook.

## 1. Входные условия и доставка

Политика revoke/reapprove утверждена в №3: история и заявки сохраняются, повторное
одобрение того же Telegram ID возвращает контекст. Отдельную систему отзыва не строить.
После публикации feature-ветки отдельным этапом собрать релиз на фактическом
актуальном production, сохранив
права, /version и бюджетную защиту legacy; проверить diff/тесты, согласовать commit
и push. V2 не получает искусственных денежных/токенных квот. Нельзя включать
неограниченный старый AI-код вместо защищённого legacy при rollback.

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
git cat-file -e "$Target^{commit}"
if ($LASTEXITCODE -ne 0) { throw 'Release commit is unavailable' }
git merge-base --is-ancestor $Before $Target
if ($LASTEXITCODE -ne 0) { throw 'Target omits current production; integrate and review first' }
git diff --name-status $Before $Target
```

При локальных изменениях/другом обслуживании остановиться. Сохранить $Before и
runtime manifest вне Git. На текущем состоянии проверка ancestry не проходит:
это ожидаемая блокировка прямого deployment, не повод применять force/reset.
Нужен уже опубликованный одобренный релиз; этот документ его не создаёт.

## 2. Проверка до остановки

Доставить $Target в отдельный *проверочный* worktree (`git worktree add --detach
$Stage $Target`), не копировать туда .env, БД, token, runtime config. Это не место
запуска бота. Создать там временный venv Python 3.12, установить requirements-dev.txt,
проверить pip check и офлайн-команды TESTING.md. Ни main.py, ни Telegram CLI не запускать.
Убедиться, что B6 Git-объект доступен; если нет — не выдавать skip за приёмку.
Проверить `scripts/v2_llm_check.py --help`, не --live.

Сверить версии production .venv с требованиями: Python 3.12, openai 3.19.2,
pydantic 2.13.5 и остальные pins requirements*.txt. Не обновлять рабочий venv
наугад. При несовпадении — остановиться и подготовить отдельный план зависимостей
с проверенным возвратом; текущий путь rollout предполагает совместимый runtime.

Подтвердить права NTFS на OPERATOR_DB, будущую v2 SQLite и backup: только текущий
оператор/администратор. Не менять токен, endpoint, сеть, прокси, Task Scheduler.
Проверить наличие нужных env по именам без вывода значений. В staging допустим
только fake transport и временные SQLite. Отсутствие BETA_ROLLOUT.md в исходной
ветке v2 — следствие отсутствия PR9, не причина пропускать его при сборке релиза.

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
git switch --detach $Target
if ($LASTEXITCODE -ne 0) { throw 'Checkout failed; keep maintenance' }
if ((git rev-parse HEAD) -ne $Target) { throw 'Unexpected HEAD' }
```

Так сохраняется исходная ветка для возврата. При будущем разрешённом выпуске
можно отдельно согласовать fast-forward основной ветки. Не применять reset/clean/stash.
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
Фактическая версия /version должна соответствовать одобренному релизу, а не
автоматически старому номеру 1.10. Поддержку команды нужно сохранить при сборке
релиза с PR10; в текущей feature-ветке её нет, новый номер не выдумывать.

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
git switch --detach $Before
if ($LASTEXITCODE -ne 0) { throw 'Rollback checkout failed; keep maintenance' }
```

Вернуть прежний handler config из сохранённого файла (или убрать созданный config,
если раньше его не было), сохранив неудачный config для диагностики. Если предыдущий
код содержит selector и нужен legacy, явный JSON `{"handler":"legacy"}` допустим
только после проверки прежней конфигурации. .env/SQLite/бюджет не восстанавливать
автоматически. Старый runtime должен иметь прежние зависимости и защиту legacy AI.

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
