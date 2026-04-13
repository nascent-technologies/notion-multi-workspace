---
name: notion-multi-workspace
description: Use the custom multi-Notion MCP server to search and fetch content across explicitly selected named workspaces from one Codex session.
---

# Notion Multi-Workspace

Use this plugin when the user needs access to multiple separate Notion
workspaces in the same session.

## Rules

- Always require an explicit `workspace` argument on every tool call.
- Treat workspace selection as mandatory before any future write action.
- Do not infer that a teamspace in one workspace exists in another.
- If the user says "Notion" without naming the workspace and the action is a
  write, ask which workspace to target.
- Prefer `list_workspaces` first when the available selectors are unclear.

## Current Tools

- `list_workspaces`
- `search`
- `fetch_page`

## Implementation Note

The current server is read-only by design. Use the bundled tools for
cross-workspace discovery and page retrieval first, then add writes only after
the routing rules are well tested.
