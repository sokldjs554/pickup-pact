# Make / collaboration automation

The same regression payload used by the n8n workflow can be routed through Make.

Suggested scenario:
1. Custom Webhook receives CI regression JSON.
2. HTTP module calls the advisory triage endpoint.
3. Router creates a Jira issue only when severity >= high.
4. Notion module appends the evidence packet to an incident database.
5. Slack posts the issue link.

The repository intentionally does not include account IDs, tokens, or claims that these external SaaS integrations are live.
