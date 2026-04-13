# Notion Multi-Workspace

A small MCP server for working with multiple Notion workspaces from one session.

## Features

- explicit `workspace` selection on every tool call
- normalized multi-workspace configuration
- explicit read and write tool surface
- page search, fetch, database query, page creation, and block append support

## Tools

- `list_workspaces()`
- `search(workspace, query, page_size=10, result_type="page")`
- `fetch_page(workspace, page_id_or_url, include_content=true, block_limit=200)`
- `fetch_database(workspace, database_id_or_url)`
- `query_database(workspace, database_id_or_url, page_size=10, start_cursor=None, filter=None, sorts=None)`
- `create_page(workspace, parent, properties, children=None)`
- `append_block_children(workspace, block_id_or_url, children)`

## Configuration

Set `NOTION_WORKSPACE_KEYS` to a comma-separated list of workspace keys. For each key, define matching name and token variables.

Required variables:

- `NOTION_WORKSPACE_KEYS`
- `NOTION_WORKSPACE_<KEY>_NAME`
- `NOTION_WORKSPACE_<KEY>_TOKEN`
- `NOTION_WORKSPACE_<KEY>_ALIASES` (optional)

Example:

```env
NOTION_WORKSPACE_KEYS=workspace-a,workspace-b,workspace-c

NOTION_WORKSPACE_WORKSPACE_A_NAME=Workspace A
NOTION_WORKSPACE_WORKSPACE_A_TOKEN=secret_workspace_a_token

NOTION_WORKSPACE_WORKSPACE_B_NAME=Workspace B
NOTION_WORKSPACE_WORKSPACE_B_TOKEN=secret_workspace_b_token

NOTION_WORKSPACE_WORKSPACE_C_NAME=Workspace C
NOTION_WORKSPACE_WORKSPACE_C_TOKEN=secret_workspace_c_token
NOTION_WORKSPACE_WORKSPACE_C_ALIASES=wksp-c,team-c
```

## Local setup

1. Copy `.env.example` to `.env`
2. Add valid Notion integration tokens
3. Run the server with Python 3

## Verification

```bash
python3 -m unittest tests/test_notion_multi_workspace_server.py
python3 scripts/smoke_test_read_side.py
python3 scripts/smoke_test_stdio.py
```

## Notes

- Workspace aliases must be unique.
- The integration must have access to the target Notion pages or databases.
- Write operations use the configured workspace integration permissions.
