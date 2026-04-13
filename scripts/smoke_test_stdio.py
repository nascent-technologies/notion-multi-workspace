#!/usr/bin/env python3
"""End-to-end stdio smoke test for the Notion multi-workspace MCP server."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


SERVER_PATH = Path(__file__).resolve().parent / "notion_multi_workspace_server.py"
SYSTEM_PYTHON = Path("/usr/bin/python3")


def default_workspace() -> str:
    return (
        os.environ.get("NOTION_SMOKE_WORKSPACE")
        or os.environ.get("NOTION_WORKSPACE_KEYS", "workspace-a").split(",")[0].strip()
        or "workspace-a"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run an end-to-end stdio smoke test against the Notion multi-workspace MCP server."
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


def send_message(process: subprocess.Popen[bytes], payload: dict[str, Any]) -> None:
    body = json.dumps(payload).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8")
    assert process.stdin is not None
    process.stdin.write(header)
    process.stdin.write(body)
    process.stdin.flush()


def read_message(process: subprocess.Popen[bytes]) -> dict[str, Any]:
    assert process.stdout is not None
    headers: dict[str, str] = {}

    while True:
        raw_line = process.stdout.readline()
        if not raw_line:
            stderr = b""
            if process.stderr is not None:
                stderr = process.stderr.read()
            raise RuntimeError(
                "Server exited before returning a response.\n"
                + stderr.decode("utf-8", errors="replace")
            )
        line = raw_line.decode("utf-8").strip()
        if not line:
            break
        name, value = line.split(":", 1)
        headers[name.lower()] = value.strip()

    length = int(headers["content-length"])
    body = process.stdout.read(length)
    return json.loads(body.decode("utf-8"))


def pretty_print(label: str, payload: dict[str, Any]) -> None:
    print(f"\n== {label} ==")
    print(json.dumps(payload, indent=2, sort_keys=True))


def ensure_tool_success(label: str, payload: dict[str, Any]) -> None:
    result = payload.get("result", {})
    if result.get("isError"):
        content = result.get("content", [])
        message = content[0]["text"] if content else "Unknown tool error"
        raise RuntimeError(f"{label} failed: {message}")


def parse_tool_text_payload(payload: dict[str, Any]) -> dict[str, Any]:
    result = payload.get("result", {})
    content = result.get("content", [])
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


def ensure_smoke_env() -> tempfile.TemporaryDirectory[str] | None:
    if os.environ.get("NOTION_WORKSPACE_KEYS"):
        return None

    tempdir = tempfile.TemporaryDirectory()
    env_path = Path(tempdir.name) / "notion-multi-workspace-smoke.env"
    env_path.write_text(
        "\n".join(
            [
                "NOTION_WORKSPACE_KEYS=workspace-a,workspace-b",
                "NOTION_WORKSPACE_WORKSPACE_A_NAME=Workspace A",
                "NOTION_WORKSPACE_WORKSPACE_A_TOKEN=secret_workspace_a_token",
                "NOTION_WORKSPACE_WORKSPACE_B_NAME=Workspace B",
                "NOTION_WORKSPACE_WORKSPACE_B_TOKEN=secret_workspace_b_token",
            ]
        )
        + "\n"
    )
    os.environ.setdefault("NOTION_MULTI_WORKSPACE_ENV_FILE", str(env_path))
    return tempdir


def call_tool(
    process: subprocess.Popen[bytes],
    request_id: int,
    name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    send_message(
        process,
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    response = read_message(process)
    ensure_tool_success(name, response)
    return response


def main() -> int:
    args = parse_args()
    tempdir = ensure_smoke_env()

    process = subprocess.Popen(
        [str(SYSTEM_PYTHON), str(SERVER_PATH)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        send_message(
            process,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05"},
            },
        )
        initialize_payload = read_message(process)
        pretty_print("initialize", initialize_payload)

        send_message(
            process,
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        )

        send_message(
            process,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        tools_payload = read_message(process)
        pretty_print("tools/list", tools_payload)

        workspaces_payload = call_tool(
            process,
            request_id=3,
            name="list_workspaces",
            arguments={"validate_tokens": args.validate_tokens},
        )
        pretty_print("list_workspaces", workspaces_payload)
        if args.validate_tokens:
            ensure_validated_tokens_ok(workspaces_payload)

        next_id = 4
        if args.query:
            search_payload = call_tool(
                process,
                request_id=next_id,
                name="search",
                arguments={
                    "workspace": args.workspace,
                    "query": args.query,
                    "page_size": 5,
                },
            )
            pretty_print("search", search_payload)
            next_id += 1

        if args.fetch_page:
            fetch_payload = call_tool(
                process,
                request_id=next_id,
                name="fetch_page",
                arguments={
                    "workspace": args.workspace,
                    "page_id_or_url": args.fetch_page,
                    "block_limit": 50,
                },
            )
            pretty_print("fetch_page", fetch_payload)

        return 0
    finally:
        if process.stdin is not None:
            process.stdin.close()
        process.terminate()
        process.wait(timeout=5)
        if tempdir is not None:
            tempdir.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
