# ОТЧЁТ CODEX №5 — STEEL AGENT V2

Дата: 2026-10-17. Кандидат подготовлен для публикации, не для самовольного запуска.
Этот отчёт входит в сам коммит; полный SHA и результат push подтверждаются
отдельным итоговым сообщением после Git-операций. До проверки remote публикация
не считается успешной. Production не изменён, живых GPT/Telegram запросов нет.

## 1. Исходное состояние

Worktree `%USERPROFILE%\.codex\.chatgpt-projects\g-p-6ab1fbdba628819190f61b5504081fff\steel-agent-v2-windows-stage4`,
ветка `fix/steel-agent-v2-windows-stage4`, исходный HEAD
`e8f3a04f2164998bff09c40f7dac477e6111d382`.
Diff №4 сохранён: scripts/v2_offline_tests.py, tests/test_steel_agent_v2_windows.py,
WINDOWS_DEPLOYMENT.md и reports/004-windows-preparation.md. Посторонних файлов
не было, исходный git diff --check чистый. Документ №4 сохраняется как история.

Проверено исправление URI: только file URI с uri=True преобразуется через
urlsplit/url2pathname перед resolve; прежняя граница staging/temp сохраняется.
Remote URI и путь вне разрешённых корней отклоняются до открытия SQLite.
Регрессия использует временную БД с пробелами/кириллицей; содержимое пользовательских
баз не нужно. Ошибка backup-теста №4 не маскировалась пропуском проверки.

## 2. GPT-6 Luna High в реальном транспорте

OpenAITransport.from_environment получил необязательные keyword overrides
model/reasoning, использует их при создании транспорта. В runtime.connect
явно передаётся model='gpt-6-luna', reasoning='high'. Нет записи в os.environ,
.env или общую конфигурацию. Credentials, endpoint/proxy продолжают использовать
существующий безопасный механизм. Вызов фабрики без override сохраняет прежнее
чтение env, в том числе для CLI; правило initial Telegram v2 задаёт runtime.

HTTP MockTransport-тест проходит настоящий connect → SteelAgent → Responses.parse
и проверяет отправленный JSON: model=gpt-6-luna, reasoning.effort=high даже при
другой OPENAI_MODEL и low в окружении. Окружение после создания и вызова неизменно.
Тест отдельно проверяет, что legacy AIService._invoke продолжает читать свою
настройку low, а три прежних теста legacy high также проходят. Старый нормализатор,
legacy-промпты/коммерческие правила и legacy-ветка startup не менялись.

Startup-лог v2 берёт фактические model/reasoning из service.transport; legacy
сохраняет прежний log и поведение. Координатор и поиск используют один транспорт,
поэтому оба получают high. SDK retries=0, timeout=180 секунд и один структурный
ремонт остаются прежними. Денежных/токенных квот и max_output_tokens не добавлено.

## 3. Production runtime и права

Production остаётся `%USERPROFILE%\uvm_ai_operator`, HEAD
`19c9657fdc0ef9005e7478ea8a886c6763a9a006`, ветка deployment/pr8-high-20261016.
Проверен именно `.venv\Scripts\python.exe`: Python 3.13.15, openai 3.19.2,
pydantic 2.13.5, python-dotenv 1.2.3, socksio 1.0.0. Совпадают основные pins
из requirements.txt. Production interpreter запустил staging-тесты в offline
launcher с изолированными SQLite. Его venv/зависимости не обновлялись.
Отдельный staging venv тоже Python 3.13.15, pip check без ошибок.

Наличие API-ключа, Telegram-токена и proxy проверено только булевыми признаками.
OPERATOR_DB не задан: сохраняется прежний operator.sqlite3 в корне production.
Значения секретов не выводились, в staging они не копировались. Все тесты
удаляют реальные API/Telegram env и выключают dotenv/network.

ACL корня production: владелец и пользователь задачи UVM_SteelBot совпадают,
у этого пользователя inherited FullControl. Следовательно ACL допускает создание
отдельной SQLite v2 рядом с legacy; реальный файл там не создавался. Доступность
свободного места/ACL следует повторить в момент будущей установки. Права на
рабочую SQLite не изменялись, заявки для тестов не читались.

## 4. Разбор 51 предупреждения Git

Операция-источник предупреждений №4 — git status --porcelain --untracked-files=normal,
запущенный ограниченным sandbox-пользователем Codex. Повтор той же команды обычным
пользователем Windows вне sandbox: exit=0, stdout пустой, stderr пустой.
Текущее production-дерево достоверно проверено как чистое в обычном контексте.

Метаданные: в корне production 52 каталога с именами tmp*/mrdiag_*, из них
51 с защищённой DACL и один общий tmp с наследуемой ACL. Это временные каталоги
вне .git и вне staging, а не повреждение Git-метаданных. Обычный пользователь
читает их метаданные. Например mrdiag_* создан 2026-10-09, общий tmp — 2026-09-25;
они старше №4/№5. Процесс, первоначально создавший каждый каталог, по ACL/времени
создания установить нельзя; история создания не выдумывается. Диагностировано
именно происхождение warnings, а не неподтверждённый автор прежних файлов.

Содержимое каталогов не выводилось, DACL не исправлялась, ничего не удалялось.
Сами предупреждения sandbox не блокируют checkout обычным deployment-пользователем.
При будущей установке всё равно повторить status и применять
--no-overwrite-ignore: конфликт локальных файлов означает STOP, не удаление.

## 5. Тесты и воспроизведение

Из staging, без production token/API/DB:

```powershell
& .\.venv\Scripts\python.exe -X utf8 -B scripts\v2_offline_tests.py `
  tests/test_steel_agent_v2.py tests/test_steel_agent_v2_prelaunch.py `
  tests/test_steel_agent_v2_windows.py tests/test_windows_operations.py `
  tests/test_access.py tests/test_telegram.py --basetemp="$env:TEMP\steel_v2_stage5"
```

**182 passed, 20 subtests passed**. С реальным production interpreter вместо
staging python и дополнительным tests/test_reasoning_high.py:
**185 passed, 20 subtests passed**.

Проверены: неизменный оригинал/partition/Decimal, отсутствие потери/двойного счёта,
адресные атомарные правки, версии/undo/stale revision, повтор Telegram update/hash,
ошибочный JSON и отказ GPT, история/reopen, inflight и ошибки commit, повреждённый
handler config, отдельная БД v2, запрет legacy path, права до commit/delivery,
Responses structured output и search с citations через HTTP mock. Polling/API
в этих тестах подменены; настоящих соединений не было. Интеграционный worker
loop запускался только как обычная функция на fake Telegram API и временных БД.

Импорты ядра/адаптера входят в проверенные тесты на обоих интерпретаторах.
Тесты не доказывают доступ ключа к живой модели или качество живого извлечения.
Full legacy pytest не запускался; B1–B10/PR9/10 не переносились.

## 6. Установка и откат — только будущий этап

Фактическая история отличается от предположения «две несовместимые ветки»:
19c9657 является предком e8f3a04 и кандидата №5. Разные имена веток не требуют
merge. Main и исходная deployment-ветка сохраняются. Полный одобренный `$Target`
после публикации сверить с origin/fix/steel-agent-v2-windows-stage4 и отчётом №5.

Поддерживаемая последовательность в WINDOWS_DEPLOYMENT.md:
1. Fetch и review одобренного SHA в staging, повторная сверка production HEAD,
   status, ancestry, процессов, maintenance, конфигурации и путей БД.
2. stop-telegram.ps1, ожидание worker/supervisor; при timeout не kill, сохранить
   maintenance. После остановки окончательный baseline и SQLite Backup API для
   legacy, существующего бюджета и v2, если существует. Integrity/hash/reopen.
3. На чистом Git `git switch --no-overwrite-ignore --detach $Target`. Это не
   reset/force и не перезапись каталога; исходная ветка остаётся для отката.
   Флаг проверен на установленном Git в одноразовом тестовом репозитории.
4. Проверить SHA/импорты/совместимые зависимости. Атомарно выбрать handler=v2
   через локальный telegram-handler.json с отдельной SQLite. .env/задачу не менять.
5. Один start-telegram.ps1, один polling; ready/handler/model=high/logs минимум
   3 минуты, права и legacy история сохранены. Живые GPT/search — только после
   отдельного разрешения платных тестов и непосредственно через Telegram.

Если production к моменту установки расходится с кандидатом, ancestry/status
останавливают процесс. Нужен новый review в отдельном staging, а не копирование
каталога или автоматическое слияние. Staging никогда не запускает второй бот.

Rollback: штатно остановить новый worker, снять НОВЫЕ snapshots текущих БД,
вернуть `$Before` через безопасный detached checkout и прежний handler config.
Не восстанавливать старую БД автоматически; не откручивать общий offset.
V2 turn/version/active и legacy история остаются на диске. Не переносить новую
заявку в старую структуру автоматически. Сам rollback в production не выполнялся.

## 7. Состав публикации и ограничения

Один логический коммит включает сохранённый №4 и №5:
- scripts/v2_offline_tests.py;
- steel_agent_v2/runtime.py, steel_agent_v2/transport.py;
- telegram_bot.py (только startup-лог выбранного v2);
- tests/test_steel_agent_v2_prelaunch.py, tests/test_steel_agent_v2_windows.py;
- docs/steel-agent-v2/WINDOWS_DEPLOYMENT.md, ARCHITECTURE.md, DECISIONS.md;
- docs/steel-agent-v2/reports/004-windows-preparation.md и этот отчёт №5.

Перед коммитом staged allowlist, diff --check и проверка credential-patterns;
SQLite/.env/backups/logs/runtime config/venv не включаются. Скан сигнатур дополняет
проверку фактического diff, не обещает аудит всей истории Git.
Удалённая ветка кандидата при предварительном ls-remote отсутствовала;
публикация обычным push с upstream, без force. SHA и проверка remote — в итоговом
сообщении. Следующий deployment требует отдельного согласования.

Нерешённых технических блокеров совместимости не найдено. Не проверены живые
Telegram/GPT/search, доступ аккаунта к модели, текущая сеть и качество извлечения;
это будущая приёмка, не доказательство успешного запуска. Windows production,
его .env/БД/worker, Task Scheduler, main и старые ветки не изменены.
