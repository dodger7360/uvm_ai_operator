# Блок 4.3 — безопасная диагностика будущих отказов

База main: `10edf48e30c9bda7d4cb15bbf1a9d12f2b05a91b`, после разрешённого merge
PR #33 с проверенным HEAD `c2e4fcc2c63376cbc587ecd68ebf35115f15a954`.
Локальный main синхронизирован. Новый кандидат — отдельный PR, без merge/deployment.

**Эпизод на 100,602 секунды не исправлен и не переобъяснён задним числом.**
Дополнительные поля применяются только к будущим исполнениям после установки
кандидата по отдельному разрешению. Живых GPT/Telegram-вызовов в этой работе нет.

## Изменения

### Точный безопасный код

В agent.py введён явный неизменяемый allowlist статических EngineError-кодов.
Например, stale_request_revision, groups_must_partition_all_items_once,
edit_evidence_not_in_message и contradictory_duplicate_patch разрешены только
при точном совпадении единственного строкового аргумента EngineError.

Префиксы не выделяются. `duplicate_characteristic:<id>`, любые динамические
значения/смещения/суффиксы, переносы строк и неизвестные сообщения дают только
request_validation. Это намеренно строже прежнего split(':',1). Pydantic
ValidationError даёт единую категорию schema_validation без полей/locations/input.
reason в событии также ограничен категориями EngineError/ValidationError/ValueError.

Private diagnostics/validation_repair для модели, исключения, ответы пользователю
и правила валидации не менялись. Динамические IDs, значения, evidence, позиции и
полные сообщения исключений в новые поля не попадают. Существующие счётчики items
и issue_count остаются агрегатами, без адресов позиций.

### Результат основного repair-цикла

Существующий emit используется для steel_v2_plan_result, один раз после успешного
Repository.finish. Структура:

- operation — существующий идентификатор Trace для корреляции с model_call,
  validation, queue-state и operation_complete;
- attempt — последняя попытка основного цикла handle (1 или 2, не число search/
  research вызовов);
- valid — контракт последнего основного плана разобран; известный отказ Engine
  также делает это поле false. Ошибка вне validation (например, при сохранении)
  отражается отдельно в outcome: valid не означает успешное завершение операции;
- primary_failure — первый безопасный код, не перезаписывается вторым отказом;
- repair_attempted — была ли вторая попытка этого цикла;
- outcome — request_saved, partial_fallback, failed, request_closed или
  no_request_change; status — прежний ограниченный Reply.status.

В partial_fallback valid=false означает отклонённый ответ модели; восстановленный
snapshot отдельно проверен прежним recovery/Engine. У обычного частичного
создания с корректным планом outcome=request_saved, status=partial, valid=true.
Невалидные попытки также остаются в steel_v2_validation с valid=false и тем же
primary_failure. SelectionError завершает обработку предметным вопросом без
нового GPT-вызова; его категория видна в terminal result.

Итог фиксирует сохранение заявки/reply/outbox, не внешнюю доставку. Доставку и
message_id по-прежнему подтверждает durable queue. Если Repository.finish не
подтвердил запись, успешный plan_result не публикуется; действуют прежние failure/
storage_unconfirmed пути. Cached replay возвращается до нового цикла и не
публикует повторный результат. Бесплатный /close без GPT не создаёт plan_result.

### SDK usage и размер запроса

steel_v2_model_call получает cached_tokens, только если
usage.input_tokens_details.cached_tokens — неотрицательный int (включая 0,
исключая bool). Missing/null/неверный тип пропускаются, не считаются нулём кеша.

Размер body читается только в существующем diagnostics.http_attempt, который
уже подключён из OpenAITransport.from_environment к SDK HTTP request hook.
В Trace хранится лишь число bytes, не request/body. Перед каждым _call значение
сбрасывается; metric получает request_bytes только после срабатывания hook.
Нет обходного измерения из payload/строки, нового клиента или middleware.
Потоковый непрочитанный body не читается принудительно: поле пропускается,
ошибка телеметрии не меняет API-исход. При нескольких HTTP hook-вызовах внутри
одного SDK-call это размер последнего подготовленного HTTP body, не их сумма;
счётчики http_attempts/sdk_retries остаются отдельными.

Логируются только числа. URL, headers, токены доступа, исходники, планы и body
не добавлены ни в метрики, ни в события. cached_tokens — счётчик usage, не текст
токенов. Без Trace или без подключённого hook request_bytes отсутствует.

## Синтетические примеры событий

Примеры сокращены до полей, важных для связи; числа и operation иллюстративные,
не извлечены из пользовательской заявки.

```json
{"event":"steel_v2_validation","operation":"000000000000","attempt":1,"valid":false,"reason":"EngineError","validation_stage":"request_validation","primary_failure":"groups_must_partition_all_items_once","diagnostic_codes":["groups_must_partition_all_items_once"]}
{"event":"steel_v2_plan_result","operation":"000000000000","attempt":2,"valid":true,"primary_failure":"groups_must_partition_all_items_once","repair_attempted":true,"outcome":"request_saved","status":"ok"}
{"event":"steel_v2_model_call","operation":"000000000000","call_index":2,"stage":"coordinate","request_bytes":78891,"cached_tokens":37,"duration_ms":12,"reason":null}
```

Вместо успешного восстановления теперь можно отличить:

```json
{"event":"steel_v2_plan_result","operation":"000000000000","attempt":2,"valid":false,"primary_failure":"schema_validation","repair_attempted":true,"outcome":"partial_fallback","status":"partial"}
{"event":"steel_v2_plan_result","operation":"000000000000","attempt":2,"valid":false,"primary_failure":"edit_evidence_not_in_message","repair_attempted":true,"outcome":"failed","status":"failed"}
```

Неизвестный/динамический код имеет primary_failure=request_validation, без
подстановки исходного текста. Unknown network outcome сохраняет один SDK-вызов,
repair_attempted=false и безопасную api_timeout/api_connection категорию.

## Проверки и пределы

- Адресно api_diagnostics + five_pipes_diagnostics + 021_ux: **49 passed за 7,10 с**.
- Один полный offline-current: **1092 passed за 140,04 с**, без skip/xfail.
  Было 1074 случая, добавлено 18, существующие сценарии сохранены.
- `git diff --check` и `git diff --cached --check`: успешно.

Проверены валидный одновызовный create, известный статический отказ → успешный
repair, два невалидных ответа → безопасный partial fallback/отказ, сохранённый
primary_failure, отсутствие чувствительных данных, неизвестные/динамические тексты исключений,
cached_tokens=0/37/missing/bool/negative/string, UTF-8 len(request.content),
непрочитанный stream, отсутствие stale размера и отсутствие network retry.
Реальный SDK HTTP mock/Engine/SQLite/outbox защищают исходные пять позиций,
Decimal, source evidence, одну версию, receipt, историю и idempotency.

Две старые проверки prefix-кода duplicate_characteristic обновлены на
request_validation: реальное исключение содержит динамический суффикс item ID.
Сценарии и проверки данных не ослаблены; это требуемое ужесточение логирования.
В тестах с неизвестным исключением Fake поставляет именно исключение для проверки
санитизации; это не доказательство возникновения такого EngineError в живой модели.

Из следующего журнала можно будет установить:

1. Какой **статический allowlisted** инвариант нарушен; иначе только безопасную
   категорию. Динамические ошибки по-прежнему не раскрываются до конкретного ID.
2. Был ли второй план, исправил ли он первичный отказ, сохранена ли заявка либо
   безопасный partial, или зафиксирован отказ без мутации.
3. Какие model_call относятся к этой операции, сколько байтов подготовлено
   request hook и сколько input tokens API отметил кешированными, если поле есть.
4. Была ли заявка сохранена, но затем отдельно сломалась доставка, по queue-state.

Эта работа не добавляет отдельные времена admission/Engine/SQL/delivery, не
восстанавливает старые payload и не гарантирует 30 секунд. Model evaluation,
сокращение INSTRUCTIONS и изменение reasoning/timeout сюда не входят.

## Отложенный план

- create/partial → typed clarification: сохранить корректную адресную связь,
  target-specific evidence и отдельное подтверждение пользователя после restart,
  без угадывания текста вопроса и разрешения произвольных пустых edit.
- pending → append: использовать сохранённый исходник без замены активной заявки.

Оба перехода остаются отдельными production-задачами. Контракты заявки, схема
SQLite, queue/delivery, доступ, Decimal, число разрешённых repair-попыток, модель,
reasoning, timeout, INSTRUCTIONS и approved knowledge не изменены.
