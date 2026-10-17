# ОТЧЁТ CODEX №2 — STEEL AGENT V2

Дата: 2026-10-17. Mac, /Users/max/uvm_ai_operator_steel_agent_v2.
Ветка feature/steel-agent-v2, HEAD 6b16c404d7ad68ec05bb7dc929e4a042d8b59adf.

**Оценка развёртывания: BLOCKED.** Прямая установка текущей feature-ветки на
Windows опасна из-за отставания от выпущенного кода и нерешённой политики
контекста после повторного допуска. Ядро и неактивное подключение подготовлены
и проверены офлайн. Разрешение на deployment этот этап не предоставляет.

## 1. Созданная структура документации

В docs/steel-agent-v2/: PRODUCT.md, AGENTS.md, ARCHITECTURE.md, NORMALIZATION.md,
TESTING.md, DECISIONS.md, WINDOWS_DEPLOYMENT.md, plans/001-implementation.md,
plans/002-prelaunch.md, reports/001-implementation.md, reports/002-prelaunch.md.
PRODUCT — объём продукта, ARCHITECTURE — реальный код, NORMALIZATION — инварианты,
TESTING — проверки/база падений, DECISIONS — решения, WINDOWS_DEPLOYMENT — операции.
Корневой AGENTS.md создан, прежнего не было: он требует читать инструкции v2
при работе над кодом/тестами, а не только внутри документации. Старые два документа
служат ссылками на единый набор. Их содержимое №1 перед переносом сохранено в
игнорируемом tmp/steel-v2/prompt1-documents; факты/ограничения перенесены в новые
документы, результат №1 зафиксирован отдельно от №2.

## 2. Результаты проверки ядра №1

HEAD/ветка совпали; реализация №1 была незакоммичена. Повтор исходных v2 тестов:
69 passed. Существенного расхождения с сохранённым отчётом №1 нет.
Проверены реальные contracts/agent/engine/repository/presentation/transport/telegram,
а также путь main → worker → access → обработчик и supervisor/locks/offset.
GPT остаётся единственным интерпретатором; старый нормализатор не вызывается v2.
Переиспользуются Database, ContractModel/ExactDecimal, quantity helpers,
dialog_history, diagnostics, TelegramAccess, TypingIndicator и safe_chunks.
Python 3.12.14, отдельный .venv, pip check — No broken requirements found.

## 3. Найденные блокирующие дефекты

- В agent.handle черновик updated переживал неудачную попытку формирования
  ответа. Если следующий план был clarify, мог сохраниться отклонённый edit.
  Сначала воспроизведено целевым падающим тестом, затем исправлено.
- Не было пути выбора адаптера из существующего worker: было только ядро/factory.
  Подготовлен opt-in, default legacy, без второго polling.
- Ветка v2 не содержит слитые PR9 (562ddf6) и PR10 (5e94aa4). Последний предоставленный
  Windows-отчёт указывает 562ddf6; Mac main — 5e94aa4. Фактический Windows HEAD
  в №2 не проверялся. Общий предок с
  локальной origin/main — a6509c3899e161a8e4b8a56547e5854c55bd31c2, а не текущий
  production. В исходной ветке нет BETA_ROLLOUT.md и /version. Прямая установка
  уберёт уже выпущенные изменения; выполнять такую замену нельзя.
- Telegram factory проверяет allowed, но не использует legacy context_floor/epoch.
  Отзыв блокирует чтение/запись, повторное одобрение открывает прежний контекст v2.
  Старый handler скрывает прежний контекст после повторного допуска. Нужно решение
  пользователя о политике; ни удаление истории, ни обход защиты не применялись.

## 4. Исправленные дефекты и тесты

В agent.py каждый проход ремонта начинает с updated=None. Fault-test подтверждает
неизменность active и отсутствие revision 2 при clarify после сбоя presentation.
В telegram_bot.py добавлен выбор runtime.selection/connect; текущие locks,
maintenance, общий offset и цикл polling сохранены. Legacy AI не конструируется
в ветке v2, v2 уважает настроенный reasoning. Неправильный config останавливает
запуск до сетевых вызовов/БД, без неявного возврата в legacy.
Новый runtime.py читает только необязательный локальный telegram-handler.json;
файл исключён из Git и на Mac не создавался. Изменения startup scripts не нужны.
Добавлены 16 предпусковых тестов: fault recovery, конфигурация, реальный run_bot
с fake API/SQLite, повтор update и правка, старые данные/offset/права, отдельный путь,
backup/reopen/запрет перезаписи и явно обозначенное поведение reapproval.
scripts/v2_sqlite_snapshot.py — операционный helper будущего backup, не функция
бота. scripts/v2_offline_tests.py дополнительно удаляет STEEL_AGENT_* из test env.

## 5. GPT-транспорт и интернет-поиск

Responses SDK openai 3.19.2, gpt-6-luna/high по умолчанию. Model/reasoning
конфигурируются. Structured AgentPlan проверяется Pydantic и RequestEngine.
SDK retries=0, timeout 180 сек/вызов; максимум два плана и один read-only search,
без бесконечных повторов, искусственных токенных/денежных квот. API errors не
размножают мутации. Метаданные вызовов/usage без секретов — в diagnostics.
Поддержка модели, Responses structured outputs и web_search сверена с официальной
документацией OpenAI и фактическим SDK; ссылки — TESTING.md.
Search требует реальных completed tool-call/citations, иначе сообщает отказ;
внешний результат не исполняется как операции. Источники сохраняются в Reply.
Живой поиск, качество GPT и доступность на аккаунте не проверены. История после
веб-ответа всё равно требует живой проверки на prompt injection, а не обещаний
абсолютной безопасности по fake transport.

## 6. Целостность SQLite

Все проверки только на временных тестовых БД; production/user SQLite не открывались,
не копировались и не изменялись. Проверены atomic commit/rollback, неизменный оригинал,
Decimal/однократный учёт, stale rejection, версии/undo, повторное открытие,
inflight при отказе записи и idempotency user/update/hash.
Интеграционный тест: integrity_check=ok у legacy и v2, сохранены legacy active,
history и права; меняется ожидаемый offset 41→43, v2 имеет ровно 2 turn и revision 2
при трёх доставленных update (один повтор). Подтверждено: старый active не попадает
в GPT как v2. Helper backup проверен на отдельной тестовой БД: повторное открытие,
совпадение hashes/counts, неизменность источника, секретный тестовый текст не
попадает в manifest; существующие backup не перезаписываются.

## 7. Готовность Telegram-адаптера к Windows

Интерфейсы соединены и проверены fake API в существующем run_bot. Авторизация,
callbacks, typing и plain delivery переиспользуются. Отдельная v2 DB защищает
старые active/history; общий offset сохраняется в operator.sqlite3.
ProcessLock и supervisor оставлены; OS-lock тесты выполнены на Mac, не на Windows.
Они не гарантируют отсутствие polling в другом каталоге/компьютере с тем же токеном:
это обязательная проверка Windows-оператора. Живой Telegram не запускался.

## 8. Конкретные файлы при переключении

После подготовки совместимого одобренного релиза доставляются telegram_bot.py,
весь steel_agent_v2/, scripts и docs. Локально создаётся telegram-handler.json
с handler=v2 и отдельным database. Файл не содержит секретов и не коммитится.
Main.py, windows_supervisor.py, windows_runtime.py, start/stop/install scripts и
Task Scheduler менять для самого выбора обработчика не требуется. .env не менялся.
В будущем релизе необходимо сохранить уже существующие production-изменения PR9/10,
в том числе корректную /version: это не реализуется подменой текущей веткой.

## 9. Результаты тестов

- Исходный v2 №1 повторён перед изменениями: 69 passed.
- В №2: 69 прежних + 16 новых v2/prelaunch сценариев в составе целевого набора.
- Целевой набор v2 + prelaunch + Windows operations + access + Telegram:
  **180 passed, 20 subtests passed**.
- Полный: **1289 passed, 7 failed, 4 deselected, 86 subtests passed**.
- Имена семи падений совпадают с базой №1: одна отсутствующая HTML fixture и шесть
  medium/high расхождений изоляции legacy. Новых падений нет; не скрывались.
- B6 38/22/27/10, read-only Git sources и hash-проверки — без skip на этом Mac.
- pip check и diff --check пройдены. Платных вызовов и реального polling — 0.

Локальные полные выводы: tmp/steel-v2/prelaunch-targeted.txt и prelaunch-full.txt.
Mock не доказывает правильность коммерческой группировки/интерпретации живой модели.

## 10. Известные нерешённые проблемы

До rollout требуется совместимый релиз на фактической версии Windows и решение
по reapproval. Изменения №1/№2 пока только в worktree, в GitHub такого релиза нет.
API/Telegram не проверены вживую; семантическая полнота характеристик, классификация
product/note, выбор intent и коммерческие решения не доказываются source coverage.
После аварии inflight разбирается вручную; автоочистки/автоповтора GPT нет.
Telegram-доставка не exactly-once, повтор ответа возможен без повтора изменения.
Legacy история не импортируется в v2, v2 изменения не переносятся в legacy при
откате. Отмена действует внутри активной заявки, не между архивными заявками.
Старые семь pytest ошибок сохранены. Их исправление не входит в №2.

## 11. План установки и отката

Полная воспроизводимая процедура: ../WINDOWS_DEPLOYMENT.md.
1. Рассмотреть diff, решить reapproval, подготовить/проверить совместимый релиз,
   отдельно разрешить commit/push, API-приёмку и deployment.
2. На Windows получить одобренный полный SHA, проверить ancestry относительно
   фактического production, состояния процессов/данных и offline staging без токенов.
3. Проверить зависимости production venv; при отличии остановиться для отдельного
   плана, не менять среду с потерей возможности возврата.
4. Штатно stop с maintenance; дождаться обоих процессов/locks. Backup каждой БД
   Connection.backup с integrity/hash/count, сохранить исходный SHA/config/offset.
5. Без force установить проверенный commit в прежнем каталоге, проверить импорты,
   локальный selector и отдельный путь v2. Один штатный старт supervisor.
6. Наблюдать ≥3 минут, проверить один polling, сохранность данных и согласованные
   живые сценарии; явно отделить проверку polling от качества GPT.
7. При сбое stop, сохранить свежие диагностические копии и логи, вернуть прежний
   код/config. Текущие SQLite и offset назад не откручивать. Сохранить v2 историю,
   запустить один прежний защищённый handler и повторить контроль.

На этом этапе ни один шаг Windows не выполнялся.

## 12. Git status и git diff --check

HEAD и upstream origin/feature/steel-agent-v2 остались на 6b16c40.
Рабочее дерево содержит незакоммиченные №1/№2: 3 tracked modified, 27 untracked
файлов. Staged изменений нет; commit/push/merge/deployment не выполнялись.
git diff --check и отдельная whitespace-проверка всех новых файлов пройдены.
Проверка всех изменённых/новых файлов не обнаружила ключей по сигнатурам,
Telegram tokens/private keys, .env или SQLite в списке изменений. Это проверка
рабочего diff, не обещание аудита всей истории Git. Корневой main checkout не менялся.

Полный перечень файлов, включая унаследованные незакоммиченные изменения №1:

```text
.gitignore
AGENTS.md
docs/STEEL_AGENT_V2_ARCHITECTURE.md
docs/STEEL_AGENT_V2_VALIDATION.md
docs/steel-agent-v2/AGENTS.md
docs/steel-agent-v2/ARCHITECTURE.md
docs/steel-agent-v2/DECISIONS.md
docs/steel-agent-v2/NORMALIZATION.md
docs/steel-agent-v2/PRODUCT.md
docs/steel-agent-v2/TESTING.md
docs/steel-agent-v2/WINDOWS_DEPLOYMENT.md
docs/steel-agent-v2/plans/001-implementation.md
docs/steel-agent-v2/plans/002-prelaunch.md
docs/steel-agent-v2/reports/001-implementation.md
docs/steel-agent-v2/reports/002-prelaunch.md
scripts/v2_llm_check.py
scripts/v2_offline_tests.py
scripts/v2_sqlite_snapshot.py
steel_agent_v2/__init__.py
steel_agent_v2/agent.py
steel_agent_v2/contracts.py
steel_agent_v2/engine.py
steel_agent_v2/presentation.py
steel_agent_v2/repository.py
steel_agent_v2/runtime.py
steel_agent_v2/telegram.py
steel_agent_v2/transport.py
telegram_bot.py
tests/test_steel_agent_v2.py
tests/test_steel_agent_v2_prelaunch.py
```

После отчёта работа остановлена до следующего согласования пользователя.
