# ОТЧЁТ CODEX №7 — первое живое испытание GPT

Дата: 2026-10-17. **NEEDS DIAGNOSTICS** — HTTP 400 и точное место отказа
подтверждены, но первопричина отклонения API не установлена по сохранённым данным.
Не считать факт BadRequestError доказательством ошибки модели, high или JSON Schema.
Новых платных запросов, повторов пользовательских сообщений и deployment не было.

## 1. Production

HEAD `039de5f7565be717890667cea2765da53bf4182b`, git status чистый.
Локальный selector — v2, отдельная steel-agent-v2.sqlite3.
Startup и runtime-код подтверждают gpt-6-luna/high.
Supervisor PID 24292 и worker PID 28188 сохранены; промежуточные PID
31172/29972 — Windows venv launchers. Локально один фактический polling-worker,
ready содержит 28188. Запуск/остановка не выполнялись.

## 2. Три операции

Анализировались только события заданного времени и метаданные ровно трёх
сообщений, предоставленных пользователем. Тексты не включены в отчёт/логи.

| № | Telegram event | sequence | Время отказа | operation | Ошибка |
|---|---|---:|---|---|---|
| 1 | telegram:9346205 | 1 | 13:03:05.544 | 63e56846ce48 | BadRequestError / api_bad_request |
| 2 | telegram:9346206 | 2 | 13:03:20.028 | e494b07386a1 | BadRequestError / api_bad_request |
| 3 | telegram:9346207 | 3 | 13:03:30.407 | 26e70b8d9b94 | BadRequestError / api_bad_request |

Для каждой операции: coordinate calls=1, http_attempts=1, sdk_retries=0.
Длительность модельной попытки: 1299 / 563 / 593 мс соответственно.
Все три исходных message совпали с предоставленными строками по SQL equality и
input_hash; в отчёт выведены только факт совпадения и диагностические ID.

Путь подтверждён по коду и событиям:
Telegram update → access.allowed → SteelAgent.handle → Repository.begin
(сохранён исходник/inflight) → OpenAITransport.plan → Responses.parse →
BadRequestError → внешний except SteelAgent.handle → Reply failed/clarify →
Repository.finish без updated (история сохраняется, claim удаляется).

Авторизация прошла: без неё begin/plan/finish недоступны. Подтверждена одна HTTP
попытка к настроенному OpenAI endpoint и полученный HTTP-ответ, классифицированный
SDK как 400. Это не успешный inference. Поскольку body/request_id реального ответа
не сохранены, происхождение отказа на стороне OpenAI или промежуточного прокси
однозначно не различается. Доступ аккаунта к модели не подтверждён успешным ответом.

До возвращения структурированного плана не дошло: локальная валидация AgentPlan
и RequestEngine.create/edit для этих сообщений не выполнялись. Не было
steel_v2_validation/ремонтной попытки; generic текст взят из внешнего except,
а не из ветки невалидного извлечения. Пользователь подтвердил получение трёх
ответов. Отдельного sendMessage message_id/ACK в диагностике нет; журнал завершения
без delivery exception сам по себе не заменяет пользовательское подтверждение.

## 3. Что потеряно и что нельзя утверждать

OpenAITransport._call сохраняет только safe_code, время, модель и usage при
наличии. HTTP status, SDK error.code/error.param и request_id исключения
терялись. Agent сохраняет только ['api_bad_request'] в Reply.diagnostics.
Тело/текст ошибки и заголовки не логируются, что защищает данные, но без
безопасных структурированных метаданных точный rejected parameter не определить.
В памяти metrics тоже нет этих полей; чтение памяти процесса не выполнялось.

Дополнительно доказан дефект диагностики: SteelAgent ловит исключение и возвращает
failed Reply, а operation() видит обычный возврат и пишет result=ok для всех трёх.
Это не свидетельство успешного API/сохранённой нормализации.

Read-only аудит contracts/transport: API Responses.parse, reasoning={'effort':'high'},
строгий AgentPlan; проверки обязательных object properties/additionalProperties
в SDK-generated JSON Schema не выявили нарушения этих двух инвариантов.
Это не полная серверная приёмка схемы. Гипотезы unsupported schema keyword,
параметры модели, доступ к модели и proxy остаются гипотезами, не исправлениями.
API-ошибки 401/403/404/429/500 и тайм-ауты не наблюдались в этих трёх операциях.
SQLite-ошибка тоже не подтверждена: finish успешно сохранил три failed turns.

## 4. Сохранность

Обе рабочие SQLite открывались только mode=ro + query_only.
V2 integrity_check=ok; steel_v2_turn=3, steel_v2_version=0,
steel_v2_inflight=0. В steel_v2_active одна служебная строка sequence, но payload
NULL: **активной заявки нет**, частичных изменений нет. История хранит исходники
и failed replies; её записи не удалялись/не исправлялись.

Legacy integrity_check=ok. Сверка с metadata baseline deployment №6:
все таблицы, schema/row hashes/counts кроме telegram_state неизменны.
Общий offset штатно продвинулся 9346205 → 9346208. Legacy active/history и
пользовательские права не затронуты. Никакого ручного восстановления не было.

## 5. Изолированное воспроизведение и патч

Новый worktree `steel-agent-v2-first-api-failure`, ветка
`fix/steel-agent-v2-first-api-failure` от точного production SHA.
Патч касается **подтверждённого пробела диагностики**, не недоказанной причины API.
До изменения: 2 failed в новых тестах, включая отсутствие http_status и
воспроизведённое operation_complete.result=ok при настоящем SDK BadRequestError.

HTTP MockTransport возвращает 400; настоящий SDK, OpenAITransport, SteelAgent,
Repository и operation() воспроизводят тот же generic отказ без вызова engine.
В mock использован пример code=invalid_json_schema: **это иллюстративный ответ,
не восстановленный реальный error.code**. Полный body содержит приватный sentinel,
и тест требует его отсутствия в логах.

Подготовлены:
- transport.api_error_metadata: ограниченный http_status; allowlist безопасных
  error.code/param; request_id только req_* ограниченной длины. Неизвестные
  строки заменяются other, message/body/headers не записываются.
- transport._call: эти метаданные и operation ID включаются в существующее
  событие steel_v2_model_call/metrics, затем исходное исключение пробрасывается.
- agent внешний except: Trace.result устанавливается в фактический safe_code,
  чтобы API-failure не завершался ложным result=ok.
- tests/test_steel_agent_v2_api_diagnostics.py: 9 регрессионных случаев,
  включая 400/401/403/404/429/500, timeout без выдуманного HTTP status, сохранение
  истории/inflight, запрет engine до успешного ответа, отсутствие скрытого retry
  и утечки произвольных error strings/body.

После патча: **194 passed, 20 subtests passed** — диагностические тесты плюс
v2/prelaunch/Windows/access/Telegram/legacy reasoning. Launcher блокирует сеть,
dotenv и пользовательские БД. Использован уже существующий staging venv,
production Python/deps не менялись. Полный legacy pytest не запускался.
Промпты, схема AgentPlan, параметры модели, retries и бизнес-логика не изменены.

## 6. Следующий безопасный шаг

Для уже работающего процесса метаданные прошлого исключения не восстановить
этим патчем; простой перезапуск прежнего кода ничего не исправит.
Предлагается отдельное согласование review/публикации/установки диагностического
патча и одного штатного перезапуска. Затем пользователь инициирует одно новое
сообщение вручную, чтобы получить status/code/param/request_id. Никаких скрытых
платных повторов старых трёх сообщений или очистки их idempotency записей.
После этого локальное воспроизведение **фактической** причины и узкое исправление.
В №7 перезапуск, commit, push и deployment запрещены и не выполнялись.

Текущий статус: NEEDS DIAGNOSTICS. Точное место HTTP400 найдено, данные сохранны,
но проблема живого GPT не объявляется решённой диагностическим патчем.
