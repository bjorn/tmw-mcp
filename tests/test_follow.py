"""Tests for follow-mode state machine and dashboard wiring.

These tests do not talk to a real game server. They construct a fake
GameClient, drive ``bot._tick_follow`` directly, and verify the
state transitions (idle / following / waiting / warping / gave up) plus
the dashboard ``follow`` snapshot section.

Run with: .venv/bin/python -m unittest client.tests.test_follow
"""

from __future__ import annotations

import os
import sys
import time
import unittest
from unittest import mock

# Allow running from anywhere: put the repo root on sys.path so
# ``import tmw_mcp`` resolves to the in-tree package.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tmw_mcp import bot
from tmw_mcp.dashboard import build_snapshot
from tmw_mcp.game import Being, GameClient


def _fresh_client(map_name: str = '008-1', px: int = 50, py: int = 50) -> GameClient:
    """A GameClient instance with just enough state to drive follow logic."""
    c = GameClient()
    c.account_id = 1
    c.player.account_id = 1
    c.player.char_name = 'Claudius'
    c.player.map_name = map_name
    c.player.x = px
    c.player.y = py
    # Make walk_to a no-op recorder so we can assert chase calls.
    c._walks: list[tuple[int, int]] = []  # type: ignore[attr-defined]
    c.walk_to = lambda x, y: c._walks.append((x, y))  # type: ignore[assignment]
    # _snap_walk_position is called from run_auto_behaviors but not from
    # _tick_follow directly. Keep it harmless.
    return c


def _add_being(client: GameClient, block_id: int, name: str,
               x: int, y: int) -> Being:
    b = Being(block_id=block_id, name=name, x=x, y=y, level=10)
    client.beings[block_id] = b
    return b


class StartFollowTest(unittest.TestCase):

    def test_resolves_visible_target_by_name(self):
        c = _fresh_client()
        _add_being(c, 2291295, 'Aurelien', 52, 50)
        msg = bot.start_follow(c, 'Aurelien')
        self.assertIn('Aurelien', msg)
        self.assertEqual(c._follow_target_name, 'Aurelien')
        self.assertEqual(c._follow_target_id, 2291295)
        self.assertEqual(c._follow_state, 'following')
        # Legacy alias still tracks the id for the dashboard's auto.* field.
        self.assertEqual(c._follow_target, 2291295)

    def test_resolves_by_numeric_id(self):
        c = _fresh_client()
        _add_being(c, 2291295, 'Aurelien', 52, 50)
        msg = bot.start_follow(c, '2291295')
        self.assertEqual(c._follow_target_name, 'Aurelien')
        self.assertEqual(c._follow_target_id, 2291295)
        self.assertEqual(c._follow_state, 'following')
        self.assertIn('Aurelien', msg)

    def test_target_not_visible_yet_waits(self):
        c = _fresh_client()
        msg = bot.start_follow(c, 'Aurelien')
        self.assertEqual(c._follow_target_name, 'Aurelien')
        self.assertEqual(c._follow_target_id, 0)
        self.assertEqual(c._follow_state, 'waiting')
        self.assertIn('waiting', msg.lower())

    def test_stop_follow_clears_state(self):
        c = _fresh_client()
        _add_being(c, 2291295, 'Aurelien', 52, 50)
        bot.start_follow(c, 'Aurelien')
        bot.stop_follow(c)
        self.assertEqual(c._follow_target_name, '')
        self.assertEqual(c._follow_target_id, 0)
        self.assertEqual(c._follow_state, 'idle')
        self.assertEqual(c._follow_target, 0)


class TickFollowTest(unittest.TestCase):

    def test_reappear_with_same_id_stays_followed(self):
        c = _fresh_client(px=50, py=50)
        target = _add_being(c, 2291295, 'Aurelien', 52, 50)
        bot.start_follow(c, 'Aurelien')
        self.assertEqual(c._follow_state, 'following')

        # Target vanishes (not on a warp tile).
        del c.beings[2291295]
        bot._tick_follow(c, tick_count=0)
        self.assertEqual(c._follow_state, 'waiting')
        self.assertEqual(c._follow_target_id, 0)

        # Target reappears with the same id; we lock back on.
        c.beings[2291295] = target
        bot._tick_follow(c, tick_count=8)  # %8==0 so chase fires if needed
        self.assertEqual(c._follow_state, 'following')
        self.assertEqual(c._follow_target_id, 2291295)
        self.assertEqual(c._follow_target, 2291295)

    def test_timeout_clears_follow(self):
        c = _fresh_client()
        _add_being(c, 2291295, 'Aurelien', 52, 50)
        bot.start_follow(c, 'Aurelien')
        # Target vanishes (not on a warp tile; just out of sight).
        del c.beings[2291295]
        # Backdate last_seen so the next tick exceeds the timeout.
        c._follow_last_seen_at = time.time() - (c._follow_timeout + 1.0)
        # First tick puts us into 'waiting' because last_seen_pos was on
        # a non-warp tile; the *next* tick (with no last_seen_pos because
        # we've already drained the warp branch on tick 1) checks timeout.
        # The timeout check happens at the very end of _tick_follow, so a
        # single tick that finds no being and no warp is enough when the
        # last-seen position is non-warp: it will set state to 'waiting'
        # but then continue past it and check the timeout.
        bot._tick_follow(c, tick_count=0)
        # We started 'following'; first tick after disappearance follows
        # the 'state == following and last_pos not on warp' branch which
        # sets state to 'waiting' and returns. The timeout check runs on
        # the *next* tick where last_pos is still set.
        # Drive a second tick.
        bot._tick_follow(c, tick_count=1)
        self.assertEqual(c._follow_state, 'idle')
        self.assertEqual(c._follow_target_name, '')

    def test_chase_walks_toward_visible_target(self):
        c = _fresh_client(px=40, py=40)
        _add_being(c, 2291295, 'Aurelien', 60, 60)
        bot.start_follow(c, 'Aurelien')
        # tick_count=0 triggers the %8==0 chase branch.
        bot._tick_follow(c, tick_count=0)
        self.assertTrue(c._walks, 'expected a walk_to call')
        # Walk target should be near (but not on) Aurelien's tile.
        wx, wy = c._walks[-1]
        self.assertNotEqual((wx, wy), (60, 60))
        self.assertGreater(wx, 40)
        self.assertGreater(wy, 40)

    def test_warp_tile_triggers_warping_state(self):
        """When the target vanishes from a warp tile, we walk onto it."""
        c = _fresh_client(map_name='009-1', px=22, py=36)
        target = _add_being(c, 2291295, 'Aurelien', 24, 36)
        bot.start_follow(c, 'Aurelien')
        self.assertEqual(c._follow_state, 'following')

        # Pretend the tile (24, 36) is a warp tile.
        fake_cmap = mock.Mock()
        fake_cmap.is_warp = lambda x, y: (x, y) == (24, 36)

        del c.beings[2291295]
        with mock.patch('tmw_mcp.bot.load_collision', return_value=fake_cmap, create=True):
            # ``bot._tick_follow`` does ``from .maps import load_collision``
            # at runtime, so patch the maps module instead.
            with mock.patch('tmw_mcp.maps.load_collision', return_value=fake_cmap):
                bot._tick_follow(c, tick_count=0)

        self.assertEqual(c._follow_state, 'warping')
        # We should have walked onto the warp tile.
        self.assertIn((24, 36), c._walks)


class MapChangeTest(unittest.TestCase):

    def test_map_change_resets_then_resumes_when_target_reappears(self):
        c = _fresh_client(map_name='009-1', px=22, py=36)
        _add_being(c, 2291295, 'Aurelien', 24, 36)
        bot.start_follow(c, 'Aurelien')

        # Simulate a map change: beings cleared, player on new map.
        c.beings.clear()
        c.player.map_name = '008-1'
        c.player.x = 25
        c.player.y = 62

        bot._tick_follow(c, tick_count=0)
        # Map mismatch resets caches and drops to 'waiting'.
        self.assertEqual(c._follow_state, 'waiting')
        self.assertEqual(c._follow_last_seen_map, '008-1')

        # Target appears on the new map (possibly with a fresh block_id).
        _add_being(c, 9999999, 'Aurelien', 26, 62)
        bot._tick_follow(c, tick_count=8)
        self.assertEqual(c._follow_state, 'following')
        self.assertEqual(c._follow_target_id, 9999999)


class SnapshotTest(unittest.TestCase):

    def test_no_follow_section_when_idle(self):
        c = _fresh_client()
        snap = build_snapshot(c, None)
        self.assertNotIn('follow', snap)

    def test_follow_section_populated_when_active(self):
        c = _fresh_client(px=40, py=40)
        _add_being(c, 2291295, 'Aurelien', 60, 60)
        bot.start_follow(c, 'Aurelien')
        snap = build_snapshot(c, None)
        self.assertIn('follow', snap)
        f = snap['follow']
        self.assertTrue(f['active'])
        self.assertEqual(f['target_name'], 'Aurelien')
        self.assertEqual(f['target_id'], 2291295)
        self.assertTrue(f['target_visible'])
        self.assertEqual(f['state'], 'following')
        self.assertEqual(f['target_pos'], [60, 60])
        self.assertIn('last_seen_pos', f)

    def test_follow_section_when_waiting(self):
        c = _fresh_client()
        bot.start_follow(c, 'Aurelien')
        snap = build_snapshot(c, None)
        f = snap.get('follow')
        self.assertIsNotNone(f)
        self.assertTrue(f['active'])
        self.assertFalse(f['target_visible'])
        self.assertEqual(f['state'], 'waiting')
        # No target_pos when target is not visible.
        self.assertNotIn('target_pos', f)


if __name__ == '__main__':
    unittest.main()
