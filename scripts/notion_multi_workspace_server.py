#!/usr/bin/env python3
"""Minimal stdio MCP server for explicit multi-workspace Notion access.

This server implements:

- list_workspaces
- search
- fetch_page
- fetch_database
- query_database
- create_page
- append_block_children

Every Notion tool call requires an explicit workspace selector. Workspace
configuration is normalized around a workspace key list so the server can safely
support more than two workspaces without changing code.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import time
from http.client import HTTPException
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import error, parse, request


SERVER_NAME = "notion-multi-workspace"
SERVER_VERSION = "0.3.1"
SUPPORTED_PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
HTTP_TIMEOUT_SECONDS = 15
READ_MAX_ATTEMPTS = 3
MAX_RETRY_DELAY_SECONDS = 5
RETRYABLE_HTTP_STATUSES = {429, 500, 502, 503, 504, 529}
MAX_BLOCK_REQUESTS = 100
MAX_BLOCK_DEPTH = 50
MAX_MESSAGE_BYTES = 4 * 1024 * 1024
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

        # POST search/query are reads; every other POST/PATCH is attempted once.
        semantic_read = method == "GET" or (
            method == "POST"
            and (path == "/search" or re.fullmatch(r"/databases/[^/]+/query", path))
        )
        attempts = READ_MAX_ATTEMPTS if semantic_read else 1
        for attempt in range(attempts):
            try:
                with request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as response:
                    raw = response.read().decode("utf-8")
                    parsed = json.loads(raw)
                    if not isinstance(parsed, dict):
                        raise ValueError("Expected a JSON object")
                    return parsed
            except error.HTTPError as exc:
                retry_delay = min(2 ** attempt, MAX_RETRY_DELAY_SECONDS)
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                if retry_after is not None:
                    try:
                        retry_delay = float(retry_after)
                        if not math.isfinite(retry_delay) or retry_delay < 0:
                            retry_delay = MAX_RETRY_DELAY_SECONDS + 1
                    except ValueError:
                        # Do not retry early when we cannot interpret the server's delay.
                        retry_delay = MAX_RETRY_DELAY_SECONDS + 1
                status = exc.code
                blocked = False
                try:
                    error_body = json.loads(exc.read(65536).decode("utf-8"))
                    blocked = isinstance(error_body, dict) and error_body.get("code") == "public_api_request_blocked"
                except (ValueError, UnicodeError, OSError, HTTPException):
                    # An unreadable 429 body might be a permanent block, not a rate limit.
                    blocked = status == 429
                finally:
                    exc.close()
                if (attempt + 1 < attempts and status in RETRYABLE_HTTP_STATUSES
                        and not blocked and retry_delay <= MAX_RETRY_DELAY_SECONDS):
                    time.sleep(retry_delay)
                    continue
                suffix = " Write outcome may be unknown; inspect Notion before retrying." if not semantic_read and status >= 500 else ""
                raise NotionApiError(
                    f"Notion returned HTTP {status} for workspace '{self.workspace.name}'.{suffix}"
                ) from exc
            except (error.URLError, OSError, HTTPException) as exc:
                if attempt + 1 < attempts:
                    time.sleep(min(2 ** attempt, MAX_RETRY_DELAY_SECONDS))
                    continue
                suffix = " Write outcome may be unknown; inspect Notion before retrying." if not semantic_read else ""
                raise NotionApiError(
                    f"Notion transport failed for workspace '{self.workspace.name}'.{suffix}"
                ) from exc
            except (ValueError, UnicodeError) as exc:
                suffix = " Write outcome may be unknown; inspect Notion before retrying." if not semantic_read else ""
                raise NotionApiError(f"Notion returned an invalid JSON object.{suffix}") from exc
        raise AssertionError("Unreachable retry state")

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

    def get_database(self, database_id_or_url: str) -> dict[str, Any]:
        database_id = canonicalize_notion_id(database_id_or_url)
        return self.request_json("GET", f"/databases/{database_id}")

    def query_database(
        self,
        database_id_or_url: str,
        page_size: int = 10,
        start_cursor: str | None = None,
        filter_payload: dict[str, Any] | None = None,
        sorts: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        database_id = canonicalize_notion_id(database_id_or_url)
        payload: dict[str, Any] = {
            "page_size": max(1, min(page_size, 100)),
        }
        if start_cursor:
            payload["start_cursor"] = start_cursor
        if filter_payload:
            payload["filter"] = filter_payload
        if sorts:
            payload["sorts"] = sorts
        return self.request_json("POST", f"/databases/{database_id}/query", payload)

    def create_page(
        self,
        parent: dict[str, Any],
        properties: dict[str, Any],
        children: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "parent": parent,
            "properties": properties,
        }
        if children:
            payload["children"] = children
        return self.request_json("POST", "/pages", payload)

    def append_block_children(
        self,
        block_id_or_url: str,
        children: list[dict[str, Any]],
    ) -> dict[str, Any]:
        block_id = canonicalize_notion_id(block_id_or_url)
        return self.request_json("PATCH", f"/blocks/{block_id}/children", {"children": children})

    def list_block_children(
        self, block_id: str, block_limit: int = 200,
        request_budget: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        """Collect siblings with explicit exhaustion and a shared request budget."""

        if request_budget is None:
            request_budget = {"remaining": MAX_BLOCK_REQUESTS}
        collected: list[dict[str, Any]] = []
        next_cursor: str | None = None
        seen_cursors: set[str] = set()
        warnings: list[str] = []
        reasons: list[str] = []
        complete = False
        while len(collected) < block_limit:
            if request_budget["remaining"] <= 0:
                reasons.append("request_limit")
                break
            query = {"page_size": min(100, block_limit - len(collected))}
            if next_cursor:
                query["start_cursor"] = next_cursor
            request_budget["remaining"] -= 1
            response = self.request_json(
                "GET", f"/blocks/{block_id}/children?" + parse.urlencode(query)
            )
            results = response.get("results")
            if not isinstance(results, list) or any(not isinstance(item, dict) for item in results):
                reasons.append("invalid_results")
                warnings.append("Notion returned malformed block results.")
                break
            remaining = block_limit - len(collected)
            collected.extend(results[:remaining])
            if len(results) > remaining:
                reasons.append("block_limit")
            status = collection_status(response, next_cursor, seen_cursors)
            warnings.extend(status["warnings"])
            if status["warnings"]:
                reasons.append("invalid_pagination")
                break
            if not response["has_more"]:
                complete = not reasons
                break
            next_cursor = response["next_cursor"]
            seen_cursors.add(next_cursor)
        if not complete and not reasons:
            reasons.append("block_limit")
        return {"results": collected, "complete": complete,
                "truncation_reasons": reasons, "warnings": warnings}


def collection_status(
    response: dict[str, Any], start_cursor: str | None = None,
    seen_cursors: set[str] | None = None,
) -> dict[str, Any]:
    """Describe collection exhaustion, never silently trusting a broken cursor."""

    warnings: list[str] = []
    has_more = response.get("has_more")
    cursor = response.get("next_cursor")
    if not isinstance(has_more, bool):
        warnings.append("Notion returned missing or invalid has_more.")
    elif has_more:
        if not isinstance(cursor, str) or not cursor.strip():
            warnings.append("Notion returned has_more without a valid next_cursor.")
        elif cursor == start_cursor or cursor in (seen_cursors or set()):
            warnings.append("Notion repeated a pagination cursor.")
    elif cursor is not None:
        warnings.append("Notion returned next_cursor while has_more is false.")
    return {"scope": "from_cursor" if start_cursor is not None else "from_start",
            "complete": start_cursor is None and has_more is False and not warnings,
            "warnings": warnings}


RENDERED_BLOCK_TYPES = {
    "paragraph", "heading_1", "heading_2", "heading_3", "bulleted_list_item",
    "numbered_list_item", "to_do", "toggle", "quote", "callout", "code",
    "divider", "bookmark", "child_page", "table_of_contents",
}


def recurse_blocks_to_markdown(
    client: NotionClient, block_id: str, max_blocks: int = 200, indent: int = 0,
) -> tuple[list[str], int, dict[str, Any]]:
    """Render a bounded block tree; completeness refers to traversal, not fidelity."""

    lines: list[str] = []
    consumed = 0
    reasons: set[str] = set()
    warnings = {"Markdown is a lossy rendering; formatting, metadata, and some block payloads are omitted."}
    request_budget = {"remaining": MAX_BLOCK_REQUESTS}
    active_ids: set[str] = set()

    def visit(parent_id: str, depth: int) -> None:
        nonlocal consumed
        if depth >= MAX_BLOCK_DEPTH:
            reasons.add("depth_limit")
            return
        if parent_id in active_ids:
            reasons.add("block_cycle")
            return
        active_ids.add(parent_id)
        collection = client.list_block_children(
            parent_id, block_limit=max_blocks - consumed, request_budget=request_budget,
        )
        reasons.update(collection["truncation_reasons"])
        warnings.update(collection["warnings"])
        blocks = collection["results"]
        for block in blocks:
            if consumed >= max_blocks:
                reasons.add("block_limit")
                break
            lines.extend(render_block_markdown(block, indent=indent + depth))
            consumed += 1
            block_type = block.get("type", "unsupported")
            if block_type not in RENDERED_BLOCK_TYPES:
                warnings.add(f"Block type '{block_type}' is represented only as a placeholder or plain text.")
            if block_type == "child_page":
                warnings.add("Child-page bodies are not fetched; fetch those pages separately.")
            if block.get("has_children"):
                if consumed >= max_blocks:
                    reasons.add("block_limit")
                elif not isinstance(block.get("id"), str) or not block["id"]:
                    reasons.add("missing_block_id")
                else:
                    visit(block["id"], depth + 1)
        active_ids.remove(parent_id)

    visit(block_id, 0)
    status = {"requested": True, "complete": not reasons, "truncated": bool(reasons),
              "truncation_reasons": sorted(reasons), "warnings": sorted(warnings),
              "rendering": "lossy_markdown"}
    return lines, consumed, status


def property_status(page: dict[str, Any]) -> dict[str, Any]:
    """Surface known truncation without claiming page properties are hydrated."""

    has_more = [name for name, value in page.get("properties", {}).items()
                if value.get("has_more") is True]
    warnings = ["Properties are simplified from the page response; property-item pagination is not fetched and completeness is not verified."]
    if has_more:
        warnings.append("Notion reports additional values for: " + ", ".join(has_more))
    return {"complete": False if has_more else None, "has_more": has_more, "warnings": warnings}


def response_results(response: dict[str, Any]) -> list[dict[str, Any]]:
    results = response.get("results")
    if not isinstance(results, list) or any(not isinstance(item, dict) for item in results):
        raise NotionApiError("Notion returned malformed collection results.")
    return results


def build_search_summary(
    workspace: WorkspaceConfig,
    response: dict[str, Any],
    query: str,
    result_type: str,
    start_cursor: str | None = None,
) -> dict[str, Any]:
    """Reduce a Notion search response to a smaller summary payload."""

    summarized_results = []
    for result in response_results(response):
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
        "has_more": response.get("has_more"),
        "collection": collection_status(response, start_cursor),
        "next_cursor": response.get("next_cursor"),
        "results": summarized_results,
    }


def build_page_summary(
    workspace: WorkspaceConfig,
    page: dict[str, Any],
    content_markdown: str | None,
    rendered_block_count: int,
    content_status: dict[str, Any] | None = None,
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
            "property_status": property_status(page),
        },
        "content_status": content_status or {
            "requested": False, "complete": None, "truncated": False,
            "truncation_reasons": [], "warnings": [], "rendering": "lossy_markdown",
        },
        "rendered_block_count": rendered_block_count,
        "content_markdown": content_markdown,
    }


def build_database_summary(workspace: WorkspaceConfig, database: dict[str, Any]) -> dict[str, Any]:
    properties = database.get("properties", {})
    simplified_properties = {
        key: {
            "type": value.get("type"),
            "id": value.get("id"),
        }
        for key, value in properties.items()
    }
    return {
        "workspace": workspace.name,
        "workspace_key": workspace.key,
        "database": {
            "id": database.get("id"),
            "title": rich_text_plain(database.get("title", [])) or "Untitled",
            "url": database.get("url"),
            "created_time": database.get("created_time"),
            "last_edited_time": database.get("last_edited_time"),
            "archived": database.get("archived"),
            "in_trash": database.get("in_trash"),
            "parent": format_parent(database.get("parent", {})),
            "properties": simplified_properties,
        },
    }


def build_database_query_summary(
    workspace: WorkspaceConfig,
    database: dict[str, Any],
    response: dict[str, Any],
    start_cursor: str | None = None,
) -> dict[str, Any]:
    summarized_results = []
    for result in response_results(response):
        if result.get("object") != "page":
            raise NotionApiError("Notion database query returned a non-page result.")
        summarized_results.append(
            {
                "id": result.get("id"),
                "title": extract_page_title(result),
                "url": result.get("url"),
                "last_edited_time": result.get("last_edited_time"),
                "parent": format_parent(result.get("parent", {})),
                "property_status": property_status(result),
                "properties": {
                    key: simplify_property_value(value)
                    for key, value in result.get("properties", {}).items()
                },
            }
        )
    return {
        "workspace": workspace.name,
        "workspace_key": workspace.key,
        "database": {
            "id": database.get("id"),
            "title": rich_text_plain(database.get("title", [])) or "Untitled",
            "url": database.get("url"),
        },
        "count": len(summarized_results),
        "has_more": response.get("has_more"),
        "collection": collection_status(response, start_cursor),
        "next_cursor": response.get("next_cursor"),
        "results": summarized_results,
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
        "read_only_tools": [
            "list_workspaces",
            "search",
            "fetch_page",
            "fetch_database",
            "query_database",
        ],
        "write_tools": ["create_page", "append_block_children"],
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
    return build_search_summary(workspace, response, query, result_type, arguments.get("start_cursor"))


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
    content_status = None
    if include_content:
        lines, rendered_block_count, content_status = recurse_blocks_to_markdown(
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
        content_status=content_status,
    )


def tool_fetch_database(arguments: dict[str, Any]) -> dict[str, Any]:
    workspaces = load_workspace_configs()
    workspace = resolve_workspace(arguments.get("workspace"), workspaces)
    database_id_or_url = arguments.get("database_id_or_url") or arguments.get("database")
    if not database_id_or_url:
        raise McpProtocolError(
            "fetch_database requires 'database_id_or_url' with a database UUID or Notion URL."
        )
    client = NotionClient(workspace)
    database = client.get_database(str(database_id_or_url))
    return build_database_summary(workspace, database)


def tool_query_database(arguments: dict[str, Any]) -> dict[str, Any]:
    workspaces = load_workspace_configs()
    workspace = resolve_workspace(arguments.get("workspace"), workspaces)
    database_id_or_url = arguments.get("database_id_or_url") or arguments.get("database")
    if not database_id_or_url:
        raise McpProtocolError(
            "query_database requires 'database_id_or_url' with a database UUID or Notion URL."
        )
    page_size = int(arguments.get("page_size", 10))
    filter_payload = arguments.get("filter")
    sorts = arguments.get("sorts")
    if filter_payload is not None and not isinstance(filter_payload, dict):
        raise McpProtocolError("query_database 'filter' must be an object.")
    if sorts is not None and not isinstance(sorts, list):
        raise McpProtocolError("query_database 'sorts' must be an array.")
    client = NotionClient(workspace)
    database = client.get_database(str(database_id_or_url))
    response = client.query_database(
        database_id_or_url=str(database_id_or_url),
        page_size=page_size,
        start_cursor=arguments.get("start_cursor"),
        filter_payload=filter_payload,
        sorts=sorts,
    )
    return build_database_query_summary(workspace, database, response, arguments.get("start_cursor"))


def tool_create_page(arguments: dict[str, Any]) -> dict[str, Any]:
    workspaces = load_workspace_configs()
    workspace = resolve_workspace(arguments.get("workspace"), workspaces)
    parent = arguments.get("parent")
    properties = arguments.get("properties")
    children = arguments.get("children")
    if not isinstance(parent, dict):
        raise McpProtocolError("create_page requires 'parent' as an object.")
    if not isinstance(properties, dict):
        raise McpProtocolError("create_page requires 'properties' as an object.")
    if children is not None and not isinstance(children, list):
        raise McpProtocolError("create_page 'children' must be an array when provided.")
    client = NotionClient(workspace)
    page = client.create_page(parent=parent, properties=properties, children=children)
    return build_page_summary(workspace, page, content_markdown=None, rendered_block_count=0)


def tool_append_block_children(arguments: dict[str, Any]) -> dict[str, Any]:
    workspaces = load_workspace_configs()
    workspace = resolve_workspace(arguments.get("workspace"), workspaces)
    block_id_or_url = arguments.get("block_id_or_url") or arguments.get("page_id_or_url") or arguments.get("block")
    children = arguments.get("children")
    if not block_id_or_url:
        raise McpProtocolError("append_block_children requires 'block_id_or_url'.")
    if not isinstance(children, list) or not children:
        raise McpProtocolError("append_block_children requires a non-empty 'children' array.")
    client = NotionClient(workspace)
    response = client.append_block_children(str(block_id_or_url), children)
    return {
        "workspace": workspace.name,
        "workspace_key": workspace.key,
        "appended_count": len(response.get("results", [])),
        "results": response.get("results", []),
    }


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
    "fetch_database": {
        "description": "Fetch one Notion database from one configured workspace.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {
                    "type": "string",
                    "description": "Workspace selector for one configured workspace or alias.",
                },
                "database_id_or_url": {
                    "type": "string",
                    "description": "The Notion database UUID or database URL to fetch.",
                },
            },
            "required": ["workspace", "database_id_or_url"],
            "additionalProperties": False,
        },
        "handler": tool_fetch_database,
    },
    "query_database": {
        "description": "Query one Notion database in one configured workspace.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {"type": "string", "description": "Workspace selector for one configured workspace or alias."},
                "database_id_or_url": {"type": "string", "description": "The Notion database UUID or URL to query."},
                "page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 10},
                "start_cursor": {"type": "string", "description": "Optional cursor for the next query page."},
                "filter": {"type": "object", "description": "Optional Notion database query filter object."},
                "sorts": {"type": "array", "description": "Optional Notion database query sorts array."},
            },
            "required": ["workspace", "database_id_or_url"],
            "additionalProperties": False,
        },
        "handler": tool_query_database,
    },
    "create_page": {
        "description": "Create one Notion page in one configured workspace under an explicit parent.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {"type": "string", "description": "Workspace selector for one configured workspace or alias."},
                "parent": {"type": "object", "description": "Notion parent object, for example {\"page_id\": ...} or {\"database_id\": ...}."},
                "properties": {"type": "object", "description": "Notion page properties payload."},
                "children": {"type": "array", "description": "Optional initial child block payloads."},
            },
            "required": ["workspace", "parent", "properties"],
            "additionalProperties": False,
        },
        "handler": tool_create_page,
    },
    "append_block_children": {
        "description": "Append child blocks to one page or block in one configured workspace.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {"type": "string", "description": "Workspace selector for one configured workspace or alias."},
                "block_id_or_url": {"type": "string", "description": "The Notion block or page UUID/URL to append children to."},
                "children": {"type": "array", "description": "Child blocks to append."},
            },
            "required": ["workspace", "block_id_or_url", "children"],
            "additionalProperties": False,
        },
        "handler": tool_append_block_children,
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
                "annotations": {
                    "readOnlyHint": name not in {"create_page", "append_block_children"},
                    "destructiveHint": False,
                    "idempotentHint": name not in {"create_page", "append_block_children"},
                    "openWorldHint": True,
                },
            }
        )
    return descriptors


def validate_arguments(arguments: Any, schema: dict[str, Any]) -> None:
    """Validate the small input-schema subset advertised by this server."""

    if not isinstance(arguments, dict):
        raise McpProtocolError("Tool arguments must be an object.")
    for name in schema.get("required", []):
        if name not in arguments:
            raise McpProtocolError(f"Missing required argument '{name}'.")
    for name, value in arguments.items():
        definition = schema["properties"].get(name)
        if definition is None:
            raise McpProtocolError(f"Unknown argument '{name}'.")
        expected = definition["type"]
        valid = {
            "string": isinstance(value, str),
            "boolean": isinstance(value, bool),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "object": isinstance(value, dict),
            "array": isinstance(value, list),
        }[expected]
        if not valid:
            raise McpProtocolError(f"Argument '{name}' must be {expected}.")
        if expected == "string" and not value.strip():
            raise McpProtocolError(f"Argument '{name}' must not be blank.")
        if expected == "integer" and not definition.get("minimum", value) <= value <= definition.get("maximum", value):
            raise McpProtocolError(f"Argument '{name}' is outside its allowed range.")
        if "enum" in definition and value not in definition["enum"]:
            raise McpProtocolError(f"Argument '{name}' is not an allowed value.")
        if expected == "array" and any(not isinstance(item, dict) for item in value):
            raise McpProtocolError(f"Argument '{name}' must contain objects.")


@dataclass
class McpSession:
    initialized: bool = False
    ready: bool = False


def handle_request(message: Any, session: McpSession | None = None) -> dict[str, Any] | None:
    """Process one MCP message; a session enforces stdio initialization order."""

    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return error_response(None, -32600, "Expected one JSON-RPC 2.0 object.")
    request_id = message.get("id")
    if "id" in message and (isinstance(request_id, bool) or not isinstance(request_id, (str, int))):
        return error_response(None, -32600, "Request id must be a string or integer.")
    if "method" not in message and "id" in message and ("result" in message or "error" in message):
        # The server sends no requests; an unsolicited response must not elicit another response.
        return None
    method = message.get("method")
    if not isinstance(method, str) or not method or "result" in message or "error" in message:
        return error_response(request_id, -32600, "Invalid request envelope.")
    params = message.get("params", {})
    if "id" not in message:
        # Notifications never run tool handlers (especially writes) and never receive replies.
        if method == "notifications/initialized" and isinstance(params, dict) and session and session.initialized:
            session.ready = True
        return None
    if not isinstance(params, dict):
        return error_response(request_id, -32602, "params must be an object.")
    if method == "ping":
        return success_response(request_id, {})
    if method == "initialize":
        if session and session.initialized:
            return error_response(request_id, -32600, "Session is already initialized.")
        version = params.get("protocolVersion")
        client_info = params.get("clientInfo")
        if (not isinstance(version, str) or not version
                or not isinstance(params.get("capabilities"), dict)
                or not isinstance(client_info, dict)
                or not isinstance(client_info.get("name"), str)
                or not isinstance(client_info.get("version"), str)):
            return error_response(request_id, -32602, "initialize requires protocolVersion, capabilities, and clientInfo.")
        if session:
            session.initialized = True
        return success_response(request_id, {
            "protocolVersion": version if version in SUPPORTED_PROTOCOL_VERSIONS else SUPPORTED_PROTOCOL_VERSIONS[-1],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
    if session and not session.ready:
        return error_response(request_id, -32600, "Complete initialization before calling methods.")
    if method == "tools/list":
        if "cursor" in params:
            return error_response(request_id, -32602, "This server has no tool-list cursor.")
        return success_response(request_id, {"tools": tool_descriptors()})
    if method == "tools/call":
        tool_name = params.get("name")
        if not isinstance(tool_name, str) or tool_name not in TOOLS:
            return error_response(request_id, -32602, "Unknown or invalid tool name.")
        arguments = params.get("arguments", {})
        try:
            validate_arguments(arguments, TOOLS[tool_name]["inputSchema"])
        except McpProtocolError as exc:
            return error_response(request_id, -32602, str(exc))
        try:
            payload = TOOLS[tool_name]["handler"](arguments)
            return success_response(request_id, make_tool_text(payload))
        except (ConfigError, McpProtocolError, NotionApiError) as exc:
            return success_response(request_id, {
                "content": [{"type": "text", "text": str(exc)}], "isError": True,
            })
        except Exception as exc:  # noqa: BLE001
            # Avoid echoing upstream payloads or credentials in unexpected exceptions.
            sys.stderr.write(f"Unexpected tool failure: {type(exc).__name__}\n")
            return error_response(request_id, -32603, "Internal tool error.")
    return error_response(request_id, -32601, "Method not found.")


def success_response(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    """Build a JSON-RPC success response."""

    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def error_response(request_id: Any, code: int, message: str) -> dict[str, Any]:
    """Build a JSON-RPC error response."""

    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def reject_json_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant: {value}")


def read_message() -> Any:
    """Read one bounded newline-delimited UTF-8 JSON message; EOF raises EOFError."""

    raw = sys.stdin.buffer.readline(MAX_MESSAGE_BYTES + 1)
    if not raw:
        raise EOFError
    if len(raw) > MAX_MESSAGE_BYTES:
        while raw and not raw.endswith(b"\n"):
            raw = sys.stdin.buffer.readline(MAX_MESSAGE_BYTES + 1)
        raise McpProtocolError("Message exceeds the 4 MiB limit.")
    if not raw.endswith(b"\n"):
        raise McpProtocolError("Message must end with a newline.")
    return json.loads(raw.decode("utf-8"), parse_constant=reject_json_constant)


def write_message(message: dict[str, Any]) -> None:
    """Write exactly one UTF-8 JSON object followed by a newline to stdout."""

    # Escaping also safely preserves a client's lone surrogate in a string ID.
    body = json.dumps(message, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(body + b"\n")
    sys.stdout.buffer.flush()


def serve_forever() -> int:
    """Keep serving after malformed messages, and exit cleanly on EOF."""

    session = McpSession()
    try:
        while True:
            try:
                message = read_message()
            except EOFError:
                return 0
            except (ValueError, UnicodeError, RecursionError, McpProtocolError):
                write_message(error_response(None, -32700, "Invalid newline-delimited JSON message."))
                continue
            response = handle_request(message, session)
            if response is not None:
                write_message(response)
    except (KeyboardInterrupt, BrokenPipeError):
        return 0


def main() -> int:
    """Entrypoint for the stdio server."""

    return serve_forever()


if __name__ == "__main__":
    raise SystemExit(main())
