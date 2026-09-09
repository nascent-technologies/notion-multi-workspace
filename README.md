# Notion Multi-Workspace

A local stdio MCP server for explicitly reading from and writing to multiple
Notion workspaces in one session.

## Features

- explicit `workspace` selection on every tool call
- normalized multi-workspace configuration
- explicit read and write tool surface
- page search, fetch, database query, page creation, and block append support
- secured local file uploads and file-block attachment

## Tools

- `list_workspaces()`
- `search(workspace, query, page_size=10, result_type="page")`
- `fetch_page(workspace, page_id_or_url, include_content=true, block_limit=200)`
- `fetch_database(workspace, database_id_or_url)`
- `query_database(workspace, database_id_or_url, page_size=10, start_cursor=None, filter=None, sorts=None)`
- `create_page(workspace, parent, properties, children=None)`
- `append_block_children(workspace, block_id_or_url, children, position=None)`
- `upload_file(workspace, file_path, filename=None, content_type=None)`
- `upload_and_append_file_block(workspace, block_id_or_url, file_path, block_type=None, caption=None, filename=None, content_type=None, position=None)`

## Configuration

Set `NOTION_WORKSPACE_KEYS` to a comma-separated list of workspace keys. For each key, define matching name and token variables.

Required variables:

- `NOTION_WORKSPACE_KEYS`
- `NOTION_WORKSPACE_<KEY>_NAME`
- `NOTION_WORKSPACE_<KEY>_TOKEN`
- `NOTION_WORKSPACE_<KEY>_ALIASES` (optional)
- `NOTION_UPLOAD_ROOTS` (required only to enable uploads)

Example:

```env
NOTION_WORKSPACE_KEYS=workspace-a,workspace-b,workspace-c

NOTION_WORKSPACE_WORKSPACE_A_NAME=Workspace A
NOTION_WORKSPACE_WORKSPACE_A_TOKEN=replace_with_workspace_a_token

NOTION_WORKSPACE_WORKSPACE_B_NAME=Workspace B
NOTION_WORKSPACE_WORKSPACE_B_TOKEN=replace_with_workspace_b_token

NOTION_WORKSPACE_WORKSPACE_C_NAME=Workspace C
NOTION_WORKSPACE_WORKSPACE_C_TOKEN=replace_with_workspace_c_token
NOTION_WORKSPACE_WORKSPACE_C_ALIASES=wksp-c,team-c

# Uploads remain disabled while this value is empty.
NOTION_UPLOAD_ROOTS=
```

To enable uploads, set `NOTION_UPLOAD_ROOTS` to one or more existing absolute
directories separated by the operating system path separator (`:` on macOS and
Linux). Never use a credential, export, or confidential-data directory as an
upload root.

## Upload safety contract

- Uploads are fail-closed: a missing or empty `NOTION_UPLOAD_ROOTS` disables
  both upload tools.
- `file_path` must be an explicit absolute path beneath an approved root. The
  undocumented `path` alias is rejected.
- Every root, parent directory, and file is opened component by component
  without following symlinks. The server holds the validated file descriptor,
  rechecks its identity, requires a regular file, and rejects files over
  20 MiB before any network request.
- Filenames and MIME types use a conservative header-safe format. The file is
  streamed in bounded chunks as exactly one multipart `file` part with an exact
  `Content-Length`; it is never buffered as one whole-file byte string.
- Upload requests use only fixed `https://api.notion.com/v1` endpoints and do
  not follow redirects or use a response-provided upload host.
- Upload success summaries omit the local path and raw Notion response bodies.

Uploads, file-backed blocks, and calls using `position` use Notion version
`2026-03-11`. Existing database operations and plain block append calls retain
the legacy `2022-06-28` behavior. Supported positions are `start`, `end`, and an
exact `after_block` object.

## Local setup

1. Copy `.env.example` to `.env`
2. Add valid Notion integration tokens
3. Leave `NOTION_UPLOAD_ROOTS` empty unless local upload access is needed
4. Run the server with Python 3

## Verification

```bash
python3 tests/test_notion_multi_workspace_server.py
python3 scripts/smoke_test_read_side.py
python3 scripts/smoke_test_stdio.py
```

The unit suite is self-contained and makes no live Notion calls. The smoke
scripts use placeholder workspaces by default; pass their explicit live-call
options only in a separately authorized environment.

## Notes

- Workspace aliases must be unique.
- The integration must have access to the target Notion pages or databases.
- Write operations use the configured workspace integration permissions.
- Confirm the selected workspace and target block before every write.
