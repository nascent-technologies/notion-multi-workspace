#!/usr/bin/env python3
"""Direct-handler smoke with synthetic configuration by default."""

from __future__ import annotations

import importlib.util
import os
import socket
import sys
import urllib.request
from unittest import mock

from smoke_test_stdio import SERVER_PATH, check_responses, parse_args, smoke_environment, smoke_requests


def load_server_module():
    spec = importlib.util.spec_from_file_location("notion_multi_workspace_server", SERVER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load server module from {SERVER_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    args = parse_args()
    # Live calls use the subprocess smoke so even a multi-request fetch has a deadline.
    if args.live:
        from smoke_test_stdio import main as stdio_main
        return stdio_main()
    messages = smoke_requests(args)
    with mock.patch.dict(os.environ, smoke_environment(False), clear=True), \
            mock.patch.object(urllib.request, "urlopen", side_effect=AssertionError("Unexpected network access")), \
            mock.patch.object(socket.socket, "connect", side_effect=AssertionError("Unexpected network access")), \
            mock.patch.object(socket, "getaddrinfo", side_effect=AssertionError("Unexpected network access")):
        module = load_server_module()
        session = module.McpSession()
        responses = []
        for message in messages:
            response = module.handle_request(message, session)
            if response is not None:
                responses.append(response)
        check_responses(messages, responses, False)
    print("PASS: offline direct-handler smoke; synthetic configuration, seven tools, write annotations verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
