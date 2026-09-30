"""Offline protocol, pagination, and transport regression coverage."""

import io
import json
import os
import socket
import subprocess
import sys
import unittest
from http.client import IncompleteRead
from pathlib import Path
from unittest import mock
from urllib import error, parse

from test_notion_multi_workspace_server import MODULE


PAGE_ID = "00000000-0000-4000-8000-000000000001"
CHILD_ID = "00000000-0000-4000-8000-000000000002"
WORKSPACE = MODULE.WorkspaceConfig("workspace-a", "Workspace A", "synthetic-token-a")
ROOT = Path(__file__).resolve().parents[1]
ENV = {
    "NOTION_MULTI_WORKSPACE_ENV_FILE": os.devnull,
    "NOTION_WORKSPACE_KEYS": "workspace-a,workspace-b",
    "NOTION_WORKSPACE_WORKSPACE_A_NAME": "Workspace A",
    "NOTION_WORKSPACE_WORKSPACE_A_TOKEN": "synthetic-token-a",
    "NOTION_WORKSPACE_WORKSPACE_B_NAME": "Workspace B",
    "NOTION_WORKSPACE_WORKSPACE_B_TOKEN": "synthetic-token-b",
    "PYTHONDONTWRITEBYTECODE": "1",
}
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-11-25", "capabilities": {},
    "clientInfo": {"name": "test-client", "version": "1"},
}}


def block(identifier=CHILD_ID, children=False, kind="paragraph"):
    return {"id": identifier, "type": kind, "has_children": children,
            kind: {"rich_text": [{"plain_text": "Example text"}]}}


def page(results, more=False, cursor=None):
    return {"results": results, "has_more": more, "next_cursor": cursor}


def http_error(status, headers=None, code="rate_limited"):
    return error.HTTPError("https://api.notion.com/v1/test", status, "Example error",
                           headers or {}, io.BytesIO(json.dumps({"code": code}).encode()))


def response(payload):
    result = mock.MagicMock()
    result.__enter__.return_value = result
    result.read.return_value = json.dumps(payload).encode()
    return result


class OfflineTests(unittest.TestCase):
    def setUp(self):
        for patch in (
            mock.patch.dict(os.environ, ENV, clear=True),
            mock.patch.object(MODULE.request, "urlopen", side_effect=AssertionError("Unexpected network access")),
            mock.patch.object(socket, "getaddrinfo", side_effect=AssertionError("Unexpected DNS access")),
            mock.patch.object(socket.socket, "connect", side_effect=AssertionError("Unexpected network access")),
            mock.patch.object(socket.socket, "connect_ex", side_effect=AssertionError("Unexpected network access")),
            mock.patch.object(MODULE.time, "sleep"),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def client(self):
        return MODULE.NotionClient(WORKSPACE)


class HttpTests(OfflineTests):
    def test_explicit_timeout_headers_and_workspace_routing(self):
        with mock.patch.object(MODULE.request, "urlopen", return_value=response({"id": PAGE_ID})) as opener:
            self.client().get_page(PAGE_ID)
        req = opener.call_args.args[0]
        self.assertEqual(opener.call_args.kwargs, {"timeout": 15})
        self.assertEqual(req.get_header("Authorization"), "Bearer synthetic-token-a")
        self.assertEqual(req.get_header("Notion-version"), "2022-06-28")
        self.assertEqual(req.full_url, f"https://api.notion.com/v1/pages/{PAGE_ID}")

    def test_semantic_read_post_search_and_query_retry(self):
        for action in (lambda c: c.search("Example"), lambda c: c.query_database(PAGE_ID)):
            with self.subTest(action=action), mock.patch.object(MODULE.request, "urlopen", side_effect=[
                http_error(503), response(page([])),
            ]) as opener:
                self.assertEqual(action(self.client()), page([]))
                self.assertEqual(opener.call_count, 2)
                self.assertTrue(all(call.args[0].method == "POST" for call in opener.call_args_list))

    def test_search_forwards_exact_query_and_keeps_nonmatching_upstream_results(self):
        query = '  Budget "Q4" 東京  '
        upstream = page([{
            "object": "page", "id": PAGE_ID,
            "properties": {"Name": {"type": "title", "title": [{"plain_text": "Unrelated title"}]}},
        }])
        for result_type in ("page", "database", "all"):
            with self.subTest(result_type=result_type), mock.patch.object(
                MODULE.request, "urlopen", return_value=response(upstream),
            ) as opener:
                result = MODULE.tool_search({
                    "workspace": "workspace-a", "query": query, "page_size": 7,
                    "result_type": result_type, "start_cursor": "previous",
                })
            req = opener.call_args.args[0]
            expected = {"query": query, "page_size": 7, "start_cursor": "previous"}
            if result_type != "all":
                expected["filter"] = {"property": "object", "value": result_type}
            self.assertEqual(req.method, "POST")
            self.assertEqual(req.full_url, "https://api.notion.com/v1/search")
            self.assertEqual(json.loads(req.data), expected)
            self.assertEqual(result["query"], query)
            self.assertEqual(result["count"], 1)
            self.assertEqual(result["results"][0]["id"], PAGE_ID)
            self.assertEqual(result["results"][0]["title"], "Unrelated title")

    def test_retryable_statuses_are_bounded(self):
        for status in (429, 500, 502, 503, 504, 529):
            with self.subTest(status=status), mock.patch.object(MODULE.request, "urlopen", side_effect=[http_error(status) for _ in range(3)]) as opener:
                with self.assertRaises(MODULE.NotionApiError):
                    self.client().get_self()
                self.assertEqual(opener.call_count, 3)

    def test_permanent_errors_and_blocked_429_not_retried(self):
        cases = [(status, "validation_error") for status in (400, 401, 403, 404, 409)]
        cases.append((429, "public_api_request_blocked"))
        for status, code in cases:
            with self.subTest(status=status, code=code), mock.patch.object(MODULE.request, "urlopen", side_effect=http_error(status, code=code)) as opener:
                with self.assertRaises(MODULE.NotionApiError):
                    self.client().get_self()
                self.assertEqual(opener.call_count, 1)

    def test_retry_after_obeyed_within_bound(self):
        with mock.patch.object(MODULE.request, "urlopen", side_effect=[http_error(429, {"Retry-After": "4"}), response({})]):
            self.client().get_self()
        MODULE.time.sleep.assert_called_once_with(4)

    def test_excessive_or_invalid_retry_after_stops(self):
        for value in ("6", "nan", "inf", "-1", "tomorrow", "Wed, 01 Oct 2026 00:00:00 GMT"):
            with self.subTest(value=value), mock.patch.object(MODULE.request, "urlopen", side_effect=http_error(429, {"Retry-After": value})) as opener:
                with self.assertRaises(MODULE.NotionApiError):
                    self.client().get_self()
                self.assertEqual(opener.call_count, 1)
        MODULE.time.sleep.assert_not_called()

    def test_read_transport_failures_include_response_body_read(self):
        for exc in (error.URLError("offline"), TimeoutError("timeout"), IncompleteRead(b"partial", 10), ConnectionResetError()):
            broken = response({})
            broken.read.side_effect = exc
            with self.subTest(exc=type(exc)), mock.patch.object(MODULE.request, "urlopen", side_effect=[broken, response({})]) as opener:
                self.assertEqual(self.client().get_self(), {})
                self.assertEqual(opener.call_count, 2)

    def test_transport_retry_exhaustion(self):
        with mock.patch.object(MODULE.request, "urlopen", side_effect=error.URLError("offline")) as opener:
            with self.assertRaises(MODULE.NotionApiError):
                self.client().search("Example")
            self.assertEqual(opener.call_count, 3)

    def test_writes_never_replayed_for_any_ambiguous_failure(self):
        for write in (lambda c: c.create_page({"page_id": PAGE_ID}, {}, [block()]),
                      lambda c: c.append_block_children(PAGE_ID, [block()])):
            for exc in (TimeoutError(), error.URLError("offline"), IncompleteRead(b"", 1), http_error(503), http_error(429)):
                with self.subTest(write=write, exc=type(exc)), mock.patch.object(MODULE.request, "urlopen", side_effect=exc) as opener:
                    with self.assertRaises(MODULE.NotionApiError):
                        write(self.client())
                    self.assertEqual(opener.call_count, 1)
        MODULE.time.sleep.assert_not_called()

    def test_write_response_read_failure_is_not_retried(self):
        broken = response({})
        broken.read.side_effect = IncompleteRead(b"partial", 50)
        with mock.patch.object(MODULE.request, "urlopen", return_value=broken) as opener:
            with self.assertRaisesRegex(MODULE.NotionApiError, "outcome may be unknown"):
                self.client().append_block_children(PAGE_ID, [block()])
            self.assertEqual(opener.call_count, 1)

    def test_invalid_success_json_is_controlled_and_never_replayed(self):
        for raw in (b"[]", b"not json", b"\xff", b""):
            bad = response({})
            bad.read.return_value = raw
            with self.subTest(raw=raw), mock.patch.object(MODULE.request, "urlopen", return_value=bad) as opener:
                with self.assertRaises(MODULE.NotionApiError):
                    self.client().create_page({}, {})
                self.assertEqual(opener.call_count, 1)


class CollectionTests(OfflineTests):
    def test_search_single_page_and_resumed_scope(self):
        for start, more, cursor, complete in ((None, False, None, True), (None, True, "next", False), ("previous", False, None, False)):
            result = MODULE.build_search_summary(WORKSPACE, page([], more, cursor), "Example", "all", start)
            self.assertEqual(result["collection"]["complete"], complete)
            self.assertEqual(result["has_more"], more)
            self.assertEqual(result["next_cursor"], cursor)
            self.assertNotIn("request_status", result)

    def test_search_preserves_explicit_status_and_requires_pagination_exhaustion(self):
        for status in ({"type": "complete"}, {"type": "incomplete"},
                       {"type": "incomplete", "incomplete_reason": "query_result_limit_reached"}):
            for start, more, cursor in ((None, False, None), (None, True, "next"),
                                        ("previous", False, None), (None, False, "unexpected")):
                with self.subTest(status=status, start=start, more=more, cursor=cursor):
                    result = MODULE.build_search_summary(
                        WORKSPACE, {**page([], more, cursor), "request_status": status}, "Example", "page", start,
                    )
                    self.assertEqual(result["request_status"], status)
                    self.assertEqual(result["collection"]["complete"],
                                     status["type"] == "complete" and start is None and not more and cursor is None)
                    if status["type"] == "incomplete":
                        self.assertIn("incomplete request", " ".join(result["collection"]["warnings"]))
                    if "incomplete_reason" in status:
                        self.assertIn("query_result_limit_reached", " ".join(result["collection"]["warnings"]))

    def test_search_malformed_request_status_cannot_claim_complete(self):
        for status in (None, [], "complete", {}, {"type": []}, {"type": "unknown"},
                       {"type": "complete", "incomplete_reason": "query_result_limit_reached"},
                       {"type": "complete", "incomplete_reason": None},
                       {"type": "incomplete", "incomplete_reason": {}},
                       {"type": "incomplete", "incomplete_reason": "untrusted\nreason\x1b"}):
            with self.subTest(status=status):
                result = MODULE.build_search_summary(
                    WORKSPACE, {**page([]), "request_status": status}, "Example", "page",
                )
                self.assertEqual(result["request_status"], status)
                self.assertFalse(result["collection"]["complete"])
                warnings = " ".join(result["collection"]["warnings"])
                self.assertIn("invalid request_status", warnings)
                self.assertNotIn("untrusted", warnings)
                self.assertNotIn("\n", warnings)
                self.assertNotIn("\x1b", warnings)

    def test_malformed_and_repeated_cursors_are_explicit(self):
        responses = [page([], True, None), page([], True, ""), page([], True, []),
                     page([], True, "previous"), page([], False, "unexpected"), {"results": []}]
        for payload in responses:
            with self.subTest(payload=payload):
                result = MODULE.build_search_summary(WORKSPACE, payload, "Example", "page", "previous")
                self.assertFalse(result["collection"]["complete"])
                self.assertTrue(result["collection"]["warnings"])

    def test_malformed_results_fail_instead_of_claiming_complete(self):
        for results in (None, {}, [None], ["bad"]):
            with self.subTest(results=results), self.assertRaises(MODULE.NotionApiError):
                MODULE.build_search_summary(WORKSPACE, page(results), "Example", "page")

    def test_multiple_block_pages_are_collected(self):
        with mock.patch.object(MODULE.NotionClient, "request_json", side_effect=[page([block()], True, "next"), page([block(PAGE_ID)])]) as fetch:
            result = self.client().list_block_children(PAGE_ID, 5)
        self.assertEqual(len(result["results"]), 2)
        self.assertTrue(result["complete"])
        self.assertIn("start_cursor=next", fetch.call_args.args[1])

    def test_block_limit_and_exact_exhaustion_differ(self):
        for more, expected in ((False, True), (True, False)):
            with self.subTest(more=more), mock.patch.object(MODULE.NotionClient, "request_json", return_value=page([block()], more, "next" if more else None)):
                lines, count, status = MODULE.recurse_blocks_to_markdown(self.client(), PAGE_ID, max_blocks=1)
                self.assertEqual(count, 1)
                self.assertEqual(status["complete"], expected)
                self.assertEqual(status["truncated"], not expected)

    def test_nested_blocks_consume_shared_budget_and_omitted_siblings_are_reported(self):
        with mock.patch.object(MODULE.NotionClient, "request_json", side_effect=[
            page([block(children=True), block("sibling")]), page([block("descendant")]),
        ]):
            lines, count, status = MODULE.recurse_blocks_to_markdown(self.client(), PAGE_ID, max_blocks=2)
        self.assertEqual(count, 2)
        self.assertTrue(status["truncated"])
        self.assertIn("block_limit", status["truncation_reasons"])
        self.assertEqual(len(lines), 2)

    def test_children_on_last_block_are_reported_as_omitted(self):
        with mock.patch.object(MODULE.NotionClient, "request_json", return_value=page([block(children=True)])) as fetch:
            _, count, status = MODULE.recurse_blocks_to_markdown(self.client(), PAGE_ID, max_blocks=1)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(count, 1)
        self.assertIn("block_limit", status["truncation_reasons"])

    def test_bad_block_cursors_stop_without_looping(self):
        for next_cursor in (None, "", {}, "same"):
            with self.subTest(cursor=next_cursor), mock.patch.object(MODULE.NotionClient, "request_json", side_effect=[
                page([block()], True, "same"), page([], True, next_cursor),
            ]) as fetch:
                result = self.client().list_block_children(PAGE_ID)
                self.assertEqual(fetch.call_count, 2)
                self.assertFalse(result["complete"])
                self.assertIn("invalid_pagination", result["truncation_reasons"])

    def test_cycle_of_distinct_cursors_is_detected(self):
        with mock.patch.object(MODULE.NotionClient, "request_json", side_effect=[page([], True, "a"), page([], True, "b"), page([], True, "a")]) as fetch:
            result = self.client().list_block_children(PAGE_ID)
        self.assertEqual(fetch.call_count, 3)
        self.assertIn("invalid_pagination", result["truncation_reasons"])

    def test_empty_pages_with_fresh_cursors_have_finite_request_budget(self):
        count = 0
        def fetch(*args):
            nonlocal count
            count += 1
            return page([], True, f"cursor-{count}")
        with mock.patch.object(MODULE.NotionClient, "request_json", side_effect=fetch):
            result = self.client().list_block_children(PAGE_ID)
        self.assertEqual(count, MODULE.MAX_BLOCK_REQUESTS)
        self.assertIn("request_limit", result["truncation_reasons"])

    def test_request_budget_is_shared_across_subtrees(self):
        with mock.patch.object(MODULE, "MAX_BLOCK_REQUESTS", 2), mock.patch.object(MODULE.NotionClient, "request_json", return_value=page([block("child", True)])) as fetch:
            _, _, status = MODULE.recurse_blocks_to_markdown(self.client(), PAGE_ID)
        self.assertEqual(fetch.call_count, 2)
        # A cycle here is sufficient to stop the second nested descent.
        self.assertIn("block_cycle", status["truncation_reasons"])
        with mock.patch.object(MODULE, "MAX_BLOCK_REQUESTS", 1), mock.patch.object(MODULE.NotionClient, "request_json", return_value=page([block("child", True)])) as fetch:
            _, _, status = MODULE.recurse_blocks_to_markdown(self.client(), PAGE_ID)
        self.assertEqual(fetch.call_count, 1)
        self.assertIn("request_limit", status["truncation_reasons"])

    def test_depth_limit_and_missing_child_id_are_explicit(self):
        with mock.patch.object(MODULE, "MAX_BLOCK_DEPTH", 1), mock.patch.object(MODULE.NotionClient, "request_json", return_value=page([block(children=True)])):
            _, _, status = MODULE.recurse_blocks_to_markdown(self.client(), PAGE_ID)
            self.assertIn("depth_limit", status["truncation_reasons"])
        with mock.patch.object(MODULE.NotionClient, "request_json", return_value=page([block(None, True)])):
            _, _, status = MODULE.recurse_blocks_to_markdown(self.client(), PAGE_ID)
            self.assertIn("missing_block_id", status["truncation_reasons"])

    def test_invalid_block_results_and_excess_results_are_not_complete(self):
        for results, reason in (("bad", "invalid_results"), ([None], "invalid_results"), ([block(), block()], "block_limit")):
            with self.subTest(results=results), mock.patch.object(MODULE.NotionClient, "request_json", return_value=page(results)):
                result = self.client().list_block_children(PAGE_ID, 1)
                self.assertFalse(result["complete"])
                self.assertIn(reason, result["truncation_reasons"])

    def test_rendering_warnings_distinguish_fidelity_from_collection(self):
        with mock.patch.object(MODULE.NotionClient, "request_json", return_value=page([block(kind="image"), block(kind="child_page")])):
            _, _, status = MODULE.recurse_blocks_to_markdown(self.client(), PAGE_ID)
        self.assertTrue(status["complete"])
        self.assertTrue(any("lossy" in warning for warning in status["warnings"]))
        self.assertTrue(any("image" in warning for warning in status["warnings"]))
        self.assertTrue(any("Child-page bodies" in warning for warning in status["warnings"]))

    def test_property_has_more_preserved_in_fetch_and_query(self):
        payload = {"id": PAGE_ID, "object": "page", "properties": {
            "Related": {"type": "relation", "relation": [{"id": CHILD_ID}], "has_more": True},
        }}
        summary = MODULE.build_page_summary(WORKSPACE, payload, None, 0)
        self.assertEqual(summary["page"]["properties"]["Related"], [CHILD_ID])
        self.assertEqual(summary["page"]["property_status"]["has_more"], ["Related"])
        self.assertIs(summary["page"]["property_status"]["complete"], False)
        query = MODULE.build_database_query_summary(WORKSPACE, {}, page([payload]))
        self.assertEqual(query["results"][0]["property_status"], summary["page"]["property_status"])
        self.assertIsNone(MODULE.property_status({"properties": {}})["complete"])

    def test_fetch_without_content_explicitly_not_requested(self):
        with mock.patch.object(MODULE.NotionClient, "get_page", return_value={"id": PAGE_ID}), mock.patch.object(MODULE.NotionClient, "list_block_children") as children:
            result = MODULE.tool_fetch_page({"workspace": "workspace-a", "page_id_or_url": PAGE_ID, "include_content": False})
        children.assert_not_called()
        self.assertIsNone(result["content_markdown"])
        self.assertEqual(result["content_status"]["requested"], False)
        self.assertIsNone(result["content_status"]["complete"])

    def test_query_filter_sorts_and_cursor_forwarded(self):
        with mock.patch.object(MODULE.NotionClient, "request_json", return_value=page([])) as call:
            self.client().query_database(PAGE_ID, 12, "cursor", {"property": "Example"}, [{"timestamp": "created_time"}])
        self.assertEqual(call.call_args.args, ("POST", f"/databases/{PAGE_ID}/query", {
            "page_size": 12, "start_cursor": "cursor", "filter": {"property": "Example"}, "sorts": [{"timestamp": "created_time"}],
        }))


class ProtocolTests(OfflineTests):
    def test_supported_versions_and_unknown_fallback(self):
        for version in (*MODULE.SUPPORTED_PROTOCOL_VERSIONS, "2099-01-01"):
            message = {**INIT, "params": {**INIT["params"], "protocolVersion": version}}
            result = MODULE.handle_request(message)["result"]
            self.assertEqual(result["protocolVersion"], version if version in MODULE.SUPPORTED_PROTOCOL_VERSIONS else MODULE.SUPPORTED_PROTOCOL_VERSIONS[-1])

    def test_malformed_envelopes_do_not_raise(self):
        invalid = [None, [], 1, "request", {}, {"jsonrpc": "1.0"},
                   {"jsonrpc": "2.0", "method": "ping", "id": None},
                   {"jsonrpc": "2.0", "method": "ping", "id": True},
                   {"jsonrpc": "2.0", "method": "ping", "id": 1.5},
                   {"jsonrpc": "2.0", "method": {}, "id": 1}]
        for message in invalid:
            with self.subTest(message=message):
                self.assertEqual(MODULE.handle_request(message)["error"]["code"], -32600)

    def test_string_and_zero_ids_are_preserved(self):
        for identifier in (0, "example-id"):
            self.assertEqual(MODULE.handle_request({"jsonrpc": "2.0", "id": identifier, "method": "ping"})["id"], identifier)

    def test_notifications_and_unsolicited_responses_get_no_reply(self):
        with mock.patch.object(MODULE.NotionClient, "create_page") as write:
            for message in (
                {"jsonrpc": "2.0", "method": "unknown"},
                {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "create_page", "arguments": {"workspace": "workspace-a", "parent": {}, "properties": {}}}},
                {"jsonrpc": "2.0", "id": 10, "result": {}},
                {"jsonrpc": "2.0", "id": 11, "error": {"code": -1, "message": "Example"}},
            ):
                self.assertIsNone(MODULE.handle_request(message))
            write.assert_not_called()

    def test_session_lifecycle_and_duplicate_initialize(self):
        session = MODULE.McpSession()
        listing = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        self.assertIn("error", MODULE.handle_request(listing, session))
        self.assertIn("result", MODULE.handle_request(INIT, session))
        self.assertIn("error", MODULE.handle_request(listing, session))
        MODULE.handle_request({"jsonrpc": "2.0", "method": "notifications/initialized"}, session)
        self.assertIn("result", MODULE.handle_request(listing, session))
        self.assertIn("error", MODULE.handle_request(INIT, session))

    def test_invalid_params_and_initialize_fields_are_protocol_errors(self):
        for params in (None, [], "bad", 0):
            result = MODULE.handle_request({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": params})
            self.assertEqual(result["error"]["code"], -32602)
        for key in ("protocolVersion", "clientInfo", "capabilities"):
            params = dict(INIT["params"])
            del params[key]
            self.assertEqual(MODULE.handle_request({**INIT, "params": params})["error"]["code"], -32602)

    def test_tools_list_annotations_include_writes(self):
        descriptors = {item["name"]: item for item in MODULE.tool_descriptors()}
        self.assertEqual(len(descriptors), 7)
        for name, descriptor in descriptors.items():
            write = name in {"create_page", "append_block_children"}
            self.assertIs(descriptor["annotations"]["readOnlyHint"], not write)
            self.assertIs(descriptor["annotations"]["idempotentHint"], not write)
            self.assertIs(descriptor["annotations"]["destructiveHint"], False)
            self.assertIs(descriptor["annotations"]["openWorldHint"], True)

    def test_argument_validation_rejects_before_network(self):
        cases = [
            ("search", []), ("search", None), ("search", {}),
            ("search", {"workspace": 2, "query": "Example"}),
            ("search", {"workspace": "workspace-a", "query": "Example", "page_size": True}),
            ("search", {"workspace": "workspace-a", "query": "Example", "page_size": 101}),
            ("search", {"workspace": "workspace-a", "query": "Example", "start_cursor": ""}),
            ("search", {"workspace": "workspace-a", "query": "Example", "unexpected": True}),
            ("list_workspaces", {"validate_tokens": "false"}),
            ("create_page", {"workspace": "workspace-a", "parent": {}, "properties": {}, "children": [1]}),
        ]
        for name, arguments in cases:
            with self.subTest(name=name, arguments=arguments):
                result = MODULE.handle_request({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
                self.assertEqual(result["error"]["code"], -32602)
        MODULE.request.urlopen.assert_not_called()

    def test_tool_failure_uses_is_error_and_unknown_method_is_protocol_error(self):
        result = MODULE.handle_request({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "fetch_page", "arguments": {"workspace": "missing", "page_id_or_url": PAGE_ID}}})
        self.assertIs(result["result"]["isError"], True)
        for name in ([], "missing"):
            unknown = MODULE.handle_request({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": name}})
            self.assertEqual(unknown["error"]["code"], -32602)
        unknown = MODULE.handle_request({"jsonrpc": "2.0", "id": 4, "method": "unknown"})
        self.assertEqual(unknown["error"]["code"], -32601)

    def test_both_writes_route_unmodified_to_selected_workspace(self):
        for name, arguments, expected_method, expected_path, upstream in (
            ("create_page", {"parent": {"page_id": PAGE_ID}, "properties": {"title": {"title": []}}, "children": [block()]}, "POST", "/pages", {"id": PAGE_ID}),
            ("append_block_children", {"block_id_or_url": PAGE_ID, "children": [block()]}, "PATCH", f"/blocks/{PAGE_ID}/children", page([block()])),
        ):
            with self.subTest(name=name), mock.patch.object(MODULE.request, "urlopen", return_value=response(upstream)) as opener:
                result = MODULE.handle_request({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": name, "arguments": {"workspace": "workspace-b", **arguments}}})
                self.assertNotIn("error", result)
                self.assertNotIn("isError", result["result"])
                req = opener.call_args.args[0]
                self.assertEqual(req.method, expected_method)
                self.assertEqual(req.full_url, MODULE.NOTION_API_BASE + expected_path)
                self.assertEqual(req.get_header("Authorization"), "Bearer synthetic-token-b")
                expected_payload = {key: value for key, value in arguments.items() if key != "block_id_or_url"}
                self.assertEqual(json.loads(req.data), expected_payload)
                self.assertEqual(opener.call_count, 1)


class StdioTests(OfflineTests):
    def run_server(self, wire):
        bootstrap = '''import runpy, socket, sys, urllib.request

def deny(*a, **k):
    raise AssertionError("Unexpected network access")
socket.socket.connect = deny
socket.socket.connect_ex = deny
socket.getaddrinfo = deny
urllib.request.urlopen = deny
runpy.run_path(sys.argv[1], run_name="__main__")
'''
        result = subprocess.run([sys.executable, "-B", "-c", bootstrap, str(ROOT / "scripts/notion_multi_workspace_server.py")], input=wire, capture_output=True, env=dict(ENV), timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertNotIn(b"Content-Length:", result.stdout)
        self.assertTrue(not result.stdout or result.stdout.endswith(b"\n"))
        return [json.loads(line) for line in result.stdout.splitlines()]

    def test_independent_newline_protocol_and_recovery(self):
        wire = b"{bad json}\n\xff\nnull\n[]\n{\"value\":NaN}\n"
        wire += json.dumps(INIT).encode() + b"\n"
        wire += b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
        wire += b'{"jsonrpc":"2.0","id":0,"method":"tools/list"}\n'
        responses = self.run_server(wire)
        self.assertEqual([item["error"]["code"] for item in responses[:5]], [-32700, -32700, -32600, -32600, -32700])
        self.assertEqual(responses[5]["result"]["protocolVersion"], "2025-11-25")
        self.assertEqual(responses[6]["id"], 0)
        names = {item["name"] for item in responses[6]["result"]["tools"]}
        self.assertIn("create_page", names)
        self.assertIn("append_block_children", names)
        self.assertEqual(len(responses), 7)

    def test_oversized_message_recovers_at_next_line(self):
        responses = self.run_server(b" " * (MODULE.MAX_MESSAGE_BYTES + 20) + b"\n" + b'{"jsonrpc":"2.0","id":0,"method":"ping"}\n')
        self.assertEqual(responses[0]["error"]["code"], -32700)
        self.assertEqual(responses[1]["id"], 0)

    def test_eof_without_newline_is_rejected_and_eof_alone_exits(self):
        self.assertEqual(self.run_server(b""), [])
        self.assertEqual(self.run_server(b'{"jsonrpc":"2.0","id":0,"method":"ping"}')[0]["error"]["code"], -32700)

    def test_unicode_and_embedded_newlines_are_escaped_on_wire(self):
        response_list = self.run_server(b'{"jsonrpc":"2.0","id":"caf\\u00e9\\nexample","method":"ping"}\n')
        self.assertEqual(response_list[0]["id"], "café\nexample")

    def test_lone_surrogate_id_does_not_drop_following_request(self):
        responses = self.run_server(
            b'{"jsonrpc":"2.0","id":"\\ud800","method":"ping"}\n'
            b'{"jsonrpc":"2.0","id":2,"method":"ping"}\n'
        )
        self.assertEqual(responses, [
            {"jsonrpc": "2.0", "id": "\ud800", "result": {}},
            {"jsonrpc": "2.0", "id": 2, "result": {}},
        ])

    def test_deeply_nested_json_recovers_at_next_line(self):
        nested = b"[" * 20000 + b"]" * 20000 + b"\n"
        self.assertLess(len(nested), MODULE.MAX_MESSAGE_BYTES)
        responses = self.run_server(
            nested + b'{"jsonrpc":"2.0","id":2,"method":"ping"}\n'
        )
        self.assertEqual(responses[0]["error"]["code"], -32700)
        self.assertEqual(responses[1], {"jsonrpc": "2.0", "id": 2, "result": {}})
        self.assertEqual(len(responses), 2)

    def test_smokes_ignore_ambient_live_configuration(self):
        environment = dict(ENV)
        environment.update({"NOTION_SMOKE_QUERY": "Must never be sent", "NOTION_SMOKE_PAGE_ID_OR_URL": PAGE_ID,
                            "NOTION_MULTI_WORKSPACE_ENV_FILE": "/nonexistent/offline-test.env", "NOTION_WORKSPACE_KEYS": "ambient"})
        for script in ("smoke_test_stdio.py", "smoke_test_read_side.py"):
            with self.subTest(script=script):
                result = subprocess.run([sys.executable, "-B", str(ROOT / "scripts" / script)], capture_output=True, env=environment, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                self.assertIn(b"PASS: offline", result.stdout)

    def test_live_flags_require_explicit_opt_in(self):
        for script in ("smoke_test_stdio.py", "smoke_test_read_side.py"):
            result = subprocess.run([sys.executable, "-B", str(ROOT / "scripts" / script), "--query", "Example"], capture_output=True, env=dict(ENV), timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b"require explicit --live", result.stderr)


if __name__ == "__main__":
    unittest.main()
