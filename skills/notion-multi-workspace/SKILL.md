---
name: notion-multi-workspace
description: Use the multi-workspace Notion MCP server to read, write, and securely upload local files in explicitly selected workspaces.
---

# Notion Multi-Workspace

Use this plugin when the user needs to read from or write to multiple Notion
workspaces in the same session.

## Rules

- Always require an explicit `workspace` argument on every tool call.
- Do not infer that a page or teamspace in one workspace exists in another.
- If the target workspace is unclear, use `list_workspaces` first.
- Before a write, confirm the selected workspace and explicit destination.
- Local uploads are disabled unless `NOTION_UPLOAD_ROOTS` lists approved
  absolute directories. Never broaden those roots merely to make a call work.
- Upload only regular files beneath an approved root. The upload tools reject
  relative paths, traversal, symlinks, unsafe names or MIME types, and files
  over 20 MiB.
- Require `file_path`; never send the unsupported `path` alias.
- Do not put credentials, exports, or confidential data under an upload root.

## Current Tools

- `list_workspaces`
- `search`
- `fetch_page`
- `fetch_database`
- `query_database`
- `create_page`
- `append_block_children`
- `upload_file`
- `upload_and_append_file_block`
