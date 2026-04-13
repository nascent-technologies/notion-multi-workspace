# Notion Multi-Workspace

Standalone Codex plugin for working with multiple separate Notion workspaces from
one session without risking cross-posting to the wrong org.

## Goal

The bundled Notion integration is typically bound to one connected workspace at
a time. This plugin provides a small local MCP server that:

- keeps multiple Notion credentials configured at once
- requires an explicit `workspace` selector on every tool call
- starts with a read-only tool surface so routing can be proven safely first

The current implementation is intentionally read-only. Write tools can be added
later once the workspace-routing rules are well tested.

## Current Tool Surface

- `list_workspaces()`
- `search(workspace, query, page_size=10, result_type="page")`
- `fetch_page(workspace, page_id_or_url, include_content=true, block_limit=200)`

## Repo Layout

- [.codex-plugin/plugin.json](./.codex-plugin/plugin.json): plugin manifest
- [.mcp.json](./.mcp.json): MCP server wiring for Codex
- [.agents/plugins/marketplace.json](./.agents/plugins/marketplace.json): repo-local plugin discovery
- [scripts/notion_multi_workspace_server.py](./scripts/notion_multi_workspace_server.py): read-only MCP server
- [scripts/smoke_test_read_side.py](./scripts/smoke_test_read_side.py): direct module smoke test
- [scripts/smoke_test_stdio.py](./scripts/smoke_test_stdio.py): end-to-end stdio smoke test
- [skills/notion-multi-workspace/SKILL.md](./skills/notion-multi-workspace/SKILL.md): guidance for agents
- [tests/test_notion_multi_workspace_server.py](./tests/test_notion_multi_workspace_server.py): unit tests

## Configuration

Use either of these approaches:

1. Copy [.env.example](./.env.example) to `.env`
2. Or set `NOTION_MULTI_WORKSPACE_ENV_FILE` to point at an external env file

### Normalized multi-workspace config

Set `NOTION_WORKSPACE_KEYS` to a comma-separated list of workspace keys. For
each key, define its name and token with matching env vars:

- `NOTION_WORKSPACE_KEYS`
- `NOTION_WORKSPACE_<KEY>_NAME`
- `NOTION_WORKSPACE_<KEY>_TOKEN`
- `NOTION_WORKSPACE_<KEY>_ALIASES` (optional, comma-separated)

Example:

```env
NOTION_WORKSPACE_KEYS=workspace-a,workspace-b,workspace-c

NOTION_WORKSPACE_NOBLE_NAME=Workspace A
NOTION_WORKSPACE_NOBLE_TOKEN=secret_workspace-a_workspace_token

NOTION_WORKSPACE_SQUIDFORM_NAME=Workspace B
NOTION_WORKSPACE_SQUIDFORM_TOKEN=secret_workspace-b_workspace_token

NOTION_WORKSPACE_DIGITAL_PRIME_NAME=Workspace C
NOTION_WORKSPACE_DIGITAL_PRIME_TOKEN=secret_digital_prime_workspace_token
NOTION_WORKSPACE_DIGITAL_PRIME_ALIASES=dp,workspacec
```

The server rejects ambiguous selectors, so aliases must stay unique across all
configured workspaces.

## Using It In Codex

When Codex is opened from this repo, the repo-local marketplace file points at
the plugin root directly. That keeps the plugin self-contained instead of
depending on another workspace's `.agents` config.

## Local Verification

Use the system Python directly. On this machine, the default `python` and
`python3` shell shims can point at a broken `pyenv` path.

### Unit tests

```bash
/usr/bin/python3 -m unittest tests/test_notion_multi_workspace_server.py
```

### Direct read-side smoke test

This loads the server module directly and optionally validates the configured
workspace tokens.

```bash
/usr/bin/python3 scripts/smoke_test_read_side.py
/usr/bin/python3 scripts/smoke_test_read_side.py --validate-tokens
```

Optional examples:

```bash
/usr/bin/python3 scripts/smoke_test_read_side.py \
  --validate-tokens \
  --workspace secondary \
  --query "meeting naming convention"

/usr/bin/python3 scripts/smoke_test_read_side.py \
  --workspace "Finance Ops" \
  --fetch-page "https://www.notion.so/..."
```

### End-to-end stdio MCP smoke test

This starts the actual MCP server, speaks JSON-RPC over stdio, and verifies the
tool contract end to end.

```bash
/usr/bin/python3 scripts/smoke_test_stdio.py
/usr/bin/python3 scripts/smoke_test_stdio.py --validate-tokens
```

Optional example:

```bash
/usr/bin/python3 scripts/smoke_test_stdio.py \
  --validate-tokens \
  --workspace fin \
  --query "meeting naming convention"
```

## Troubleshooting

- `401 API token is invalid`: replace the token in `.env` or in the env file
  pointed to by `NOTION_MULTI_WORKSPACE_ENV_FILE`
- Search returns no results: make sure the integration behind that workspace
  token has access to the target pages or databases in Notion
- `Unknown workspace ...`: run `list_workspaces` and use one of the advertised
  keys, names, or aliases
- The repo's default `python` or `python3` points at a broken shim: use
  `/usr/bin/python3` for both tests and local MCP runs

## Next Step

Add write-safe tools such as `create_page` and `update_page`, while keeping the
same explicit workspace routing requirement for every write.
