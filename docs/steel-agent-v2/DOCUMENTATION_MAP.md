# Карта документации Steel Agent v2

18.10.2026, аудит №24. Редакция для review владельца; карта не утверждает
предлагаемые бизнес-правила и не разрешает deployment. Исторические названия
«правила», «архитектура», «release» и даты не дают документу приоритета.

## Порядок чтения новых чатов и Codex

1. Корневой AGENTS и AGENTS v2; затем PRODUCT, DECISIONS, PROJECT_STATE.
2. Для предметной задачи — NORMALIZATION, NORMALIZATION_CASES, PROCUREMENT_PIPELINE.
3. Для плана — ROADMAP; для кода/проверок/эксплуатации — ARCHITECTURE, TESTING,
   CONCURRENT_QUEUE_DESIGN, STRUCTURED_OUTPUTS и WINDOWS_DEPLOYMENT по области задачи.
4. Исторические источники и отчёты читать как основания/свидетельства, сверяя
   конфликты C01–C13 в DECISIONS. Новое явное решение владельца имеет приоритет;
   неразрешённое противоречие обозначить и не превращать в правило самостоятельно.

**Единственный актуальный набор продуктовых требований — docs/steel-agent-v2.**
«Действующий» означает текущий документ с отмеченными уровнями утверждения,
не автоматическое одобрение всей редакции №23/24. Справочный текст описывает
актив/наблюдение старого поколения; его алгоритмы и числа не включаются в v2.
Старые инструкции запуска/отката не исполнять, production не обновлять по main.

## Зафиксированные деревья и полный учёт

База №23: `fce8d58e1bcbeb01c6a6a33078f5624cc200cd8e`; origin/main после fetch:
`5e94aa4932f18c298a41685a4fa81c64c466ef7d`. Сравнение выполнено git ls-files / git ls-tree / diff.

| Снимок | Все Markdown | Корень | docs/ | tmp/ |
|---|---:|---:|---:|---:|
| №23 | 61 | 5 | 54 | 2 |
| origin/main | 27 | 4 | 21 | 2 |

Общих путей 25: 22 одинаковых blob, 3 разных; только main — 2, только №23 — 36.
Различаются ARCHITECTURE.md, DECISIONS.md (исторические пометки №23) и
docs/NORMALIZATION_STAGE_ONE.md (в main есть поздняя ссылка на beta-процедуру
вместо отдельного тестового бота). Это не основание переносить beta-бюджеты.
Объединение перечней содержит 63 пути. Два новых документа №24 добавляют
в docs-ветку карту и отчёт: итоговое дерево №24 содержит 63 Markdown.

Ниже перечислены все пути обоих исходных деревьев. «Обе» означает присутствие
пути в обоих закреплённых снимках, а не равенство текущих веток/реализаций.

| Путь | Назначение | Статус / преемник v2 | Источник |
|---|---|---|---|
| [AGENTS.md](../../AGENTS.md) | Входная инструкция Codex | действующий; [карта](DOCUMENTATION_MAP.md) | только №23 |
| [ARCHITECTURE.md](../../ARCHITECTURE.md) | Прежние AIService, запуск, контекст, Low/Medium | исторический; [ARCHITECTURE.md](ARCHITECTURE.md) | обе |
| [DECISIONS.md](../../DECISIONS.md) | Корневые ADR, включая старый scope справочника | исторический; [DECISIONS.md](DECISIONS.md) | обе |
| [PLAN.md](../../PLAN.md) | Исходная закупочная цель и старый поэтапный план | исторический; [PRODUCT.md](PRODUCT.md) | обе |
| [TASKS.md](../../TASKS.md) | Старый backlog и результаты 13 октября | исторический; [ROADMAP.md](ROADMAP.md) | обе |
| [docs/COMMERCIAL_NORMALIZATION.md](../COMMERCIAL_NORMALIZATION.md) | Прежние таблицы групп сталей и разовые решения | исторический; требует решения C02; [NORMALIZATION.md](NORMALIZATION.md) | обе |
| [docs/DIALOG_CONTEXT_114_1.md](../DIALOG_CONTEXT_114_1.md) | Контекст, уточнения и отдельные расчёты старого поколения | исторический; [NORMALIZATION.md](NORMALIZATION.md) | обе |
| [docs/DOMAIN_RULES.md](../DOMAIN_RULES.md) | Сохранение признаков; старые консолидация/вывод типа/масса | исторический; требует решения C01–C04/C13; [NORMALIZATION.md](NORMALIZATION.md) | обе |
| [docs/INVENTORY_FILE_AUDIT.md](../INVENTORY_FILE_AUDIT.md) | Свидетельство конкретного XLSB-среза, не новый аудит файла | справочный; [PROCUREMENT_PIPELINE.md](PROCUREMENT_PIPELINE.md) | обе |
| [docs/INVENTORY_IMPORT_SPEC.md](../INVENTORY_IMPORT_SPEC.md) | Смысл J/N/B и открытые правила импорта, не готовый адаптер | справочный; требует решения C12; [PROCUREMENT_PIPELINE.md](PROCUREMENT_PIPELINE.md) | обе |
| [docs/METAL_REFERENCE.md](../METAL_REFERENCE.md) | Сортамент/provenance/reference vs market; старый scope 32 | справочный; требует решения C04/C05; [PROCUREMENT_PIPELINE.md](PROCUREMENT_PIPELINE.md) | обе |
| [docs/METAL_REFERENCE_LOOKUP.md](../METAL_REFERENCE_LOOKUP.md) | Контракт прежнего read-only reader и ограничения кандидатов | справочный; [PROCUREMENT_PIPELINE.md](PROCUREMENT_PIPELINE.md) | обе |
| [docs/NOMENCLATURE_CONTRACT.md](../NOMENCLATURE_CONTRACT.md) | Прежний контракт атрибутов/происхождения/контекста | справочный; [NORMALIZATION.md](NORMALIZATION.md) | обе |
| [docs/NORMALIZATION_STAGE_ONE.md](../NORMALIZATION_STAGE_ONE.md) | Старые GradeRule, расчёты, проекты Bitrix/почты | исторический; требует решения C02/C04/C11; [DECISIONS.md](DECISIONS.md) | обе |
| [docs/P0_NORMALIZER_HANDOFF.md](../P0_NORMALIZER_HANDOFF.md) | Старые P0 проверки, команды релиза и калькуляторный приоритет | исторический; [NORMALIZATION_CASES.md](NORMALIZATION_CASES.md) | обе |
| [docs/PROMPTING.md](../PROMPTING.md) | Процесс постановки задач; прежний приоритет PLAN/AIService | исторический; [AGENTS.md](AGENTS.md) | обе |
| [docs/REAL_TELEGRAM_114_1_4.md](../REAL_TELEGRAM_114_1_4.md) | Свидетельства старых отказов, условий трубы/фитинга и массы | исторический; требует решения C04; [NORMALIZATION_CASES.md](NORMALIZATION_CASES.md) | обе |
| [docs/RECOGNITION_SERVICE.md](../RECOGNITION_SERVICE.md) | Старый локальный распознаватель, не v2-маршрут | справочный; [NORMALIZATION.md](NORMALIZATION.md) | обе |
| [docs/RELEASE_106.md](../RELEASE_106.md) | Старый состав релиза, тесты и отменённый откат | исторический; [WINDOWS_DEPLOYMENT.md](WINDOWS_DEPLOYMENT.md) | обе |
| [docs/SESSION_2026-10-13.md](../SESSION_2026-10-13.md) | Наблюдения прежней сессии, не состояние v2 | исторический; [PROJECT_STATE.md](PROJECT_STATE.md) | обе |
| [docs/STEEL_AGENT_V2_ARCHITECTURE.md](../STEEL_AGENT_V2_ARCHITECTURE.md) | Указатель на v2 | справочный; [PRODUCT.md](PRODUCT.md) | только №23 |
| [docs/STEEL_AGENT_V2_VALIDATION.md](../STEEL_AGENT_V2_VALIDATION.md) | Указатель на v2 | справочный; [TESTING.md](TESTING.md) | только №23 |
| [docs/TECHNICAL_MATCHING.md](../TECHNICAL_MATCHING.md) | Прежние статусы сравнения и недостаточности данных | справочный; [PROCUREMENT_PIPELINE.md](PROCUREMENT_PIPELINE.md) | обе |
| [docs/TELEGRAM_DIALOG_CONTEXT_REVIEW.md](../TELEGRAM_DIALOG_CONTEXT_REVIEW.md) | Исторический review: адресность, прежний undo/access | исторический; [PROJECT_STATE.md](PROJECT_STATE.md) | обе |
| [docs/TELEGRAM_WORK_ASSISTANT.md](../TELEGRAM_WORK_ASSISTANT.md) | Прежний router, помощник, лимиты и partial fallback | исторический; [ARCHITECTURE.md](ARCHITECTURE.md) | обе |
| [docs/WINDOWS_OPERATIONS.md](../WINDOWS_OPERATIONS.md) | Справка прежнего supervisor/Task; не current queue runbook | исторический; [WINDOWS_DEPLOYMENT.md](WINDOWS_DEPLOYMENT.md) | обе |
| [docs/steel-agent-v2/AGENTS.md](AGENTS.md) | Обязательные правила агента и порядок чтения | действующий (с уровнями утверждения); — | только №23 |
| [docs/steel-agent-v2/ARCHITECTURE.md](ARCHITECTURE.md) | Действующий v2-маршрут и ответственность компонентов | действующий (с уровнями утверждения); — | только №23 |
| [docs/steel-agent-v2/CONCURRENT_QUEUE_DESIGN.md](CONCURRENT_QUEUE_DESIGN.md) | Durable queue, FIFO/offset/unknown/остановка | действующий (с уровнями утверждения); — | только №23 |
| [docs/steel-agent-v2/DECISIONS.md](DECISIONS.md) | Решения владельца, C01–C13 и открытые вопросы | действующий (с уровнями утверждения); — | только №23 |
| [docs/steel-agent-v2/NORMALIZATION.md](NORMALIZATION.md) | Предметные требования, текущие инварианты и границы | действующий (с уровнями утверждения); — | только №23 |
| [docs/steel-agent-v2/NORMALIZATION_CASES.md](NORMALIZATION_CASES.md) | Разрешённые примеры и статус предметной приёмки | действующий (с уровнями утверждения); — | только №23 |
| [docs/steel-agent-v2/PERSISTENT_STARTUP_GUARD.md](PERSISTENT_STARTUP_GUARD.md) | Отменённый проект №18 | исторический; [DECISIONS](DECISIONS.md), [Windows](WINDOWS_DEPLOYMENT.md) | только №23 |
| [docs/steel-agent-v2/PROCUREMENT_PIPELINE.md](PROCUREMENT_PIPELINE.md) | Целевые источники, matching, остатки/цены | действующий (с уровнями утверждения); — | только №23 |
| [docs/steel-agent-v2/PRODUCT.md](PRODUCT.md) | Назначение и утверждённые продуктовые цели | действующий (с уровнями утверждения); — | только №23 |
| [docs/steel-agent-v2/PROJECT_STATE.md](PROJECT_STATE.md) | Фактическое состояние и пределы доказательств | действующий (с уровнями утверждения); — | только №23 |
| [docs/steel-agent-v2/ROADMAP.md](ROADMAP.md) | Цель 14 дней и 12-недельная последовательность | действующий (с уровнями утверждения); — | только №23 |
| [docs/steel-agent-v2/STRUCTURED_OUTPUTS.md](STRUCTURED_OUTPUTS.md) | Документированный SDK wire-контракт и границы проверки | действующий (с уровнями утверждения); — | только №23 |
| [docs/steel-agent-v2/TELEGRAM_PRESENTATION.md](TELEGRAM_PRESENTATION.md) | Существующий formatter/search, не целевой новый UX | действующий (с уровнями утверждения); — | только №23 |
| [docs/steel-agent-v2/TESTING.md](TESTING.md) | Области регрессии, mock/live и текущая база | действующий (с уровнями утверждения); — | только №23 |
| [docs/steel-agent-v2/WINDOWS_DEPLOYMENT.md](WINDOWS_DEPLOYMENT.md) | Единственный актуальный forward-only runbook | действующий (с уровнями утверждения); — | только №23 |
| [docs/steel-agent-v2/plans/001-implementation.md](plans/001-implementation.md) | План начального этапа | исторический; [ROADMAP](ROADMAP.md) | только №23 |
| [docs/steel-agent-v2/plans/002-prelaunch.md](plans/002-prelaunch.md) | План начального этапа | исторический; [ROADMAP](ROADMAP.md) | только №23 |
| [docs/steel-agent-v2/reports/001-implementation.md](reports/001-implementation.md) | Свидетельство этапа 001 | исторический; [PROJECT_STATE](PROJECT_STATE.md) | только №23 |
| [docs/steel-agent-v2/reports/002-prelaunch.md](reports/002-prelaunch.md) | Свидетельство этапа 002 | исторический; [PROJECT_STATE](PROJECT_STATE.md) | только №23 |
| [docs/steel-agent-v2/reports/003-publication.md](reports/003-publication.md) | Свидетельство этапа 003 | исторический; [PROJECT_STATE](PROJECT_STATE.md) | только №23 |
| [docs/steel-agent-v2/reports/004-windows-preparation.md](reports/004-windows-preparation.md) | Свидетельство этапа 004 | исторический; [PROJECT_STATE](PROJECT_STATE.md) | только №23 |
| [docs/steel-agent-v2/reports/005-windows-release.md](reports/005-windows-release.md) | Свидетельство этапа 005 | исторический; [PROJECT_STATE](PROJECT_STATE.md) | только №23 |
| [docs/steel-agent-v2/reports/007-first-live-failure.md](reports/007-first-live-failure.md) | Свидетельство этапа 007 | исторический; [PROJECT_STATE](PROJECT_STATE.md) | только №23 |
| [docs/steel-agent-v2/reports/009-structured-outputs-schema.md](reports/009-structured-outputs-schema.md) | Свидетельство этапа 009 | исторический; [PROJECT_STATE](PROJECT_STATE.md) | только №23 |
| [docs/steel-agent-v2/reports/011-telegram-ux-and-search.md](reports/011-telegram-ux-and-search.md) | Свидетельство этапа 011 | исторический; [PROJECT_STATE](PROJECT_STATE.md) | только №23 |
| [docs/steel-agent-v2/reports/013-web-search-runtime.md](reports/013-web-search-runtime.md) | Свидетельство этапа 013 | исторический; [PROJECT_STATE](PROJECT_STATE.md) | только №23 |
| [docs/steel-agent-v2/reports/015-concurrency-and-request-recovery.md](reports/015-concurrency-and-request-recovery.md) | Свидетельство этапа 015 | исторический; [PROJECT_STATE](PROJECT_STATE.md) | только №23 |
| [docs/steel-agent-v2/reports/016-concurrent-queue-implementation.md](reports/016-concurrent-queue-implementation.md) | Свидетельство этапа 016 | исторический; [PROJECT_STATE](PROJECT_STATE.md) | только №23 |
| [docs/steel-agent-v2/reports/017-queue-predeployment-validation.md](reports/017-queue-predeployment-validation.md) | Свидетельство этапа 017 | исторический; [PROJECT_STATE](PROJECT_STATE.md) | только №23 |
| [docs/steel-agent-v2/reports/018-persistent-startup-guard.md](reports/018-persistent-startup-guard.md) | Свидетельство этапа 018 | исторический; [PROJECT_STATE](PROJECT_STATE.md) | только №23 |
| [docs/steel-agent-v2/reports/019-forward-only-deployment.md](reports/019-forward-only-deployment.md) | Свидетельство этапа 019 | исторический; [PROJECT_STATE](PROJECT_STATE.md) | только №23 |
| [docs/steel-agent-v2/reports/021-live-test-and-typing.md](reports/021-live-test-and-typing.md) | Свидетельство этапа 021 | исторический; [PROJECT_STATE](PROJECT_STATE.md) | только №23 |
| [docs/steel-agent-v2/reports/023-product-foundation.md](reports/023-product-foundation.md) | Свидетельство этапа 023 | исторический; [PROJECT_STATE](PROJECT_STATE.md) | только №23 |
| [tmp/23met_research/23met_reference_extraction_strategy.md](../../tmp/23met_research/23met_reference_extraction_strategy.md) | Исследовательский проект извлечения локальных HTML, не scraper | справочный; [PROCUREMENT_PIPELINE.md](PROCUREMENT_PIPELINE.md) | обе |
| [tmp/23met_research/23met_reference_findings.md](../../tmp/23met_research/23met_reference_findings.md) | Наблюдения снимков 23MET, не норматив/актуальные цены | справочный; [PROCUREMENT_PIPELINE.md](PROCUREMENT_PIPELINE.md) | обе |
| [docs/BETA_ROLLOUT.md](https://github.com/dodger7360/uvm_ai_operator/blob/5e94aa4932f18c298a41685a4fa81c64c466ef7d/docs/BETA_ROLLOUT.md) | Прежняя beta: бюджеты, tools, Low/Medium и возврат кода | исторический; [DECISIONS](DECISIONS.md) / [Windows](WINDOWS_DEPLOYMENT.md) | только origin/main, закреплён SHA |
| [docs/BOT_VERSION.md](https://github.com/dodger7360/uvm_ai_operator/blob/5e94aa4932f18c298a41685a4fa81c64c466ef7d/docs/BOT_VERSION.md) | Прежние /version и APP_VERSION, отсутствуют в активном v2 | исторический; [DECISIONS](DECISIONS.md) / [Windows](WINDOWS_DEPLOYMENT.md) | только origin/main, закреплён SHA |

Добавлены №24: [DOCUMENTATION_MAP.md](DOCUMENTATION_MAP.md) и
[reports/024-documentation-reconciliation.md](reports/024-documentation-reconciliation.md).
Первый — действующая навигация для review, второй — свидетельство аудита.

## Как использовать наследие

- Уникальные требования без потерь перенесены в NORMALIZATION и PROCUREMENT_PIPELINE;
  предложенные новые проверки — в NORMALIZATION_CASES. Подробности — отчёт №24.
- Старые составные марки/нормативы/шапки/условия полезны как предметные требования,
  а не как разрешение переносить AIService, GradeRule, router или NomenclatureSpec целиком.
- Вложенные reports/ не задают текущую политику. №15–18 и PERSISTENT_STARTUP_GUARD
  не отменяют forward-only №19. №23/24 не объявлены автоматически утверждёнными.
- INVENTORY_FILE_AUDIT — свидетельство старой книги; не повторный доступ к файлу.
  Старые примеры заявок/складов из документов не копировать в новые фикстуры или отчёты.
- tmp/23met_research содержит два tracked Markdown, несмотря на имя tmp.
  Это архив исследования, не разрешение scraper/запросов и не актуальный прайс.
