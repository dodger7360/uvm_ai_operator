# Codex №18 — постоянная защита Windows запуска и rollback

2026-10-18. **REQUIRES DESIGN APPROVAL. Не разрешение deployment.**

## 1. Проверенное исходное состояние

Production остался на `aa11380dfe1ed646fec2355b22880fdb03b0d706`, detached,
чистое дерево. Supervisor 7424, worker/ready 9500; parent 28700/4588 — штатные venv
redirectors, не дополнительные polling-процессы. В конце проверки PID/SHA/Action
совпали с исходными. Действующий бот не останавливался и не перезапускался.

Task `UVM_SteelBot`: Ready, Principal RunLevel=Limited, MultipleInstances=IgnoreNew,
ExecutionTimeLimit=PT0S. Action — production `.venv\Scripts\pythonw.exe`, аргумент
production `windows_supervisor.py`, WorkingDirectory=production. Это прочитано
через Task Scheduler, не восстановлено по старому отчёту. Task не менялся.
Ручной production start запускает тот же in-checkout supervisor.

Production/test Python 3.13.15; OpenAI 3.19.2, Pydantic 2.13.5, httpx2 2.13.1;
pytest 9.1.1. Test `pip check`: No broken requirements found. Пакеты не обновлялись.
Значения `.env`, токены, переписка и реальные SQLite для испытаний не читались/не копировались.

Основа worktree: `8dee73a5f0896d204c67df14d9697b2bb7f3fff3`, чистая до работы.
Отдельные worktree/branch: `steel-agent-v2-persistent-guard` /
`feature/steel-agent-v2-persistent-guard`. Main и исходная ветка №17 не менялись.

## 2. Причина обхода и граница решения

Старый selector с legacy или без config не открывает v2 Repository/queue. Git
checkout также заменяет start/supervisor из проекта. Потому whitelist таблиц
работает лишь для старого v2, а Git-based проверка не является постоянной границей.
Даже полностью закрытая queue ещё не означает завершённый безопасный rollback.

Подготовлен внешний self-contained guard с замороженными копиями существующих
supervisor/locks. Installer помещает их в новый административно защищённый каталог,
предлагаемый путь — `C:\ProgramData\UVM_Steel-Guard-v1`; manifest — `runtime.json`
в нём. Реальных файлов по этому пути в №18 не создавалось.

Task Scheduler должен запускать этот guard, ручной запуск — постоянный Start-UVM.
Guard проверяет policy до supervisor и в каждом fresh worker до импорта checkout.
Новая Windows-служба не добавляется. Используются прежние locks/ready/restart/
maintenance, без force kill. Сохранены path labels для старого stop-telegram.ps1.
Один supervisor/worker/poller; три execution threads по-прежнему внутри worker.

В новом кандидате in-checkout start-telegram.ps1 — только facade постоянного
Start-UVM, без fallback и снятия maintenance до preflight; StartupAttempts передаётся.
Frozen supervisor импортируется из установленного bundle, не из текущего Git SHA.

## 3. Какие точки защищены и что ещё нужно утвердить

| Точка | Подготовленная защита | Состояние production |
|---|---|---|
| Task Scheduler | Внешний guard supervise до каждого worker | Старый Action сохранён; требуется отдельная смена |
| Новый start-telegram.ps1 | Делегирует ProgramData Start-UVM; missing guard = отказ | Файл production не заменён |
| Постоянный ручной Start-UVM | Полный policy check, locks, затем запуск guard supervisor | Не установлен |
| Frozen supervisor restart | Каждый child сначала guard, несовместимый exit=7 permanent | Проверено на временном launch environment |
| Старый start/прямой old supervisor/main | Не перехватывается произвольная команда из старого checkout | Не объявляется защищённой |

**Нужно согласовать постоянную ручную точку и вывод старых in-checkout команд из
штатных способов запуска после cutover.** Если старый start из любого SHA обязан
остаться штатным, текущая архитектура не закрывает требование: нужен отдельно
согласованный постоянный OS execution control/proxy. Не подменять этот вопрос
документальным запретом и не объявлять READY. Защита от администратора, сознательно
обходящего утверждённый путь, не обещается.

## 4. Модель совместимости и данных

Manifest version=1: явные canonical пути, approvals полного SHA/queue protocol/
handlers, hashes пяти installed файлов, phase/epoch/offset_floor и rollback receipt.
Базы сравниваются также samefile: hardlink не позволяет подменить legacy/v2 одним
файлом. Относительный/алиасный config к той же v2 DB нормализуется, иной файл запрещён.
UNC runtime paths не принимаются. Секретов, запросов или API credentials в manifest нет.

Active требует approval **и** tracked descriptor из того же HEAD: protocol=1,
schema=1, write-before-ack, snapshot/history/outbox, quarantine-unknown, guard_epoch.
Untracked descriptor либо старый SHA, лишь добавленный в approvals, не достаточны.
Проверяются известные tables/queue columns/PK/states, epoch SQLite=manifest,
handler=v2 и настоящий DB path. Legacy/отсутствующий config при active запрещены
независимо от pending или полностью закрытой queue. Protocol declaration — договор
review/test конкретного релиза, не автоматическое доказательство произвольного кода.

Для compatible active recovery допустимо offset_floor ≤ mirror ≤ cursor. Mirror
lag предусмотрен write-before-ack; ahead запрещён. Для retirement по-прежнему
**mirror==cursor**. Guard не двигает offset, не выполняет GPT/API/migration/recovery.
Только mode=ro; missing DB не создаётся. Неизвестное состояние — отказ без произвольного
текста исключения. Child OPERATOR_DB закрепляется manifest путём только в его процессе;
`.env` и глобальное окружение не меняются. Модель/reasoning не менялись.

## 5. Активация и rollback

CLI launch/update закреплены за единственным installed `runtime.json`; другой
manifest через аргумент CLI не принимается. Проверка proposal сама не запускает код.
FSM: baseline → armed → active → armed → retired. Initial baseline разрешён только
до появления queue, protocol=0. Armed всегда запрещает запуск. До миграции сначала
публикуется armed и проверяются обе постоянные launch points. Новый epoch задаётся
явно; offline v2_queue_epoch связывает существующую мигрированную metadata с manifest.
Это отдельная операция, не побочный эффект startup guard; core и offset неизменны.
Active публикуется последним. Schema migrations нового типа guard не добавляет.

Proposal builder только создаёт review artifact. Update требует exact SHA256 одних
и тех же прочитанных bytes, maintenance, обоих свободных OS locks, допустимого FSM.
Temp/fsync/owner Administrators/os.replace исключают частичный JSON; ошибка сохранения
или owner assignment оставляет старый документ. Installer задаёт protected DACL:
Administrators/SYSTEM FullControl, runtime RX; проверяются owner/reparse/write ACL.

Для epoch-bound prepare-rollback дополнительно требует установленный guard/manifest,
phase=armed, тот же epoch/path/cursor — до архива и перед DROP. Старое равенство offset
сохранено. Pending, unknown, inflight и неотправленный outbox запрещают процедуру.
Guard сам ничего не удаляет. После archive+явного DROP оператор делает retire proposal.
Он проверяет archive hash/integrity/schema/epoch/drained/core hashes и текущий mirror.
Устаревший закрытый архив не может скрыть новые принятые pending jobs: arming уже
закрепил более новый cursor. После retired архив/receipt остаются обязательны.
Никаких старых SQLite поверх новых, offset rewind или автоматического replay.

Удаление/повреждение manifest, ручной DROP либо пустая очередь не превращаются
автоматически в разрешение baseline/старого SHA. Нужен проверенный retirement.

В черновой реализации обнаружен и исправлен дефект порядка: общий launch check
retired proposal отклонял ещё установленный protocol=1 и мешал опубликовать receipt
до checkout старого кода. Сначала сквозной тест упал с queue_activation_required.
Теперь admin transition проверяет данные/receipt/core/offset, а launch независимо
запрещает текущий protocol=1 в retired. Тот же тест проходит полный цикл от baseline
до создания заявки, архива, retirement **до checkout**, отказа нового и допуска old SHA.
Никакой код не запускается самой операцией публикации receipt.

## 6. Тесты

Финальная целевая регрессия: **462 passed за 121.92 s**, без skip/xfail.
Исходные 416 случаев (включая 4 ранее красных) зелёные; добавлено 46 guard cases.
Полный legacy pytest не запускался. Launcher отключал сеть, credentials и production
SQLite; fake OpenAI/Telegram и только временные БД. Внешний guard реально запускался
отдельными Python процессами, с copied frozen supervisor, synthetic main/locks;
проверялось отсутствие child marker при отказе, не только bool вспомогательной функции.

Четыре №17 сохранили сценарии pending+уже подтверждённый legacy offset и ожидаемый запрет:
candidate/настоящий published aa11380 × legacy/нет config. Изменена точка наблюдения:
вместо ожидания, что старый чистый selector сам вдруг узнает очередь, проверяется
постоянный launch boundary. Старый selector специально всё ещё возвращает legacy;
реальный внешний guard отказывает до импорта worker. Для двух published случаев
сделан изолированный локальный Git clone/checkout настоящего aa11380 без `.env`/DB.
Это проверка требуемого эффекта сильнее unit-проверки selector, не skip/xfail.

Покрыты: совместимый launch; три варианта old handler/config; pending/unknown/closed;
outbox/inflight; offset ahead и безопасный compatible lag; archived retirement;
missing/broken manifest; unknown version/layout; DB недоступна; hardlinks; смена SHA
после установки; duplicate supervisor; config DB substitution; atomic replace/owner
failure; exact confirmation/locks; stale archive/manual DROP; epoch activation gap;
readonly hashes; отказ start с maintenance; PowerShell parse и facade с fake install env;
запрет подмены единственного protected manifest через CLI.
Core/Decimal/edit/undo, SQLite/history, Access, Windows runtime/supervisor, wire schema,
API diagnostics, Web Search и gpt-6-luna/high остались зелёными.

Административная установка/DACL в ProgramData **не выполнялись**; owner authority
в тестах update моделируется. До фактической активации нужны elevated install,
проверка защищённого interpreter/ACL под реальным limited task account и read-only
проверка Action. Cross-account DPAPI из №17 не аттестован этим этапом.

Воспроизведение: прежний набор из отчёта №17 + `tests/test_steel_agent_v2_guard.py`,
через `scripts/v2_offline_tests.py` с новым basetemp. Git diff --check/security scan
проверены перед commit. SDK не вызывался вживую, новые форматы/нормализация не менялись.

## 7. Артефакты, установка и итог

- ops/windows_guard: guard.py, queue-protocol.json, Install-Guard.ps1,
  Test-GuardInstall.ps1, Start-UVM.ps1.
- scripts/v2_guard_proposal.py, v2_queue_epoch.py; минимальное дополнение queue_status.
- start-telegram.ps1 facade, два профильных тестовых файла.
- PERSISTENT_STARTUP_GUARD.md, WINDOWS_DEPLOYMENT.md, ARCHITECTURE.md и этот отчёт.

Точные будущие команды и порядок: [PERSISTENT_STARTUP_GUARD.md](../PERSISTENT_STARTUP_GUARD.md).
Он включает preflight → stop → согласованные Backup API → install guard/ACL → baseline
check → отдельно разрешённая смена Action/manual entrypoints → armed → approved full
SHA/queue migration/epoch → active → permanent start → один polling/три execution threads
→ monitoring/manual parallel tests → stop/unknown → verified retirement → old SHA.
Ни один из этих deployment шагов в №18 не выполнялся.

Один логический коммит в `feature/steel-agent-v2-persistent-guard`; полный SHA,
remote verification и Git status сообщаются после commit/push. Это опубликованный
кандидат для согласования, а не установленная защита. Production Git/процессы,
пользовательские SQLite, история, `.env`, Task Scheduler и main не изменены.

Итог: **REQUIRES DESIGN APPROVAL** — требуется утвердить постоянную точку запуска
и её отдельную установку; физически возможный старый ручной путь не замаскирован
обещанием полной защиты. При отказе от смены этой эксплуатационной границы — BLOCKED,
а не READY. Живые API, polling и deployment не выполнялись.
