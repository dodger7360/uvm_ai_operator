# Report 009 — offline Structured Outputs correction

Status: **FIX PREPARED**, for publication in `fix/steel-agent-v2-wire-schema` and a
separately authorized deployment/live API check. HTTP 400 is not declared resolved.

## Baseline and cause

Base: `eea24fb5021116993945e1ddb71be9b6a02816e7`.
Windows; Python 3.13.15, OpenAI SDK 3.19.2, Pydantic 2.13.5.
An isolated `fix/steel-agent-v2-wire-schema` worktree was created from this commit.

The known live error is HTTP 400 / `invalid_json_schema` /
`text.format.schema`, before generation. This task made no network model calls.

Captured the final JSON request through the installed SDK's
`Responses.parse` and fake HTTP transport. This includes the SDK's strict-schema
conversion, not just Pydantic's raw schema. Concrete unsupported profile paths:

* `$.text.format.schema.properties.answer.default` = empty string.
* `$.text.format.schema.properties.view.default` = `normalized`.

These originate in internal Python defaults. SDK conversion removes null
defaults but retains these non-null values. Required lists and closed objects
were already correct. Nullable anyOf branches and local references are not
identified as faults. Neither numeric constraints nor general string patterns
are removed as allegedly unsupported features.

Another boundary mismatch was found at
`$defs.Item.properties.quantity.anyOf`: the schema advertised number/string/null
while internal `ExactDecimal` rejects floats. The Pydantic string branch also
generated a negative-lookahead regex. The adapter instead advertises string/null;
Python still rejects nonfinite, negative and inexact float input. The regex is
not asserted to be the confirmed server rejection cause.

The live diagnostic lacks the server's nested error explanation. Defaults are
the concrete offline incompatibility corrected here; which node the real server
rejected first is not observable from the available safe metadata. See
[Structured Outputs evidence](../STRUCTURED_OUTPUTS.md) for sources and limits.

## Minimal change

`WireAgentPlan` inherits `AgentPlan`; only its schema representation is adapted.
After SDK parsing, conversion explicitly revalidates the original `AgentPlan`.
No prompts, model, reasoning, engine, storage schema or metal rules change.
All seven intents and all three operation kinds remain. Decimal never passes
through float. Existing atomic commits, history, idempotency and failure handling
remain in force.

Old fake responses returned successful JSON without inspecting the request
schema. They exercised parsing and business validation but not API input-schema
compatibility. The new tests inspect the actual serialized request.

## Schema size

Compact UTF-8 JSON of `text.format.schema`, installed SDK:

| Metric | Original | Adapter |
|---|---:|---:|
| Bytes | 4490 | 4532 |
| Object properties, definitions counted once | 41 | 41 |
| Definitions | 6 | 6 |
| Object/array depth, expanding refs | 7 | 7 |
| Enum values | 22 | 22 |

These are below documented limits (5000 properties, 10 nesting levels, 1000 enum
values). No size optimization or removal of information was necessary.

## Tests

Before implementation: **2 failed, 17 passed**. Failures were final HTTP schema
defaults and numeric quantity exposure. Tests were not relaxed to make them pass.

Added 20 offline cases: final SDK schema and actual high payload, original SDK
defaults, nullable quantity, seven intents, set/regroup/undo, exact Decimal,
technical attributes/groups, malformed replies, actual engine/SQLite creation,
edit/undo, rejected response with unchanged state, replay, history and reopening.

First combined regression run: **217 passed, 20 subtests passed**. The final run,
including the complete SDK/engine/SQLite scenario: **218 passed, 20 subtests passed**
in 12.27 seconds. `git diff --check` passed.
Target suite includes all v2 tests, Windows operations, Telegram, access and
legacy high configuration. No full legacy pytest or paid API calls.

## Safety and delivery

Files: `steel_agent_v2/wire.py`, `steel_agent_v2/transport.py`,
`tests/test_steel_agent_v2_wire_schema.py`,
`docs/steel-agent-v2/STRUCTURED_OUTPUTS.md`, this report.

Production HEAD was read back as the original `eea24fb5021116993945e1ddb71be9b6a02816e7`,
with no tracked changes. No production files, databases, configuration, processes,
Telegram messages or polling were changed. No production databases were used in
tests. No main update, merge or deployment is part of this task.

Remaining risk: offline mocks cannot prove live schema acceptance or GPT quality.
The published candidate needs separate deployment approval and one authorized
live request. Preserve the diagnostic release until then; do not restart it as
part of this task.
