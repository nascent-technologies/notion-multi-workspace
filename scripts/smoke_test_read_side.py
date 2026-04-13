#!/usr/bin/env python3
"""Local smoke test for the read-only Notion multi-workspace server."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any


SERVER_PATH = Path(__file__).resolve().parent / "notion_multi_workspace_server.py"


def load_server_module():
    spec = importlib.util.spec_from_file_location(
        "notion_multi_workspace_server", SERVER_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load server module from {SERVER_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def default_workspace() -> str:
    return (
        os.environ.get("NOTION_SMOKE_WORKSPACE")
        or os.environ.get("NOTION_WORKSPACE_PRIMARY_NAME")
        or os.environ.get("NOTION_WORKSPACE_PRIMARY_TOKEN") and "primary"
        or os.environ.get("NOTION_WORKSPACE_KEYS", "primary").split(",")[0].strip()
        or "primary"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a direct read-side smoke test against the Notion multi-workspace server."
    )
    parser.add_argument(
        "--workspace",
        default=default_workspace(),
        help="Workspace selector to use for optional search/fetch calls.",
    )
    parser.add_argument(
        "--query",
        default=os.environ.get("NOTION_SMOKE_QUERY"),
        help="Optional search query to run after initialization.",
    )
    parser.add_argument(
        "--fetch-page",
        dest="fetch_page",
        default=os.environ.get("NOTION_SMOKE_PAGE_ID_OR_URL"),
        help="Optional Notion page id or URL to fetch after initialization.",
    )
    parser.add_argument(
        "--validate-tokens",
        action="store_true",
        help="Validate the configured Notion tokens via list_workspaces.",
    )
    return parser.parse_args()


def pretty_print(label: str, payload: dict[str, Any]) -> None:
    print(f"\n== {label} ==")
    print(json.dumps(payload, indent=2, sort_keys=True))


def ensure_not_error(label: str, payload: dict[str, Any]) -> None:
    if payload.get("isError"):
        raise RuntimeError(f"{label} failed: {payload['content'][0]['text']}")


def parse_tool_text_payload(payload: dict[str, Any]) -> dict[str, Any]:
    content = payload.get("content", [])
    if not content:
        raise RuntimeError("Tool returned no content payload to inspect.")
    return json.loads(content[0]["text"])


def ensure_validated_tokens_ok(payload: dict[str, Any]) -> None:
    parsed = parse_tool_text_payload(payload)
    failures = [
        workspace
        for workspace in parsed.get("workspaces", [])
        if workspace.get("token_status") != "ok"
    ]
    if failures:
        summary = ", ".join(
            f"{workspace['name']} ({workspace.get('token_status', 'unknown')})"
            for workspace in failures
        )
        raise RuntimeError(f"Token validation failed: {summary}")


def ensure_smoke_env(module) -> tempfile.TemporaryDirectory[str] | None:
    if os.environ.get(module.WORKSPACE_KEYS_ENV_VAR):
        return None
    if os.environ.get("NOTION_WORKSPACE_PRIMARY_NAME") and os.environ.get("NOTION_TOKEN_PRIMARY"):
        return None

    tempdir = tempfile.TemporaryDirectory()
    env_path = Path(tempdir.name) / "notion-multi-workspace-smoke.env"
    env_path.write_text(
        "\n".join(
            [
                "NOTION_WORKSPACE_KEYS=primary,secondary",
                "NOTION_WORKSPACE_PRIMARY_NAME=Workspace A",
                "NOTION_WORKSPACE_PRIMARY_TOKEN=secret_primary_workspace_token",
                "NOTION_WORKSPACE_SECONDARY_NAME=Workspace B",
                "NOTION_WORKSPACE_SECONDARY_TOKEN=secret_secondary_workspace_token",
            ]
        )
        + "\n"
    )
    os.environ.setdefault(module.DOTENV_ENV_VAR, str(env_path))
    return tempdir


def main() -> int:
    args = parse_args()
    module = load_server_module()
    tempdir = ensure_smoke_env(module)

    try:
        initialize_payload = module.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05"},
            }
        )
        pretty_print("initialize", initialize_payload)

        tools_payload = module.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {},
            }
        )
        pretty_print("tools/list", tools_payload)

        workspaces_payload = module.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "list_workspaces",
                    "arguments": {"validate_tokens": args.validate_tokens},
                },
            }
        )
        pretty_print("list_workspaces", workspaces_payload)
        ensure_not_error("list_workspaces", workspaces_payload["result"])
        if args.validate_tokens:
            ensure_validated_tokens_ok(workspaces_payload["result"])

        next_id = 4

        if args.query:
            search_payload = module.handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": next_id,
                    "method": "tools/call",
                    "params": {
                        "name": "search",
                        "arguments": {
                            "workspace": args.workspace,
                            "query": args.query,
                            "page_size": 5,
                        },
                    },
                }
            )
            pretty_print("search", search_payload)
            ensure_not_error("search", search_payload["result"])
            next_id += 1

        if args.fetch_page:
            fetch_payload = module.handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": next_id,
                    "method": "tools/call",
                    "params": {
                        "name": "fetch_page",
                        "arguments": {
                            "workspace": args.workspace,
                            "page_id_or_url": args.fetch_page,
                            "block_limit": 50,
                        },
                    },
                }
            )
            pretty_print("fetch_page", fetch_payload)
            ensure_not_error("fetch_page", fetch_payload["result"])

        return 0
    finally:
        if tempdir is not None:
            tempdir.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
