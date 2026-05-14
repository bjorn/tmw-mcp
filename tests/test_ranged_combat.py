"""Tests for ranged combat: equipping a bow plus ammo and auto-attacking.

Live regression: equipping Bow plus Snowball (an ammo item) and calling
tmw_attack caused the bot to walk onto the target tile instead of firing.
Snowball/Arrow counts never decreased. This module covers the equip
flow (server replies 0x00aa for the bow, 0x013c for the ammo), the
attack-range packet (0x013a) update, and the run_auto_behaviors logic
that decides whether to walk closer or fire.

Run with: .venv/bin/python -m unittest client.tests.test_ranged_combat
"""

from __future__ import annotations

import os
import struct
import sys
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tmw_mcp import packets
from tmw_mcp.bot import run_auto_behaviors
from tmw_mcp.game import Being, GameClient
from tmw_mcp.packets import (
    InventoryItem,
    PACKET_SIZES,
    build_equip_item,
    parse_packet,
)


class FakeConn:
    """Stand-in for a TMW Connection that records sends and replays scripted
    server packets. No real socket. Mirrors the (small) public surface the
    GameClient relies on: send_packet, recv_packet_nonblock, has_data, close.
    """

    def __init__(self):
        self.sent: list[bytes] = []
        self._queue: list = []  # already parsed packets, returned in order

    # Pretend to write to the server; just stash the bytes.
    def send_packet(self, data: bytes, msg=None):
        self.sent.append(bytes(data))

    # The GameClient asks for incoming packets a few different ways; this
    # returns the next scripted reply (already parsed) or None when empty.
    def recv_packet_nonblock(self, timeout: float = 0.0):
        if self._queue:
            return self._queue.pop(0)
        return None

    def has_data(self) -> bool:
        return bool(self._queue)

    def close(self):
        pass

    # Helpers used by the tests to script incoming traffic.
    def push_packet(self, pkt):
        """Queue a *parsed* packet object so process_packets returns it."""
        self._queue.append(pkt)

    def push_bytes(self, pkt_id: int, payload: bytes):
        """Queue a fixed-size raw packet by parsing it through parse_packet.

        This exercises the real packet parser end-to-end. *payload* is the
        bytes AFTER the 2-byte packet ID. Total length must match the
        PACKET_SIZES entry.
        """
        size = PACKET_SIZES[pkt_id]
        assert size is not None and size == 2 + len(payload), (
            f'size mismatch for 0x{pkt_id:04x}: '
            f'table says {size}, payload is {len(payload)} (+ 2 id)'
        )
        data = struct.pack('<H', pkt_id) + payload
        self._queue.append(parse_packet(pkt_id, data))

    def last_sent_id(self) -> int | None:
        if not self.sent:
            return None
        return struct.unpack_from('<H', self.sent[-1], 0)[0]

    def sent_ids(self) -> list[int]:
        return [struct.unpack_from('<H', d, 0)[0] for d in self.sent]


def make_client_at(x: int, y: int) -> GameClient:
    """Build a GameClient with a fake map_conn and player placed at (x, y)."""
    c = GameClient()
    c.map_conn = FakeConn()
    c.account_id = 1000
    c.player.x = x
    c.player.y = y
    c.player.map_name = 'testmap'
    c.player.speed = 150  # ms/tile
    return c


def add_being(c: GameClient, block_id: int, name: str, x: int, y: int,
              hp: int = 100, max_hp: int = 100):
    b = Being()
    b.block_id = block_id
    b.name = name
    b.species = 1002
    b.x = x
    b.y = y
    b.hp = hp
    b.max_hp = max_hp
    b.direction = 0
    b.speed = 150
    c.beings[block_id] = b


# Item ids from client-data/items/.
BOW_ID = 1200
SNOWBALL_ID = 5260
ARROW_ID = 1199


# Convenience builders for server->client packets that the tests need.
def equip_result_bytes(ioff2: int, equip_point: int, success: int = 1) -> bytes:
    """0x00aa: index(2) + equip_point(2) + success(1) = 5 bytes after id."""
    return struct.pack('<HHB', ioff2, equip_point, success)


def attack_range_bytes(value: int) -> bytes:
    """0x013a: attack_range(2)."""
    return struct.pack('<H', value)


def arrow_equip_bytes(ioff2: int) -> bytes:
    """0x013c: ioff2(2)."""
    return struct.pack('<H', ioff2)


def add_inventory(c: GameClient, ioff2: int, name_id: int, amount: int = 1):
    """Stuff an InventoryItem directly. Skips the full 0x01ee parse path
    since these tests do not need it; equip results just key off the index.
    """
    c.inventory[ioff2] = InventoryItem(
        index=ioff2, name_id=name_id, item_type=0, amount=amount,
    )


class EquipPacketTest(unittest.TestCase):
    """The client sends 0x00a9 (equip) using the ioff2 the server gave us."""

    def test_equip_sends_a9_packet(self):
        pkt = build_equip_item(5, 0)
        self.assertEqual(len(pkt), 6)
        pid, idx, epos = struct.unpack('<HHH', pkt)
        self.assertEqual(pid, 0x00a9)
        self.assertEqual(idx, 5)
        self.assertEqual(epos, 0)


class EquipBowFlowTest(unittest.TestCase):
    """Equipping a bow: server replies 0x00aa then 0x013a (attack_range)."""

    def test_equip_bow_updates_attack_range(self):
        c = make_client_at(50, 50)
        add_inventory(c, ioff2=4, name_id=BOW_ID)
        self.assertEqual(c._attack_range, 1)

        # Server reply: equip ack for slot WEAPON (0x0002), then range=5.
        c.map_conn.push_bytes(0x00aa, equip_result_bytes(4, 0x0002, 1))
        c.map_conn.push_bytes(0x013a, attack_range_bytes(5))
        c.process_packets(timeout=0.0)

        self.assertEqual(c.inventory[4].equipped, 0x0002,
                         'Bow should be flagged equipped in slot WEAPON')
        self.assertEqual(c._attack_range, 5,
                         'AttackRange packet must update _attack_range')


class EquipAmmoFlowTest(unittest.TestCase):
    """Equipping ammo: server replies 0x013c (arrow_equip notify),
    NOT a regular 0x00aa equipitem ack. Without 0x013c handling the
    client never marks the ammo as equipped.
    """

    def test_snowball_arrow_equip_marks_ammo_equipped(self):
        c = make_client_at(50, 50)
        add_inventory(c, ioff2=6, name_id=SNOWBALL_ID, amount=101)

        # The server signals ammo equipped with 0x013c (ioff2 only).
        # No 0x00aa is sent for ammo. tmwa also sends a 0x013b ("arrow
        # fail/info") with type=3; the client can ignore that, but
        # ammo equip status must be tracked from 0x013c.
        c.map_conn.push_bytes(0x013c, arrow_equip_bytes(6))
        c.process_packets(timeout=0.0)

        self.assertTrue(
            c.inventory[6].equipped,
            'Ammo must be flagged equipped after 0x013c arrow_equip notify',
        )
        # The flag should encode the ARROW slot (EPOS::ARROW = 0x8000).
        self.assertEqual(c.inventory[6].equipped & 0x8000, 0x8000,
                         'Ammo equip flag should set the ARROW bit (0x8000)')


class AttackRangePersistenceTest(unittest.TestCase):
    """Equipping bow then ammo should leave _attack_range > 1."""

    def test_bow_plus_snowball_keeps_ranged(self):
        c = make_client_at(50, 50)
        add_inventory(c, ioff2=4, name_id=BOW_ID)
        add_inventory(c, ioff2=6, name_id=SNOWBALL_ID, amount=101)

        # Bow.
        c.map_conn.push_bytes(0x00aa, equip_result_bytes(4, 0x0002, 1))
        c.map_conn.push_bytes(0x013a, attack_range_bytes(5))
        # Ammo: server sends 0x013c, recalc may bump range slightly.
        c.map_conn.push_bytes(0x013c, arrow_equip_bytes(6))
        c.map_conn.push_bytes(0x013a, attack_range_bytes(6))
        c.process_packets(timeout=0.0)

        self.assertGreater(c._attack_range, 1)
        self.assertTrue(c.inventory[4].equipped)
        self.assertTrue(c.inventory[6].equipped)


class AutoAttackRangedWalkTest(unittest.TestCase):
    """run_auto_behaviors must stop walking at attack_range, NOT walk
    onto the target tile, when a ranged weapon is equipped.
    """

    def test_ranged_attack_stops_short_of_target(self):
        c = make_client_at(50, 50)
        c._attack_range = 5
        add_being(c, block_id=2000, name='Tortuga', x=60, y=50)
        c._auto_attack_target = 2000
        # Force walk_arrival in the past so the "attack" branch is reachable
        # if we happen to land in range; for this test we're out of range.
        c._walk_arrival = 0.0

        # tick % 4 == 0 triggers the auto-attack branch.
        run_auto_behaviors(c, tick_count=0)

        # We should have sent a walk packet, not an attack packet.
        ids = c.map_conn.sent_ids()
        self.assertIn(0x0085, ids, 'walk_to should send a 0x0085 walk packet')
        self.assertNotIn(0x0089, ids,
                         'no attack packet should be sent while out of range')

        # And the walk destination should be range-1 tiles short of the
        # target, NOT on top of it. We can decode the last walk packet to
        # verify (build_walk encodes dest in a 3-byte position1 blob).
        walk_pkt = next(p for p in c.map_conn.sent if
                        struct.unpack_from('<H', p, 0)[0] == 0x0085)
        from tmw_mcp.packets import decode_pos1
        dx, dy, _ = decode_pos1(walk_pkt[2:5])
        self.assertNotEqual((dx, dy), (60, 50),
                            'walk must not target the same tile as the monster')
        # Chebyshev distance to monster should equal attack_range - 1 = 4.
        self.assertEqual(max(abs(dx - 60), abs(dy - 50)), 4)

    def test_ranged_attack_fires_when_in_range(self):
        c = make_client_at(55, 50)  # 5 tiles west of monster
        c._attack_range = 5
        add_being(c, block_id=2000, name='Tortuga', x=60, y=50)
        c._auto_attack_target = 2000
        c._walk_arrival = 0.0  # not walking

        run_auto_behaviors(c, tick_count=0)
        ids = c.map_conn.sent_ids()
        self.assertIn(0x0089, ids,
                      'in-range tick should send an attack (0x0089) packet')

    def test_melee_attack_walks_onto_adjacent_tile(self):
        """Sanity: with attack_range=1, the bot walks to an adjacent tile.
        This is the pre-existing melee behavior and must keep working.
        """
        c = make_client_at(50, 50)
        c._attack_range = 1
        add_being(c, block_id=2000, name='Maggot', x=55, y=50)
        c._auto_attack_target = 2000
        c._walk_arrival = 0.0

        run_auto_behaviors(c, tick_count=0)
        walk_pkt = next(p for p in c.map_conn.sent if
                        struct.unpack_from('<H', p, 0)[0] == 0x0085)
        from tmw_mcp.packets import decode_pos1
        dx, dy, _ = decode_pos1(walk_pkt[2:5])
        # With range=1, dest = target.x - sx*0 = target.x. Walking onto
        # the target tile is the legacy melee behavior; the server
        # snaps us to the adjacent tile when we get there.
        self.assertEqual((dx, dy), (55, 50))


class RunAutoBehaviorsModuleTest(unittest.TestCase):
    """Regression: a recent follow-mode refactor accidentally deleted the
    'def run_auto_behaviors(...)' header, turning the entire auto-attack /
    hunt body into orphan code inside _tick_follow. That made tmw_attack
    walk-into-range and hunt mode silently do nothing whenever the bot
    was not also in follow mode. Keep this assertion so the function
    can't get inlined into another one again without the test noticing.
    """

    def test_run_auto_behaviors_is_module_level_callable(self):
        from tmw_mcp import bot as bot_module
        self.assertTrue(
            callable(getattr(bot_module, 'run_auto_behaviors', None)),
            'bot.run_auto_behaviors must exist as a module-level function',
        )


class EquipFallbackTest(unittest.TestCase):
    """If the server's 0x013a doesn't arrive (or arrives later), the
    EquipResult handler should seed _attack_range from the item DB so
    the auto-attack still knows the bow is ranged.
    """

    def test_bow_equip_without_013a_uses_xml_attack_range(self):
        c = make_client_at(50, 50)
        add_inventory(c, ioff2=4, name_id=BOW_ID)
        self.assertEqual(c._attack_range, 1)

        # Only the EquipResult arrives; no 0x013a. The XML for item 1200
        # (Bow) has attack-range="5", so we should land at 5.
        c.map_conn.push_bytes(0x00aa, equip_result_bytes(4, 0x0002, 1))
        c.process_packets(timeout=0.0)
        self.assertEqual(c._attack_range, 5)

    def test_unequip_weapon_resets_to_melee(self):
        c = make_client_at(50, 50)
        add_inventory(c, ioff2=4, name_id=BOW_ID)
        # Equip first.
        c.map_conn.push_bytes(0x00aa, equip_result_bytes(4, 0x0002, 1))
        c.process_packets(timeout=0.0)
        self.assertEqual(c._attack_range, 5)

        # Unequip: server sends 0x00ac with the same equip_point (WEAPON).
        c.map_conn.push_bytes(0x00ac, equip_result_bytes(4, 0x0002, 1))
        c.process_packets(timeout=0.0)
        self.assertEqual(c._attack_range, 1,
                         'Unequipping the weapon must drop range back to 1')
        self.assertEqual(c.inventory[4].equipped, 0)


class EquipOtherItemsTest(unittest.TestCase):
    """Equipping armor / shield / hat / non-bow weapons should not move
    the attack range away from whatever 0x013a last said.
    """

    def test_hat_equip_keeps_attack_range(self):
        c = make_client_at(50, 50)
        c._attack_range = 5  # pretend bow already equipped
        # Hat: arbitrary inventory slot, EPOS::HAT = 0x0100.
        add_inventory(c, ioff2=8, name_id=2000)
        c.map_conn.push_bytes(0x00aa, equip_result_bytes(8, 0x0100, 1))
        c.process_packets(timeout=0.0)
        self.assertEqual(c.inventory[8].equipped, 0x0100)
        self.assertEqual(c._attack_range, 5,
                         'Equipping a hat must not touch attack range')

    def test_shield_equip_keeps_attack_range(self):
        c = make_client_at(50, 50)
        c._attack_range = 1
        add_inventory(c, ioff2=9, name_id=2003)  # Leather Shield-ish
        c.map_conn.push_bytes(0x00aa, equip_result_bytes(9, 0x0020, 1))  # SHIELD
        c.process_packets(timeout=0.0)
        self.assertEqual(c.inventory[9].equipped, 0x0020)
        self.assertEqual(c._attack_range, 1)

    def test_melee_weapon_with_no_attack_range_attr(self):
        """A weapon item not in the XML cache, or one without attack-range,
        must NOT crash. The server's 0x013a is the source of truth.
        """
        c = make_client_at(50, 50)
        # Item id 999 is not a real item; item_attack_range returns None.
        add_inventory(c, ioff2=10, name_id=999)
        c.map_conn.push_bytes(0x00aa, equip_result_bytes(10, 0x0002, 1))
        # Then 0x013a arrives later.
        c.map_conn.push_bytes(0x013a, attack_range_bytes(2))
        c.process_packets(timeout=0.0)
        self.assertEqual(c.inventory[10].equipped, 0x0002)
        # The DB fallback is None, so _attack_range stays at default 1
        # until 0x013a flips it to 2.
        self.assertEqual(c._attack_range, 2)


class HuntModeRunsTest(unittest.TestCase):
    """Sanity: when hunt mode is on and a monster is visible, the bot
    locks onto it. This needs run_auto_behaviors to be reachable from
    the game loop with no follow target set.
    """

    def test_hunt_mode_picks_nearest_target(self):
        c = make_client_at(50, 50)
        c._attack_range = 5
        c._hunt_type = 'tortuga'
        c._hunt_home = (50, 50)
        add_being(c, block_id=3001, name='Tortuga', x=53, y=50)
        add_being(c, block_id=3002, name='Tortuga', x=58, y=50)
        run_auto_behaviors(c, tick_count=0)
        # The closer tortuga should have been picked.
        self.assertEqual(c._auto_attack_target, 3001)

    def test_hunt_mode_ranged_does_not_walk_onto_target(self):
        """When hunt mode locks onto a far monster with a bow, the
        initial walk must stop short, NOT step onto the target tile.
        """
        c = make_client_at(50, 50)
        c._attack_range = 5
        c._hunt_type = 'tortuga'
        c._hunt_home = (50, 50)
        # Far enough away to trigger a walk-toward step.
        add_being(c, block_id=4001, name='Tortuga', x=60, y=50)
        run_auto_behaviors(c, tick_count=0)
        self.assertEqual(c._auto_attack_target, 4001)
        # A walk packet should have been issued.
        walk_pkts = [p for p in c.map_conn.sent
                     if struct.unpack_from('<H', p, 0)[0] == 0x0085]
        self.assertEqual(len(walk_pkts), 1)
        from tmw_mcp.packets import decode_pos1
        dx, dy, _ = decode_pos1(walk_pkts[0][2:5])
        self.assertNotEqual((dx, dy), (60, 50),
                            'hunt mode must not walk onto a ranged target')
        self.assertEqual(max(abs(dx - 60), abs(dy - 50)), 4)


if __name__ == '__main__':
    unittest.main()
