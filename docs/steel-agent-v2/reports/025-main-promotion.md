# Отчёт №25 — Steel Agent v2: перенос основной линии в main

18.10.2026. **STEEL AGENT V2 — MAIN PROMOTED**.
Операция управления GitHub-ветками, не deployment. Содержимое исходного
коммита v2 не изменено; новый main должен оставаться на точном SHA ниже.

## 1. Обязательные исходные версии

| Объект | Проверенное значение до записи |
|---|---|
| GitHub main | `5e94aa4932f18c298a41685a4fa81c64c466ef7d` |
| docs/steel-agent-v2-documentation-reconciliation | `600dedccfd390f76786d9d58b53a834dbc1c65a0` |
| GitHub default branch | main |
| legacy/main-before-v2-20261018 | отсутствовала |
| Windows production | `4f7babf3e9aebee76e582737c2fe31680a1fa528`, чистый detached checkout |

Истории расходились после общего предка
`a6509c3899e161a8e4b8a56547e5854c55bd31c2`: шесть уникальных коммитов старого
main и пятнадцать v2. Их не объединяли и не перебазировали.

## 2. Авторизация и предварительные зависимости

GitHub CLI gh не обнаружен. Использована существующая авторизация Git Credential
Manager для штатного GitHub REST API; административные права репозитория подтверждены.
Новые токены не создавались, интерактивная выдача credentials отключена; секрет
использовался только в памяти и не записывался в отчёт, файлы или вывод команд.

До операций: открытых PR — 0; обе исходные ветки protected=false, branch protection
не настроена; repository rulesets, включая inherited, — 0. GitHub Actions enabled,
allowed_actions=all, workflows=0; webhooks=0. Архивная ветка отсутствовала.
Выявленных зависимостей, требующих остановки перед переименованием, нет.

## 3. Фактически выполненные административные операции

| Шаг | Штатная операция GitHub | Подтверждение до следующего шага |
|---|---|---|
| A | PATCH repository: default_branch = docs/steel-agent-v2-documentation-reconciliation | HTTP 200, повторный GET подтвердил временной default; оба SHA неизменны |
| B | POST branches/main/rename → legacy/main-before-v2-20261018 | HTTP 201; ls-remote подтвердил архив на старом SHA и отсутствие main; default оставался временным |
| C | POST branches/docs%2Fsteel-agent-v2-documentation-reconciliation/rename → main | HTTP 201; main на точном v2 SHA, архив сохранён, прежнее имя v2 отсутствует |
| D | Повторная проверка repository/default и Git refs | default_branch=main автоматически после C; дополнительный PATCH не потребовался |

Каждый шаг выполнялся отдельной операцией после проверки предыдущего результата.
Ветви переименованы API, а не заменены push другого содержимого. Частично
выполненных/неподтверждённых шагов не осталось. Не было обратной перезаписи.

## 4. Итоговые имена, SHA и default

- [main](https://github.com/dodger7360/uvm_ai_operator/tree/main) →
  `600dedccfd390f76786d9d58b53a834dbc1c65a0`.
- [legacy/main-before-v2-20261018](https://github.com/dodger7360/uvm_ai_operator/tree/legacy/main-before-v2-20261018) →
  `5e94aa4932f18c298a41685a4fa81c64c466ef7d`.
- GitHub default_branch и удалённый symbolic HEAD: **main** на v2 SHA.

До и непосредственно после переименования — 32 ветки. Все остальные 30 GitHub
веток сохранили свои имена и SHA. Исторические feature-ветки не удалялись.
Отчётная ветка, описанная ниже, добавляется отдельно после успешной операции.

## 5. Дерево и старые файлы

API подтвердил неизменность commit tree и родителей:

| Коммит | Tree SHA |
|---|---|
| Новый main 600dedcc | `0734c40f099274884fd605b244a19b7c64b937e0` |
| Архив 5e94aa49 | `22e4071c4d75a919b0fde4f410a2e24c0d2c1a48` |

По Git-объектам проверены steel_agent_v2/, telegram_bot.py с единственным
run_queue/v2-маршрутом и без сборки AIService/TelegramService/WorkAssistantService,
а также PRODUCT, DECISIONS, NORMALIZATION, NORMALIZATION_CASES,
PROCUREMENT_PIPELINE, DOCUMENTATION_MAP, ROADMAP, PROJECT_STATE и отчёты №23/24
в docs/steel-agent-v2. Дерево нового main в точности равно исходному 600dedcc.

PLAN.md, корневые ARCHITECTURE/DECISIONS, DOMAIN_RULES, METAL_REFERENCE,
NORMALIZATION_STAGE_ONE и остальные старые файлы остаются в состоянии 600dedcc.
Их исторические/справочные статусы не менялись. Main-only BETA_ROLLOUT/BOT_VERSION
остаются в архиве; ссылки по точному старому SHA сохраняют тот же Git-объект.
Старый ИИ-код из прежнего main не подмешивался в новое дерево.

## 6. Сохранность истории

Оба исходных SHA доступны через GitHub API и удалённые refs. Родители коммитов
совпали с исходными Git-объектами. Сохранены 58 достижимых коммитов старого main
и 67 достижимых коммитов v2; одинаковый HEAD сохраняет всю свою исходную историю.
Ни merge, ни cherry-pick, ни rebase, ни reset/clean, ни force-push не выполнялись.

## 7. Защита, PR и Actions после операции

Новый main и архив protected=false, protection endpoint возвращает отсутствие
конфигурации; rulesets=0. **Новый main не защищён, как не был защищён старый main.**
Защита не была потеряна при переносе на архив: исходной защиты не существовало.
Новые административные политики не устанавливались.

Открытых PR по-прежнему 0, ошибочно перенаправленных PR нет. Workflows=0,
webhooks=0; настройки Actions enabled/allowed_actions=all не изменились.
Другие видимые GitHub-зависимости не менялись. Внешние скрипты/CI вне доступных
GitHub workflows/hooks не инвентаризировались; использование старых имён refs
в локальных копиях следует проверить при следующей работе.

## 8. Windows production — только чтение

HEAD до/после: `4f7babf3e9aebee76e582737c2fe31680a1fa528`, Git чистый, detached.
Native supervisor PID 11872, worker PID 10324; telegram.ready содержит 10324.
Оба процесса созданы 18.10.2026 около 11:12:04 местного времени, задолго до
переименования. PID 21856/22360 — их venv redirectors, не второй бот.

Не выполнялись checkout/pull/fetch в production, start/stop/restart, Telegram/API
тесты, чтение/изменение SQLite, .env или Task Scheduler. CommandLine/credentials
не выводились. Удалённый новый main **не означает обновления Windows production**.
Runtime оставлен работающим на прежнем SHA. Проверка процессов не является
новой приёмкой polling, GPT или качества нормализации.

## 9. Отчёт и ограничения

Локальная копия: 025-main-promotion-report.md вне production и исходного checkout.
Для review подготовлена отдельная ветка
[docs/steel-agent-v2-main-promotion-report](https://github.com/dodger7360/uvm_ai_operator/tree/docs/steel-agent-v2-main-promotion-report)
от нового main 600dedcc; единственное изменение — этот Markdown-отчёт.
Результат публикации отчётной ветки подтверждается финальным ответом после push.
Она не сливается в main; main обязан оставаться на 600dedcc.

Остаточные ограничения: отсутствие защиты main; возможные закэшированные refs
в старых локальных копиях; неаудированные внешние потребители названий веток.
Критических неразобранных последствий в проверенных GitHub настройках/PR нет.
Python, GPT-инструкции, зависимости и содержимое целевого коммита не изменены.

## 10. Следующая сессия Codex

Не делать pull/checkout в production. Для разработки предпочтительна свежая
локальная копия вне runtime: clone нового main, проверка его опубликованного SHA,
затем отдельный worktree/feature или fix-ветка от актуального origin/main.
Не отправлять старую локальную main на origin: она могла остаться на старом SHA.
Исходная локальная docs-ветка после GitHub rename имеет устаревшее имя upstream;
не воссоздавать её случайным push. Массовый prune/очистка здесь не выполнялись.

Сначала читать AGENTS и docs/steel-agent-v2/DOCUMENTATION_MAP.md, затем PRODUCT,
DECISIONS, PROJECT_STATE и предметную спецификацию. Изменения попадают в main
после review/проверок; deployment — отдельное разрешение. Legacy-ветка только
архив, не fallback. Перенос полезных парсеров — отдельные согласованные задачи.
№26 автоматически не начинается, текущий Telegram не обновляется.
