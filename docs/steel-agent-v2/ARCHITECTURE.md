# Фактическая архитектура

## Подготовлено №18: постоянный guard запуска

Внешний административно защищённый bundle и manifest проверяют release + queue
protocol + epoch до supervisor и каждого worker, независимо от handler. Используются
зафиксированные копии прежнего supervisor/locks; новая ОС-служба не добавлена.
Guard только читает SQLite. Явный offline `v2_queue_epoch.py` связывает queue metadata
с armed manifest; core schema/RequestEngine не меняются. Epoch-bound rollback CLI
дополнительно требует armed manifest до DROP. Новый start facade не имеет fallback.
Статус REQUIRES DESIGN APPROVAL: Task Scheduler/постоянная ручная точка не переключены,
production не менялся. Старые in-checkout CLI не считаются автоматически перехваченными.
Подробности — [PERSISTENT_STARTUP_GUARD.md](PERSISTENT_STARTUP_GUARD.md).

## Актуализация №16: durable queue

Начиная с №16 ветка v2 в run_bot использует `queue_runtime.run_queue`.
Один прежний ProcessLock и один poller; отдельный scheduler thread и по умолчанию
три execution threads, независимые GPT/Telegram-клиенты. Внутри user_id FIFO,
между пользователями справедливое распределение. .env/Task Scheduler/supervisor
не заменяются. Настройки `telegram-queue.json` локальные и исключены из Git.

`queue.py` добавляет четыре таблицы version=1 в отдельную v2 SQLite:
steel_v2_inbox, steel_v2_attempt, steel_v2_outbox, steel_v2_queue_meta.
Admission commit предшествует продвижению общего legacy offset; повтор update
дедуплицируется. Repo finish в одной транзакции сохраняет snapshot/version/history,
завершает claim и записывает все части ответа. Ни API-вызов, ни обычная доставка
не удерживают SQLite write transaction. Стадии/unknown/ограничения —
[CONCURRENT_QUEUE_DESIGN.md](CONCURRENT_QUEUE_DESIGN.md).

Авторизация линейризуется legacy BEGIN IMMEDIATE gate, затем v2 commit.
Генерация approved/revision связывает admission, выполнение и delivery-start:
revoke/reapprove не возобновляет старый job. Callback/admin тоже durable, фактические
уведомления журналируются; одобрение требует настоящего успеха отправки. Авария
между access commit и completion карантинируется, transition не повторяется.
Bootstrap credential `/claim` защищается DPAPI, не хранится plaintext.

Обычный обработчик `telegram.handle_update` остаётся доступен для изолированных
прямых тестов; production poller v2 больше не вызывает его синхронно.
Legacy poller не меняется. Оригиналы и все прежние версии остаются в core-таблицах;
техническая очистка source records касается только непересекающихся, нессылаемых
service whitespace. Все инварианты затем проверяются заново.

Unknown GPT/delivery не повторяются автоматически. Вопрос об append получает
clarify: новый append не реализован, создание заявки не является заменой добавления.
Технические коды validation исключены из пользовательского сообщения.
WireAgentPlan/Decimal/RequestEngine operations не расширены.

Для rollback старый v2 Repository сам блокирует дополнительную queue-схему.
Нужны offline gate, verified archive и лишь затем удаление operational таблиц
при полностью закрытой очереди. Core/history/offset не заменяются. Не переключать
handler на legacy для обхода этой проверки. Полный порядок — WINDOWS_DEPLOYMENT.md.

Следующие разделы описывают исходный синхронный путь до №16 и компоненты ядра;
в части очереди, авторизации и rollback приоритет имеет эта актуализация.

## Поток

`main.py → telegram_bot.main → ProcessLock → run_bot → TelegramAccess → dispatch`

В run_bot сохраняются прежние getUpdates, maintenance, готовность и offset в
operator.sqlite3. Отсутствующий telegram-handler.json выбирает legacy.
Явный локальный JSON `{ "handler":"v2", "database":"steel-agent-v2.sqlite3" }`
выбирает steel_agent_v2.runtime.connect и steel_agent_v2.telegram.handle_update.
Некорректный JSON/режим останавливает запуск кодом 7, без скрытого fallback.
Файл исключён из Git; в этом этапе он не создавался. Другого polling нет.

## Компоненты

- **SteelAgent (agent.py)**: один GPT-план по message, актуальному snapshot,
  recent_changes, Python totals, can_undo, bounded history и source_lines.
  Intent: create_request, edit_request, show_request, answer_question,
  web_search, free_chat, clarify; до двух попыток плана. Диагностика направляется
  на один ремонт. Транспортная ошибка не повторяется. Черновики попыток изолированы.
- **contracts.py**: Pydantic frozen/extra=forbid. AgentPlan, Extraction,
  SourceRecord, Item, CommercialGroup, Operation, RequestSnapshot, Reply.
  Контракт ограничивает операции выбранным intent; expected id/revision для edit.
- **OpenAITransport (transport.py)**: Responses.parse с WireAgentPlan → AgentPlan,
  Responses.create с web_search. По умолчанию gpt-6-luna/high; модель и reasoning
  из OPENAI_MODEL/OPENAI_REASONING_EFFORT, v2 не наследует принудительную legacy
  установку high. SDK retries=0, timeout=180 секунд на вызов, store=False.
  В Telegram runtime №5 явно создаётся gpt-6-luna/high через параметры фабрики,
  независимо от legacy env; глобальное окружение и legacy не меняются. Вызов
  фабрики без override (например CLI) сохраняет чтение env.
  Токенных и денежных квот нет. Метрики вызовов/времени/токенов — через diagnostics;
  в памяти хранятся последние 256 метрик. Содержимое сообщений в эти логи не пишется.
- **RequestEngine (engine.py)**: чистая проверка структуры, ссылок, partition,
  source evidence, Decimal, адресных patch/regroup/undo; без предметных steel-правил.
  Подробные инварианты — NORMALIZATION.md.
- **Repository (repository.py)**: отдельная явно заданная SQLite, четыре таблицы
  steel_v2_active/version/turn/inflight. Файл с чужими таблицами отклоняется.
  До GPT фиксируется исходное сообщение в inflight. finish атомарно сохраняет
  version+active+turn и удаляет claim, проверяя sequence и право доступа.
  Повтор user_id/event_id/hash возвращает сохранённый Reply без модели/операции.
- **presentation.py**: plain text, группы, исходные единицы/количества, отличия,
  вопросы; суммы считает Python. Оригинал выводится без перезаписи.
- **telegram.py**: существующие access.route/allowed, TypingIndicator, safe_chunks,
  diagnostics. Разрешены личные сообщения; проверка прав до обработки, при записи
  и между частями ответа. Стабильный event_id — telegram:update_id.

## История и совместимость

Legacy operator.sqlite3 остаётся источником прав и общего offset. Active/history
legacy не загружаются в ядро v2 и не переписываются. Factory запрещает совпадение
пути v2 с legacy; Repository дополнительно отклоняет чужую схему.
V2 active загружается только из отдельной БД. История ограничена 20 сообщениями /
12000 символами через dialog_history; слишком длинные пары могут исключаться.
Текущее сообщение/активный snapshot не обрезаются ради экономии.
Свободный диалог общий для пользователя, обсуждение архивной заявки исключается.

Отзыв права блокирует чтение/commit/доставку. После повторного одобрения того же
аккаунта v2 вновь видит его прежнюю историю: legacy context_floor/epoch не
перенесены. Промпт №3 утвердил сохранение данных и возврат контекста тому же
Telegram ID после повторного одобрения; отдельная система отзыва сейчас не нужна.

При аварии claim сохраняется. Оператор после подтверждения смерти worker и
защищённого сохранения исходника может вызвать abandon(user,event). Это не
автоматическая отмена и не повтор GPT. Telegram-ответ может доставиться повторно
после обрыва; применение операции повторно исключено сохранённым event.

## Поиск

Отдельный read-only вызов: tools=[web_search], tool_choice=required,
без max_tool_calls=1. Требуются completed web_search_call, завершённый финальный message и
безопасными HTTP(S) URL; ссылки возвращаются и сохраняются в Reply. Отсутствие
поддержки/источников даёт явный отказ. Поисковый ответ не разбирается как план
операций; последующий координатор получает историю как недоверенные данные.
Доступность на конкретном аккаунте и сопротивление модели prompt injection в
последующем диалоге требуют живой проверки; абсолютной гарантии по mock нет.

## Подготовлено, но не проверено вживую

Подключение существующего worker и CLI scripts/v2_llm_check.py подготовлены.
Windows-ветка релиза ещё не собрана на текущем production: здесь отсутствуют PR9/10.
Живые GPT, web_search и Telegram не запускались. Текущие проверки — TESTING.md.
Порядок будущего переключения — WINDOWS_DEPLOYMENT.md.

## Будущие возможности

RAG, постоянная обучаемая память/база знаний, fine-tuning, Bitrix24, Outlook/почта,
остатки, поставщики, парсеры цен и дополнительные интерфейсы заморожены.
