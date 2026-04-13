#!/usr/bin/env python3
"""Minimal stdio MCP server for explicit multi-workspace Notion reads.

This server is intentionally read-only and implements:

- list_workspaces
- search
- fetch_page

Every Notion tool call requires an explicit workspace selector. Workspace
configuration is normalized around a workspace key list so the server can safely
support more than two workspaces without changing code.
"""

from __future__ import annotations

import json
import os
import re
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import error, parse, request


SERVER_NAME = "notion-multi-workspace"
SERVER_VERSION = "0.3.0"
NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
DOTENV_ENV_VAR = "NOTION_MULTI_WORKSPACE_ENV_FILE"
DOTENV_PATH = PLUGIN_ROOT / ".env"
WORKSPACE_KEYS_ENV_VAR = "NOTION_WORKSPACE_KEYS"
UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}"
)


class ConfigError(RuntimeError):
    """Raised when the plugin configuration is incomplete."""


class NotionApiError(RuntimeError):
    """Raised when the Notion API responds with an error."""


class McpProtocolError(RuntimeError):
    """Raised when a request is invalid."""


@dataclass(frozen=True)
class WorkspaceConfig:
    """Configured Notion workspace binding."""

    key: str
    name: str
    token: str
    extra_aliases: tuple[str, ...] = ()

    @property
    def aliases(self) -> tuple[str, ...]:
        candidates = {self.key, self.name.strip().lower(), slugify(self.name)}
        candidates.update(alias.strip().lower() for alias in self.extra_aliases if alias.strip())
        candidates.update(slugify(alias) for alias in self.extra_aliases if alias.strip())
        return tuple(sorted(alias for alias in candidates if alias))


EXPECTED_ENV_VARS = (WORKSPACE_KEYS_ENV_VAR,)


def slugify(value: str) -> str:
    """Normalize a value for selector matching."""

    normalized = re.sub(r"[^a-z0-9]+", "-", value.strip().lower())
    return normalized.strip("-")


def load_dotenv(path: Path) -> None:
    """Load a simple KEY=VALUE env file without external dependencies."""

    if not path.exists():
        return

    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if value and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def resolve_dotenv_path() -> Path:
    """Resolve the env file path, allowing an external override."""

    override = os.getenv(DOTENV_ENV_VAR)
    if override:
        return Path(override).expanduser()
    return DOTENV_PATH


def parse_workspace_keys(raw_value: str) -> list[str]:
    """Parse a comma-separated workspace key list."""

    keys: list[str] = []
    seen: set[str] = set()
    for chunk in raw_value.split(","):
        candidate = slugify(chunk)
        if not candidate:
            continue
        if candidate in seen:
            raise ConfigError(f"Duplicate workspace key '{candidate}' in {WORKSPACE_KEYS_ENV_VAR}.")
        seen.add(candidate)
        keys.append(candidate)
    if not keys:
        raise ConfigError(
            f"{WORKSPACE_KEYS_ENV_VAR} must list at least one workspace key."
        )
    return keys


def env_key_fragment(key: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", key.upper())


def workspace_name_env_var(key: str) -> str:
    return f"NOTION_WORKSPACE_{env_key_fragment(key)}_NAME"


def workspace_token_env_var(key: str) -> str:
    return f"NOTION_WORKSPACE_{env_key_fragment(key)}_TOKEN"


def workspace_aliases_env_var(key: str) -> str:
    return f"NOTION_WORKSPACE_{env_key_fragment(key)}_ALIASES"


def split_aliases(raw_value: str | None) -> tuple[str, ...]:
    if not raw_value:
        return ()
    aliases: list[str] = []
    seen: set[str] = set()
    for chunk in raw_value.split(","):
        alias = chunk.strip()
        if not alias:
            continue
        normalized = slugify(alias)
        if normalized in seen:
            continue
        seen.add(normalized)
        aliases.append(alias)
    return tuple(aliases)


def load_workspace_configs() -> dict[str, WorkspaceConfig]:
    """Load all configured workspace bindings from the normalized env model."""

    load_dotenv(resolve_dotenv_path())

    raw_keys = os.getenv(WORKSPACE_KEYS_ENV_VAR)
    if not raw_keys:
        raise ConfigError(
            "Missing environment variable NOTION_WORKSPACE_KEYS. "
            "Set it to a comma-separated list of workspace keys."
        )

    workspace_keys = parse_workspace_keys(raw_keys)
    configs: dict[str, WorkspaceConfig] = {}
    missing: list[str] = []
    alias_owners: dict[str, str] = {}

    for key in workspace_keys:
        name_var = workspace_name_env_var(key)
        token_var = workspace_token_env_var(key)
        name = os.getenv(name_var)
        token = os.getenv(token_var)
        if not name:
            missing.append(name_var)
        if not token:
            missing.append(token_var)
        if not name or not token:
            continue

        config = WorkspaceConfig(
            key=key,
            name=name,
            token=token,
            extra_aliases=split_aliases(os.getenv(workspace_aliases_env_var(key))),
        )

        for alias in config.aliases:
            owner = alias_owners.get(alias)
            if owner and owner != key:
                raise ConfigError(
                    f"Workspace selector alias '{alias}' is ambiguous between '{owner}' and '{key}'."
                )
            alias_owners[alias] = key

        configs[key] = config

    if missing:
        raise ConfigError("Missing environment variables: " + ", ".join(sorted(missing)))

    return configs


def resolve_workspace(
    selector: str | None, workspaces: dict[str, WorkspaceConfig]
) -> WorkspaceConfig:
    """Resolve a workspace selector to a configured binding."""

    if not selector:
        raise McpProtocolError(
            "Missing required 'workspace'. Use list_workspaces to see available names."
        )

    normalized = slugify(selector)
    for workspace in workspaces.values():
        if normalized in workspace.aliases:
            return workspace

    options = ", ".join(
        sorted(
            {workspace.key for workspace in workspaces.values()}
            | {workspace.name for workspace in workspaces.values()}
        )
    )
    raise McpProtocolError(
        f"Unknown workspace '{selector}'. Available selectors: {options}"
    )


def canonicalize_notion_id(raw_value: str) -> str:
    """Normalize a Notion UUID to the dashed format expected by the API."""

    match = UUID_RE.search(raw_value)
    if not match:
        raise McpProtocolError(
            "Could not find a Notion page ID in the provided value."
        )
    identifier = re.sub(r"[^0-9a-fA-F]", "", match.group(0)).lower()
    return (
        f"{identifier[0:8]}-"
        f"{identifier[8:12]}-"
        f"{identifier[12:16]}-"
        f"{identifier[16:20]}-"
        f"{identifier[20:32]}"
    )


def rich_text_plain(rich_text: list[dict[str, Any]]) -> str:
    """Extract plain text from Notion rich text fragments."""

    return "".join(fragment.get("plain_text", "") for fragment in rich_text)


def format_parent(parent: dict[str, Any]) -> dict[str, Any]:
    """Return a compact parent summary."""

    parent_type = parent.get("type", "unknown")
    summary: dict[str, Any] = {"type": parent_type}
    for key in (
        "page_id",
        "database_id",
        "workspace",
        "block_id",
        "data_source_id",
    ):
        if key in parent:
            summary[key] = parent[key]
    return summary


def simplify_user(user: dict[str, Any]) -> str:
    """Return a human-friendly user label."""

    return user.get("name") or user.get("id", "unknown-user")


def simplify_property_value(prop: dict[str, Any]) -> Any:
    """Reduce Notion property payloads to plain JSON-friendly values."""

    prop_type = prop.get("type")
    if prop_type == "title":
        return rich_text_plain(prop.get("title", []))
    if prop_type == "rich_text":
        return rich_text_plain(prop.get("rich_text", []))
    if prop_type == "number":
        return prop.get("number")
    if prop_type == "select":
        selected = prop.get("select")
        return selected.get("name") if selected else None
    if prop_type == "status":
        status = prop.get("status")
        return status.get("name") if status else None
    if prop_type == "multi_select":
        return [item.get("name") for item in prop.get("multi_select", [])]
    if prop_type == "date":
        return prop.get("date")
    if prop_type == "checkbox":
        return prop.get("checkbox")
    if prop_type == "people":
        return [simplify_user(person) for person in prop.get("people", [])]
    if prop_type == "relation":
        return [item.get("id") for item in prop.get("relation", [])]
    if prop_type == "url":
        return prop.get("url")
    if prop_type == "email":
        return prop.get("email")
    if prop_type == "phone_number":
        return prop.get("phone_number")
    if prop_type == "created_time":
        return prop.get("created_time")
    if prop_type == "last_edited_time":
        return prop.get("last_edited_time")
    if prop_type == "created_by":
        return simplify_user(prop.get("created_by", {}))
    if prop_type == "last_edited_by":
        return simplify_user(prop.get("last_edited_by", {}))
    if prop_type == "formula":
        formula = prop.get("formula", {})
        inner_type = formula.get("type")
        if inner_type:
            return formula.get(inner_type)
        return formula
    if prop_type == "files":
        files: list[dict[str, Any]] = []
        for file_value in prop.get("files", []):
            entry = {"name": file_value.get("name")}
            file_type = file_value.get("type")
            if file_type and file_type in file_value:
                entry["url"] = file_value[file_type].get("url")
            files.append(entry)
        return files
    if prop_type == "rollup":
        rollup = prop.get("rollup", {})
        inner_type = rollup.get("type")
        if inner_type == "array":
            return rollup.get("array", [])
        return rollup.get(inner_type)
    return prop.get(prop_type) if prop_type else prop


def extract_page_title(page: dict[str, Any]) -> str:
    """Find the display title for a Notion page payload."""

    properties = page.get("properties", {})
    for value in properties.values():
        if value.get("type") == "title":
            title = rich_text_plain(value.get("title", []))
            if title:
                return title
    return "Untitled"


def extract_result_title(result: dict[str, Any]) -> str:
    """Find the display title for a search result."""

    if result.get("object") == "page":
        return extract_page_title(result)
    title = rich_text_plain(result.get("title", []))
    return title or "Untitled"


def render_block_markdown(block: dict[str, Any], indent: int = 0) -> list[str]:
    """Render a Notion block to markdown-ish text."""

    block_type = block.get("type", "unsupported")
    payload = block.get(block_type, {})
    prefix = "  " * indent

    if block_type == "paragraph":
        text = rich_text_plain(payload.get("rich_text", []))
        return [prefix + text] if text else []
    if block_type == "heading_1":
        return [prefix + "# " + rich_text_plain(payload.get("rich_text", []))]
    if block_type == "heading_2":
        return [prefix + "## " + rich_text_plain(payload.get("rich_text", []))]
    if block_type == "heading_3":
        return [prefix + "### " + rich_text_plain(payload.get("rich_text", []))]
    if block_type == "bulleted_list_item":
        return [prefix + "- " + rich_text_plain(payload.get("rich_text", []))]
    if block_type == "numbered_list_item":
        return [prefix + "1. " + rich_text_plain(payload.get("rich_text", []))]
    if block_type == "to_do":
        checked = "x" if payload.get("checked") else " "
        return [prefix + f"- [{checked}] " + rich_text_plain(payload.get("rich_text", []))]
    if block_type == "toggle":
        return [prefix + "- " + rich_text_plain(payload.get("rich_text", []))]
    if block_type == "quote":
        return [prefix + "> " + rich_text_plain(payload.get("rich_text", []))]
    if block_type == "callout":
        return [prefix + "Callout: " + rich_text_plain(payload.get("rich_text", []))]
    if block_type == "code":
        text = rich_text_plain(payload.get("rich_text", []))
        language = payload.get("language") or "plain text"
        return [prefix + f"```{language}", text, prefix + "```"]
    if block_type == "divider":
        return [prefix + "---"]
    if block_type == "bookmark":
        return [prefix + "Bookmark: " + (payload.get("url") or "")]
    if block_type == "child_page":
        return [prefix + "Child page: " + payload.get("title", "Untitled child page")]
    if block_type == "table_of_contents":
        return [prefix + "[Table of contents]"]

    text = rich_text_plain(payload.get("rich_text", [])) if isinstance(payload, dict) else ""
    if text:
        return [prefix + text]
    return [prefix + f"[{block_type}]"]


class NotionClient:
    """Thin stdlib wrapper around the Notion REST API."""

    def __init__(self, workspace: WorkspaceConfig) -> None:
        self.workspace = workspace

    def request_json(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        body = None
        headers = {
            "Authorization": f"Bearer {self.workspace.token}",
            "Notion-Version": NOTION_VERSION,
            "Accept": "application/json",
        }
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = request.Request(
            NOTION_API_BASE + path,
            data=body,
            headers=headers,
            method=method,
        )

        try:
            with request.urlopen(req) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except error.HTTPError as exc:
            raw = exc.read().decode("utf-8")
            message = raw
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                message = parsed.get("message") or parsed.get("code") or raw
            raise NotionApiError(
                f"{method} {path} failed for workspace '{self.workspace.name}': "
                f"{exc.code} {message}"
            ) from exc
        except error.URLError as exc:
            raise NotionApiError(
                f"Could not reach Notion for workspace '{self.workspace.name}': {exc.reason}"
            ) from exc

    def get_self(self) -> dict[str, Any]:
        return self.request_json("GET", "/users/me")

    def search(
        self,
        query: str,
        page_size: int = 10,
        result_type: str = "page",
        start_cursor: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "query": query,
            "page_size": max(1, min(page_size, 100)),
        }
        if result_type in {"page", "database"}:
            payload["filter"] = {"property": "object", "value": result_type}
        if start_cursor:
            payload["start_cursor"] = start_cursor
        return self.request_json("POST", "/search", payload)

    def get_page(self, page_id_or_url: str) -> dict[str, Any]:
        page_id = canonicalize_notion_id(page_id_or_url)
        return self.request_json("GET", f"/pages/{page_id}")

    def list_block_children(
        self, block_id: str, block_limit: int = 200
    ) -> list[dict[str, Any]]:
        collected: list[dict[str, Any]] = []
        next_cursor: str | None = None

        while len(collected) < block_limit:
            query = {"page_size": min(100, block_limit - len(collected))}
            if next_cursor:
                query["start_cursor"] = next_cursor
            path = f"/blocks/{block_id}/children"
            if query:
                path += "?" + parse.urlencode(query)
            response = self.request_json("GET", path)
            results = response.get("results", [])
            if not isinstance(results, list):
                break
            collected.extend(results)
            if not response.get("has_more"):
                break
            next_cursor = response.get("next_cursor")
            if not next_cursor:
                break

        return collected[:block_limit]


def recurse_blocks_to_markdown(
    client: NotionClient,
    block_id: str,
    max_blocks: int = 200,
    indent: int = 0,
) -> tuple[list[str], int]:
    """Recursively render a page's block tree up to a block limit."""

    lines: list[str] = []
    consumed = 0
    for block in client.list_block_children(block_id, block_limit=max_blocks):
        if consumed >= max_blocks:
            break
        lines.extend(render_block_markdown(block, indent=indent))
        consumed += 1
        if block.get("has_children") and consumed < max_blocks:
            child_lines, child_consumed = recurse_blocks_to_markdown(
                client,
                block.get("id", ""),
                max_blocks=max_blocks - consumed,
                indent=indent + 1,
            )
            lines.extend(child_lines)
            consumed += child_consumed
    return lines, consumed


def build_search_summary(
    workspace: WorkspaceConfig,
    response: dict[str, Any],
    query: str,
    result_type: str,
) -> dict[str, Any]:
    """Reduce a Notion search response to a smaller summary payload."""

    summarized_results = []
    for result in response.get("results", []):
        summarized_results.append(
            {
                "object": result.get("object"),
                "id": result.get("id"),
                "title": extract_result_title(result),
                "url": result.get("url"),
                "parent": format_parent(result.get("parent", {})),
                "last_edited_time": result.get("last_edited_time"),
            }
        )

    return {
        "workspace": workspace.name,
        "workspace_key": workspace.key,
        "query": query,
        "result_type": result_type,
        "count": len(summarized_results),
        "has_more": bool(response.get("has_more")),
        "next_cursor": response.get("next_cursor"),
        "results": summarized_results,
    }


def build_page_summary(
    workspace: WorkspaceConfig,
    page: dict[str, Any],
    content_markdown: str | None,
    rendered_block_count: int,
) -> dict[str, Any]:
    """Reduce a Notion page response plus content into a single payload."""

    simplified_properties = {
        key: simplify_property_value(value)
        for key, value in page.get("properties", {}).items()
    }
    return {
        "workspace": workspace.name,
        "workspace_key": workspace.key,
        "page": {
            "id": page.get("id"),
            "title": extract_page_title(page),
            "url": page.get("url"),
            "created_time": page.get("created_time"),
            "last_edited_time": page.get("last_edited_time"),
            "archived": page.get("archived"),
            "in_trash": page.get("in_trash"),
            "parent": format_parent(page.get("parent", {})),
            "properties": simplified_properties,
        },
        "rendered_block_count": rendered_block_count,
        "content_markdown": content_markdown,
    }


def make_tool_text(payload: dict[str, Any]) -> dict[str, Any]:
    """Wrap a tool response as MCP text content."""

    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload, indent=2, sort_keys=True),
            }
        ]
    }


def tool_list_workspaces(arguments: dict[str, Any]) -> dict[str, Any]:
    """Return the configured workspaces and optional token validation."""

    workspaces = load_workspace_configs()
    validate = bool(arguments.get("validate_tokens", False))
    summaries = []
    for workspace in workspaces.values():
        summary = {
            "key": workspace.key,
            "name": workspace.name,
            "aliases": sorted(workspace.aliases),
        }
        if validate:
            try:
                user = NotionClient(workspace).get_self()
                summary["token_status"] = "ok"
                summary["bot_user_id"] = user.get("id")
                summary["bot_name"] = user.get("name")
                summary["bot_type"] = user.get("type")
            except Exception as exc:  # noqa: BLE001
                summary["token_status"] = "error"
                summary["error"] = str(exc)
        summaries.append(summary)
    return {
        "workspace_count": len(summaries),
        "workspaces": summaries,
        "read_only_tools": ["list_workspaces", "search", "fetch_page"],
    }


def tool_search(arguments: dict[str, Any]) -> dict[str, Any]:
    """Search a specific configured Notion workspace."""

    workspaces = load_workspace_configs()
    workspace = resolve_workspace(arguments.get("workspace"), workspaces)
    query = (arguments.get("query") or "").strip()
    if not query:
        raise McpProtocolError("search requires a non-empty 'query'.")

    page_size = int(arguments.get("page_size", 10))
    result_type = str(arguments.get("result_type", "page")).strip().lower() or "page"
    if result_type not in {"page", "database", "all"}:
        raise McpProtocolError(
            "search 'result_type' must be one of: page, database, all."
        )

    client = NotionClient(workspace)
    response = client.search(
        query=query,
        page_size=page_size,
        result_type=result_type,
        start_cursor=arguments.get("start_cursor"),
    )
    return build_search_summary(workspace, response, query, result_type)


def tool_fetch_page(arguments: dict[str, Any]) -> dict[str, Any]:
    """Fetch a page and optionally render its content."""

    workspaces = load_workspace_configs()
    workspace = resolve_workspace(arguments.get("workspace"), workspaces)
    page_id_or_url = arguments.get("page_id_or_url") or arguments.get("page")
    if not page_id_or_url:
        raise McpProtocolError(
            "fetch_page requires 'page_id_or_url' with a page UUID or Notion URL."
        )

    include_content = bool(arguments.get("include_content", True))
    block_limit = int(arguments.get("block_limit", 200))
    block_limit = max(1, min(block_limit, 500))

    client = NotionClient(workspace)
    page = client.get_page(str(page_id_or_url))

    content_markdown = None
    rendered_block_count = 0
    if include_content:
        lines, rendered_block_count = recurse_blocks_to_markdown(
            client,
            page.get("id", ""),
            max_blocks=block_limit,
        )
        content_markdown = "\n".join(line for line in lines if line is not None).strip()

    return build_page_summary(
        workspace=workspace,
        page=page,
        content_markdown=content_markdown,
        rendered_block_count=rendered_block_count,
    )


TOOLS: dict[str, dict[str, Any]] = {
    "list_workspaces": {
        "description": (
            "List the configured Notion workspaces and optional token health."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "validate_tokens": {
                    "type": "boolean",
                    "description": (
                        "When true, call Notion for each workspace to verify the token."
                    ),
                    "default": False,
                }
            },
            "additionalProperties": False,
        },
        "handler": tool_list_workspaces,
    },
    "search": {
        "description": (
            "Search one configured Notion workspace. The workspace selector is required."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {
                    "type": "string",
                    "description": (
                        "Workspace selector for one configured workspace or alias."
                    ),
                },
                "query": {
                    "type": "string",
                    "description": "Search query for Notion content.",
                },
                "page_size": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 10,
                },
                "result_type": {
                    "type": "string",
                    "enum": ["page", "database", "all"],
                    "default": "page",
                },
                "start_cursor": {
                    "type": "string",
                    "description": "Optional cursor for the next search page.",
                },
            },
            "required": ["workspace", "query"],
            "additionalProperties": False,
        },
        "handler": tool_search,
    },
    "fetch_page": {
        "description": (
            "Fetch a single page from one configured Notion workspace and render "
            "its content to markdown-like text."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {
                    "type": "string",
                    "description": (
                        "Workspace selector for one configured workspace or alias."
                    ),
                },
                "page_id_or_url": {
                    "type": "string",
                    "description": "The Notion page UUID or page URL to fetch.",
                },
                "include_content": {
                    "type": "boolean",
                    "default": True,
                },
                "block_limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 500,
                    "default": 200,
                },
            },
            "required": ["workspace", "page_id_or_url"],
            "additionalProperties": False,
        },
        "handler": tool_fetch_page,
    },
}


def tool_descriptors() -> list[dict[str, Any]]:
    """Return MCP tool descriptors without local handler functions."""

    descriptors = []
    for name, tool in TOOLS.items():
        descriptors.append(
            {
                "name": name,
                "description": tool["description"],
                "inputSchema": tool["inputSchema"],
            }
        )
    return descriptors


def handle_request(message: dict[str, Any]) -> dict[str, Any] | None:
    """Process one JSON-RPC request."""

    method = message.get("method")
    params = message.get("params", {})
    request_id = message.get("id")

    if method == "notifications/initialized":
        return None
    if method == "ping":
        return success_response(request_id, {})
    if method == "initialize":
        client_protocol = params.get("protocolVersion") or "2024-11-05"
        return success_response(
            request_id,
            {
                "protocolVersion": client_protocol,
                "capabilities": {
                    "tools": {
                        "listChanged": False,
                    }
                },
                "serverInfo": {
                    "name": SERVER_NAME,
                    "version": SERVER_VERSION,
                },
            },
        )
    if method == "tools/list":
        return success_response(request_id, {"tools": tool_descriptors()})
    if method == "tools/call":
        tool_name = params.get("name")
        arguments = params.get("arguments") or {}
        if tool_name not in TOOLS:
            return error_response(request_id, -32601, f"Unknown tool '{tool_name}'.")
        try:
            payload = TOOLS[tool_name]["handler"](arguments)
            return success_response(request_id, make_tool_text(payload))
        except (ConfigError, McpProtocolError, NotionApiError) as exc:
            return success_response(
                request_id,
                {
                    "content": [{"type": "text", "text": str(exc)}],
                    "isError": True,
                },
            )
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(traceback.format_exc())
            return error_response(request_id, -32000, str(exc))

    return error_response(request_id, -32601, f"Method '{method}' not found.")


def success_response(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    """Build a JSON-RPC success response."""

    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def error_response(request_id: Any, code: int, message: str) -> dict[str, Any]:
    """Build a JSON-RPC error response."""

    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def read_message() -> dict[str, Any] | None:
    """Read one Content-Length framed JSON-RPC message from stdin."""

    headers: dict[str, str] = {}
    while True:
        raw_line = sys.stdin.buffer.readline()
        if not raw_line:
            return None
        line = raw_line.decode("utf-8").strip()
        if not line:
            break
        if ":" not in line:
            raise McpProtocolError(f"Malformed header line: {line}")
        name, value = line.split(":", 1)
        headers[name.lower()] = value.strip()

    if "content-length" not in headers:
        raise McpProtocolError("Missing Content-Length header.")
    length = int(headers["content-length"])
    body = sys.stdin.buffer.read(length)
    if not body:
        return None
    return json.loads(body.decode("utf-8"))


def write_message(message: dict[str, Any]) -> None:
    """Write one Content-Length framed JSON-RPC response to stdout."""

    body = json.dumps(message).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8")
    sys.stdout.buffer.write(header)
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()


def serve_forever() -> int:
    """Run the MCP stdio server loop."""

    try:
        while True:
            message = read_message()
            if message is None:
                return 0
            response = handle_request(message)
            if response is not None and message.get("id") is not None:
                write_message(response)
    except KeyboardInterrupt:
        return 0
    except Exception:  # noqa: BLE001
        sys.stderr.write(traceback.format_exc())
        return 1


def main() -> int:
    """Entrypoint for the stdio server."""

    return serve_forever()


if __name__ == "__main__":
    raise SystemExit(main())
