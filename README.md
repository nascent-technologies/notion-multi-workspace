# Notion Multi-Workspace

A local MCP server for reading and writing across explicitly selected Notion workspaces. It uses Python's standard library and separate integration tokens for each configured workspace.

The implemented tools search and read pages, fetch and query databases, create pages, and append blocks. Writes are enabled and use the selected integration's Notion permissions. Broader Notion functionality is described in the [product roadmap](docs/product/multi-notion-product-roadmap.md); that document describes future work, not the current API surface.

## Requirements and setup

- Python 3.11 or newer, available to the MCP client as `python3` (or an absolute Python executable path).
- A local MCP client that supports newline-delimited JSON-RPC over stdio.
- One Notion integration token per intended workspace, with access to the pages/databases you want to use. Enable the content capabilities needed for your intended reads and writes in Notion.

Clone the repository and prepare the configuration:

```sh
git clone https://github.com/nascent-technologies/notion-multi-workspace.git
cd notion-multi-workspace
cp .env.example .env
```

Replace the placeholders in `.env` locally. The sample values are synthetic and cannot authenticate. Never commit a populated env file or paste credentials into an issue.

```env
NOTION_WORKSPACE_KEYS=workspace-a,workspace-b
NOTION_WORKSPACE_WORKSPACE_A_NAME=Workspace A
NOTION_WORKSPACE_WORKSPACE_A_TOKEN=replace_with_workspace_a_token
NOTION_WORKSPACE_WORKSPACE_A_ALIASES=team-a
NOTION_WORKSPACE_WORKSPACE_B_NAME=Workspace B
NOTION_WORKSPACE_WORKSPACE_B_TOKEN=replace_with_workspace_b_token
```

Keys are normalized to lowercase hyphenated selectors; environment-variable fragments use uppercase underscores (`workspace-a` becomes `WORKSPACE_A`). Names and aliases must resolve unambiguously. Each content call selects one workspace; there is no implicit default workspace or automatic cross-workspace copy.

For each key, `NAME` and `TOKEN` are required; `ALIASES` is optional. The default env file is resolved relative to the server's installation directory, not the caller's working directory. To keep configuration outside a checkout or plugin cache, set `NOTION_MULTI_WORKSPACE_ENV_FILE` to an absolute env-file path. Existing process environment values take precedence. Restart the server after changing configuration.

Tokens remain in the local env file/process environment and are sent as bearer credentials to `https://api.notion.com/v1`. File storage is plaintext; protect access to your configuration using your operating system. The server does not log tokens or run a separate hosted service. Workspace names are local labels: a successful token check does not prove that a label matches the intended Notion workspace.

## Connect a local client

For an MCP client accepting JSON server configuration, replace both absolute paths below with your local paths:

```json
{
  "mcpServers": {
    "notion-multi-workspace": {
      "command": "python3",
      "args": ["/absolute/path/notion-multi-workspace/scripts/notion_multi_workspace_server.py"],
      "env": {
        "NOTION_MULTI_WORKSPACE_ENV_FILE": "/absolute/path/notion-workspaces.env"
      }
    }
  }
}
```

For Codex, the equivalent local `config.toml` entry is:

```toml
[mcp_servers.notion-multi-workspace]
command = "python3"
args = ["/absolute/path/notion-multi-workspace/scripts/notion_multi_workspace_server.py"]

[mcp_servers.notion-multi-workspace.env]
NOTION_MULTI_WORKSPACE_ENV_FILE = "/absolute/path/notion-workspaces.env"
```

Use an absolute executable path if the client cannot find Python. Start a fresh client session and confirm the seven tools below are discovered. The server waits for MCP messages; running it directly is not an interactive terminal application.

The repository also contains a local Codex marketplace entry at `.agents/plugins/marketplace.json` and a compatibility plugin manifest at `.codex-plugin/plugin.json`. Its bundled `.mcp.json` uses a relative script path, so the host must launch it from the plugin root. Use the absolute-path configuration above for clients that do not establish that working directory. Keep secrets outside a plugin cache because updates can replace it. See the official [Codex MCP configuration](https://developers.openai.com/codex/mcp) and [plugin packaging](https://developers.openai.com/plugins/build/plugins) documentation for client-specific setup.

This is a local stdio integration. A public GitHub repository or installed local plugin does not by itself make these tools available to a cloud assistant or publish the plugin in a public directory.

## Tools

| Tool | Arguments and behavior |
| --- | --- |
| `list_workspaces` | `validate_tokens=false`; lists configured selectors. Setting it to `true` performs a live token-health read for each workspace. |
| `search` | `workspace`, nonempty `query`, `page_size=10`, `result_type="page"`, optional `start_cursor`. Types: `page`, `database`, `all`. |
| `fetch_page` | `workspace`, `page_id_or_url`, `include_content=true`, `block_limit=200` (maximum 500). |
| `fetch_database` | `workspace`, `database_id_or_url`. Returns compact database metadata. |
| `query_database` | `workspace`, `database_id_or_url`, `page_size=10`, optional `start_cursor`, `filter`, `sorts`. |
| `create_page` | `workspace`, explicit `parent`, `properties`, optional `children`. Performs one write attempt. |
| `append_block_children` | `workspace`, `block_id_or_url`, nonempty `children`. Performs one write attempt. |

MCP tool annotations describe reads and writes; they are hints for clients, not an authorization boundary. Supplied IDs/URLs are resolved under the explicitly selected token. Notion enforces the integration's permissions. Creating a page or appending blocks can take effect immediately.

## Completeness and failure handling

Search and database queries return `has_more`, `next_cursor`, and `collection` status. Pass the next cursor with the same query and workspace to continue. A response starting from a cursor describes a suffix of the results; reaching its end does not make that response the full collection. Search uses Notion's title-search endpoint, not a full-text content index or exhaustive workspace inventory.

Page responses include `content_status`: whether content was requested, whether collection completed, whether it was truncated, truncation reasons, and rendering warnings. A block budget can omit nested descendants or remaining siblings. Increasing the limit helps only up to 500 blocks; traversal also stops at 100 child-list requests or depth 50 and reports the corresponding reason. There is no resumable full-page export in this release. `content_status.complete` concerns collected blocks, not a lossless representation: Markdown drops formatting and some block types. HTTP failures return errors rather than claiming a partial read succeeded.

`property_status.complete` is unknown unless an explicit truncation indicator makes it false. Relations, people, formulas, and other properties can require separate property-item requests, which this server does not implement. Neither empty search results nor a successful fetch proves that the integration can see every relevant page. Responses preserve workspace keys, source IDs/URLs and available edit timestamps for attribution.

HTTP socket operations have a 15-second timeout. Classified reads, including POST search and database queries, make at most three attempts. Transient HTTP 429/500/502/503/504/529 and transport failures may be retried; blocked-access errors are not. `Retry-After` delays up to five seconds are honored; longer or invalid delays are reported without retrying early. Writes are not automatically retried. A timeout or transport failure during a write can leave its outcome uncertain; inspect the target before manually repeating it. These bounds apply to network operations, not a guarantee of total wall-clock duration for a recursively fetched page.

The Notion REST API version remains `2022-06-28`. Modern multi-source database behavior, comments, attachment contents, full property hydration, and new endpoint coverage are outside this release. Reads are requested live rather than served from a persistent local index; Notion's own indexing and access rules still apply.

## Offline verification

From the repository root:

```sh
python3 -B -m unittest discover -s tests -v
python3 -B scripts/smoke_test_read_side.py
python3 -B scripts/smoke_test_stdio.py
```

The default tests and smoke checks use synthetic configuration and prevent Notion network access. They exercise both read and write behavior with mocked HTTP responses. No integration token is needed. CI runs the offline suite and protocol smoke checks; it does not request an AI review or perform live Notion operations.

Live token/content validation requires the explicit `--live` flag. After choosing the intended configuration, `python3 -B scripts/smoke_test_stdio.py --live --validate-tokens` checks the configured tokens. To read selected content, also supply an explicit `--workspace` with `--query` or `--fetch-page`. These are live reads, outside CI; passing them does not verify every feature against a current Notion account.

## License

[MIT](LICENSE).
