# AGENTS.md

Before changing a domain rule:
- identify the bounded context,
- state the invariant,
- add a failing test,
- preserve event-time evidence,
- consider duplicate and out-of-order delivery,
- update OpenAPI/AsyncAPI when the boundary changes.

For SQL changes, include an `EXPLAIN (ANALYZE, BUFFERS)` plan in the PR description when a realistic dataset is available.

Do not claim AWS, Datadog, Elastic APM, Slack, Jira, Notion, n8n or Make are live unless execution evidence is committed.
