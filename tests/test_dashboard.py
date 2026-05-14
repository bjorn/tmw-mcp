"""Smoke tests for the dashboard server.

These tests construct a DashboardServer against a fake game-client
object and verify that /state, /events, and /map all work end-to-end
over a real TCP socket bound to localhost. No game server is needed.

Run with: .venv/bin/python -m unittest client/tests/test_dashboard.py
"""

import json
import os
import sys
import threading
import time
import unittest
import urllib.request

# Allow running from anywhere: put the repo root on sys.path so
# ``import tmw_mcp`` resolves to the in-tree package.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tmw_mcp.dashboard import (
    AGGRO_RANGES,
    DashboardServer,
    OperatorHooks,
    StateBroadcaster,
    build_snapshot,
)
from tmw_mcp.game import GameClient


class _FakeBeing:
    def __init__(self, block_id, name, species, x, y, hp=0, max_hp=0, level=0):
        self.block_id = block_id
        self.name = name
        self.species = species
        self.x = x
        self.y = y
        self.hp = hp
        self.max_hp = max_hp
        self.level = level
        self.direction = 0
        self.speed = 150


class _FakeFloorItem:
    def __init__(self, block_id, name_id, amount, x, y):
        self.block_id = block_id
        self.name_id = name_id
        self.amount = amount
        self.x = x
        self.y = y


class _FakeInvItem:
    def __init__(self, index, name_id, amount, equipped=0):
        self.index = index
        self.name_id = name_id
        self.amount = amount
        self.equipped = equipped


class _FakePlayer:
    def __init__(self):
        self.char_name = 'Claudius'
        self.map_name = '008-1'
        self.x = 45
        self.y = 85
        self.direction = 0
        self.hp = 318
        self.max_hp = 318
        self.sp = 52
        self.max_sp = 52
        self.base_level = 42
        self.job_level = 2
        self.base_exp = 39484
        self.next_base_exp = 42102
        self.job_exp = 100
        self.next_job_exp = 1000
        self.zeny = 18530
        self.weight = 200
        self.max_weight = 2000
        self.str_ = 35
        self.agi = 30
        self.vit = 20
        self.int_ = 10
        self.dex = 15
        self.luk = 5
        self.status_point = 0
        self.atk1 = 50
        self.def1 = 12


class _FakeClient:
    """Just enough of GameClient's surface for build_snapshot()."""

    def __init__(self):
        self.account_id = 1234
        self.player = _FakePlayer()
        self.beings = {
            110001442: _FakeBeing(
                110001442, 'Tortuga', 1024, 43, 82, hp=400, max_hp=535
            ),
            2283848: _FakeBeing(
                2283848, 'Hi=)', 0, 43, 47, level=56
            ),
            555: _FakeBeing(555, 'Bee', 1029, 46, 86, hp=10, max_hp=10),
            # A warp NPC: species 45, server-assigned 'w<digits>' name.
            110001485: _FakeBeing(110001485, 'w110001485', 45, 50, 90),
            # Another warp variant: only the name marks it (species 0).
            110001486: _FakeBeing(110001486, 'w110001486', 0, 51, 90),
        }
        self.floor_items = {
            1: _FakeFloorItem(1, 535, 1, 50, 80),
        }
        self.inventory = {
            15: _FakeInvItem(15, 1201, 1, equipped=1),
            16: _FakeInvItem(16, 535, 5),
        }
        self.npc_id = 0
        self.npc_dialog_open = False
        self.npc_dialog = []
        self.npc_choices = []
        self.npc_waiting_next = False
        self.npc_waiting_close = False
        self.npc_waiting_choice = False
        self.npc_waiting_input = ''
        self.chat_log = ['hello there', '[party] following you']
        # Structured chat entries with kinds + arrival timestamps. The
        # dashboard prefers this over chat_log when available.
        self.chat_entries = [
            {'ts': 1700000000.0, 'kind': 'say', 'text': 'hello there'},
            {'ts': 1700000001.0, 'kind': 'party',
             'text': '[party] following you'},
            {'ts': 1700000002.0, 'kind': 'server',
             'text': 'Server : welcome'},
            {'ts': 1700000003.0, 'kind': 'gm', 'text': '[GM] heads up'},
            {'ts': 1700000004.0, 'kind': 'whisper',
             'text': '[whisper from foo] hi'},
            {'ts': 1700000005.0, 'kind': 'whisper_out',
             'text': '[whisper to foo] hey back'},
        ]
        self.whisper_log = []
        self._hunt_type = 'Tortuga,Pinkie'
        self._hunt_home = (45, 85)
        self._walk_dest = (46, 86)
        self._path_queue = [(48, 88)]
        self._auto_attack_target = 0
        self._follow_target = 0
        self._attack_range = 1


def _fake_item_name(name_id):
    return {1201: 'Leather Gloves', 535: 'Acorn'}.get(name_id, f'item#{name_id}')


class BuildSnapshotTest(unittest.TestCase):

    def test_full_snapshot_shape(self):
        client = _FakeClient()
        snap = build_snapshot(client, _fake_item_name)
        self.assertEqual(snap['map'], '008-1')
        self.assertEqual(snap['self']['name'], 'Claudius')
        self.assertEqual(snap['self']['x'], 45)
        self.assertEqual(snap['self']['hp'], 318)
        self.assertEqual(snap['self']['stats']['str'], 35)

        kinds = sorted(b['kind'] for b in snap['beings'])
        # Tortuga + Bee are monsters; 'Hi=)' is a player; two warps.
        self.assertEqual(
            kinds, ['monster', 'monster', 'player', 'warp', 'warp']
        )
        warps = [b for b in snap['beings'] if b['kind'] == 'warp']
        self.assertEqual(len(warps), 2)

        bee = next(b for b in snap['beings'] if b['name'] == 'Bee')
        self.assertEqual(bee['aggro_range'], AGGRO_RANGES['Bee'])

        self.assertEqual(snap['floor_items'][0]['item'], 'Acorn')
        self.assertTrue(snap['hunt']['active'])
        self.assertEqual(snap['hunt']['home'], [45, 85])
        self.assertIn('walk_path', snap)
        self.assertEqual(snap['walk_path'][0], [45, 85])

        equipped = [i for i in snap['inventory'] if i['equipped']]
        self.assertEqual(len(equipped), 1)
        self.assertEqual(equipped[0]['item'], 'Leather Gloves')

        # Chat entries surface kind + ts straight from chat_entries.
        chat = snap['chat_recent']
        self.assertEqual(chat[0]['kind'], 'say')
        self.assertAlmostEqual(chat[0]['ts'], 1700000000.0)
        kinds_seen = {m['kind'] for m in chat}
        self.assertIn('server', kinds_seen)
        self.assertIn('whisper', kinds_seen)
        self.assertIn('whisper_out', kinds_seen)
        self.assertIn('gm', kinds_seen)
        # The outgoing whisper line keeps its "to <target>" format.
        out = next(m for m in chat if m['kind'] == 'whisper_out')
        self.assertIn('foo', out['text'])
        self.assertIn('hey back', out['text'])

    def test_minimal_client(self):
        """A bare client (mid-login state) must not raise."""

        class _Empty:
            pass

        empty = _Empty()
        empty.player = _FakePlayer()
        snap = build_snapshot(empty, None)
        self.assertIn('self', snap)


class BroadcasterTest(unittest.TestCase):

    def test_drop_oldest_on_overflow(self):
        b = StateBroadcaster()
        c = b.subscribe()
        # Fill way past the queue cap.
        for i in range(200):
            b.publish({'i': i})
        # Queue is bounded, but the latest must always be in it.
        items = []
        while True:
            try:
                items.append(c.q.get_nowait())
            except Exception:
                break
        # Some items must have been dropped (otherwise the cap is broken).
        self.assertGreater(c.dropped, 0)
        # The most recent publish must have arrived.
        self.assertEqual(items[-1]['i'], 199)


class DashboardServerTest(unittest.TestCase):

    def setUp(self):
        self.client = _FakeClient()
        self.server = DashboardServer.start(
            port=0,  # let the OS pick a free port
            state_provider=lambda: build_snapshot(self.client, _fake_item_name),
        )
        # Wait a moment for the poller to push at least one snapshot.
        deadline = time.time() + 3.0
        while self.server.broadcaster.latest() is None and time.time() < deadline:
            time.sleep(0.05)

    def tearDown(self):
        self.server.stop()

    def _url(self, path):
        return f'http://127.0.0.1:{self.server.port}{path}'

    def test_state_endpoint(self):
        with urllib.request.urlopen(self._url('/state'), timeout=3.0) as r:
            self.assertEqual(r.status, 200)
            payload = json.loads(r.read().decode('utf-8'))
        self.assertEqual(payload['self']['name'], 'Claudius')
        self.assertEqual(payload['map'], '008-1')

    def test_index_html(self):
        with urllib.request.urlopen(self._url('/'), timeout=3.0) as r:
            body = r.read().decode('utf-8')
        self.assertIn('Claudius', body)
        self.assertIn('/static/dashboard.js', body)

    def test_static_js(self):
        with urllib.request.urlopen(
            self._url('/static/dashboard.js'), timeout=3.0
        ) as r:
            self.assertEqual(r.status, 200)
            self.assertIn('EventSource', r.read().decode('utf-8'))

    def test_events_delivers_frame(self):
        # Open the SSE stream, read until we get at least one data: line.
        req = urllib.request.Request(self._url('/events'))
        with urllib.request.urlopen(req, timeout=3.0) as r:
            buf = b''
            deadline = time.time() + 3.0
            data_line = None
            while time.time() < deadline:
                chunk = r.read(2048)
                if not chunk:
                    break
                buf += chunk
                for raw in buf.split(b'\n\n'):
                    for line in raw.split(b'\n'):
                        if line.startswith(b'data: '):
                            data_line = line[6:].decode('utf-8')
                            break
                    if data_line:
                        break
                if data_line:
                    break
        self.assertIsNotNone(data_line, 'no SSE data frame received')
        payload = json.loads(data_line)
        self.assertEqual(payload['self']['name'], 'Claudius')

    def test_unknown_route(self):
        try:
            urllib.request.urlopen(self._url('/nope'), timeout=3.0)
            self.fail('expected 404')
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)


def _post_json(url, body, timeout=3.0):
    raw = json.dumps(body).encode('utf-8')
    req = urllib.request.Request(
        url,
        data=raw,
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    return urllib.request.urlopen(req, timeout=timeout)


class OperatorEndpointsTest(unittest.TestCase):
    """Cover the POST /op/* endpoints end-to-end over a real socket."""

    def setUp(self):
        self.client = _FakeClient()
        self.walks: list[tuple[int, int]] = []
        self.attacks: list[int] = []
        self.says: list[str] = []
        self.notifies: list[str] = []
        self.hooks = OperatorHooks(
            walk=lambda x, y: self.walks.append((x, y)),
            attack=lambda bid: self.attacks.append(bid),
            say=lambda t: self.says.append(t),
            notify=lambda t: self.notifies.append(t),
            known_being_ids=lambda: list(self.client.beings.keys()),
        )
        self.server = DashboardServer.start(
            port=0,
            state_provider=lambda: build_snapshot(self.client, _fake_item_name),
            op_hooks=self.hooks,
        )
        deadline = time.time() + 3.0
        while self.server.broadcaster.latest() is None and time.time() < deadline:
            time.sleep(0.05)

    def tearDown(self):
        self.server.stop()

    def _url(self, path):
        return f'http://127.0.0.1:{self.server.port}{path}'

    def test_op_say_triggers_hooks(self):
        with _post_json(self._url('/op/say'), {'text': 'hello world'}) as r:
            self.assertEqual(r.status, 200)
            payload = json.loads(r.read().decode('utf-8'))
        self.assertTrue(payload.get('ok'))
        # Operator input must NOT be broadcast as public chat; it's a
        # private channel from Bjorn to Claude.
        self.assertEqual(self.says, [])
        # Notification is prefixed for the Claude session.
        self.assertEqual(self.notifies, ['[Operator] hello world'])
        # The operator-echo line will be merged into the next snapshot.
        deadline = time.time() + 2.0
        found = False
        while time.time() < deadline:
            snap = self.server.broadcaster.latest() or {}
            for m in snap.get('chat_recent', []):
                if (m.get('kind') == 'operator'
                        and 'hello world' in m.get('text', '')):
                    found = True
                    break
            if found:
                break
            time.sleep(0.1)
        self.assertTrue(found, 'operator echo missing from chat tail')

    def test_op_say_rejects_empty(self):
        try:
            _post_json(self._url('/op/say'), {'text': '   '})
            self.fail('expected 400')
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)
        self.assertEqual(self.says, [])

    def test_op_walk_triggers_callback(self):
        with _post_json(self._url('/op/walk'), {'x': 42, 'y': 87}) as r:
            self.assertEqual(r.status, 200)
            payload = json.loads(r.read().decode('utf-8'))
        self.assertTrue(payload.get('ok'))
        self.assertEqual(self.walks, [(42, 87)])
        # Notify carries the same coordinates so Claude sees the action.
        self.assertEqual(self.notifies, ['[Operator] walk to (42,87)'])

    def test_op_walk_rejects_non_integer(self):
        try:
            _post_json(self._url('/op/walk'), {'x': 'foo', 'y': 10})
            self.fail('expected 400')
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)
        self.assertEqual(self.walks, [])

    def test_op_attack_unknown_being_returns_404(self):
        try:
            _post_json(self._url('/op/attack'), {'being_id': 999999})
            self.fail('expected 404')
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)
        self.assertEqual(self.attacks, [])

    def test_op_attack_known_being_dispatches(self):
        # Bee is in self.client.beings as 555.
        with _post_json(self._url('/op/attack'), {'being_id': 555}) as r:
            self.assertEqual(r.status, 200)
        self.assertEqual(self.attacks, [555])
        self.assertEqual(self.notifies, ['[Operator] attack #555'])


class _StubConn:
    """Minimal stand-in for the ``Connection`` object on a GameClient.

    ``GameClient.whisper`` only ever calls ``send_packet`` on its
    ``map_conn``, so we just need to capture the bytes to make sure the
    real send path runs without touching the network.
    """

    def __init__(self):
        self.sent: list[bytes] = []

    def send_packet(self, data: bytes) -> None:
        self.sent.append(data)


class OutgoingWhisperTest(unittest.TestCase):
    """The server does not echo our outgoing whispers, so the bot must
    fabricate a chat entry locally for the dashboard. See game.whisper().
    """

    def test_whisper_appends_chat_entry(self):
        client = GameClient()
        client.map_conn = _StubConn()
        client.whisper('Bjorn', 'on my way')

        # The send packet still went out.
        self.assertEqual(len(client.map_conn.sent), 1)
        # The local chat ring gained a structured entry tagged whisper_out.
        self.assertTrue(client.chat_entries)
        entry = client.chat_entries[-1]
        self.assertEqual(entry['kind'], 'whisper_out')
        self.assertIn('Bjorn', entry['text'])
        self.assertIn('on my way', entry['text'])
        self.assertGreater(entry['ts'], 0)
        # And the plain chat_log mirrors it so log tails see it too.
        self.assertTrue(client.chat_log[-1].startswith('[whisper to Bjorn]'))

    def test_whisper_surfaces_in_snapshot(self):
        client = GameClient()
        client.map_conn = _StubConn()
        client.whisper('Bjorn', 'pong')

        snap = build_snapshot(client, None)
        chat = snap.get('chat_recent') or []
        self.assertTrue(chat, 'expected chat_recent to be populated')
        out = [m for m in chat if m.get('kind') == 'whisper_out']
        self.assertEqual(len(out), 1)
        self.assertIn('Bjorn', out[0]['text'])
        self.assertIn('pong', out[0]['text'])


if __name__ == '__main__':
    unittest.main()
