# Codex №27 — профессиональное представление заявки

**PREPARED — READY FOR REVIEW. Deployment не выполнялся.**
18.10.2026, Windows, отдельный worktree/ветка `fix/steel-agent-v2-professional-output`.
База origin/main проверена точно: `36771fe7bf7c62438d01df8b56eb85e791a92d4e`.

## Что исправлено

Изучены presentation.py, agent.py, transport.py и существующие UX-тесты.
Create/edit вызывают render; обычный show тоже использует его. Служебный заголовок,
групповые промежуточные суммы, повторные предупреждения и фраза о ненужности уточнений
добавлялись formatter, а не требовались контрактом GPT. UUID и ID уже скрывались;
их отсутствие закреплено в новых проверках. Агент/transport и GPT-инструкции не менялись.

Normalized и forward теперь содержат отдельные полные строки позиций; одна позиция
без номера, несколько — с устойчивыми номерами. Город, явные неизвестные, вопросы
и необработанные фрагменты сохранены. Версия хранится в БД и доступна в explanation;
групповые причины/различия — там же, суммы — в totals. Даже полностью одинаковые
позиции остаются отдельными: никакого нового суммирования или изменения групп.

Существующий safe_chunks в telegram_operator.py используется и v2 queue: минимально
изменён **только этот helper**, чтобы предпочитать целые строки. Длинная одиночная
строка делится с сохранением символов и лимита 3200 UTF-16. Код очереди, outbox,
доставка, typing, SQLite, supervisor, права и модель не изменены. Старый AI не включён.

## Точный пример до / после

Синтетический исходник, не production-заявка:

```text
Лист 8×1500×6000 мм С245 ГОСТ 19903-2015 — 1,25 т
Лист 10×1500×6000 мм 09Г2С ГОСТ 19281-2014 — 750 кг
```

До: выполнен render из Git-объекта исходного main на том же структурированном снимке:

```text
Заявка, версия 1.
Группа 1: Листы для закупки
  1. Лист 8×1500×6000 мм; С245; ГОСТ 19903-2015 — 1,25 т
  2. Лист 10×1500×6000 мм; 09Г2С; ГОСТ 19281-2014 — 750 кг
  Количество: 2 т
  В группе есть технические отличия; взаимозаменяемость не подтверждена.
Не требуется уточнений.
```

После: новый render, те же items/groups/количества:

```text
1. Лист 8×1500×6000 мм; С245; ГОСТ 19903-2015 — 1,25 т
2. Лист 10×1500×6000 мм; 09Г2С; ГОСТ 19281-2014 — 750 кг
```

Это сравнение formatter на заданных фактах, а не оплаченный запрос GPT. Подпись
коммерческой группы не является характеристикой товара и сохранена в данных/разборе.

## Проверки

- **444 passed за 56,63 с**: актуальные v2, wire schema, Web Search, RequestEngine,
  Decimal, история, доступ, очередь/recovery/FIFO/typing, Windows runtime/supervisor,
  включая 27 новых presentation-проверок.
- **132 passed, 20 subtests passed за 5,38 с**: отдельно исторические Telegram,
  reasoning, общий helper и typing. Не доказательство качества старого AI и не fallback.
- Новый тест через Repository/Executor/fake Telegram провёл 120 позиций, несколько
  durable частей, точный порядок и восстановление всего текста без потерь.
- Проверены разные/одинаковые позиции, марки/нормативы, неизвестные марка/ГОСТ/единица,
  очень точные Decimal, явные вопросы/unresolved, оригинал, группы, редактирование и undo.
- Python 3.13.15, OpenAI 3.19.2, Pydantic 2.13.5. `pip check`: No broken requirements found.

Применён `scripts/v2_offline_tests.py`, который убирает реальные credentials,
блокирует сеть и доступ к пользовательским SQLite. Первые sandbox-прогоны
упёрлись в WinError 5 временных каталогов pytest; окончательные результаты выше
получены с обычным Windows-токеном вне ограничения песочницы, не с admin.
Исправлены ошибки самого нового теста (подстрока « т» совпала с «требует», чрезмерные
pytest IDs), а прежние assertions о версии/сумме перенесены в соответствующие views.
Проверки не удалены и не скрыты через skip/xfail.

Для воспроизведения в отдельном worktree с совместимым Python:

```powershell
python -B scripts/v2_offline_tests.py tests/test_steel_agent_v2_027_presentation.py tests/test_steel_agent_v2.py tests/test_steel_agent_v2_prelaunch.py tests/test_steel_agent_v2_windows.py tests/test_steel_agent_v2_wire_schema.py tests/test_steel_agent_v2_api_diagnostics.py tests/test_steel_agent_v2_ux_search.py tests/test_steel_agent_v2_search_runtime.py tests/test_windows_operations.py tests/test_access.py tests/test_steel_agent_v2_015_reproduction.py tests/test_steel_agent_v2_queue.py tests/test_steel_agent_v2_017_acceptance.py tests/test_steel_agent_v2_forward_only.py tests/test_steel_agent_v2_021_ux.py --basetemp=.isolated-027-current
python -B scripts/v2_offline_tests.py tests/legacy/test_sequential_poller.py tests/legacy/test_reasoning_high.py tests/test_telegram_operator.py tests/test_telegram.py tests/test_typing.py tests/test_normalizer_stabilization.py --basetemp=.isolated-027-history
```

## Что остаётся нерешённым

Formatter показывает только сохранённые факты: не восстанавливает пропуски GPT и
не определяет обязательность отсутствующего ГОСТа/марки. Не исправлены структурные
отказы/duplicate_characteristic, неполнота извлечения, append, выбор GPT подробного
view на краткий вопрос и произвольный custom/свободный answer. Explanation по явному
запросу всё ещё содержит заявку с разбором. Источники Web Search и Markdown в
произвольном ответе не переделывались. Новых сортаментных Python-правил нет;
пустая knowledge-база не подключена. Качество живой нормализации этим не доказано.

## Публикация и production

Изменены presentation.py, только safe_chunks в telegram_operator.py, два существующих
тестовых файла и новый tests/test_steel_agent_v2_027_presentation.py. Обновлены текущие
спецификации представления/нормализации, PROJECT_STATE, DECISIONS, ARCHITECTURE,
TESTING и ROADMAP (T0 первой функциональной работы 18.10.2026; цель 01.11.2026).
Исторические отчёты и экспертные знания не переписаны.

Проверены 71 Markdown и 422 локальные ссылки/anchors: битых нет.
Diff --check и review отсутствия секретов/production данных пройдены.
Полный SHA публикации сообщается в финальном ответе; версия этого отчёта определяется
его Git-коммитом. Feature/PR не сливаются автоматически.
Production Git HEAD только прочитан: `4f7babf3e9aebee76e582737c2fe31680a1fa528`,
tracked-файлы без изменений. Процессы, .env, Task Scheduler и пользовательские БД
не тронуты. Следующий шаг **после review и отдельного разрешения** — выпуск Windows
и короткая ручная проверка настоящего Telegram, без заявления о живом успехе заранее.
