#!/usr/bin/env python3
"""Independent newline-stdio smoke test; synthetic and offline unless --live."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


SERVER_PATH = Path(__file__).resolve().parent / "notion_multi_workspace_server.py"
OFFLINE_TIMEOUT_SECONDS = 10
LIVE_TIMEOUT_SECONDS = 120
# This harness does not import the server or reuse its framing implementation.
OFFLINE_RUNNER = """
import runpy, socket, sys, urllib.request

def deny_network(*args, **kwargs):
    raise RuntimeError("Unexpected network access in offline smoke test")

socket.create_connection = deny_network
socket.getaddrinfo = deny_network
socket.socket.connect = deny_network
socket.socket.connect_ex = deny_network
urllib.request.urlopen = deny_network
runpy.run_path(sys.argv[1], run_name="__main__")
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Use real configuration and allow explicitly requested Notion reads.")
    parser.add_argument("--workspace", default=None)
    parser.add_argument("--query", default=None)
    parser.add_argument("--fetch-page", default=None)
    parser.add_argument("--validate-tokens", action="store_true")
    args = parser.parse_args()
    if not args.live and (args.query or args.fetch_page or args.validate_tokens):
        parser.error("Notion reads require explicit --live.")
    return args


def smoke_environment(live: bool) -> dict[str, str]:
    if live:
        return dict(os.environ)
    environment = {key: value for key, value in os.environ.items() if not key.startswith("NOTION_")}
    environment.update({
        "NOTION_MULTI_WORKSPACE_ENV_FILE": os.devnull,
        "NOTION_WORKSPACE_KEYS": "workspace-a,workspace-b",
        "NOTION_WORKSPACE_WORKSPACE_A_NAME": "Workspace A",
        "NOTION_WORKSPACE_WORKSPACE_A_TOKEN": "synthetic-token-a",
        "NOTION_WORKSPACE_WORKSPACE_B_NAME": "Workspace B",
        "NOTION_WORKSPACE_WORKSPACE_B_TOKEN": "synthetic-token-b",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    return environment


def smoke_requests(args: argparse.Namespace) -> list[dict[str, Any]]:
    workspace = args.workspace or (
        os.environ.get("NOTION_WORKSPACE_KEYS", "workspace-a").split(",")[0].strip()
        if args.live else "workspace-a"
    )
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "independent-smoke-client", "version": "1"},
        }},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "list_workspaces", "arguments": {"validate_tokens": args.validate_tokens},
        }},
    ]
    if args.query:
        messages.append({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
            "name": "search", "arguments": {"workspace": workspace, "query": args.query, "page_size": 5},
        }})
    if args.fetch_page:
        messages.append({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {
            "name": "fetch_page", "arguments": {"workspace": workspace, "page_id_or_url": args.fetch_page, "block_limit": 50},
        }})
    return messages


def check_responses(messages: list[dict[str, Any]], responses: list[dict[str, Any]], live: bool) -> None:
    expected_ids = [message["id"] for message in messages if "id" in message]
    if [response.get("id") for response in responses] != expected_ids:
        raise RuntimeError("Missing, duplicate, or out-of-order responses (notifications must have none).")
    for response in responses:
        if response.get("jsonrpc") != "2.0" or "error" in response:
            raise RuntimeError(f"Protocol failure: {response}")
        if response.get("result", {}).get("isError"):
            raise RuntimeError(f"Tool failure: {response['result']}")
    if responses[0]["result"]["protocolVersion"] != "2025-11-25":
        raise RuntimeError("Protocol negotiation failed.")
    descriptors = {tool["name"]: tool for tool in responses[1]["result"]["tools"]}
    expected_tools = {"list_workspaces", "search", "fetch_page", "fetch_database", "query_database", "create_page", "append_block_children"}
    if descriptors.keys() != expected_tools:
        raise RuntimeError("Expected all seven tools, including both writes.")
    for name, descriptor in descriptors.items():
        if descriptor["annotations"]["readOnlyHint"] != (name not in {"create_page", "append_block_children"}):
            raise RuntimeError(f"Incorrect annotation for {name}.")
    workspaces = json.loads(responses[2]["result"]["content"][0]["text"])
    if not live and [item["key"] for item in workspaces["workspaces"]] != ["workspace-a", "workspace-b"]:
        raise RuntimeError("Offline smoke did not use synthetic workspaces.")
    validate = messages[3]["params"]["arguments"]["validate_tokens"]
    if validate and any(item.get("token_status") != "ok" for item in workspaces["workspaces"]):
        raise RuntimeError("Live token validation failed.")


def main() -> int:
    args = parse_args()
    messages = smoke_requests(args)
    command = [sys.executable, "-B", str(SERVER_PATH)] if args.live else [sys.executable, "-B", "-c", OFFLINE_RUNNER, str(SERVER_PATH)]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=smoke_environment(args.live))
    try:
        wire = b"".join(json.dumps(message).encode("utf-8") + b"\n" for message in messages)
        stdout, stderr = process.communicate(wire, timeout=LIVE_TIMEOUT_SECONDS if args.live else OFFLINE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.communicate(timeout=5)
        raise RuntimeError("Smoke test exceeded its subprocess deadline.") from exc
    if process.returncode:
        raise RuntimeError(f"Server exited with {process.returncode}: {stderr.decode('utf-8', errors='replace')}")
    if not stdout.endswith(b"\n") or b"Content-Length:" in stdout:
        raise RuntimeError("Expected newline-delimited JSON output.")
    responses = [json.loads(line) for line in stdout.splitlines()]
    check_responses(messages, responses, args.live)
    print(f"PASS: {'live' if args.live else 'offline'} stdio smoke; {len(responses)} responses, seven tools, write annotations verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
