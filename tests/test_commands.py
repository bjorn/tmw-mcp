"""Tests for the transport-independent tool command catalog and dispatch.

Run with: .venv/bin/python -m unittest tests.test_commands
"""

from __future__ import annotations

import asyncio
import inspect
import os
import sys
import unittest
from unittest.mock import MagicMock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tmw_mcp.commands import TOOL_SPECS, execute_tool_command
from tmw_mcp.mcp_server import mcp


class TestToolSpecParity(unittest.TestCase):
    """TOOL_SPECS must mirror the MCP tool surface exactly."""

    def test_name_sets_match(self):
        mcp_names = {t.name for t in asyncio.run(mcp.list_tools())}
        spec_names = {s['name'] for s in TOOL_SPECS}
        self.assertEqual(mcp_names, spec_names)

    def test_schema_parity(self):
        mcp_tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
        for spec in TOOL_SPECS:
            with self.subTest(tool=spec['name']):
                mcp_schema = mcp_tools[spec['name']].inputSchema
                spec_params = spec['parameters']
                mcp_props = mcp_schema.get('properties', {})
                spec_props = spec_params.get('properties', {})
                self.assertEqual(set(mcp_props), set(spec_props))
                for prop, spec_prop in spec_props.items():
                    self.assertEqual(mcp_props[prop]['type'],
                                     spec_prop['type'],
                                     f'{spec["name"]}.{prop} type')
                self.assertEqual(set(mcp_schema.get('required', [])),
                                 set(spec_params.get('required', [])))

    def test_every_command_dispatches(self):
        src = inspect.getsource(execute_tool_command)
        for spec in TOOL_SPECS:
            with self.subTest(command=spec['command']):
                self.assertIn(f"cmd == '{spec['command']}'", src)


class TestDispatchDefaults(unittest.TestCase):
    """Optional parameters must fall back to the MCP defaults."""

    def test_buy_defaults_count_to_one(self):
        client = MagicMock()
        execute_tool_command(client, 'buy', {'name_id': 5})
        client.buy_items.assert_called_once_with([(1, 5)])

    def test_sell_defaults_count_to_one(self):
        client = MagicMock()
        execute_tool_command(client, 'sell', {'index': 3})
        client.sell_items.assert_called_once_with([(3, 1)])


if __name__ == '__main__':
    unittest.main()
