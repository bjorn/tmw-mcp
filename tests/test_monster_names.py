"""Regression tests for live monster-name resolution.

monsters.xml is downloaded on a background thread after login, so the
server can flood being packets before the name table is ready. The being
handlers must NOT freeze the ``species:<id>`` fallback into ``b.name``;
the display name has to resolve live so it self-heals once the table loads.
"""

from __future__ import annotations

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tmw_mcp import monsters
from tmw_mcp.game import GameClient
from tmw_mcp.monsters import being_display_name
from tmw_mcp.packets import BeingSpawn, BeingVisible


class MonsterNameTest(unittest.TestCase):

    def setUp(self):
        monsters._reset_for_tests()

    def tearDown(self):
        monsters._reset_for_tests()

    def _load_table(self, mapping):
        """Simulate monsters.xml finishing its download."""
        monsters._name_cache.update(mapping)
        monsters._loaded = True

    def test_spawn_does_not_freeze_fallback(self):
        client = GameClient()
        # Table not loaded yet: the being arrives before monsters.xml.
        client._handle_packet(
            BeingSpawn(block_id=42, species=1017, x=10, y=10)
        )
        b = client.beings[42]
        # The stored name must stay empty, never the species fallback string.
        self.assertEqual(b.name, '')
        self.assertNotIn('species:', b.name)
        # Display falls back to the live fallback while the table is missing.
        self.assertEqual(being_display_name(b), 'species:1017')

    def test_visible_does_not_freeze_fallback(self):
        client = GameClient()
        client._handle_packet(
            BeingVisible(block_id=43, species=1017, x=5, y=5)
        )
        self.assertEqual(client.beings[43].name, '')

    def test_display_self_heals_once_table_loads(self):
        client = GameClient()
        client._handle_packet(
            BeingSpawn(block_id=42, species=1017, x=10, y=10)
        )
        b = client.beings[42]
        self.assertEqual(being_display_name(b), 'species:1017')

        # monsters.xml finishes downloading after the being already spawned.
        self._load_table({1017: 'Bat'})

        # No new packet arrived, yet the display name now resolves correctly.
        self.assertEqual(b.name, '')
        self.assertEqual(being_display_name(b), 'Bat')

    def test_server_name_response_wins(self):
        client = GameClient()
        client._handle_packet(
            BeingSpawn(block_id=44, species=1017, x=1, y=1)
        )
        from tmw_mcp.packets import BeingNameResponse
        client._handle_packet(
            BeingNameResponse(block_id=44, name='Cave Bat')
        )
        b = client.beings[44]
        self.assertEqual(b.name, 'Cave Bat')
        # Authoritative name takes precedence over the species table.
        self._load_table({1017: 'Bat'})
        self.assertEqual(being_display_name(b), 'Cave Bat')

    def test_non_monster_species_has_empty_display(self):
        b = self._make_being(species=200, name='')
        self.assertEqual(being_display_name(b), '')

    def _make_being(self, species, name):
        from tmw_mcp.game import Being
        return Being(block_id=1, species=species, name=name)


if __name__ == '__main__':
    unittest.main()
