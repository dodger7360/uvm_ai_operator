> ИСТОРИЧЕСКИЙ ДОКУМЕНТ ПРЕЖНЕГО КОНТУРА — пометка №24.
> Не задаёт активные правила или запуск Steel Agent v2. Действующий преемник:
> [PROJECT_STATE.md](steel-agent-v2/PROJECT_STATE.md); [приоритеты и конфликты](steel-agent-v2/DOCUMENTATION_MAP.md).
> Исходный текст ниже сохранён как свидетельство; старые процедуры не выполнять.

# Telegram dialogue context: review report

Base: `4dba1730bbb64928e74248c8be83fa9a32c34508`.
Branch: `fix/telegram-dialog-context`, isolated worktree; no production deployment.

## Reproduced defects and causes

Five mandatory regressions failed on the base code before implementation.

Independent review found one **Medium** risk: without an explicit row number,
the model could target only one source of a consolidated display row, leaving
the merged line inconsistent. It could also propose a grade absent from an
unscoped command. The handler now asks for scope and saves no partial change;
it requires confirmation for unstated grade proposals. Explicit group templates
remain supported. Regressions cover these cases. No Critical or High findings
remain.

| Scenario | Root cause | Resolution |
| --- | --- | --- |
| A: consolidated grade edit | `telegram_service.local_edit_plan` forced source selection for all fields and omitted «допиши»; model IDs were not bound to the named display row | Deterministic grade broadcast over the existing group; `scoped_plan` checks model targets and expands only equal grade edits; quantity remains source-specific |
| A: scope replies | A short reply depended on incomplete proposal/context; `new` fallback could reparse an edit | Reuse persisted `PendingOperation` command/question/answers; scope replies confirm only bound grade proposals; edits cannot fall through to new extraction |
| B: display order/template | No presentation state or typed display intent; «Пропиши ...» with quantity matched `is_new_request` | Separate `display`/`show` routes and JSON preference; compare diameter, grade and aggregate quantity; changed А500 requires confirmation |
| C: pieces plus derived mass | `exact_source_attributes` used the last `QUANTITY_RE` match, selecting tonnes after `=`; source matching/identity treated annotation as an independent qualifier | Shared `quantity.without_mass_annotation` used for literal recovery, coverage and consolidation; keep full raw source and exact pieces |
| D: slash-separated grades | `exact_source_attributes` raised `ambiguous_grades` before constructing/saving `ActiveRequest`; `PendingClarification` supports quantity/unit only | Save source request with a typed alternatives proposal in the existing `PendingOperation`; confirm only literal sets, with fingerprint/user binding |
| E: intentions | Show and short scope answers had no deterministic route; edit verbs/ordinal prefixes were incomplete | Explicit routes, typed show/display proposals, «Во второй», and safe acknowledgement handling with/without a session |

## Source invariants

All source rows, original quantities/units, original snapshots and source IDs
remain intact. Six scenario-A sources still produce four derived display groups.
Metres from scenario D remain metres; no normative pipe mass is invented.
The four-piece sheet retains `Decimal('4')`, unit pieces, and exact estimated
mass `Decimal('1.6956')`. Repeat normalization and consolidation do not multiply
the annotation or treat it as another item. Edits regenerate derived output.

## Compatibility and validation

No SQLite migration, new database, dependency or production-state copy is needed.
Old JSON defaults the added display flag to false; existing source fingerprints
are stable. Test databases and transport are isolated and temporary. New
regressions use the real OpenAI SDK with mock HTTP responses; no live OpenAI or
Telegram requests occur. Existing Windows operations tests remain unchanged.

The worktree initially lacked three ignored archived reference HTML files needed
by an existing 23MET test. Those public reference snapshots were copied read-only
from the existing checkout and SHA-256 verified, solely to run the unchanged test.
They remain ignored. No 23MET parser/test, normative reference or production file
was edited, downloaded or bypassed.

Final full suite target: **538 passed, 4 deselected, 86 subtests passed**. The
new file contains 54 offline regression cases. Two existing expectations were
updated to require an explicit scope instead of accepting a partial or
unstated grade edit; no tests were disabled. `git diff --check` passes.
Production checks and the final branch/file status are also recorded in the
user-facing report.

Production remains on clean `main` at the base commit, with the same worker PID
1424 and supervisor PID 8076 (started 01:19:02 on 14 October), and the same task
definition and .env checksum. SQLite was not modified by this work. A read-only
audit reports integrity `ok`, 2 active requests, 28 history snapshots, 2 users
and 3 access events. Its file checksum differs from the earlier publication
baseline: production logs show successful `sqlite_save` operations, most recently
at 09:05:46, matching the latest database history time. Those are live bot
operations; isolated test databases were never the production file.

## Remaining work

- Undo of the last committed-in-history field change needs a separate design for
  audit history and permissions. It is explicitly unsupported in this change.
- Free language not covered by deterministic commands still uses the existing
  low/medium classification pipeline. Mock integration verifies control of its
  proposed operations, not the accuracy of a new live LLM response.
- These changes require separate review and a later authorized deployment.
  No commit, push, main merge, production restart or supervisor/task change is
  part of this task.
