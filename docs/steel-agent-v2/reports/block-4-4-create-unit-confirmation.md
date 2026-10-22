# Блок 4.4 — единица, предложенная при создании, и согласие после restart

База main: `aa777dd2f9676ba789baa91817525bb3b80e4e8a`, после разрешённого merge
PR #34 с проверенным HEAD `017866b42bc1fff1a35fc9e055c5ef4c70603949`.
Кандидат — fix/v2-create-unit-confirmation. Новый PR без merge/deployment.

## Изменение контракта

- UnitProposal содержит только target_id/unit/question. Нет source evidence и
  нет списка альтернативных targets. Unit — т/кг/г/м/мм/см/шт, существующие
  канонические единицы измеряемых операций.
- WireCreatePlan и AgentPlan допускают один необязательный unit_proposal только
  для create_request. Старое clarification по-прежнему принадлежит только clarify;
  произвольные вопросы/характеристики/количества при create не разрешены.
- PendingClarification.proposal теперь может содержать старый Clarification или
  UnitProposal. Старые receipts и отсутствие новых optional полей сериализуются
  прежним образом; новых таблиц/DDL нет.
- Отдельный kind=confirm_unit разрешён только как единственный referenced edit,
  без remaining_questions/mass/search/display и дополнительных операций.

Пример синтетического create-предложения:

```json
{"unit_proposal":{"target_id":"i2","unit":"т","question":"Подтвердите единицу второй позиции: т?"}}
```

Это не подтверждённый факт. Agent проверяет единственный существующий target,
неизвестные current/original unit, unit_text и unit_source_id. При необходимости
добавляет ровно связанный вопрос в questions; совпадающий вопрос не дублирует,
несколько одинаковых вопросов отклоняет. Известная единица не стирается ради
предложения. Все пять items и Decimal сохраняются до получения ответа.

## Согласие и source evidence

После reopen GPT получает тот же typed proposal, event, request ID и revision
из сохранённого Reply. Для явного согласия он выбирает edit_request с точными
expected_request_id/revision, clarification_event_id и одной операцией:

```json
{"kind":"confirm_unit","target_id":"i2","value":"т","evidence":"да"}
```

Неиспользуемые поля полной SDK Operation равны null/пустым спискам.
Значение «т» берётся из сохранённого предложения, но **не считается evidence
исходной строки**. Evidence этой операции — буквальная цитата всего текущего
пользовательского ответа (с допустимыми внешними пробелами). Python не парсит
русское «да»: семантический выбор accept/clarify/другой темы остаётся GPT.

Trusted unit_consent передаёт в Engine только Agent после чтения соответствующего
receipt из Repository; это не поле, которое модель может прислать в AgentPlan.
Engine независимо сверяет event/request/revision, точный target/value, неизвестную
unit и существование ровно одного связанного вопроса. Меняется только unit одного
item, удаляется только proposal.question. Обычные set/evidence проверки не менялись.

Change содержит field=unit, before=null, after=т, evidence=да, message=да и
confirmation_event_id. Новое optional поле отсутствует в старых Change JSON.
Оригинальная строка и originals не меняются, unit_text/unit_source_id не получают
ложную цитату или единицу другой позиции. Журнал телеметрии этих данных не пишет.

## Persistence и отзыв связи

Repository.finish не изменён: прежняя транзакция сохраняет snapshot/version,
turn.reply с PendingClarification и outbox. Именно Reply хранит binding, поэтому
очистка payload доставленного outbox и новое создание Database/Repository/Queue
его не теряют. Никакого восстановления смысла из строки вопроса или памяти нет.

Repository.clarification проверяет пользователя и текущие request/revision, читая
последний turn. Agent и Engine дополнительно требуют точного event. При успешной
операции новая revision/Reply уже не несут старый binding. Undo восстанавливает
unknown unit и questions из версии до согласия, но не возвращает право применить
старое согласие в новой revision. Другая заявка/пользователь/версия и неподходящий
ответ либо отказ не разрешают мутацию. После неуспешного typed подтверждения
binding истекает; это не ослабляет прежнее поведение старых Clarification.

Если GPT считает ответ неоднозначным, он выдаёт clarify без операций/нового
generic proposal: snapshot не меняется, сохранённые target/unit/event остаются
для следующего явного ответа. Произвольный extra set, смена value/target,
заимствование «т» как evidence вместо «да», частичная цитата ответа, overrides
questions и ссылки на чужие события отклоняются без частичной записи.

## Число GPT-вызовов на update

| Шаг | Вызовы |
|---|---:|
| Обычный корректный create пяти труб, включая готовое «т т» → «т» | 1 |
| Create пяти труб с неизвестной единицей и typed unit proposal | 1 |
| «да» с правильным confirm_unit после reopen | 1 |
| Неоднозначный ответ с clarify | 1, без версии |
| Undo | 1, отдельная новая версия |
| Заведомо неверные планы в негативных сценариях | 2, прежний repair budget |
| Повтор доставленного Telegram update | 0 новых вызовов/отправок/версий |

Создание и подтверждение — два разных пользовательских сообщения, не два
обязательных вызова ради создания. После обычного create лишний вопрос не возникает.

## Проверки

Использован существующий test_steel_agent_v2_five_pipes_diagnostics.py: реальный
SDK HTTP mock, WireCoordinatorPlan/AgentPlan, Engine, SQLite, queue, outbox и fake
Telegram receipts. Подменяются только внешние OpenAI/Telegram ответы.

- Create/partial → compact доставленного outbox → reopen → «да», с неизвестной unit
  до согласия, изменением только i2, неизменными остальными четырьмя и источником.
- С чужим нерешённым вопросом: снимается только unit question, статус остаётся partial.
- Чужой target, value, event, request, revision; generic set вместо confirm_unit;
  extra operation, borrowed evidence, questions override, missing ref, selection
  вместо прямого ID и неполная цитата ответа — отсутствие мутации/новой версии.
- Чужой пользователь без active и со своим active/такими же i2/unit: чужой event
  не разрешает запись даже при правильных собственных request ID/revision.
- Неоднозначный ответ, другая тема/отказ, замена заявки, новая версия и undo;
  replay, одна версия на успешное согласие, сохранность Decimal/records/originals,
  история, delivery receipt и integrity_check.
- Несуществующий/известный target, ложный unit_text, duplicate question,
  неподдерживаемая unit и список вместо одного proposal не заменяют прежний active.
- Старые generic create+clarification ответы по-прежнему отвергаются; строковый
  вопрос без явного unit_proposal не превращается в binding догадкой Python.

Адресно create/wire/031/simple_create: **112 passed за 22,34 с**.
Адресно lifecycle/semantic_edits/api_diagnostics/five_pipes: **120 passed за 20,18 с**.
Дополнительная адресная проверка compact/reopen после вопроса: **2 passed за 2,72 с**.
Первый полный прогон: **1117 passed, 1 failed за 141,31 с**. Единственная ошибка —
старый queue-тест сравнивал весь supported_edit_operations без нового confirm_unit.
В ожидаемый список добавлена только новая операция; остальные assertions о
сохранности заявки/доставке не изменены. Адресная перепроверка этого сценария:
**1 passed за 1,15 с**.
Повторный полный: **1116 passed, 2 failed за 138,88 с**. Оба падения — точная
сверка approved artifact: добавленный раздел NORMALIZATION.md изменил только
хеш источника в build(), а не BR/CASE/коэффициенты. NORMALIZATION.md восстановлен
без изменений; контракт описан в отдельном UNIT_CONFIRMATION.md и связан с
DECISIONS/PROJECT_STATE. Artifact/manifest и проверки не менялись. Адресно обе
проверки: **2 passed за 1,09 с**, build() целиком равен исходному artifact.
Повторные полные прогоны понадобились только для этих найденных несовпадений;
production-код после первого полного прогона не менялся.
Финальный полный offline-current: **1118 passed за 120,99 с**, без skip/xfail.
Было 1092 случая, добавлено 26; прежние сценарии сохранены.
`git diff --check` и `git diff --cached --check`: успешно.

Mock заранее задаёт target, предложение и смысл ответа. Эти проверки доказывают
исполнение и сохранность, но не качество понимания русского языка настоящей GPT,
её выбор нового поля/операции или длительность живого API. Новая схема/инструкция
потребуют отдельно разрешённой живой проверки. Случай строкового вопроса без
typed proposal остаётся ограничением, а не скрыто объявленным исправлением.

BR-007, Decimal-арифметика и исходное source evidence, доступ, queue/delivery,
модель, reasoning, timeout, repair budget и approved knowledge не изменялись.
Установленный production не трогался; deployment, платных API и субагентов нет.
Pending → append остаётся отложенной задачей. Общий движок вопросов не добавлен.
