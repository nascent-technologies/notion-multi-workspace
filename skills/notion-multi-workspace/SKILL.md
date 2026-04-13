---
name: notion-multi-workspace
description: Use the multi-workspace Notion MCP server to search and fetch content from explicitly selected workspaces.
---

# Notion Multi-Workspace

Use this plugin when the user needs to read from multiple Notion workspaces in the same session.

## Rules

- Always require an explicit `workspace` argument on every tool call.
- Do not infer that a page or teamspace in one workspace exists in another.
- If the target workspace is unclear, use `list_workspaces` first.

## Current Tools

- `list_workspaces`
- `search`
- `fetch_page`
