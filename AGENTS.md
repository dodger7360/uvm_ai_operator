# Instructions for this repository

Start with [DOCUMENTATION_MAP.md](docs/steel-agent-v2/DOCUMENTATION_MAP.md): it
classifies every tracked Markdown source and defines the reading order. Current
product requirements live in docs/steel-agent-v2, not in the newest-looking
filename or an origin/main document. Historical reports are evidence, not policy.

Before every new Codex task, read [PRODUCT.md](docs/steel-agent-v2/PRODUCT.md),
[DECISIONS.md](docs/steel-agent-v2/DECISIONS.md),
[PROJECT_STATE.md](docs/steel-agent-v2/PROJECT_STATE.md), and the related domain
specifications linked there. For v2, its tests, Telegram wiring and documentation,
also follow [docs/steel-agent-v2/AGENTS.md](docs/steel-agent-v2/AGENTS.md).

If a task contradicts an approved owner decision, stop and identify the conflict.
Distinguish approved decisions from proposed documentation awaiting owner review;
do not establish an unapproved business rule yourself. Python test counts alone
do not prove AI domain quality. After approved product changes, update these
permanent documents; do not leave decisions only in a report or chat.

The root ARCHITECTURE.md and DECISIONS.md retain historical material. Current v2
implementation is described in [v2 architecture](docs/steel-agent-v2/ARCHITECTURE.md).

Work in the explicitly assigned worktree. Do not alter production, start Telegram,
use paid APIs, copy secrets/user databases, or deploy without direct authorization.
