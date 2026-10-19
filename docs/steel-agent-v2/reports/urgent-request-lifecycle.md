# Срочное восстановление очереди и lifecycle заявок — 19.10.2026

Статус: очередь production восстановлена; исправления ядра — кандидат для review,
без merge и без deployment нового кода. Один PR, отдельная feature-ветка
fix/steel-agent-v2-request-lifecycle от origin/main9d15d966.
Полный HEAD/URL PR публикуются в итоговом ответе; этот отчёт входит в сам коммит.

## А. QUEUE RECOVERED

Перед действиями повторно подтверждены ровно:
- update9346272 execution_unknown;
- update9346273 queued того же владельца, без attempts/outbox;
- других незавершённых/неизвестных событий нет.
Пользовательские ID и переписка не извлекались в репозиторий.

Существующий stop-telegram.ps1 штатно остановил production; оба OS locks свободны,
maintenance сохранён. Новый проверенный SQLite Backup API набор:
C:\Users\usoshin\uvm_ai_operator_backups\queue_recovery_20261019_203035.
Все три базы: повторное открытие, integrity_check=ok, совпадают source/copy
counts/row/schema hashes; файловые SHA256 и manifests сохранены локально.
Там же прежний handler config и полный исходный SHA.

9346272 закрыт только штатным v2_queue_status.py --abandon с точным локальным
confirmation. Для9346273 под двумя locks доказано, что это единственный queued
кандидат; применены существующие claim(), затем deny(). Прямых SQL UPDATE/DELETE,
DROP, изменения cursor/offset, GPT replay или отправки не было. Встроенные методы
выполнили свою штатную очистку точного inflight (их не было).

Итог:9346272 rejected,9346273 denied; оба исходника архивированы в turn/inbox,
active payload неизменён, versions16; cursor=offset=9346274. Архивирование второго
события добавило одну запись истории. FIFO разблокирована.

Бот запущен на ТОМ ЖЕ SHA9d15d96634bc0c69cd795e205128f34fa5792be7.
Native supervisor10324, worker25940, один polling, v2/gpt-6-luna/high.
208секунд наблюдения: без новых ERROR/CRITICAL, Conflict409 или restart loops.
Все три SQLite целостны; unknown/inflight/pending0.
Отдельный результат QUEUE RECOVERED сообщён владельцу до начала разработки.
Task Scheduler/.env/account/модель/права не менялись.

## Б. Подтверждённые причины и патч

Живое испытание19:12–19:14: первые три coordinate завершились clarify,
без validation diagnostics. Существующий pending указывал на старый отказ,
новый список после вопроса не был закреплён как pending source.
Финальный coordinate завершился APIConnectionError/api_connection через60073мс,
без подтверждённого GPT-ответа. Причина сетевого разрыва (прокси/сервер/соединение)
не установлена: inner exception не сохранялся в безопасных логах. Нет основания
называть это ошибкой структуры CASE-002 или менять расчёты массы.
SDK max_retries=0 и запрет повторения неизвестного исхода сохранены.

Минимальные изменения:
- transport.INSTRUCTIONS: самостоятельный список — create_request из текущего
  сообщения; старый pending его не подменяет. GPT определяет намерение.
- Совместимый AgentPlan.awaiting_new_request для действительно необходимого
  вопроса о новом списке: известный завершённый clarify сохраняет прежний
  pending_source marker в turn. Подтверждение использует точный pending_event_id
  и сохранённый исходник после restart. При новом успешном create старый pending
  перестаёт быть рабочим, прежний active заменяется только при commit.
- close_request и детерминированный /close: прежняя транзакция Repository.finish
  обнуляет только active.payload, сохраняя sequence, versions и turns. Outbox
  фиксируется в той же транзакции. /close проходит authorization/idempotency,
  calls=0. Естественные фразы понимает тот же GPT с проверкой request/revision.
- После close история рабочего контекста ограничена границей закрытия; pending
  и clarification старой заявки не возвращаются. Сами данные остаются в SQLite.
- append_request: только новые товары текущего сообщения проверяются тем же
  RequestEngine.create. Новые item/record/group ID получают устойчивый namespace
  события; offsets и characteristic.source_ids переносятся в составной исходник.
  Старые items/originals/facts/groups/annotations не переписываются.
  Создаётся новая revision/undo; группы нового фрагмента добавляются отдельно,
  коммерческое объединение старых и новых позиций не навязывается.
  Display hints и mass item pointers перенесены на новые ID. Прежние grouping,
  sorting, completeness, quantity/evidence и mass validation работают без ослабления.
- Неверный append не применяет частичный fallback к действующей заявке:
  источник остаётся в истории, активные данные и версии сохраняются.

Новых SQL-таблиц/миграций, зависимостей, моделей, парсеров, второй БД, клиента,
polling или изменений theoretical_mass/quantity/normalizer не добавлено.
Approved CASE-001–006,25позиций,BR-001–011,9коэффициентов, manifest и artifact
не изменялись. Knowledge --check сохраняет версию
sha256:9a4bc0b78ede7364744706d86d1ac4d0cb907dd3a790b614d22817b2a24f4262.

## Адресный аудит legacy-маршрута

main.py → telegram_bot.main → v2 runtime/run_queue → SteelAgent/RequestEngine.
RequestEngine.totals использует quantity.normalize_quantity/sum_quantities.
quantity.py импортирует normalizer.UNIT_ALIASES; это таблица единиц и числовые
функции, а не вызов старого normalize_request/AIService или маршрутизатора.
Тест запрещает normalizer.normalize_request и проходит create/append/close.
Общие legacy-модули не удалены и не переписаны. Старый AI fallback не включён.

## В. Испытания

Новый test_steel_agent_v2_request_lifecycle.py:18детерминированных тестов:
новыйCASE002 поверх старогоactive ±pending; сохранение списка/подтверждение после
restart; пятый швеллер5т; адресное изменение только швеллера; display IDs;
двухшаговый undo; неизвестная доставка без replay; атомарный rollback при ошибке
finish_hook; /close без GPT и три естественные close-фразы с mock-планами;
free_chat без старого рабочего контекста; авторизация/пользователи; настоящий SDK
с fake HTTP; отсутствие legacy normalize_request; проверенные mass-аннотации
с прежними источниками после remap. Адресный набор:18passed.

Четыре прежних тестовых файла точечно актуализированы под явное решение владельца:
015/021/queue больше не утверждают запрет append, но продолжают требовать clarify
при неоднозначной ссылке и неизменность заявки; wire_schema учитывает новый
bool-default в исходной схеме, а прежний Wire adapter удаляет его для strict SDK.
Ни один тест не удалён/skip/xfail; проверки инвариантов сохранены.

Полная актуальная офлайн-регрессия:1025passed (прежние1007 плюс18новых).
Историческая группа:132passed,20subtests passed.
build --check, git diff --check, AST/ссылки/scope и адресная проверка секретов:
пройдены. Окончательный HEAD — в итоговом ответе и PR. Только разрешённый CASE
и синтетика; live API не вызывались.

## Изменённые файлы и ограничения

Код: agent.py, contracts.py, engine.py, repository.py, transport.py.
Тесты: новый request_lifecycle; существующие015_reproduction,021_ux,queue,wire_schema.
Документы: DECISIONS,PRODUCT,PROJECT_STATE и этот отчёт.

Интенты create/append/close и связь подтверждения с исходником в тестах заданы:
mock и SDK schema не доказывают качество реальной GPT.
Append получает новые позиции из текущего сообщения; неоднозначную ссылку на
старые списки модель уточняет, не копируя произвольную старую заявку.
При close данные архивируются, но команда reopen не добавлена.
История оригинала append — точный прежний исходник + перевод строки + новый
исходник, с сохранением полного журнала версий; undo возвращает прежний snapshot,
добавление остаётся доступным в истории и версии.

Окончательный пользовательский критерий ещё НЕ достигнут: после отдельного
review/merge/разрешённого Windows-выпуска владелец проверит в Telegram:
CASE002 при старом active/pending → самостоятельная новая заявка;
подтверждение сохранённого списка; «Добавь швеллер20ПСт3—5т»;
адресная правка; /close; обычный чат; сохранность количества и undo.
До этого новая функциональность не объявляется принятой в production.
Текущий production оставлен работающим на9d15d966; дальнейшие функции не начаты.
