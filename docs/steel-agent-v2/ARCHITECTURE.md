# Фактическая архитектура

## Дополнение этапа 6 — расчётная масса (кандидат)

MassSpec от прежнего GPT ссылается на текущие характеристики; он не содержит
арифметического результата. theoretical_mass.py выбирает запись MassCatalog,
проверяет единицы/применимость, рассчитывает Decimal. Производные TheoreticalMass
хранятся в прежнем JSON snapshot и версиях вместе с ID/методом/источником/редакцией.
Нет SQL-миграций, другой БД, старого нормализатора или новых сетевых клиентов.
mass_reference.json — малый read-only seed с тремя узко применимыми основаниями;
будущий адаптер SQLite Reference должен возвращать тот же MassReference с точными
строками чисел и provenance, не включать прежний AI reader целиком.
Длина длинномера и схема резки проверяются одним piece_length перед обоими
штучными методами; кг/шт требует совпадающей справочной длины. user/uvm_default
сохраняются отдельно от исходника; чтение старого небезопасного snapshot не
показывает массу как проверенную. owner_indicative с approval_scope отличается
от observed и от reviewed. Полный knowledge-каталог к runtime не подключён.

Для нового интернет-коэффициента используется существующий transport.search,
затем один ограниченный вызов того же coordinator для извлечения evidence.
Оба проходят прежний AttemptTransport; неизвестный исход не повторяется.
PendingMass хранится в существующем Reply/turn, связан с request/revision/user.
После явного подтверждения выбираются конкретные кандидаты, engine считает и
фиксирует версию/историю/ответ/outbox одной прежней транзакцией. До подтверждения
мутации нет. Известным коэффициентам новый поиск не нужен. Очередь/typing/
polling/Windows runtime не изменены. Подробности — [отчёт](reports/stage-6-theoretical-mass.md).

Сверено с `4f7babf3e9aebee76e582737c2fe31680a1fa528` в №23, 18.10.2026.
Продукт и целевые изменения — [PRODUCT.md](PRODUCT.md), живые подтверждения
и ограничения — [PROJECT_STATE.md](PROJECT_STATE.md). Будущие интеграции ниже
не следует принимать за существующий код.

## Решение владельца №19: forward-only

> Steel Agent v2 развивается по стратегии forward-only. Откат на устаревший код не является поддерживаемым штатным сценарием. Сохранность данных, очереди и восстановление после аварий остаются обязательными.

Штатные точки запуска — существующие Task Scheduler Action, windows_supervisor.py
и start-telegram.ps1. Внешний guard, ProgramData, manifest/epoch и административная
установка исключены. start-telegram.ps1 восстановлен в точности из production aa11380;
stop/supervisor/ProcessLock не меняются. Старые документы №15–18 остаются историей,
включая PERSISTENT_STARTUP_GUARD.md; их процедуры rollback/retirement не применять.

Единственный ИИ-мозг Telegram — Steel Agent v2, GPT-6 Luna High. В telegram_bot
полностью отсутствуют сборка AIService/WorkAssistantService/TelegramService и старый
синхронный intent dispatch. Единственный рабочий путь — runtime.connect → run_queue.
selection() принимает только явный handler=v2: отсутствие или legacy config
завершает startup до обращения к Telegram, без fallback и переключателей режима.
Старый адаптер перенесён в legacy_telegram_adapter.py исключительно для сравнения;
ни main, ни telegram_bot, ни v2 его не импортируют. Старые парсеры/БД сохраняются.
Данные и известная queue schema проверяются внутри Repository/DurableQueue, без
внешнего SHA allowlist. Подтверждение опубликованного полного SHA — этап установки.
Unknown закрывает обработку только затронутого пользователя, не очищается и не
повторяется автоматически. Неподдерживаемая схема/повреждение/cursor ahead — отказ.

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
Legacy poller не входит в активный маршрут. Оригиналы и версии остаются в core-таблицах;
техническая очистка source records касается только непересекающихся, нессылаемых
service whitespace. Все инварианты затем проверяются заново.

Unknown GPT/delivery не повторяются автоматически. Вопрос об append получает
clarify: новый append не реализован, создание заявки не является заменой добавления.
Технические коды validation исключены из пользовательского сообщения.
WireAgentPlan/Decimal/RequestEngine operations не расширены.

Удаление operational queue-таблиц больше не является операцией эксплуатации.
Диагностика и явное offline разрешение unknown сохранены; --check-forward проверяет
схему и направление cursor/offset, а не разрешение downgrade. Новые совместимые
изменения добавляются миграциями вперёд. При сбое — maintenance, backup, диагностика,
следующий совместимый коммит. Подробнее — WINDOWS_DEPLOYMENT.md.

Стадии и восстановление уточняет CONCURRENT_QUEUE_DESIGN.md; старые синхронные
эксплуатационные сценарии сохраняются только в исторических отчётах.

## Поток

`main.py → telegram_bot.main → ProcessLock → run_bot → runtime.selection/connect → queue_runtime.run_queue`

Run_queue выполняет единственный getUpdates; maintenance и готовность остаются
в прежнем lifecycle, общий offset — в operator.sqlite3. Живой запуск требует
telegram-handler.json с handler=v2; fallback отсутствует.
Явный локальный JSON `{ "handler":"v2", "database":"steel-agent-v2.sqlite3" }`
выбирает отдельную БД v2; queue Executor вызывает SteelAgent. Возвращаемый фабрикой
прямой telegram.handle_update используется в изолированных тестах, не production dispatch.
Некорректный JSON/режим останавливает запуск кодом 7, без скрытого fallback.
Файл исключён из Git; текущий deployment сохраняет существующий config. Другого polling нет.

## Компоненты

- **SteelAgent (agent.py)**: один GPT-план по message, актуальному snapshot,
  recent_changes, Python totals, can_undo, bounded history и source_lines.
  Intent: create_request, edit_request, show_request, answer_question,
  web_search, free_chat, clarify; до двух попыток плана. Диагностика направляется
  на один ремонт. Транспортная ошибка не повторяется. Черновики попыток изолированы.
- **contracts.py**: Pydantic frozen/extra=forbid. AgentPlan, Extraction,
  SourceRecord, Item, CommercialGroup, Operation, RequestSnapshot, Reply.
  Контракт ограничивает операции выбранным intent; expected id/revision для edit.
- **OpenAITransport (transport.py)**: кандидат №28 — Responses.create со strict WireAgentPlan
  schema, затем WireAgentPlan → AgentPlan; в production пока Responses.parse.
  Web Search — Responses.create с web_search. По умолчанию gpt-6-luna/high; модель и reasoning
  из OPENAI_MODEL/OPENAI_REASONING_EFFORT только при вызове фабрики без override.
  SDK retries=0, timeout=180 секунд на вызов, store=False.
  В Telegram runtime №5 явно создаётся gpt-6-luna/high через параметры фабрики,
  независимо от legacy env; глобальное окружение не меняется. Queue Executor
  создаёт отдельный transport с теми же явными параметрами. Вызов
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
- **presentation.py**: кандидат №27 — краткие plain-text строки с полными фактами
  каждого участника группы и исходными единицами/количествами; неизвестные/вопросы
  видимы. Групповой разбор — explanation, суммы Python — totals, оригинал без перезаписи.
  В production пока прежняя версия formatter; deployment не выполнялся.
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

При аварии действует durable recovery: неизвестный исход GPT/доставки помещается
в unknown, автоматически не повторяется. Продолжаются лишь доказанно не начатые
операции/части доставки. Ручное закрытие — через документированную offline CLI
процедуру с backup, maintenance, обоими locks и точным подтверждением. Прямой
старый abandon(user,event) не является процедурой эксплуатации очереди.

## Поиск

Отдельный read-only вызов: tools=[web_search], tool_choice=required,
без max_tool_calls=1. Требуются completed web_search_call, завершённый финальный message и
безопасными HTTP(S) URL; ссылки возвращаются и сохраняются в Reply. Отсутствие
поддержки/источников даёт явный отказ. Поисковый ответ не разбирается как план
операций; последующий координатор получает историю как недоверенные данные.
Доступность на конкретном аккаунте и сопротивление модели prompt injection в
последующем диалоге требуют живой проверки; абсолютной гарантии по mock нет.

## Проверка и текущая готовность

Очередь развёрнута №20, патч typing №22; конкретные живые результаты и ограничения
перечислены в PROJECT_STATE.md. Typing подтверждён кодом/офлайн; его работа в Telegram
подтверждена владельцем в задании №26. RequestEngine/контракты не доказывают профессиональной полноты
характеристик. Проверки — TESTING.md, будущие отдельно разрешённые обновления —
WINDOWS_DEPLOYMENT.md. Нужды возвращать PR9/10 или старый ИИ в текущий путь нет.

## Будущие возможности

Документы, остатки и поставщики включены в целевой план №23, но не подключены:
[ROADMAP.md](ROADMAP.md), [PROCUREMENT_PIPELINE.md](PROCUREMENT_PIPELINE.md).
Bitrix24/почта — позднее. RAG, fine-tuning и постоянная обучаемая память не являются
утверждёнными обязательными технологиями. База знаний проекта в GitHub — документация,
а не новая runtime-память GPT. Второй AI pipeline без отдельного решения не создаётся.

№26 добавляет только [документационную базу знаний](knowledge/README.md).
Её автоматической загрузки в Telegram пока нет. Ближайшая функциональная
интеграция первой группы должна использовать то же ядро и версионированный
approved-пакет без второго AI pipeline, новой БД и сетевого GitHub на каждую заявку.

## Кандидат №28: bounded repair и partial recovery

Coordinate использует тот же SDK strict JSON Schema WireAgentPlan через
Responses.create; завершённый final_answer (или отсутствие phase по совместимому
контракту SDK) валидируется локально WireAgentPlan → AgentPlan → RequestEngine.
Commentary не является планом. Это позволяет сохранить отклонённое предложение
**только в приватной памяти ремонта**, без payload в логах. Максимум два API-вызова
координации; refusal/incomplete/HTTP/timeout не повторяются этим механизмом.

recovery.py — проверка/реконструкция частичных subplans, не второй нормализатор.
Выбранные позиции валидируются независимо, затем вместе с unresolved по полному
исходнику. Совмещения разных предложений в один результат нет. Новая SQLite не нужна:
исходник/метки pending/неактивный проверенный снимок хранятся в существующем turn/Reply.
Reply.pending_snapshot исключается из JSON при None: старые receipt остаются совместимы.
DDL и queue schema=1 не меняются. Finish/outbox используют прежнюю одну транзакцию.

Контракт AgentPlan дополнен pending_event_id для явно выбранного сохранённого
исходника; Operation — resolve.resolution и set.replace_from. Подробная семантика
в NORMALIZATION. Работающий production не обновлён, зависимость от formatter №27 сохранена.
