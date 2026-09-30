"""Check the public package's entry points and release identity without credentials."""

import ast
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PackagingTests(unittest.TestCase):
    def test_plugin_release_matches_server_and_exposes_writes(self):
        manifest = json.loads((ROOT / '.codex-plugin/plugin.json').read_text())
        tree = ast.parse((ROOT / 'scripts/notion_multi_workspace_server.py').read_text())
        constants = {
            target.id: ast.literal_eval(node.value)
            for node in tree.body
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
            and target.id in {'SERVER_NAME', 'SERVER_VERSION'}
        }
        self.assertEqual(manifest['name'], constants['SERVER_NAME'])
        self.assertEqual(manifest['version'], constants['SERVER_VERSION'])
        self.assertTrue({'Read', 'Write'} <= set(manifest['interface']['capabilities']))
        self.assertNotIn('read-only', manifest['description'].lower())
        self.assertTrue((ROOT / manifest['skills'] / 'notion-multi-workspace/SKILL.md').is_file())
        mcp = json.loads((ROOT / manifest['mcpServers']).read_text())
        server = mcp['mcpServers'][manifest['name']]
        self.assertTrue((ROOT / server['args'][0]).is_file())
        self.assertNotIn('env', server, 'The public launcher must not embed credentials.')

    def test_marketplace_resolves_the_public_package(self):
        marketplace = json.loads((ROOT / '.agents/plugins/marketplace.json').read_text())
        plugin = marketplace['plugins'][0]
        self.assertEqual(plugin['name'], 'notion-multi-workspace')
        self.assertEqual((ROOT / plugin['source']['path']).resolve(), ROOT)
        self.assertIn('MIT License', (ROOT / 'LICENSE').read_text())

    def test_sample_credentials_are_obvious_placeholders(self):
        entries = dict(
            line.split('=', 1)
            for line in (ROOT / '.env.example').read_text().splitlines()
            if line and not line.startswith('#')
        )
        for key in entries['NOTION_WORKSPACE_KEYS'].split(','):
            fragment = key.upper().replace('-', '_')
            prefix = f'NOTION_WORKSPACE_{fragment}'
            self.assertIn(prefix + '_NAME', entries)
            self.assertEqual(entries[prefix + '_TOKEN'], f'replace_with_{fragment.lower()}_token')


if __name__ == '__main__':
    unittest.main()
