# ОТЧЁТ CODEX №4 — STEEL AGENT V2

Дата: 2026-10-17. **READY — локальный технический кандидат для review.**
Это не deployment, не опубликованный совместимый релиз и не живая AI-приёмка.
Нужны проверка diff, отдельное решение о публикации кандидата и разрешение
переключения. В №4 нет commit/push/merge и нет изменения production.

## 1. Проверенная Windows-установка

Production: `%USERPROFILE%\uvm_ai_operator`, ветка
`deployment/pr8-high-20261016`, HEAD
`19c9657fdc0ef9005e7478ea8a886c6763a9a006`.
Tracked diff отсутствует, git status --porcelain пустой. Полная проверка untracked
выдала 51 предупреждение о недоступных старых временных каталогах: отсутствие
скрытых файлов внутри них не утверждается. Перед реальным переключением проверку
повторить с доступом к этим каталогам; не удалять/перезаписывать их автоматически.

Python 3.13.15. Установленные основные версии совпадают с требованиями v2:
openai 3.19.2, pydantic 2.13.5, python-dotenv 1.2.3, socksio 1.0.0;
pytest 9.1.1. Рабочий venv не изменялся.

Task Scheduler: `UVM_SteelBot`, состояние Ready, запускает существующий
`.venv\Scripts\pythonw.exe windows_supervisor.py` с cwd production,
RestartCount=3. Настройки задачи не изменены.

Дерево процессов на момент проверки:
- supervisor: launcher PID 20808 → фактический Python PID 19692;
- worker: launcher PID 13444 → фактический Python PID 16856;
- `telegram.ready` содержит 16856, maintenance отсутствует.

Это две пары launcher/interpreter Windows venv, не два polling-worker.
Локально обнаружен один фактический worker. Внешние компьютеры с тем же токеном
без Telegram API не проверялись; OS lock защищает только этот каталог.
Запуск/остановка и попытки захвата production locks не выполнялись.

Последние 50 строк каждого рабочего лога прочитаны обратным seek, без печати
текста заявок. Найдены исторические сетевые тайм-ауты 2026-10-16; в хвостах нет
подтверждённых Conflict 409, import/auth/SQLite ошибок или traceback.
Первичный поиск цифр 409 дал ложное совпадение с миллисекундами timestamp,
SQLite — с информационной записью. Проверены уровни/коды событий, а не наличие
подстрок. Старый polling_connected/живой процесс не доказывает текущую доставку.
Telegram API, getMe/getUpdates и тестовые сообщения не вызывались.

## 2. Данные и авторизация

OPERATOR_DB отсутствует в .env и проверенных внешних user/machine настройках;
штатный worker использует `operator.sqlite3` в production root.
Проверка настоящей БД выполнена только sqlite mode=ro + query_only, без
конструкторов репозиториев и CREATE/ALTER. integrity_check=ok.

Безопасный baseline: telegram_active=2, telegram_history=97,
telegram_access_user=3 (один approved admin, два approved employee),
telegram_access_event=5, offset=9346205 на момент чтения.
Состав: telegram_active/history/state, telegram_access_user/event/bootstrap/
claim_attempt, normalization_rule/event. Тексты/ID пользователей не выводились.
`ai-budget.sqlite3` существует (8192 байта), не открывался/не менялся.
Ни одна пользовательская БД или .env не копировалась в staging.

Offset остаётся в прежнем telegram_state через TelegramRepository.
Права обслуживаются прежними AccessRepository/TelegramAccess, включая проверку
до обработки/commit/delivery и повторное одобрение. Эти файлы не отличаются от
production. V2 хранит active/version/turn/inflight в отдельной явной SQLite;
путь той же legacy БД и чужая схема отклоняются. Старые active/history не
переносятся автоматически, не очищаются и не переинтерпретируются.

## 3. Получение и сравнение кода

git fetch origin выполнен. origin/feature/steel-agent-v2 точно равен
`e8f3a04f2164998bff09c40f7dac477e6111d382`.
От него создан отдельный worktree `steel-agent-v2-windows-stage4`, ветка
`fix/steel-agent-v2-windows-stage4`; основной checkout не переключался.

Production 19c9657 является предком e8f3a04. Важное отличие от предположения
публикационного runbook: именно эта Windows-установка не содержит PR9/10,
/version и постоянных legacy-квот. Возвращать их не требуется и не разрешено.
Сравнение windows_supervisor.py, windows_runtime.py, start-telegram.ps1,
stop-telegram.ps1, requirements.txt, database.py, repositories/access_repository.py
и repositories/telegram_repository.py не показывает изменений.
Минимальная интеграция уже включена в telegram_bot.run_bot: локальный selector
выбирает dispatch v2 в том же worker, polling и offset остаются прежними.
Полного merge архитектур и переноса B1–B10 не требуется.

## 4. Окружение и проверки

Создан отдельный `.venv` в staging на Python 3.13.15; установлены
requirements-dev.txt и requirements.txt. pip check: No broken requirements found.
Python 3.12 на этом Windows не обнаружен: результаты Mac/3.12 не выдаются за
Windows-тесты. Никакие зависимости production не обновлены.

Импорты steel_agent_v2.runtime/telegram/repository/transport, telegram_bot,
windows_runtime и windows_supervisor прошли без вызова main/run_bot, клиента
или конструкторов БД. CLI v2_llm_check проверен только с --help, не --live.

Офлайн-команда из staging:

```powershell
& .\.venv\Scripts\python.exe -X utf8 -B scripts\v2_offline_tests.py `
  tests/test_steel_agent_v2.py tests/test_steel_agent_v2_prelaunch.py `
  tests/test_steel_agent_v2_windows.py tests/test_windows_operations.py `
  tests/test_access.py tests/test_telegram.py --basetemp="$env:TEMP\steel_v2_stage4" --tb=short
```

Launcher удаляет реальные API/Telegram env, отключает dotenv, сеть и внешние
pytest-плагины, ограничивает SQLite staging/temp. Использованы fake/HTTP
MockTransport и временные SQLite; operational tests запускают только тестовые
процессы locks, не Telegram polling.

Исходно **179 passed, 1 failed, 20 subtests passed**. Дефект Windows:
Path('file:///C:/...?...') в guard воспринимался как обычный файловый путь.
Законный временный backup блокировался как доступ к пользовательской БД.
Исправлен только разбор SQLite URI в scripts/v2_offline_tests.py:
urlsplit + url2pathname, resolve и прежняя проверка разрешённых корней.
UNC/remote URI отклоняются, путь вне staging/temp остаётся запрещённым.
Добавлен subprocess regression с пробелами/кириллицей в имени, read-only URI,
запретом внешнего URI до открытия SQLite и отказом remote-host.

После исправления **181 passed, 20 subtests passed**.
Проверены структурированные Responses SDK parse и search через HTTP mock,
ошибки/тайм-ауты без скрытых повторов, source/Decimal/partition, адресные правки,
undo, атомарность, stale revision, replay/event hash, конкурентные сообщения,
история и повторное открытие БД, inflight, отказ сохранения, отзыв прав,
plain delivery и dispatch через настоящий worker loop с подменённым Telegram API.
Старый AI при handler=v2 не конструируется; offset общий; legacy active/history
и права не меняются. Четыре B6 корпуса проверены через Git-объекты только как
контролируемый транспорт, без импорта исправлений legacy.
Полный legacy pytest не запускался, исторические падения не ремонтировались.

## 5. OpenAI без реальных вызовов

Наличие OPENAI_API_KEY и Telegram-токена подтверждено только булевыми признаками.
Значения не печатались/не копировались. OPENAI_MODEL=gpt-6-luna.
OPENAI_BASE_URL не задан: ожидается штатный endpoint SDK; конфигурация прокси
присутствует, не изменялась. Соединение и действительность ключа не проверялись.

В существующем .env reasoning=low, fallback=medium. Legacy 19c9657 принудительно
переводит primary/retry на high. V2 уважает OPENAI_REASONING_EFFORT и при нынешнем
.env получит low, а не constructor default high; внешние user/machine override
этих настроек не обнаружены. Настройки не исправлялись скрыто. Если для запуска
нужен high, это отдельное явное решение перед переключением.

Transport v2: Responses.parse(text_format=AgentPlan), store=False,
reasoning.effort из конфигурации, SDK retries=0, timeout=180 секунд; один
адресный ремонт структуры, без квот денег/токенов. Поиск отдельным Responses.create:
web_search, tool_choice=required, max_tool_calls=1; успех требует завершённого
search_call и безопасных URL citations. Неуспех поиска не меняет заявку.

Совместимость заявленных возможностей проверена по официальным страницам
[GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna),
[Structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs),
[Web search](https://developers.openai.com/api/docs/guides/tools-web-search).
Поддержка Responses, structured outputs, web search, low/high описана в документации;
доступ именно этого аккаунта и реальная надёжность ответов остаются непроверенными.

## 6. Будущее переключение — НЕ выполнялось

1. Согласовать локальный diff, получить отдельный опубликованный SHA кандидата
   `$Target`. Сейчас такого SHA нет: e8f3a04 — опубликованное ядро, но локальная
   Windows-правка ещё не включена. Commit/push/установка в №4 запрещены.
2. В production установить `$Before`, ветку, чистоту Git и отсутствие параллельного
   maintenance. Повторно проверить PID/ready, пути БД и текущую политику reasoning.
   Требовать `git merge-base --is-ancestor $Before $Target` и сверенный diff.
3. Штатно `stop-telegram.ps1 -TimeoutSeconds 900`. Дождаться обоих процессов;
   при тайм-ауте сохранить maintenance и STOP, без kill/второго worker.
4. Снять окончательный baseline counts/hashes/schema/offset/rights. В защищённом
   backup-каталоге вне Git сохранить SHA и текущий handler config, если есть.
   .env не перезаписывать. Connection.backup каждой фактической SQLite, включая
   существующий budget и старый v2, с новыми destination/manifest. Не копировать WAL.
5. Проверить каждый backup через integrity_check и повторное открытие/hash/count.
   Использовать scripts/v2_sqlite_snapshot.py из проверочного worktree; его
   test_sqlite_snapshot_is_reopenable_identical_and_does_not_overwrite прошёл.
   Реальные production-backup в №4 не создавались.
6. При остановленных процессах установить одобренный код штатным checkout
   `$Target` (detached deployment сохраняет исходную ветку); не reset/clean/merge.
   Проверить точный SHA и импорты прежним совместимым production interpreter.
7. Атомарно создать telegram-handler.json с handler=v2 и отдельным путём
   steel-agent-v2.sqlite3 относительно production. Не использовать legacy/budget
   или их aliases. Existing v2 проверить на schema/inflight, не заменить пустой.
   Подробные PowerShell-команды — WINDOWS_DEPLOYMENT.md. .env/задачу не менять.
8. Один `start-telegram.ps1`: он снимает maintenance и запускает штатный supervisor.
   Проверить один фактический worker/polling, ready, telegram_handler=v2,
   polling_connected, отсутствие новых 409/auth/import/SQLite ошибок минимум 3 минуты.
9. Сверить права/legacy active/history по counts/hash; общий offset не должен
   откатываться. V2 integrity_check=ok, отдельная новая история сохраняется.
10. Только после отдельного разрешения платных тестов пользователь проверяет
    GPT-6 Luna и поиск с источниками непосредственно в существующем Telegram,
    создание/правку/undo и сохранение контекста. Mock не подтверждает доставку.

## 7. Откат с сохранением новой истории

Сохранить maintenance, штатно остановить новый worker, не убивать inflight.
Снять НОВЫЕ backup всех текущих БД и сохранить причину/claim/код ошибки.
На чистом Git вернуть `$Before` через `git switch --detach $Before` и прежний
handler config. У 19c9657 selector отсутствует: старый код использует legacy;
новый v2-файл остаётся сохранённым и старым кодом не читается.
Не восстанавливать старую SQLite поверх свежей при обычной ошибке кода.
Legacy active/history/права и новый offset сохраняются; v2 turn/version/active
тоже сохраняются отдельным файлом. Не импортировать active v2 в legacy автоматически.
Перед одним штатным запуском убедиться в прежних зависимостях и отсутствии
живого нового worker. При повреждении данных/неясном inflight — STOP для разбора.
Статический rollback проверен совместимостью исходного кода/зависимостей и
тестами snapshots/replay; реальный останов/откат production не выполнялся.

## 8. Файлы и Git

HEAD staging e8f3a04, ветка fix/steel-agent-v2-windows-stage4.
Локальный незакоммиченный diff:
- scripts/v2_offline_tests.py — безопасный URI path guard на Windows;
- tests/test_steel_agent_v2_windows.py — регрессия guard;
- docs/steel-agent-v2/WINDOWS_DEPLOYMENT.md — фактическая совместимость, Python,
  условность PR9/10/version/quota и сохранение исходной политики при rollback;
- docs/steel-agent-v2/reports/004-windows-preparation.md — этот отчёт.

Ядро, промпты, модели, legacy-нормализация, пользовательские схемы и startup
скрипты не изменены. Venv — только локальный игнорируемый staging-файл/каталог.
Новые таблицы создавались исключительно во временных SQLite тестов.
После проверки — остановка работы. Ни публикации кандидата, ни deployment,
ни живой приёмки в этом этапе не было.
