# Responses Structured Outputs boundary

## Internal contract and API schema

`AgentPlan` is the authoritative Python contract. `WireAgentPlan` inherits its
fields and validators and adapts only the JSON Schema exposed to the SDK.
`OpenAITransport.plan()` uses `Responses.parse(text_format=WireAgentPlan)` and
explicitly converts the parsed object back through `AgentPlan.model_validate`.
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
