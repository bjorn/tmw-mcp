"""Regression tests for clearing stale NPC dialog state.

The dashboard and the MCP `state` output read the NPC dialog fields on
GameClient (npc_id, npc_dialog_open, npc_dialog, npc_choices, and the
npc_waiting_* flags). Those fields used to linger after the dialog was
closed, so the dashboard kept showing an old NPC's dialog box (with the
speaker rendered as NPC#<id>, since the being was gone). The dialog state
is cleared when we acknowledge a close.

A map warp does NOT clear it: TMWA's pc_setpos cancels trade and closes
storage on a warp but deliberately leaves sd->npc_id intact, so an NPC can
warp the player and keep talking. We mirror that here.
"""

from __future__ import annotations

import os
import struct
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tmw_mcp.game import GameClient
from tmw_mcp.packets import ChangeMap, PACKET_SIZES, parse_packet


class FakeConn:
    """Minimal stand-in for a Connection: records sends, no socket."""

    def __init__(self):
        self.sent: list[bytes] = []

    def send_packet(self, data: bytes, msg=None):
        self.sent.append(bytes(data))

    def close(self):
        pass


class NpcDialogResetTest(unittest.TestCase):

    def _populate_dialog(self, client: GameClient):
        """Put the client into a state with an open NPC dialog."""
        client.npc_id = 1234
        client.npc_dialog_open = True
        client.npc_dialog.extend(['Hello there.', 'Take this reward.'])
        client.npc_choices.extend(['Yes', 'No'])
        client.npc_waiting_next = False
        client.npc_waiting_close = True
        client.npc_waiting_choice = False
        client.npc_waiting_input = ''

    def _assert_cleared(self, client: GameClient):
        self.assertEqual(client.npc_id, 0)
        self.assertFalse(client.npc_dialog_open)
        self.assertEqual(client.npc_dialog, [])
        self.assertEqual(client.npc_choices, [])
        self.assertFalse(client.npc_waiting_next)
        self.assertFalse(client.npc_waiting_close)
        self.assertFalse(client.npc_waiting_choice)
        self.assertEqual(client.npc_waiting_input, '')

    def _assert_preserved(self, client: GameClient):
        self.assertEqual(client.npc_id, 1234)
        self.assertTrue(client.npc_dialog_open)
        self.assertEqual(client.npc_dialog, ['Hello there.', 'Take this reward.'])
        self.assertEqual(client.npc_choices, ['Yes', 'No'])
        self.assertTrue(client.npc_waiting_close)

    def test_close_response_clears_dialog(self):
        client = GameClient()
        client.map_conn = FakeConn()
        self._populate_dialog(client)

        # npc_close_response only fires its body when waiting to close.
        client.npc_close_response()

        # The close packet was actually sent, then the state was wiped.
        self.assertTrue(client.map_conn.sent)
        self._assert_cleared(client)

    def test_change_map_preserves_dialog(self):
        """A warp must NOT end the NPC session (TMWA keeps sd->npc_id)."""
        client = GameClient()
        client.map_conn = FakeConn()
        self._populate_dialog(client)

        client._handle_packet(ChangeMap(map_name='002-1', x=50, y=60))

        self._assert_preserved(client)

    def test_change_map_from_bytes_preserves_dialog(self):
        """Same as above but parsed end-to-end through the packet parser."""
        client = GameClient()
        client.map_conn = FakeConn()
        self._populate_dialog(client)

        pkt_id = 0x0091
        size = PACKET_SIZES[pkt_id]
        # 16-byte map name, then two int16 coords (and any trailing pad).
        map_name = b'002-1.gat'.ljust(16, b'\x00')
        body = map_name + struct.pack('<HH', 50, 60)
        payload = body[: size - 2].ljust(size - 2, b'\x00')
        data = struct.pack('<H', pkt_id) + payload
        client._handle_packet(parse_packet(pkt_id, data))

        self._assert_preserved(client)


if __name__ == '__main__':
    unittest.main()
