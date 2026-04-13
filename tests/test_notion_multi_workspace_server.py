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

    def env_snapshot(self) -> dict[str, Optional[str]]:
        tracked = {
            MODULE.WORKSPACE_KEYS_ENV_VAR,
            MODULE.DOTENV_ENV_VAR,
        }
        for key in ("primary", "secondary", "finance", "workspace-c"):
            tracked.add(MODULE.workspace_name_env_var(key))
            tracked.add(MODULE.workspace_token_env_var(key))
            tracked.add(MODULE.workspace_aliases_env_var(key))
        return {name: os.environ.get(name) for name in tracked}

    def test_list_workspaces_returns_all_configured_bindings(self) -> None:
        previous = self.env_snapshot()
        try:
            os.environ[MODULE.WORKSPACE_KEYS_ENV_VAR] = "primary,secondary,finance"
            os.environ[MODULE.workspace_name_env_var("primary")] = "Workspace A"
            os.environ[MODULE.workspace_token_env_var("primary")] = "secret_primary"
            os.environ[MODULE.workspace_name_env_var("secondary")] = "Workspace B"
            os.environ[MODULE.workspace_token_env_var("secondary")] = "secret_secondary"
            os.environ[MODULE.workspace_name_env_var("finance")] = "Finance Ops"
            os.environ[MODULE.workspace_token_env_var("finance")] = "secret_finance"
            os.environ[MODULE.workspace_aliases_env_var("finance")] = "fin,accounting"

            payload = MODULE.tool_list_workspaces({"validate_tokens": False})
            self.assertEqual(payload["workspace_count"], 3)
            self.assertEqual([item["key"] for item in payload["workspaces"]], ["primary", "secondary", "finance"])
            self.assertIn("accounting", payload["workspaces"][2]["aliases"])
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
                            "NOTION_WORKSPACE_KEYS=primary,secondary,finance",
                            "NOTION_WORKSPACE_PRIMARY_NAME=Workspace A",
                            "NOTION_WORKSPACE_PRIMARY_TOKEN=secret_primary",
                            "NOTION_WORKSPACE_SECONDARY_NAME=Workspace B",
                            "NOTION_WORKSPACE_SECONDARY_TOKEN=secret_secondary",
                            "NOTION_WORKSPACE_FINANCE_NAME=Finance Ops",
                            "NOTION_WORKSPACE_FINANCE_TOKEN=secret_finance",
                            "NOTION_WORKSPACE_FINANCE_ALIASES=fin,acct",
                        ]
                    )
                    + "\n"
                )
                os.environ[MODULE.DOTENV_ENV_VAR] = str(env_path)

                configs = MODULE.load_workspace_configs()

            self.assertEqual(configs["primary"].name, "Workspace A")
            self.assertEqual(configs["secondary"].token, "secret_secondary")
            self.assertEqual(configs["finance"].extra_aliases, ("fin", "acct"))
        finally:
            self.restore_env(previous)

    def test_hyphenated_workspace_keys_map_to_underscore_env_vars(self) -> None:
        self.assertEqual(
            MODULE.workspace_name_env_var("workspace-c"),
            "NOTION_WORKSPACE_DIGITAL_PRIME_NAME",
        )
        self.assertEqual(
            MODULE.workspace_token_env_var("workspace-c"),
            "NOTION_WORKSPACE_DIGITAL_PRIME_TOKEN",
        )
        self.assertEqual(
            MODULE.workspace_aliases_env_var("workspace-c"),
            "NOTION_WORKSPACE_DIGITAL_PRIME_ALIASES",
        )

    def test_canonicalize_notion_id_from_raw_and_url(self) -> None:
        raw_id = "33daa70e43f1801c8441e88c36e22608"
        expected = "33daa70e-43f1-801c-8441-e88c36e22608"
        url = "https://www.notion.so/Page-Title-33daa70e43f1801c8441e88c36e22608?pvs=4"

        self.assertEqual(MODULE.canonicalize_notion_id(raw_id), expected)
        self.assertEqual(MODULE.canonicalize_notion_id(url), expected)

    def test_resolve_workspace_matches_key_name_and_aliases(self) -> None:
        workspaces = {
            "primary": MODULE.WorkspaceConfig("primary", "Workspace A", "token-a"),
            "secondary": MODULE.WorkspaceConfig("secondary", "Workspace B", "token-b"),
            "finance": MODULE.WorkspaceConfig("finance", "Finance Ops", "token-c", ("fin", "accounting")),
        }

        self.assertEqual(MODULE.resolve_workspace("primary", workspaces).name, "Workspace A")
        self.assertEqual(MODULE.resolve_workspace("workspace-a", workspaces).key, "primary")
        self.assertEqual(MODULE.resolve_workspace("Workspace B", workspaces).key, "secondary")
        self.assertEqual(MODULE.resolve_workspace("accounting", workspaces).key, "finance")

    def test_load_workspace_configs_rejects_ambiguous_aliases(self) -> None:
        previous = self.env_snapshot()
        try:
            os.environ[MODULE.WORKSPACE_KEYS_ENV_VAR] = "primary,secondary"
            os.environ[MODULE.workspace_name_env_var("primary")] = "Workspace A"
            os.environ[MODULE.workspace_token_env_var("primary")] = "secret_primary"
            os.environ[MODULE.workspace_aliases_env_var("primary")] = "shared"
            os.environ[MODULE.workspace_name_env_var("secondary")] = "Workspace B"
            os.environ[MODULE.workspace_token_env_var("secondary")] = "secret_secondary"
            os.environ[MODULE.workspace_aliases_env_var("secondary")] = "shared"

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
