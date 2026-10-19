# Responses Structured Outputs boundary

## P0: compact creation, 19.10.2026

`OpenAITransport.plan()` sends one `WireCoordinatorPlan` object with nested
`plan: WireCreatePlan | WireActionPlan` (anyOf below the object root).
Creation requires only intent/extraction/pending_event_id/display_layout/mass_specs;
the action branch keeps the full operation contract. Both convert to the same
authoritative `AgentPlan`, then RequestEngine validates evidence and invariants.
Old flat WireAgentPlan responses remain readable; stored snapshots/receipts and
SQLite do not change. Known invalid completed replies retain one bounded repair;
unknown API outcomes are never retried by this change. Actual SDK fake HTTP tests
inspect the strict schema and create/edit/undo paths. Historical sections follow.

## Candidate: semantic editing, stage 2

Operation.selection replaces target_id for display/where/all selection. Closed
nested predicates use existing label/characteristic values; component matching
specifies an existing field, literal separator and 1-based component, without a
product-specific Python parser. Cardinality one/all is explicit.
set_quantity/add_quantity/set_length carry the source number and unit evidence,
not model-computed results. The same strict WireAgentPlan adapter strips defaults,
preserves required/nullable properties and uses the installed Responses SDK.

DisplayOrder belongs only to stored Reply, not to the model's wire plan.
PendingClarification optionally stores the unresolved operation with unit evidence.
Null new receipt fields are omitted for compatibility; SQLite schema is unchanged.
Real SDK fake HTTP tests inspect the final payload, including selection and measures.

## Candidate №31

AgentPlan adds nullable clarification and clarification_event_id; Operation.kind
adds convert_mass without a model-computed numeric result. The new nested object
uses the same closed/required/nullable SDK profile. PendingClarification is a
stored Reply field, not a wire operation or new SQLite schema. Old Reply receipts
omit clarification when null, retaining byte compatibility. Real installed SDK
HTTP mock tests inspect the complete strict schema and multi-turn parsing.
The original literal-evidence set validator remains unchanged.

## Internal contract and API schema

`AgentPlan` is the authoritative Python contract. `WireAgentPlan` inherits its
fields and validators and adapts only the JSON Schema exposed to the SDK.
In candidate №28, `OpenAITransport.plan()` sends the SDK-generated strict format
with `Responses.create`, then validates the completed final text with WireAgentPlan
and explicitly converts it through `AgentPlan.model_validate`. The production
version still uses the earlier `Responses.parse(text_format=WireAgentPlan)`.
The engine continues validating coverage, evidence, identifiers, quantities,
group membership and mutations before committing state.

Two API-boundary adjustments are intentional:

* Remove schema `default` annotations. Python defaults remain unchanged. The SDK
  marks every property required, including nullable properties; the model must
  supply explicit values, not rely on server-side defaults.
* Encode quantity as string or null. Decimal validation and arithmetic stay in
  Python. This avoids advertising a JSON-number branch that can deserialize to
  float, and avoids sending Pydantic's generated Decimal lookahead expression.
  No claim is made that `pattern` in general is unsupported.

No fields, intents or operations are removed. `additionalProperties: false`,
required properties, enums, nullable unions and references remain strict.
Numeric range and nonempty constraints unrelated to the quantity representation
are preserved. Search remains a separate Responses call with the official tool.

## Evidence and limits

With OpenAI SDK 3.19.2 / Pydantic 2.13.5, the original **serialized HTTP body**
contained `schema.properties.answer.default` and `schema.properties.view.default`.
The SDK strips null defaults but leaves non-null defaults. Its local parsing does
not establish that the server accepts the input schema.

The [official Structured Outputs guide](https://developers.openai.com/api/docs/guides/structured-outputs)
documents a supported subset, required fields, nullable unions, closed objects,
references and limits. Defaults are outside the API profile used here. A
[first-hand SDK issue](https://github.com/openai/openai-agents-python/issues/4390)
also reports rejection of surviving non-null defaults (Azure); this is supporting
evidence, not proof of which keyword the production request rejected first.
The production diagnostic provided `invalid_json_schema` and `text.format.schema`
without the nested path. Therefore offline inspection proves the incompatible
wire elements and the local correction, **not elimination of the live HTTP 400**.
The server's first offending path and endpoint acceptance still require an
authorized live test after a separately approved deployment.

`tests/test_steel_agent_v2_wire_schema.py` captures the real SDK-generated request
using fake HTTP, checks the supported project profile and parses synthetic
responses with the installed SDK. Its schema auditor is a regression guard,
not an implementation of OpenAI's server validator. SQLite tests exercise the
actual engine, persistence, undo, failed validation and replay without network.

No live API calls are part of these tests.

## Изменение границы валидации — №28

SDK Responses.parse валидирует содержимое до возврата Response; при schema-ошибке
агент не получал предыдущий план. Теперь Responses.create получает тот же
`type_to_text_format_param(WireAgentPlan)` установленного SDK 3.19.2; строгая схема,
nullable decimal string, отсутствие default, модель и reasoning сохранены.
После подтверждённого completed final message выполняются WireAgentPlan и все
внутренние validators. ValidationError переносит исходное предложение только в
память одного ремонта; JSON/содержимое в диагностику не пишутся. Refusal, incomplete,
неизвестный phase и сетевые ошибки не превращаются в автоматический повтор.

Дополнения pending_event_id, resolve.resolution, replace_from проверяются фактическим
HTTP MockTransport и прежним subset_issues. Snapshot не записывается из wire-ответа
без проверки. Общие основания: [официальная документация Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses);
точная phase-семантика дополнительно сверена с установленным SDK, не с предположением
о живом ответе. Платного теста API в №28 не было.
