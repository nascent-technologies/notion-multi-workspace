import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Optional


REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO_ROOT / "scripts" / "notion_multi_workspace_server.py"
SPEC = importlib.util.spec_from_file_location("notion_multi_workspace_server", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class NotionMultiWorkspaceServerTests(unittest.TestCase):
    def restore_env(self, previous: dict[str, Optional[str]]) -> None:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_list_workspaces_returns_two_configured_bindings(self) -> None:
        previous = {name: os.environ.get(name) for name in MODULE.EXPECTED_ENV_VARS}
        try:
            os.environ["NOTION_WORKSPACE_PRIMARY_NAME"] = "Workspace A"
            os.environ["NOTION_TOKEN_PRIMARY"] = "secret_primary"
            os.environ["NOTION_WORKSPACE_SECONDARY_NAME"] = "Workspace B"
            os.environ["NOTION_TOKEN_SECONDARY"] = "secret_secondary"

            payload = MODULE.tool_list_workspaces({"validate_tokens": False})
            self.assertEqual(len(payload["workspaces"]), 2)
            self.assertEqual(payload["workspaces"][0]["name"], "Workspace A")
            self.assertEqual(payload["workspaces"][1]["name"], "Workspace B")
        finally:
            self.restore_env(previous)

    def test_load_workspace_configs_honors_env_file_override(self) -> None:
        env_names = set(MODULE.EXPECTED_ENV_VARS) | {MODULE.DOTENV_ENV_VAR}
        previous = {name: os.environ.get(name) for name in env_names}
        try:
            for name in MODULE.EXPECTED_ENV_VARS:
                os.environ.pop(name, None)

            with tempfile.TemporaryDirectory() as tmpdir:
                env_path = Path(tmpdir) / "notion-multi-workspace.env"
                env_path.write_text(
                    "\n".join(
                        [
                            "NOTION_WORKSPACE_PRIMARY_NAME=Workspace A",
                            "NOTION_TOKEN_PRIMARY=secret_primary",
                            "NOTION_WORKSPACE_SECONDARY_NAME=Workspace B",
                            "NOTION_TOKEN_SECONDARY=secret_secondary",
                        ]
                    )
                    + "\n"
                )
                os.environ[MODULE.DOTENV_ENV_VAR] = str(env_path)

                configs = MODULE.load_workspace_configs()

            self.assertEqual(configs["primary"].name, "Workspace A")
            self.assertEqual(configs["secondary"].token, "secret_secondary")
        finally:
            self.restore_env(previous)

    def test_canonicalize_notion_id_from_raw_and_url(self) -> None:
        raw_id = "33daa70e43f1801c8441e88c36e22608"
        expected = "33daa70e-43f1-801c-8441-e88c36e22608"
        url = "https://www.notion.so/Page-Title-33daa70e43f1801c8441e88c36e22608?pvs=4"

        self.assertEqual(MODULE.canonicalize_notion_id(raw_id), expected)
        self.assertEqual(MODULE.canonicalize_notion_id(url), expected)

    def test_resolve_workspace_matches_key_and_name_aliases(self) -> None:
        workspaces = {
            "primary": MODULE.WorkspaceConfig("primary", "Workspace A", "token-a"),
            "secondary": MODULE.WorkspaceConfig("secondary", "Workspace B", "token-b"),
        }

        self.assertEqual(MODULE.resolve_workspace("primary", workspaces).name, "Workspace A")
        self.assertEqual(MODULE.resolve_workspace("workspace-a", workspaces).key, "primary")
        self.assertEqual(MODULE.resolve_workspace("Workspace B", workspaces).key, "secondary")

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
