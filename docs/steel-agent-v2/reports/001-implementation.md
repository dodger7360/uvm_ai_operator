# ОТЧЁТ CODEX №1 — STEEL AGENT V2

Реконструкция сохранённого отчёта №1, сверенная в №2 с файлами и повтором тестов.
База: 6b16c404d7ad68ec05bb7dc929e4a042d8b59adf, feature/steel-agent-v2.

1. Реализовано изолированное ядро: GPT-координатор, извлечение, группы, правки,
   undo, диалог, read-only поиск и история.
2. Переиспользованы ContractModel/ExactDecimal, Decimal helpers quantity,
   Database, dialog_history, diagnostics, TelegramAccess/TypingIndicator/safe_chunks.
3. Созданы 8 модулей steel_agent_v2: __init__, contracts, engine, repository,
   presentation, transport, agent, telegram; scripts/v2_offline_tests.py,
   scripts/v2_llm_check.py, tests/test_steel_agent_v2.py и прежний VALIDATION.md.
4. Из существующих файлов изменён только docs/STEEL_AGENT_V2_ARCHITECTURE.md.
5. Нормализация проверяет покрытие текста, ссылки и однократность групп;
   арифметика Python; один диагностический ремонт, отказ сохраняет исходник.
6. Правки адресные и атомарные; undo через версии. Диалог читает текущий snapshot.
7. Поиск подготовлен в Responses SDK, проверен подменой HTTP; не запускался вживую.
8. 69 passed v2; полный набор 1273 passed / 7 исходных failed / 4 deselected /
   86 subtests passed. Четыре B6: 38/22/27/10 позиций. Подробности — TESTING.md.
9. Живое качество не доказано; Windows не подключён, legacy история не импортируется,
   после аварии нужен ручной разбор inflight; сеть не даёт exactly-once доставки.
10. CLI живых испытаний подготовлен; платные вызовы не выполнялись.
11. HEAD не менялся, изменения незакоммичены, diff --check пройден. Main, Windows,
   .env и пользовательские SQLite не менялись; commit/push/deploy не выполнялись.

Дополнения №2 хранятся отдельно; этот отчёт не обновляется под новые результаты.
