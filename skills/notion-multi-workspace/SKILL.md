---
name: notion-multi-workspace
description: Search, read, create pages, and append blocks in explicitly selected Notion workspaces.
---

# Notion Multi-Workspace

Use this plugin to work with multiple Notion workspaces in one local MCP session.

## Routing and results

- Every content operation requires an explicit `workspace`. `list_workspaces` is the discovery exception.
- Resolve an unclear workspace with `list_workspaces`; use the returned key or alias.
- Preserve workspace keys and source URLs when combining results. Do not infer that similarly named pages are the same across workspaces.
- Follow `has_more` and `next_cursor` for search and database queries. Check collection scope and completeness before claiming all matches were read.
- Inspect page content status, truncation reasons, rendering warnings, and property status. Markdown is lossy; unknown property completeness is not evidence of completeness.

## Writes

- `create_page` and `append_block_children` are available with the integration's existing Notion permissions.
- Use the user's intended workspace and explicit parent or block ID. Workspace labels come from local configuration, not verified ownership of a supplied URL.
- Write failures can have an uncertain outcome. Inspect the target before attempting the same write again; the server does not automatically replay writes.

## Current Tools

- `list_workspaces`
- `search`
- `fetch_page`
- `fetch_database`
- `query_database`
- `create_page`
- `append_block_children`

This package runs locally over stdio. Installing it does not establish a cloud-assistant connection.
