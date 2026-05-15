"""Regression tests for floor item state tracking."""

from __future__ import annotations

import os
import struct
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tmw_mcp.game import GameClient
from tmw_mcp.packets import ItemRemove, parse_packet


class FloorItemTest(unittest.TestCase):

    def test_item_remove_packet_clears_floor_item(self):
        client = GameClient()

        # 0x009e: block_id, name_id, identify flag, x, y, subx, suby, amount.
        dropped = parse_packet(
            0x009e,
            struct.pack('<HIHBHHBBH', 0x009e, 2, 753, 1, 36, 32, 0, 0, 1),
        )
        client._handle_packet(dropped)
        self.assertIn(2, client.floor_items)

        removed = parse_packet(0x00a1, struct.pack('<HI', 0x00a1, 2))
        self.assertIsInstance(removed, ItemRemove)
        client._handle_packet(removed)

        self.assertNotIn(2, client.floor_items)


if __name__ == '__main__':
    unittest.main()
