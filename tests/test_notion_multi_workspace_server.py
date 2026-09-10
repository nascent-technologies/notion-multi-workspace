import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from typing import Optional


REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO_ROOT / "scripts" / "notion_multi_workspace_server.py"
SPEC = importlib.util.spec_from_file_location("notion_multi_workspace_server", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class FakeHttpResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.stream = io.BytesIO(json.dumps(payload).encode("utf-8"))
        self.read_sizes: list[int] = []

    def __enter__(self) -> "FakeHttpResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, amount: int = -1) -> bytes:
        self.read_sizes.append(amount)
        return self.stream.read(amount)


class RecordingOpener:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = list(responses)
        self.requests: list[object] = []
        self.request_data_types: list[type[object]] = []
        self.bodies: list[bytes] = []

    def open(self, req: object, timeout: int) -> FakeHttpResponse:
        self.requests.append(req)
        data = req.data
        self.request_data_types.append(type(data))
        if data is None:
            body = b""
        elif isinstance(data, bytes):
            body = data
        else:
            body = b"".join(data)
        self.bodies.append(body)
        return FakeHttpResponse(self.responses.pop(0))


class HostileError(Exception):
    def __str__(self) -> str:
        raise RuntimeError("string conversion must not be attempted")


class FakeStdin:
    def __init__(self, raw: bytes) -> None:
        self.buffer = io.BytesIO(raw)


class NotionMultiWorkspaceServerTests(unittest.TestCase):
    def restore_env(self, previous: dict[str, Optional[str]]) -> None:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def env_snapshot(self) -> dict[str, Optional[str]]:
        tracked = {
            MODULE.WORKSPACE_KEYS_ENV_VAR,
            MODULE.DOTENV_ENV_VAR,
            MODULE.UPLOAD_ROOTS_ENV_VAR,
        }
        for key in ("workspace-a", "workspace-b", "workspace-c", "team-space"):
            tracked.add(MODULE.workspace_name_env_var(key))
            tracked.add(MODULE.workspace_token_env_var(key))
            tracked.add(MODULE.workspace_aliases_env_var(key))
        return {name: os.environ.get(name) for name in tracked}

    def test_list_workspaces_returns_all_configured_bindings(self) -> None:
        previous = self.env_snapshot()
        try:
            os.environ[MODULE.WORKSPACE_KEYS_ENV_VAR] = "workspace-a,workspace-b,workspace-c"
            os.environ[MODULE.workspace_name_env_var("workspace-a")] = "Workspace A"
            os.environ[MODULE.workspace_token_env_var("workspace-a")] = "secret_a"
            os.environ[MODULE.workspace_name_env_var("workspace-b")] = "Workspace B"
            os.environ[MODULE.workspace_token_env_var("workspace-b")] = "secret_b"
            os.environ[MODULE.workspace_name_env_var("workspace-c")] = "Workspace C"
            os.environ[MODULE.workspace_token_env_var("workspace-c")] = "secret_c"
            os.environ[MODULE.workspace_aliases_env_var("workspace-c")] = "team-c,ops-c"

            payload = MODULE.tool_list_workspaces({"validate_tokens": False})
            self.assertEqual(payload["workspace_count"], 3)
            self.assertEqual([item["key"] for item in payload["workspaces"]], ["workspace-a", "workspace-b", "workspace-c"])
            self.assertIn("ops-c", payload["workspaces"][2]["aliases"])
        finally:
            self.restore_env(previous)

    def test_load_workspace_configs_honors_env_file_override(self) -> None:
        previous = self.env_snapshot()
        try:
            for name in previous:
                os.environ.pop(name, None)

            with tempfile.TemporaryDirectory() as tmpdir:
                env_path = Path(tmpdir) / "notion-multi-workspace.env"
                env_path.write_text(
                    "\n".join(
                        [
                            "NOTION_WORKSPACE_KEYS=workspace-a,workspace-b,workspace-c",
                            "NOTION_WORKSPACE_WORKSPACE_A_NAME=Workspace A",
                            "NOTION_WORKSPACE_WORKSPACE_A_TOKEN=secret_a",
                            "NOTION_WORKSPACE_WORKSPACE_B_NAME=Workspace B",
                            "NOTION_WORKSPACE_WORKSPACE_B_TOKEN=secret_b",
                            "NOTION_WORKSPACE_WORKSPACE_C_NAME=Workspace C",
                            "NOTION_WORKSPACE_WORKSPACE_C_TOKEN=secret_c",
                            "NOTION_WORKSPACE_WORKSPACE_C_ALIASES=team-c,ops-c",
                        ]
                    )
                    + "\n"
                )
                os.environ[MODULE.DOTENV_ENV_VAR] = str(env_path)

                configs = MODULE.load_workspace_configs()

            self.assertEqual(configs["workspace-a"].name, "Workspace A")
            self.assertEqual(configs["workspace-b"].token, "secret_b")
            self.assertEqual(configs["workspace-c"].extra_aliases, ("team-c", "ops-c"))
        finally:
            self.restore_env(previous)

    def test_hyphenated_workspace_keys_map_to_underscore_env_vars(self) -> None:
        self.assertEqual(
            MODULE.workspace_name_env_var("team-space"),
            "NOTION_WORKSPACE_TEAM_SPACE_NAME",
        )
        self.assertEqual(
            MODULE.workspace_token_env_var("team-space"),
            "NOTION_WORKSPACE_TEAM_SPACE_TOKEN",
        )
        self.assertEqual(
            MODULE.workspace_aliases_env_var("team-space"),
            "NOTION_WORKSPACE_TEAM_SPACE_ALIASES",
        )

    def test_canonicalize_notion_id_from_raw_and_url(self) -> None:
        raw_id = "33daa70e43f1801c8441e88c36e22608"
        expected = "33daa70e-43f1-801c-8441-e88c36e22608"
        url = "https://www.notion.so/Page-Title-33daa70e43f1801c8441e88c36e22608?pvs=4"

        self.assertEqual(MODULE.canonicalize_notion_id(raw_id), expected)
        self.assertEqual(MODULE.canonicalize_notion_id(url), expected)

    def test_resolve_workspace_matches_key_name_and_aliases(self) -> None:
        workspaces = {
            "workspace-a": MODULE.WorkspaceConfig("workspace-a", "Workspace A", "token-a"),
            "workspace-b": MODULE.WorkspaceConfig("workspace-b", "Workspace B", "token-b"),
            "workspace-c": MODULE.WorkspaceConfig("workspace-c", "Workspace C", "token-c", ("team-c", "ops-c")),
        }

        self.assertEqual(MODULE.resolve_workspace("workspace-a", workspaces).name, "Workspace A")
        self.assertEqual(MODULE.resolve_workspace("workspace-a", workspaces).key, "workspace-a")
        self.assertEqual(MODULE.resolve_workspace("Workspace B", workspaces).key, "workspace-b")
        self.assertEqual(MODULE.resolve_workspace("ops-c", workspaces).key, "workspace-c")

    def test_load_workspace_configs_rejects_ambiguous_aliases(self) -> None:
        previous = self.env_snapshot()
        try:
            os.environ[MODULE.WORKSPACE_KEYS_ENV_VAR] = "workspace-a,workspace-b"
            os.environ[MODULE.workspace_name_env_var("workspace-a")] = "Workspace A"
            os.environ[MODULE.workspace_token_env_var("workspace-a")] = "secret_a"
            os.environ[MODULE.workspace_aliases_env_var("workspace-a")] = "shared"
            os.environ[MODULE.workspace_name_env_var("workspace-b")] = "Workspace B"
            os.environ[MODULE.workspace_token_env_var("workspace-b")] = "secret_b"
            os.environ[MODULE.workspace_aliases_env_var("workspace-b")] = "shared"

            with self.assertRaises(MODULE.ConfigError):
                MODULE.load_workspace_configs()
        finally:
            self.restore_env(previous)

    def test_extract_page_title_and_property_simplification(self) -> None:
        page = {
            "properties": {
                "Name": {
                    "type": "title",
                    "title": [{"plain_text": "Loan Margin Call Spec"}],
                },
                "Status": {
                    "type": "status",
                    "status": {"name": "In Progress"},
                },
                "Reviewers": {
                    "type": "people",
                    "people": [{"name": "Julian Kocher"}, {"id": "user-2"}],
                },
            }
        }

        self.assertEqual(MODULE.extract_page_title(page), "Loan Margin Call Spec")
        simplified = {
            key: MODULE.simplify_property_value(value)
            for key, value in page["properties"].items()
        }
        self.assertEqual(simplified["Status"], "In Progress")
        self.assertEqual(simplified["Reviewers"], ["Julian Kocher", "user-2"])

    def test_build_database_summary_and_query_summary(self) -> None:
        workspace = MODULE.WorkspaceConfig("workspace-a", "Workspace A", "token-a")
        database = {
            "id": "db-1",
            "title": [{"plain_text": "Project Tracker"}],
            "url": "https://www.notion.so/db-1",
            "created_time": "2026-04-13T00:00:00.000Z",
            "last_edited_time": "2026-04-13T01:00:00.000Z",
            "archived": False,
            "in_trash": False,
            "parent": {"type": "workspace", "workspace": True},
            "properties": {
                "Name": {"id": "title", "type": "title"},
                "Status": {"id": "status", "type": "status"},
            },
        }
        response = {
            "results": [
                {
                    "object": "page",
                    "id": "page-1",
                    "url": "https://www.notion.so/page-1",
                    "last_edited_time": "2026-04-13T02:00:00.000Z",
                    "parent": {"type": "database_id", "database_id": "db-1"},
                    "properties": {
                        "Name": {"type": "title", "title": [{"plain_text": "Task A"}]},
                        "Status": {"type": "status", "status": {"name": "Open"}},
                    },
                }
            ],
            "has_more": False,
            "next_cursor": None,
        }

        db_summary = MODULE.build_database_summary(workspace, database)
        query_summary = MODULE.build_database_query_summary(workspace, database, response)

        self.assertEqual(db_summary["database"]["title"], "Project Tracker")
        self.assertEqual(query_summary["count"], 1)
        self.assertEqual(query_summary["results"][0]["title"], "Task A")
        self.assertEqual(query_summary["results"][0]["properties"]["Status"], "Open")

    def test_list_workspaces_reports_read_and_write_tool_sets(self) -> None:
        workspace = MODULE.WorkspaceConfig("workspace-a", "Workspace A", "token-a")
        with mock.patch.object(MODULE, "load_workspace_configs", return_value={"workspace-a": workspace}):
            payload = MODULE.tool_list_workspaces({})

        self.assertEqual(payload["read_only_tools"], [
            "list_workspaces",
            "search",
            "fetch_page",
            "fetch_database",
            "query_database",
        ])
        self.assertEqual(
            payload["write_tools"],
            [
                "create_page",
                "append_block_children",
                "upload_file",
                "upload_and_append_file_block",
            ],
        )

    def test_upload_surface_declares_current_version_and_strict_file_path(self) -> None:
        self.assertEqual(MODULE.SERVER_VERSION, "0.4.0")
        self.assertEqual(
            getattr(MODULE, "FILE_UPLOAD_NOTION_VERSION", None),
            "2026-03-11",
        )

        upload_tool = MODULE.TOOLS.get("upload_file")
        attach_tool = MODULE.TOOLS.get("upload_and_append_file_block")
        self.assertIsNotNone(upload_tool)
        self.assertIsNotNone(attach_tool)

        for tool in (upload_tool, attach_tool):
            assert tool is not None
            schema = tool["inputSchema"]
            self.assertIn("file_path", schema["required"])
            self.assertNotIn("path", schema["properties"])
            self.assertFalse(schema["additionalProperties"])

    def test_secure_upload_primitives_are_declared(self) -> None:
        self.assertTrue(callable(getattr(MODULE, "parse_upload_roots", None)))
        self.assertTrue(callable(getattr(MODULE, "open_approved_upload", None)))
        self.assertTrue(callable(getattr(MODULE, "validate_upload_filename", None)))
        self.assertTrue(callable(getattr(MODULE, "validate_upload_content_type", None)))

    def test_multipart_and_upload_client_primitives_are_declared(self) -> None:
        self.assertTrue(callable(getattr(MODULE, "MultipartFileBody", None)))
        self.assertTrue(callable(getattr(MODULE, "build_notion_opener", None)))
        self.assertTrue(callable(getattr(MODULE, "validate_file_upload", None)))

    def test_attachment_primitives_are_declared(self) -> None:
        self.assertTrue(callable(getattr(MODULE, "normalize_position", None)))
        self.assertTrue(callable(getattr(MODULE, "infer_upload_block_type", None)))
        self.assertTrue(callable(getattr(MODULE, "build_file_upload_block", None)))

    def test_parse_upload_roots_is_explicit_absolute_and_fail_closed(self) -> None:
        for missing in (None, "", "   "):
            with self.subTest(missing=missing):
                with self.assertRaises(MODULE.McpProtocolError):
                    MODULE.parse_upload_roots(missing)

        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as first, tempfile.TemporaryDirectory(
            dir=REPO_ROOT
        ) as second:
            roots = MODULE.parse_upload_roots(os.pathsep.join((first, second)))
            self.assertEqual(roots, (Path(first), Path(second)))

            with self.assertRaises(MODULE.ConfigError):
                MODULE.parse_upload_roots("relative/uploads")
            with self.assertRaises(MODULE.ConfigError):
                MODULE.parse_upload_roots("/")
            with self.assertRaises(MODULE.ConfigError):
                MODULE.parse_upload_roots(f"{first}{os.pathsep}{os.pathsep}{second}")
            with self.assertRaises(MODULE.ConfigError):
                MODULE.parse_upload_roots(str(Path(first) / ".." / Path(second).name))

    def test_parse_upload_roots_rejects_files_and_symlinked_components(self) -> None:
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmpdir:
            base = Path(tmpdir)
            regular_file = base / "not-a-directory"
            regular_file.write_text("x")
            with self.assertRaises(MODULE.ConfigError):
                MODULE.parse_upload_roots(str(regular_file))

            target = base / "real-root"
            target.mkdir()
            linked = base / "linked-root"
            linked.symlink_to(target, target_is_directory=True)
            with self.assertRaises(MODULE.ConfigError):
                MODULE.parse_upload_roots(str(linked))

    def test_root_walk_closes_descriptors_when_a_close_fails(self) -> None:
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmpdir:
            real_open = os.open
            real_close = os.close
            opened_descriptors: list[int] = []
            injected_failure = False

            def tracking_open(
                path: object, flags: int, *args: object, **kwargs: object
            ) -> int:
                descriptor = real_open(path, flags, *args, **kwargs)
                opened_descriptors.append(descriptor)
                return descriptor

            def fail_first_close(descriptor: int) -> None:
                nonlocal injected_failure
                if not injected_failure:
                    injected_failure = True
                    raise OSError("synthetic close failure")
                real_close(descriptor)

            with mock.patch.object(
                MODULE.os, "open", side_effect=tracking_open
            ), mock.patch.object(MODULE.os, "close", side_effect=fail_first_close):
                with self.assertRaises(MODULE.ConfigError):
                    MODULE._open_absolute_directory_nofollow(Path(tmpdir))

            self.assertGreaterEqual(len(opened_descriptors), 2)
            for descriptor in set(opened_descriptors):
                with self.assertRaises(OSError):
                    os.fstat(descriptor)

    def test_upload_filename_and_content_type_reject_header_injection(self) -> None:
        self.assertEqual(MODULE.validate_upload_filename("wiki-shot.png"), "wiki-shot.png")
        self.assertEqual(MODULE.validate_upload_content_type("image/png"), "image/png")

        for unsafe in (
            "",
            ".",
            "..",
            "../shot.png",
            "folder/shot.png",
            "folder\\shot.png",
            "shot\r\nX-Evil: yes.png",
            "quote\".png",
            "nul\x00.png",
        ):
            with self.subTest(filename=repr(unsafe)):
                with self.assertRaises(MODULE.McpProtocolError):
                    MODULE.validate_upload_filename(unsafe)

        for unsafe in (
            "",
            "image",
            "image/png; charset=utf-8",
            "image/png\r\nX-Evil: yes",
            " image/png",
            "image/png ",
            "image/\x00png",
        ):
            with self.subTest(content_type=repr(unsafe)):
                with self.assertRaises(MODULE.McpProtocolError):
                    MODULE.validate_upload_content_type(unsafe)

    def test_open_approved_upload_holds_and_streams_a_regular_file(self) -> None:
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmpdir:
            root = Path(tmpdir)
            nested = root / "nested"
            nested.mkdir()
            file_path = nested / "wiki-shot.png"
            contents = b"trusted-upload-bytes"
            file_path.write_bytes(contents)
            roots = MODULE.parse_upload_roots(str(root))

            try:
                approved = MODULE.open_approved_upload(str(file_path), roots)
            except Exception as exc:  # pragma: no cover - makes RED an assertion failure
                self.fail(f"approved regular file was rejected: {type(exc).__name__}")

            with approved:
                self.assertEqual(approved.size, len(contents))
                self.assertEqual(approved.local_name, "wiki-shot.png")
                chunks = list(approved.iter_chunks(chunk_size=5))
                self.assertEqual(b"".join(chunks), contents)
                self.assertTrue(all(0 < len(chunk) <= 5 for chunk in chunks))
                approved.revalidate()

            with self.assertRaises(OSError):
                os.fstat(approved.fileno())

    def test_open_approved_upload_rejects_outside_traversal_and_links(self) -> None:
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmpdir, tempfile.TemporaryDirectory(
            dir=REPO_ROOT
        ) as outside_dir:
            root = Path(tmpdir)
            roots = MODULE.parse_upload_roots(str(root))
            outside = Path(outside_dir) / "outside.txt"
            outside.write_text("outside")

            with self.assertRaises(MODULE.McpProtocolError):
                MODULE.open_approved_upload("relative.txt", roots)
            with self.assertRaises(MODULE.McpProtocolError):
                MODULE.open_approved_upload(str(outside), roots)

            inside = root / "inside.txt"
            inside.write_text("inside")
            traversal = f"{root}/nested/../inside.txt"
            with self.assertRaises(MODULE.McpProtocolError):
                MODULE.open_approved_upload(traversal, roots)

            linked_file = root / "linked-file.txt"
            linked_file.symlink_to(outside)
            with self.assertRaises(MODULE.McpProtocolError):
                MODULE.open_approved_upload(str(linked_file), roots)

            real_directory = root / "real-directory"
            real_directory.mkdir()
            (real_directory / "payload.txt").write_text("inside")
            linked_directory = root / "linked-directory"
            linked_directory.symlink_to(real_directory, target_is_directory=True)
            with self.assertRaises(MODULE.McpProtocolError):
                MODULE.open_approved_upload(
                    str(linked_directory / "payload.txt"), roots
                )

    def test_open_approved_upload_rejects_nonregular_and_oversize_files(self) -> None:
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmpdir:
            root = Path(tmpdir)
            roots = MODULE.parse_upload_roots(str(root))

            directory_leaf = root / "directory"
            directory_leaf.mkdir()
            with self.assertRaises(MODULE.McpProtocolError):
                MODULE.open_approved_upload(str(directory_leaf), roots)

            fifo = root / "named-pipe"
            os.mkfifo(fifo)
            with mock.patch.object(MODULE.os, "open", wraps=os.open) as open_mock:
                with self.assertRaises(MODULE.McpProtocolError):
                    MODULE.open_approved_upload(str(fifo), roots)
            self.assertFalse(
                any(call.args and call.args[0] == fifo.name for call in open_mock.call_args_list)
            )

            oversize = root / "oversize.bin"
            with oversize.open("wb") as stream:
                stream.truncate(MODULE.MAX_UPLOAD_BYTES + 1)
            with self.assertRaises(MODULE.McpProtocolError):
                MODULE.open_approved_upload(str(oversize), roots)

    def test_open_approved_upload_rejects_path_replacement_before_network(self) -> None:
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmpdir:
            root = Path(tmpdir)
            original = root / "payload.txt"
            replacement = root / "replacement.txt"
            original.write_text("original")
            replacement.write_text("replacement")
            try:
                approved = MODULE.open_approved_upload(
                    str(original), MODULE.parse_upload_roots(str(root))
                )
            except Exception as exc:  # pragma: no cover - makes RED an assertion failure
                self.fail(f"approved regular file was rejected: {type(exc).__name__}")
            try:
                os.replace(replacement, original)
                with self.assertRaises(MODULE.McpProtocolError):
                    approved.revalidate()
            finally:
                approved.close()

    def test_leaf_descriptor_is_closed_when_post_open_validation_fails(self) -> None:
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmpdir:
            root = Path(tmpdir)
            file_path = root / "payload.txt"
            file_path.write_text("payload")
            observed_descriptors: list[int] = []

            def fail_fstat(descriptor: int) -> os.stat_result:
                observed_descriptors.append(descriptor)
                raise OSError("synthetic fstat failure")

            with mock.patch.object(MODULE.os, "fstat", side_effect=fail_fstat):
                with self.assertRaises(MODULE.McpProtocolError):
                    MODULE.open_approved_upload(
                        str(file_path), MODULE.parse_upload_roots(str(root))
                    )

            self.assertEqual(len(observed_descriptors), 1)
            with self.assertRaises(OSError):
                os.fstat(observed_descriptors[0])

    def test_parent_descriptor_is_closed_when_component_open_fails(self) -> None:
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmpdir:
            root = Path(tmpdir)
            blocked = root / "blocked"
            blocked.mkdir()
            (blocked / "payload.txt").write_text("payload")
            roots = MODULE.parse_upload_roots(str(root))
            real_open = os.open
            observed_descriptors: list[int] = []

            def tracking_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
                if path == "blocked":
                    raise OSError("synthetic component failure")
                descriptor = real_open(path, flags, *args, **kwargs)
                observed_descriptors.append(descriptor)
                return descriptor

            with mock.patch.object(MODULE.os, "open", side_effect=tracking_open):
                with self.assertRaises(MODULE.McpProtocolError):
                    MODULE.open_approved_upload(str(blocked / "payload.txt"), roots)

            for descriptor in set(observed_descriptors):
                with self.assertRaises(OSError):
                    os.fstat(descriptor)

    def test_leaf_walk_closes_old_and_new_descriptors_when_close_fails(self) -> None:
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmpdir:
            root = Path(tmpdir)
            nested = root / "nested"
            nested.mkdir()
            file_path = nested / "payload.txt"
            file_path.write_text("payload")
            roots = MODULE.parse_upload_roots(str(root))
            real_open = os.open
            real_close = os.close
            opened_descriptors: list[int] = []
            descriptor_to_fail: int | None = None
            injected_failure = False

            def tracking_open(
                path: object, flags: int, *args: object, **kwargs: object
            ) -> int:
                nonlocal descriptor_to_fail
                descriptor = real_open(path, flags, *args, **kwargs)
                opened_descriptors.append(descriptor)
                if path == "nested":
                    candidate = kwargs.get("dir_fd")
                    if isinstance(candidate, int):
                        descriptor_to_fail = candidate
                return descriptor

            def fail_parent_close(descriptor: int) -> None:
                nonlocal injected_failure
                if descriptor == descriptor_to_fail and not injected_failure:
                    injected_failure = True
                    raise OSError("synthetic close failure")
                real_close(descriptor)

            with mock.patch.object(
                MODULE.os, "open", side_effect=tracking_open
            ), mock.patch.object(MODULE.os, "close", side_effect=fail_parent_close):
                with self.assertRaises(MODULE.McpProtocolError):
                    MODULE.open_approved_upload(str(file_path), roots)

            self.assertTrue(injected_failure)
            for descriptor in set(opened_descriptors):
                with self.assertRaises(OSError):
                    os.fstat(descriptor)

    def test_multipart_body_streams_one_exact_file_part_with_content_length(self) -> None:
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmpdir:
            root = Path(tmpdir)
            file_path = root / "wiki-shot.png"
            contents = b"a" * (70 * 1024)
            file_path.write_bytes(contents)
            approved = MODULE.open_approved_upload(
                str(file_path), MODULE.parse_upload_roots(str(root))
            )
            with approved:
                body = MODULE.MultipartFileBody(
                    approved, "wiki-shot.png", "image/png"
                )
                self.assertNotIsInstance(body, (bytes, bytearray, memoryview))
                chunks = list(body)
                encoded = b"".join(chunks)

                self.assertEqual(len(encoded), body.content_length)
                self.assertEqual(encoded.count(b'name="file"'), 1)
                self.assertEqual(encoded.count(b'filename="wiki-shot.png"'), 1)
                self.assertEqual(encoded.count(b"Content-Type: image/png"), 1)
                self.assertNotIn(b"part_number", encoded)
                self.assertIn(contents, encoded)
                self.assertTrue(
                    all(
                        0 < len(chunk) <= MODULE.UPLOAD_CHUNK_BYTES
                        for chunk in chunks[1:-1]
                    )
                )
                with self.assertRaises(MODULE.McpProtocolError):
                    list(body)

    def test_file_upload_response_requires_exact_uuid_lifecycle_and_id_match(self) -> None:
        upload_id = "33daa70e-43f1-801c-8441-e88c36e22608"
        payload = {
            "object": "file_upload",
            "id": upload_id,
            "status": "pending",
            "upload_url": "https://attacker.invalid/steal",
            "filename": "ignored-raw-name.png",
        }
        self.assertEqual(
            MODULE.validate_file_upload(payload, expected_status="pending"),
            {"id": upload_id, "status": "pending"},
        )

        invalid_payloads = (
            {**payload, "object": "page"},
            {**payload, "id": f"prefix-{upload_id}"},
            {**payload, "status": "uploaded"},
            {**payload, "status": "failed"},
        )
        for invalid in invalid_payloads:
            with self.subTest(payload=invalid):
                with self.assertRaises(MODULE.NotionApiError):
                    MODULE.validate_file_upload(invalid, expected_status="pending")

        with self.assertRaises(MODULE.NotionApiError):
            MODULE.validate_file_upload(
                {**payload, "status": "uploaded"},
                expected_status="uploaded",
                expected_id="11111111-1111-1111-1111-111111111111",
            )

    def test_build_notion_opener_refuses_redirects(self) -> None:
        opener = MODULE.build_notion_opener()
        redirect_handler_type = getattr(MODULE, "NoRedirectHandler", None)
        self.assertIsNotNone(redirect_handler_type)
        self.assertTrue(
            any(isinstance(handler, redirect_handler_type) for handler in opener.handlers)
        )

    def test_upload_small_file_uses_fixed_modern_endpoints_and_streaming_body(self) -> None:
        upload_id = "33daa70e-43f1-801c-8441-e88c36e22608"
        opener = RecordingOpener(
            [
                {
                    "object": "file_upload",
                    "id": upload_id,
                    "status": "pending",
                    "upload_url": "https://attacker.invalid/steal",
                },
                {
                    "object": "file_upload",
                    "id": upload_id,
                    "status": "uploaded",
                },
            ]
        )
        workspace = MODULE.WorkspaceConfig(
            "workspace-a", "Workspace A", "token-under-test"
        )
        try:
            client = MODULE.NotionClient(workspace, opener=opener)
        except Exception as exc:  # pragma: no cover - makes RED an assertion failure
            self.fail(f"NotionClient rejected an injected opener: {type(exc).__name__}")

        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmpdir:
            root = Path(tmpdir)
            file_path = root / "wiki-shot.png"
            file_path.write_bytes(b"trusted-file-bytes")
            approved = MODULE.open_approved_upload(
                str(file_path), MODULE.parse_upload_roots(str(root))
            )
            with approved:
                try:
                    result = client.upload_small_file(
                        approved,
                        filename="wiki-shot.png",
                        content_type="image/png",
                    )
                except Exception as exc:  # pragma: no cover - RED assertion failure
                    self.fail(f"upload flow is unavailable: {type(exc).__name__}")

        self.assertEqual(result, {"id": upload_id, "status": "uploaded"})
        self.assertEqual(
            [req.full_url for req in opener.requests],
            [
                "https://api.notion.com/v1/file_uploads",
                f"https://api.notion.com/v1/file_uploads/{upload_id}/send",
            ],
        )
        self.assertEqual(
            [req.get_method() for req in opener.requests], ["POST", "POST"]
        )
        self.assertEqual(len(opener.requests), len(opener.bodies))
        for req, body in zip(opener.requests, opener.bodies):
            headers = dict(req.header_items())
            self.assertEqual(headers["Authorization"], "Bearer token-under-test")
            self.assertEqual(
                headers["Notion-version"], MODULE.FILE_UPLOAD_NOTION_VERSION
            )
            self.assertEqual(int(headers["Content-length"]), len(body))

        self.assertEqual(
            json.loads(opener.bodies[0].decode("utf-8")),
            {
                "mode": "single_part",
                "filename": "wiki-shot.png",
                "content_type": "image/png",
            },
        )
        self.assertIs(opener.request_data_types[1], MODULE.MultipartFileBody)
        multipart_body = opener.bodies[1]
        self.assertEqual(multipart_body.count(b'name="file"'), 1)
        self.assertNotIn(b"attacker.invalid", multipart_body)

    def test_position_validation_is_bounded_and_matches_modern_contract(self) -> None:
        block_id = "33daa70e43f1801c8441e88c36e22608"
        canonical = "33daa70e-43f1-801c-8441-e88c36e22608"
        self.assertIsNone(MODULE.normalize_position(None))
        self.assertEqual(MODULE.normalize_position({"type": "start"}), {"type": "start"})
        self.assertEqual(MODULE.normalize_position({"type": "end"}), {"type": "end"})
        self.assertEqual(
            MODULE.normalize_position(
                {"type": "after_block", "after_block": {"id": block_id}}
            ),
            {"type": "after_block", "after_block": {"id": canonical}},
        )

        invalid_positions = (
            {},
            {"type": "middle"},
            {"type": "start", "extra": True},
            {"type": "after_block"},
            {"type": "after_block", "after_block": {"id": "not-a-uuid"}},
            {
                "type": "after_block",
                "after_block": {"id": block_id, "extra": True},
            },
            "start",
            {"type": "start", "padding": "x" * 600},
        )
        for invalid in invalid_positions:
            with self.subTest(position=invalid):
                with self.assertRaises(MODULE.McpProtocolError):
                    MODULE.normalize_position(invalid)

    def test_file_upload_block_is_minimal_typed_and_caption_bounded(self) -> None:
        upload_id = "33daa70e43f1801c8441e88c36e22608"
        canonical = "33daa70e-43f1-801c-8441-e88c36e22608"
        self.assertEqual(MODULE.infer_upload_block_type("image/png"), "image")
        self.assertEqual(MODULE.infer_upload_block_type("application/pdf"), "pdf")
        self.assertEqual(MODULE.infer_upload_block_type("audio/wav"), "audio")
        self.assertEqual(MODULE.infer_upload_block_type("video/mp4"), "video")
        self.assertEqual(
            MODULE.infer_upload_block_type("application/octet-stream"), "file"
        )

        block = MODULE.build_file_upload_block(
            "image", upload_id, caption="Home screenshot"
        )
        self.assertEqual(
            block,
            {
                "object": "block",
                "type": "image",
                "image": {
                    "type": "file_upload",
                    "file_upload": {"id": canonical},
                    "caption": [
                        {
                            "type": "text",
                            "text": {"content": "Home screenshot"},
                        }
                    ],
                },
            },
        )
        with self.assertRaises(MODULE.McpProtocolError):
            MODULE.build_file_upload_block("bookmark", upload_id)
        with self.assertRaises(MODULE.McpProtocolError):
            MODULE.build_file_upload_block("file", upload_id, caption="x" * 2001)

    def test_plain_append_stays_legacy_while_position_uses_modern_version(self) -> None:
        workspace = MODULE.WorkspaceConfig("workspace-a", "Workspace A", "token")
        block_id = "33daa70e43f1801c8441e88c36e22608"
        children = [{"object": "block", "type": "divider", "divider": {}}]

        legacy_opener = RecordingOpener([{"results": []}])
        legacy_client = MODULE.NotionClient(workspace, opener=legacy_opener)
        legacy_client.append_block_children(block_id, children)
        legacy_request = legacy_opener.requests[0]
        self.assertEqual(
            dict(legacy_request.header_items())["Notion-version"], MODULE.NOTION_VERSION
        )
        self.assertEqual(json.loads(legacy_opener.bodies[0]), {"children": children})

        modern_opener = RecordingOpener([{"results": []}])
        modern_client = MODULE.NotionClient(workspace, opener=modern_opener)
        try:
            modern_client.append_block_children(
                block_id, children, position={"type": "start"}
            )
        except Exception as exc:  # pragma: no cover - makes RED an assertion failure
            self.fail(f"modern position append is unavailable: {type(exc).__name__}")
        modern_request = modern_opener.requests[0]
        self.assertEqual(
            dict(modern_request.header_items())["Notion-version"],
            MODULE.FILE_UPLOAD_NOTION_VERSION,
        )
        self.assertEqual(
            json.loads(modern_opener.bodies[0]),
            {"children": children, "position": {"type": "start"}},
        )

    def test_upload_tool_requires_file_path_and_disabled_roots_fail_before_network(self) -> None:
        previous = self.env_snapshot()
        workspace = MODULE.WorkspaceConfig("workspace-a", "Workspace A", "token")
        try:
            os.environ.pop(MODULE.UPLOAD_ROOTS_ENV_VAR, None)
            with mock.patch.object(
                MODULE, "load_workspace_configs", return_value={"workspace-a": workspace}
            ), mock.patch.object(MODULE, "build_notion_opener") as opener_factory:
                with self.assertRaisesRegex(MODULE.McpProtocolError, "disabled"):
                    MODULE.tool_upload_file(
                        {"workspace": "workspace-a", "file_path": "/tmp/file.txt"}
                    )
                opener_factory.assert_not_called()

            with self.assertRaisesRegex(MODULE.McpProtocolError, "file_path"):
                MODULE.tool_upload_file(
                    {"workspace": "workspace-a", "path": "/tmp/file.txt"}
                )
        finally:
            self.restore_env(previous)

    def test_upload_tool_returns_only_a_safe_summary(self) -> None:
        previous = self.env_snapshot()
        upload_id = "33daa70e-43f1-801c-8441-e88c36e22608"
        workspace = MODULE.WorkspaceConfig("workspace-a", "Workspace A", "token")
        opener = RecordingOpener(
            [
                {
                    "object": "file_upload",
                    "id": upload_id,
                    "status": "pending",
                    "upload_url": "https://attacker.invalid/steal",
                },
                {
                    "object": "file_upload",
                    "id": upload_id,
                    "status": "uploaded",
                    "secret_extra": "must-not-escape",
                },
            ]
        )
        try:
            with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmpdir:
                root = Path(tmpdir)
                file_path = root / "wiki-shot.png"
                file_path.write_bytes(b"png-bits")
                os.environ[MODULE.UPLOAD_ROOTS_ENV_VAR] = str(root)
                with mock.patch.object(
                    MODULE,
                    "load_workspace_configs",
                    return_value={"workspace-a": workspace},
                ), mock.patch.object(
                    MODULE, "build_notion_opener", return_value=opener
                ):
                    try:
                        payload = MODULE.tool_upload_file(
                            {
                                "workspace": "workspace-a",
                                "file_path": str(file_path),
                            }
                        )
                    except Exception as exc:  # pragma: no cover - RED assertion failure
                        self.fail(f"upload tool is unavailable: {type(exc).__name__}")

                self.assertEqual(
                    payload,
                    {
                        "workspace": "Workspace A",
                        "workspace_key": "workspace-a",
                        "file_upload": {
                            "id": upload_id,
                            "status": "uploaded",
                            "filename": "wiki-shot.png",
                            "content_type": "image/png",
                            "content_length": 8,
                        },
                    },
                )
                serialized = json.dumps(payload)
                self.assertNotIn(str(root), serialized)
                self.assertNotIn("upload_url", serialized)
                self.assertNotIn("secret_extra", serialized)
        finally:
            self.restore_env(previous)

    def test_upload_and_append_uses_modern_attachment_contract_without_raw_results(self) -> None:
        previous = self.env_snapshot()
        upload_id = "33daa70e-43f1-801c-8441-e88c36e22608"
        parent_id = "11111111111111111111111111111111"
        workspace = MODULE.WorkspaceConfig("workspace-a", "Workspace A", "token")
        opener = RecordingOpener(
            [
                {"object": "file_upload", "id": upload_id, "status": "pending"},
                {"object": "file_upload", "id": upload_id, "status": "uploaded"},
                {
                    "results": [
                        {"id": "sensitive-raw-block-id", "private": "raw-result"}
                    ]
                },
            ]
        )
        try:
            with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmpdir:
                root = Path(tmpdir)
                file_path = root / "wiki-shot.png"
                file_path.write_bytes(b"png-bits")
                os.environ[MODULE.UPLOAD_ROOTS_ENV_VAR] = str(root)
                with mock.patch.object(
                    MODULE,
                    "load_workspace_configs",
                    return_value={"workspace-a": workspace},
                ), mock.patch.object(
                    MODULE, "build_notion_opener", return_value=opener
                ):
                    try:
                        payload = MODULE.tool_upload_and_append_file_block(
                            {
                                "workspace": "workspace-a",
                                "block_id_or_url": parent_id,
                                "file_path": str(file_path),
                                "caption": "Home screenshot",
                                "position": {"type": "start"},
                            }
                        )
                    except Exception as exc:  # pragma: no cover - RED assertion failure
                        self.fail(f"upload-and-append is unavailable: {type(exc).__name__}")

                append_request = opener.requests[2]
                self.assertEqual(
                    dict(append_request.header_items())["Notion-version"],
                    MODULE.FILE_UPLOAD_NOTION_VERSION,
                )
                append_payload = json.loads(opener.bodies[2])
                self.assertEqual(append_payload["position"], {"type": "start"})
                self.assertEqual(append_payload["children"][0]["type"], "image")
                self.assertEqual(
                    append_payload["children"][0]["image"]["file_upload"]["id"],
                    upload_id,
                )
                self.assertEqual(payload["appended_count"], 1)
                self.assertEqual(payload["appended_block_type"], "image")
                serialized = json.dumps(payload)
                self.assertNotIn(str(root), serialized)
                self.assertNotIn("sensitive-raw-block-id", serialized)
                self.assertNotIn("raw-result", serialized)
        finally:
            self.restore_env(previous)

    def test_upload_and_append_validates_every_append_argument_before_network(self) -> None:
        previous = self.env_snapshot()
        workspace = MODULE.WorkspaceConfig("workspace-a", "Workspace A", "token")
        valid_block_id = "33daa70e-43f1-801c-8441-e88c36e22608"
        try:
            with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmpdir:
                root = Path(tmpdir)
                file_path = root / "wiki-shot.png"
                file_path.write_bytes(b"png-bits")
                os.environ[MODULE.UPLOAD_ROOTS_ENV_VAR] = str(root)
                invalid_arguments = (
                    {"block_id_or_url": "not-a-block-id"},
                    {"block_id_or_url": valid_block_id, "block_type": "bookmark"},
                    {"block_id_or_url": valid_block_id, "caption": {"text": "bad"}},
                    {"block_id_or_url": valid_block_id, "caption": "x" * 2001},
                    {"block_id_or_url": valid_block_id, "caption": "bad\x00caption"},
                )
                with mock.patch.object(
                    MODULE,
                    "load_workspace_configs",
                    return_value={"workspace-a": workspace},
                ), mock.patch.object(
                    MODULE, "build_notion_opener"
                ) as opener_factory, mock.patch.object(
                    MODULE.NotionClient, "upload_small_file"
                ) as upload:
                    for extra_arguments in invalid_arguments:
                        with self.subTest(extra_arguments=extra_arguments):
                            with self.assertRaises(MODULE.McpProtocolError):
                                MODULE.tool_upload_and_append_file_block(
                                    {
                                        "workspace": "workspace-a",
                                        "file_path": str(file_path),
                                        **extra_arguments,
                                    }
                                )
                    opener_factory.assert_not_called()
                    upload.assert_not_called()
        finally:
            self.restore_env(previous)

    def test_handle_request_totally_redacts_expected_and_unexpected_errors(self) -> None:
        sensitive_values = (
            "Bearer REDACTION_SENTINEL_TOKEN",
            "/Users/operator/private/upload.png",
            "upstream-private-response-body",
        )

        def raise_sensitive(_arguments: dict[str, object]) -> None:
            raise RuntimeError(" :: ".join(sensitive_values))

        def raise_expected(_arguments: dict[str, object]) -> None:
            raise MODULE.McpProtocolError(" :: ".join(sensitive_values))

        for name, handler in (
            ("raise_sensitive", raise_sensitive),
            ("raise_expected", raise_expected),
        ):
            stderr = io.StringIO()
            tool = {
                "description": "test error boundary",
                "inputSchema": {"type": "object"},
                "handler": handler,
            }
            with mock.patch.dict(MODULE.TOOLS, {name: tool}, clear=False):
                with contextlib.redirect_stderr(stderr):
                    try:
                        response = MODULE.handle_request(
                            {
                                "jsonrpc": "2.0",
                                "id": 99,
                                "method": "tools/call",
                                "params": {"name": name, "arguments": {}},
                            }
                        )
                    except Exception as exc:  # pragma: no cover - RED assertion failure
                        self.fail(
                            f"error boundary raised instead of responding: {type(exc).__name__}"
                        )

            serialized = json.dumps(response)
            combined = serialized + stderr.getvalue()
            for sensitive in sensitive_values:
                self.assertNotIn(sensitive, combined)
            self.assertLessEqual(len(serialized), MODULE.MAX_PUBLIC_ERROR_BYTES)
            self.assertLessEqual(len(stderr.getvalue()), MODULE.MAX_SAFE_STDERR_BYTES)

    def test_handle_request_does_not_stringify_hostile_exceptions(self) -> None:
        def explode(_arguments: dict[str, object]) -> None:
            raise HostileError()

        stderr = io.StringIO()
        tool = {
            "description": "test hostile exception",
            "inputSchema": {"type": "object"},
            "handler": explode,
        }
        with mock.patch.dict(MODULE.TOOLS, {"explode": tool}, clear=False):
            with contextlib.redirect_stderr(stderr):
                try:
                    response = MODULE.handle_request(
                        {
                            "jsonrpc": "2.0",
                            "id": 100,
                            "method": "tools/call",
                            "params": {"name": "explode", "arguments": {}},
                        }
                    )
                except Exception as exc:  # pragma: no cover - RED assertion failure
                    self.fail(
                        f"hostile exception escaped boundary: {type(exc).__name__}"
                    )
        self.assertIsInstance(response, dict)
        self.assertIn("internal", json.dumps(response))
        self.assertLessEqual(len(stderr.getvalue()), MODULE.MAX_SAFE_STDERR_BYTES)

    def test_expected_error_redaction_is_total_for_hostile_notion_subclasses(self) -> None:
        class HostileNotionError(MODULE.NotionApiError):
            def __getattribute__(self, name: str) -> object:
                if name in {"status", "api_code"}:
                    raise RuntimeError("REDACTION_SENTINEL_ATTRIBUTE")
                return super().__getattribute__(name)

        hostile_error = HostileNotionError()

        def explode(_arguments: dict[str, object]) -> None:
            raise hostile_error

        tool = {
            "description": "test hostile expected exception",
            "inputSchema": {"type": "object"},
            "handler": explode,
        }
        stderr = io.StringIO()
        with mock.patch.dict(MODULE.TOOLS, {"explode": tool}, clear=False):
            with contextlib.redirect_stderr(stderr):
                try:
                    response = MODULE.handle_request(
                        {
                            "jsonrpc": "2.0",
                            "id": 101,
                            "method": "tools/call",
                            "params": {"name": "explode", "arguments": {}},
                        }
                    )
                except Exception as exc:  # pragma: no cover - RED assertion failure
                    self.fail(
                        f"hostile expected exception escaped: {type(exc).__name__}"
                    )
        serialized = json.dumps(response)
        self.assertNotIn("REDACTION_SENTINEL_ATTRIBUTE", serialized)
        self.assertLessEqual(len(serialized), MODULE.MAX_PUBLIC_ERROR_BYTES)
        self.assertLessEqual(len(stderr.getvalue()), MODULE.MAX_SAFE_STDERR_BYTES)

    def test_handle_request_rejects_malformed_shapes_with_bounded_generic_errors(self) -> None:
        messages = (
            (
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": []},
                -32600,
            ),
            ({
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": ["unhashable"], "arguments": {}},
            }, -32602),
            ({
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "search", "arguments": []},
            }, -32602),
            ({
                "jsonrpc": "2.0",
                "id": 4,
                "method": "private-" + "x" * 5000,
            }, -32601),
        )
        for message, expected_code in messages:
            with self.subTest(message_id=message["id"]):
                try:
                    response = MODULE.handle_request(message)
                except Exception as exc:  # pragma: no cover - RED assertion failure
                    self.fail(f"malformed request escaped: {type(exc).__name__}")
                serialized = json.dumps(response)
                self.assertLessEqual(len(serialized), MODULE.MAX_PUBLIC_ERROR_BYTES)
                self.assertNotIn("private-", serialized)
                self.assertIn("error", response)
                self.assertEqual(response["error"]["code"], expected_code)

        huge_id_response = MODULE.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 10**1000,
                "method": "unsupported",
            }
        )
        self.assertIsNone(huge_id_response["id"])
        self.assertLessEqual(
            len(json.dumps(huge_id_response)), MODULE.MAX_PUBLIC_ERROR_BYTES
        )

    def test_token_validation_failure_is_redacted_without_string_conversion(self) -> None:
        workspace = MODULE.WorkspaceConfig("workspace-a", "Workspace A", "token")
        with mock.patch.object(
            MODULE, "load_workspace_configs", return_value={"workspace-a": workspace}
        ), mock.patch.object(MODULE.NotionClient, "get_self", side_effect=HostileError()):
            try:
                payload = MODULE.tool_list_workspaces({"validate_tokens": True})
            except Exception as exc:  # pragma: no cover - RED assertion failure
                self.fail(f"token validation leaked an exception: {type(exc).__name__}")

        serialized = json.dumps(payload)
        self.assertEqual(
            payload["workspaces"][0].get("error"),
            {"category": "internal", "message": "Internal server error."},
        )
        self.assertLessEqual(len(serialized), MODULE.MAX_PUBLIC_ERROR_BYTES)

    def test_notion_success_response_reads_at_most_the_bounded_limit(self) -> None:
        response = FakeHttpResponse({"payload": "x" * MODULE.MAX_NOTION_RESPONSE_BYTES})
        with self.assertRaises(MODULE.NotionApiError):
            MODULE.NotionClient._read_json_response(response)
        self.assertEqual(response.read_sizes, [MODULE.MAX_NOTION_RESPONSE_BYTES + 1])

    def test_stdio_boundary_never_emits_tracebacks_or_raw_header_lines(self) -> None:
        raw_header = b"private-/Users/operator/REDACTION_SENTINEL\r\n\r\n"
        with mock.patch.object(MODULE.sys, "stdin", FakeStdin(raw_header)):
            with self.assertRaises(MODULE.McpProtocolError) as caught:
                MODULE.read_message()
        self.assertNotIn("private-", str(caught.exception))
        self.assertNotIn("/Users/", str(caught.exception))

        stderr = io.StringIO()
        with mock.patch.object(MODULE, "read_message", side_effect=HostileError()):
            with contextlib.redirect_stderr(stderr):
                self.assertEqual(MODULE.serve_forever(), 1)
        self.assertNotIn("Traceback", stderr.getvalue())
        self.assertNotIn("string conversion", stderr.getvalue())
        self.assertLessEqual(len(stderr.getvalue()), MODULE.MAX_SAFE_STDERR_BYTES)

    def test_stdio_rejects_duplicate_and_excessive_header_lines(self) -> None:
        duplicate_length = b"Content-Length: 2\r\nContent-Length: 2\r\n\r\n{}"
        excessive_duplicates = (
            b"X-Test: value\r\n" * (MODULE.MAX_MCP_HEADERS + 1)
            + b"Content-Length: 2\r\n\r\n{}"
        )
        for raw in (duplicate_length, excessive_duplicates):
            with self.subTest(raw_length=len(raw)):
                with mock.patch.object(MODULE.sys, "stdin", FakeStdin(raw)):
                    with self.assertRaises(MODULE.McpProtocolError):
                        MODULE.read_message()

    def test_upload_lifecycle_failure_stops_before_follow_on_requests(self) -> None:
        upload_id = "33daa70e-43f1-801c-8441-e88c36e22608"
        workspace = MODULE.WorkspaceConfig("workspace-a", "Workspace A", "token")
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmpdir:
            root = Path(tmpdir)
            file_path = root / "wiki-shot.png"
            file_path.write_bytes(b"png-bits")

            wrong_state_opener = RecordingOpener(
                [{"object": "file_upload", "id": upload_id, "status": "uploaded"}]
            )
            approved = MODULE.open_approved_upload(
                str(file_path), MODULE.parse_upload_roots(str(root))
            )
            with approved, self.assertRaises(MODULE.NotionApiError):
                MODULE.NotionClient(
                    workspace, opener=wrong_state_opener
                ).upload_small_file(
                    approved, filename="wiki-shot.png", content_type="image/png"
                )
            self.assertEqual(len(wrong_state_opener.requests), 1)

            mismatched_id_opener = RecordingOpener(
                [
                    {"object": "file_upload", "id": upload_id, "status": "pending"},
                    {
                        "object": "file_upload",
                        "id": "11111111-1111-1111-1111-111111111111",
                        "status": "uploaded",
                    },
                ]
            )
            approved = MODULE.open_approved_upload(
                str(file_path), MODULE.parse_upload_roots(str(root))
            )
            with approved, self.assertRaises(MODULE.NotionApiError):
                MODULE.NotionClient(
                    workspace, opener=mismatched_id_opener
                ).upload_small_file(
                    approved, filename="wiki-shot.png", content_type="image/png"
                )
            self.assertEqual(len(mismatched_id_opener.requests), 2)

    def test_file_replacement_after_create_is_rejected_before_send_request(self) -> None:
        upload_id = "33daa70e-43f1-801c-8441-e88c36e22608"
        workspace = MODULE.WorkspaceConfig("workspace-a", "Workspace A", "token")
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmpdir:
            root = Path(tmpdir)
            file_path = root / "wiki-shot.png"
            replacement = root / "replacement.png"
            file_path.write_bytes(b"original")
            replacement.write_bytes(b"replacement")

            class ReplaceAfterCreateOpener(RecordingOpener):
                def open(self, req: object, timeout: int) -> FakeHttpResponse:
                    response = super().open(req, timeout)
                    if len(self.requests) == 1:
                        os.replace(replacement, file_path)
                    return response

            opener = ReplaceAfterCreateOpener(
                [
                    {
                        "object": "file_upload",
                        "id": upload_id,
                        "status": "pending",
                    },
                    {
                        "object": "file_upload",
                        "id": upload_id,
                        "status": "uploaded",
                    },
                ]
            )
            approved = MODULE.open_approved_upload(
                str(file_path), MODULE.parse_upload_roots(str(root))
            )
            with approved, self.assertRaises(MODULE.McpProtocolError):
                MODULE.NotionClient(workspace, opener=opener).upload_small_file(
                    approved, filename="wiki-shot.png", content_type="image/png"
                )
            self.assertEqual(len(opener.requests), 1)

    def test_notion_client_rejects_nonfixed_or_fragmented_request_paths(self) -> None:
        workspace = MODULE.WorkspaceConfig("workspace-a", "Workspace A", "token")
        client = MODULE.NotionClient(workspace, opener=RecordingOpener([]))
        for invalid in (
            "https://attacker.invalid/steal",
            "//attacker.invalid/steal",
            "/../users/me",
            "/%2e%2e/users/me",
            "/users/%2E%2E/me",
            "/users/me#private",
            "/users\\attacker",
            "/users\r\nX-Evil: yes",
        ):
            with self.subTest(path=repr(invalid)):
                with self.assertRaises(MODULE.McpProtocolError):
                    client.request_json("GET", invalid)

    def test_http_error_body_is_never_read_and_response_is_closed_and_redacted(self) -> None:
        workspace = MODULE.WorkspaceConfig("workspace-a", "Workspace A", "token")
        body = io.BytesIO(
            b'{"message":"Bearer REDACTION_SENTINEL /Users/operator/private"}'
        )
        http_error = MODULE.error.HTTPError(
            "https://api.notion.com/v1/users/me",
            302,
            "redirect with private detail",
            {},
            body,
        )

        class HttpErrorOpener:
            def open(self, req: object, timeout: int) -> FakeHttpResponse:
                raise http_error

        client = MODULE.NotionClient(workspace, opener=HttpErrorOpener())
        with self.assertRaises(MODULE.NotionApiError) as caught:
            client.request_json("GET", "/users/me")
        self.assertEqual(
            MODULE.safe_error_record(caught.exception),
            {
                "category": "notion_api",
                "message": "Notion API request failed.",
                "status": 302,
            },
        )
        self.assertTrue(body.closed)

    def test_public_docs_and_plugin_metadata_match_secured_write_capability(self) -> None:
        plugin = json.loads((REPO_ROOT / ".codex-plugin" / "plugin.json").read_text())
        self.assertEqual(plugin["version"], "0.4.0")
        self.assertEqual(
            plugin["interface"]["capabilities"],
            ["Interactive", "Read", "Write"],
        )
        self.assertNotIn("read-only", json.dumps(plugin).lower())

        readme = (REPO_ROOT / "README.md").read_text()
        skill = (REPO_ROOT / "skills" / "notion-multi-workspace" / "SKILL.md").read_text()
        env_example = (REPO_ROOT / ".env.example").read_text()
        for document in (readme, skill):
            self.assertIn("upload_file", document)
            self.assertIn("upload_and_append_file_block", document)
            self.assertIn("NOTION_UPLOAD_ROOTS", document)
            self.assertIn("20 MiB", document)
        self.assertIn("NOTION_UPLOAD_ROOTS=", env_example)
        self.assertNotIn("NOTION_UPLOAD_ROOTS=/", env_example)
        self.assertNotIn("secret_workspace", env_example)
        self.assertIn(
            "python3 tests/test_notion_multi_workspace_server.py", readme
        )
        self.assertIn("Python 3.9 or newer", readme)
        self.assertNotIn(
            "python3 -m unittest tests/test_notion_multi_workspace_server.py",
            readme,
        )

    def test_render_block_markdown_handles_common_blocks(self) -> None:
        heading = {
            "type": "heading_2",
            "heading_2": {"rich_text": [{"plain_text": "Overview"}]},
        }
        todo = {
            "type": "to_do",
            "to_do": {
                "checked": True,
                "rich_text": [{"plain_text": "Confirm workspace routing"}],
            },
        }

        self.assertEqual(MODULE.render_block_markdown(heading), ["## Overview"])
        self.assertEqual(
            MODULE.render_block_markdown(todo),
            ["- [x] Confirm workspace routing"],
        )


if __name__ == "__main__":
    unittest.main()
