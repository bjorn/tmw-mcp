"""
TMW dashboard: an in-process HTTP server that exposes a live view of the
bot's state to a browser.

This module is intentionally dependency-free (stdlib only): a threaded
HTTP server runs on a daemon thread, serves a tiny static UI from
``dashboard_static/``, and pushes JSON state snapshots over Server-Sent
Events to any connected browser.

The bot loop is the producer. It calls :func:`broadcast_state` (cheap;
just enqueues into bounded per-client queues, dropping the oldest frame
on overflow). A background poller thread inside the server also rebuilds
the snapshot at ~5 Hz directly from a ``GameClient`` so that even idle
bot loops keep the view fresh.

Localhost only. No auth. Never bind to 0.0.0.0.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

log = logging.getLogger(__name__)

STATIC_DIR = os.path.join(os.path.dirname(__file__), 'dashboard_static')

# Per-client SSE queue depth. Frames beyond this are dropped (oldest first)
# so a slow browser cannot back up the bot loop.
_CLIENT_QUEUE_SIZE = 16

# How often the background poller rebuilds the state snapshot (seconds).
_POLL_INTERVAL = 0.2

# Hardcoded aggro ranges for common aggressive monsters. The dashboard
# uses these to draw a translucent danger disc around them so Bjorn can
# see when Claudius is wandering into trouble. Values are in tiles.
# Conservative; tune as needed.
AGGRO_RANGES: dict[str, int] = {
    'Bee': 3,
    'Red Scorpion': 4,
    'Black Scorpion': 4,
    'Scorpion': 3,
    'Duck': 3,
    'Pink Flower': 0,
    'Mountain Snake': 3,
    'Grass Snake': 3,
    'Cave Snake': 3,
    'Forest Mushroom': 3,
    'Spiky Mushroom': 3,
    'Mauve Plant': 0,
    'Pinkie': 0,
}


# ---------------------------------------------------------------------------
# Operator hooks
# ---------------------------------------------------------------------------


@dataclass
class OperatorHooks:
    """Glue between the dashboard HTTP server and the bot.

    All callbacks are optional. They are invoked from the HTTP server
    thread, so each implementation must marshal back to the right
    thread itself (the bot already does this via its command queue
    and ``asyncio.run_coroutine_threadsafe`` style hop).

    * ``walk(x, y) -> str | None`` triggers a pathfinding walk.
    * ``attack(being_id) -> str | None`` starts a continuous attack.
    * ``say(text) -> str | None`` speaks publicly in-game.
    * ``notify(text) -> None`` pushes a channel notification to the
      Claude Code session connected via MCP (so the operator's intent
      lands in Claude's conversation, prefixed with ``[Operator] ...``).
    * ``known_being_ids() -> iterable of int`` is consulted by
      ``/op/attack`` to validate the being_id before dispatching.
    """

    walk: Callable[[int, int], str | None] | None = None
    attack: Callable[[int], str | None] | None = None
    say: Callable[[str], str | None] | None = None
    notify: Callable[[str], None] | None = None
    known_being_ids: Callable[[], Any] | None = None


# Maximum operator chat / walk input length. Generous but cheap to enforce.
_MAX_OP_TEXT = 500


# ---------------------------------------------------------------------------
# State broadcaster
# ---------------------------------------------------------------------------


@dataclass
class _Client:
    """A connected SSE browser. Each gets its own bounded queue."""

    q: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=_CLIENT_QUEUE_SIZE))
    dropped: int = 0


class StateBroadcaster:
    """Pub/sub fan-out from the bot loop to all connected SSE clients."""

    def __init__(self) -> None:
        self._clients: list[_Client] = []
        self._lock = threading.Lock()
        self._latest: dict | None = None

    def subscribe(self) -> _Client:
        c = _Client()
        with self._lock:
            self._clients.append(c)
            latest = self._latest
        if latest is not None:
            try:
                c.q.put_nowait(latest)
            except queue.Full:
                pass
        return c

    def unsubscribe(self, c: _Client) -> None:
        with self._lock:
            try:
                self._clients.remove(c)
            except ValueError:
                pass

    def latest(self) -> dict | None:
        with self._lock:
            return self._latest

    def publish(self, state: dict) -> None:
        """Enqueue a snapshot for every connected client. Never blocks."""
        with self._lock:
            self._latest = state
            clients = list(self._clients)
        for c in clients:
            # Bounded queue: drop oldest to keep up with a slow consumer.
            while True:
                try:
                    c.q.put_nowait(state)
                    break
                except queue.Full:
                    try:
                        c.q.get_nowait()
                        c.dropped += 1
                    except queue.Empty:
                        break


# ---------------------------------------------------------------------------
# State snapshot builder
# ---------------------------------------------------------------------------


# Warp NPCs in TMW share two telltales: the server-side species id 45,
# and a server-assigned name that looks like ``w110001485`` (a 'w'
# followed by a numeric block id). Either match classifies the being
# as a warp so the renderer can draw it as a tile decoration instead
# of a labeled NPC dot.
_WARP_NAME_RE = re.compile(r'^w\d+$')


def _is_warp_being(b) -> bool:
    """Return True if ``b`` looks like a server-side warp NPC."""
    if getattr(b, 'species', 0) == 45:
        return True
    name = getattr(b, 'name', '') or ''
    return bool(_WARP_NAME_RE.match(name))


def _kind_from_prefix(text: str) -> str:
    """Best-effort chat kind classification from a preformatted line.

    Only used as a fallback for callers that did not populate the
    structured ``chat_entries`` list (e.g. the test fakes).
    """
    if text.startswith('[whisper to'):
        return 'whisper_out'
    if text.startswith('[whisper'):
        return 'whisper'
    if text.startswith('[party]') or text.startswith('[Party]'):
        return 'party'
    if text.startswith('[GM]'):
        return 'gm'
    if text.startswith('[Operator]'):
        return 'operator'
    if text.startswith('Server :') or text.startswith('Server:'):
        return 'server'
    if text.startswith('#'):
        return 'channel'
    return 'say'


def _being_kind(b) -> str:
    """Classify a being for the UI: warp, monster, player, or npc.

    The TMW protocol does not have a clean type flag for beings, so we
    infer:
      * looks like a warp (species 45 or 'w<digits>' name) -> warp
      * species >= 1002 -> monster (matches monster_name() in game.py)
      * has HP or a level reported -> player
      * otherwise -> NPC

    The level fallback catches remote players where the server has not
    yet pushed our copy of their max_hp.
    """
    if _is_warp_being(b):
        return 'warp'
    if getattr(b, 'species', 0) >= 1002:
        return 'monster'
    if getattr(b, 'max_hp', 0) > 0 or getattr(b, 'level', 0) > 0:
        return 'player'
    return 'npc'


def _being_name(b) -> str:
    """Display name for a being, resolving the monster species fallback live.

    ``b.name`` is only populated from an authoritative server name response,
    so monsters seen before ``monsters.xml`` loaded carry an empty name; fall
    back to the species table here (it self-heals once the overlay loads).
    """
    name = getattr(b, 'name', '') or ''
    if name:
        return name
    species = getattr(b, 'species', 0)
    if species >= 1002:
        try:
            from .monsters import monster_name
            return monster_name(species)
        except Exception:
            return ''
    return ''


def build_snapshot(client, item_name: Callable[[int], str] | None = None) -> dict:
    """Build a JSON-serialisable snapshot dict from a live GameClient.

    Designed to fail soft: any missing attribute is omitted rather than
    raising. The bot may be in many half-loaded states (e.g. mid-login).
    """
    snap: dict[str, Any] = {'ts': time.time()}

    p = getattr(client, 'player', None)
    if p is None:
        return snap

    snap['map'] = getattr(p, 'map_name', '')

    snap['self'] = {
        'name': getattr(p, 'char_name', ''),
        'x': getattr(p, 'x', 0),
        'y': getattr(p, 'y', 0),
        'direction': getattr(p, 'direction', 0),
        'hp': getattr(p, 'hp', 0),
        'hp_max': getattr(p, 'max_hp', 0),
        'sp': getattr(p, 'sp', 0),
        'sp_max': getattr(p, 'max_sp', 0),
        'level': getattr(p, 'base_level', 0),
        'job_level': getattr(p, 'job_level', 0),
        'exp': getattr(p, 'base_exp', 0),
        'exp_max': getattr(p, 'next_base_exp', 0),
        'job_exp': getattr(p, 'job_exp', 0),
        'job_exp_max': getattr(p, 'next_job_exp', 0),
        'zeny': getattr(p, 'zeny', 0),
        'weight': getattr(p, 'weight', 0),
        'weight_max': getattr(p, 'max_weight', 0),
        'stats': {
            'str': getattr(p, 'str_', 0),
            'agi': getattr(p, 'agi', 0),
            'vit': getattr(p, 'vit', 0),
            'int': getattr(p, 'int_', 0),
            'dex': getattr(p, 'dex', 0),
            'luk': getattr(p, 'luk', 0),
        },
        'status_point': getattr(p, 'status_point', 0),
        'atk': getattr(p, 'atk1', 0),
        'def': getattr(p, 'def1', 0),
    }

    account_id = getattr(client, 'account_id', 0)
    beings_dict = getattr(client, 'beings', {}) or {}
    beings = []
    for bid, b in beings_dict.items():
        if bid == account_id:
            continue
        kind = _being_kind(b)
        entry = {
            'id': bid,
            'kind': kind,
            'name': _being_name(b),
            'x': getattr(b, 'x', 0),
            'y': getattr(b, 'y', 0),
            'direction': getattr(b, 'direction', 0),
        }
        max_hp = getattr(b, 'max_hp', 0)
        if max_hp:
            entry['hp'] = getattr(b, 'hp', 0)
            entry['hp_max'] = max_hp
        level = getattr(b, 'level', 0)
        if level:
            entry['level'] = level
        # Aggro range for the danger overlay.
        if kind == 'monster' and entry['name'] in AGGRO_RANGES:
            entry['aggro_range'] = AGGRO_RANGES[entry['name']]
        beings.append(entry)
    snap['beings'] = beings

    floor_items = []
    for it in (getattr(client, 'floor_items', {}) or {}).values():
        name = ''
        if item_name is not None:
            try:
                name = item_name(getattr(it, 'name_id', 0))
            except Exception:
                name = ''
        floor_items.append({
            'id': getattr(it, 'block_id', 0),
            'name_id': getattr(it, 'name_id', 0),
            'item': name,
            'amount': getattr(it, 'amount', 0),
            'x': getattr(it, 'x', 0),
            'y': getattr(it, 'y', 0),
        })
    snap['floor_items'] = floor_items

    # Hunt state lives on the client object as a handful of underscore
    # attributes that bot.run_auto_behaviors maintains.
    hunt_type = getattr(client, '_hunt_type', '') or ''
    hunt_home = getattr(client, '_hunt_home', None)
    if hunt_type or hunt_home:
        snap['hunt'] = {
            'active': bool(hunt_type),
            'targets': [t.strip() for t in hunt_type.split(',') if t.strip()],
            'home': list(hunt_home) if hunt_home else None,
            'radius': 8,  # roam radius from bot.run_auto_behaviors
            'leash': 20,
        }

    # Active walk: the queued waypoints from walk_path plus the current
    # in-progress destination. The first entry is always Claudius'
    # current position so the canvas can draw a continuous polyline.
    walk_path: list[list[int]] = []
    walk_dest = getattr(client, '_walk_dest', None)
    if walk_dest is not None:
        walk_path.append([p.x, p.y])
        walk_path.append([walk_dest[0], walk_dest[1]])
    path_queue = getattr(client, '_path_queue', None) or []
    for wp in path_queue:
        try:
            walk_path.append([int(wp[0]), int(wp[1])])
        except (TypeError, IndexError):
            continue
    if walk_path:
        snap['walk_path'] = walk_path

    snap['npc_dialog'] = {
        'open': bool(getattr(client, 'npc_dialog_open', False)),
        'speaker': _npc_name(client),
        'lines': list(getattr(client, 'npc_dialog', []) or []),
        'choices': list(getattr(client, 'npc_choices', []) or []),
        'waiting': _npc_waiting(client),
    }

    # Chat: prefer the structured ``chat_entries`` ring buffer (carries
    # per-message ts + kind). Fall back to the plain ``chat_log`` if a
    # caller passes a stripped-down test client without entries.
    chat_entries = list(getattr(client, 'chat_entries', []) or [])
    if chat_entries:
        chat_recent = [
            {'text': e.get('text', ''),
             'kind': e.get('kind', 'say'),
             'ts': e.get('ts', 0)}
            for e in chat_entries
        ]
    else:
        chat_log = list(getattr(client, 'chat_log', []) or [])
        now = time.time()
        chat_recent = [
            {'text': m, 'kind': _kind_from_prefix(m), 'ts': now}
            for m in chat_log
        ]
        whisper_log = list(getattr(client, 'whisper_log', []) or [])
        for sender, msg in whisper_log[-10:]:
            chat_recent.append({
                'text': f'[whisper from {sender}] {msg}',
                'kind': 'whisper',
                'ts': now,
            })
    # Operator-echoed lines are merged in by the DashboardServer poller
    # (see ``_inject_op_log``), so build_snapshot stays bot-side-only.
    snap['chat_recent'] = chat_recent[-40:]

    inventory_dict = getattr(client, 'inventory', {}) or {}
    inv = []
    for idx in sorted(inventory_dict.keys()):
        item = inventory_dict[idx]
        name = ''
        if item_name is not None:
            try:
                name = item_name(getattr(item, 'name_id', 0))
            except Exception:
                name = ''
        inv.append({
            'slot': idx,
            'name_id': getattr(item, 'name_id', 0),
            'item': name,
            'qty': getattr(item, 'amount', 0),
            'equipped': bool(getattr(item, 'equipped', 0)),
        })
    snap['inventory'] = inv

    snap['auto'] = {
        'attack_target': getattr(client, '_auto_attack_target', 0) or 0,
        'follow_target': getattr(client, '_follow_target', 0) or 0,
        'attack_range': getattr(client, '_attack_range', 1) or 1,
    }

    # Follow mode: dashboard panel + connecting line on the map. The bot
    # keeps follow keyed by name (survives portal warps), and exposes a
    # small state machine via _follow_state.
    follow_name = getattr(client, '_follow_target_name', '') or ''
    if follow_name:
        follow_id = getattr(client, '_follow_target_id', 0) or 0
        target = beings_dict.get(follow_id) if follow_id else None
        target_pos = None
        if target is not None:
            target_pos = [getattr(target, 'x', 0), getattr(target, 'y', 0)]
        last_pos = getattr(client, '_follow_last_seen_pos', None)
        follow_entry: dict[str, Any] = {
            'active': True,
            'target_name': follow_name,
            'target_id': follow_id,
            'target_visible': target is not None,
            'state': getattr(client, '_follow_state', 'idle') or 'idle',
            'last_seen_at': getattr(client, '_follow_last_seen_at', 0.0) or 0.0,
            'timeout': getattr(client, '_follow_timeout', 0.0) or 0.0,
        }
        if target_pos is not None:
            follow_entry['target_pos'] = target_pos
        if last_pos is not None:
            follow_entry['last_seen_pos'] = list(last_pos)
        snap['follow'] = follow_entry

    return snap


def _npc_name(client) -> str | None:
    npc_id = getattr(client, 'npc_id', 0)
    if not npc_id:
        return None
    beings = getattr(client, 'beings', {}) or {}
    being = beings.get(npc_id)
    if being is not None and getattr(being, 'name', ''):
        return being.name
    return f'NPC#{npc_id}'


def _npc_waiting(client) -> str | None:
    if getattr(client, 'npc_waiting_next', False):
        return 'next'
    if getattr(client, 'npc_waiting_close', False):
        return 'close'
    if getattr(client, 'npc_waiting_choice', False):
        return 'choice'
    wi = getattr(client, 'npc_waiting_input', '')
    if wi:
        return f'input_{wi}'
    return None


# ---------------------------------------------------------------------------
# HTTP handlers
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    """Routes: / and /static for the UI, /state for one-shot JSON,
    /events for SSE, /map/<name> for collision data."""

    # Set by DashboardServer.serve_forever.
    server_version = 'TMW-Dashboard/1.0'
    sys_version = ''

    def log_message(self, fmt, *args):  # noqa: N802 (stdlib name)
        # Route access logs through our logger at debug, not stderr.
        log.debug('http %s - %s', self.address_string(), fmt % args)

    # The DashboardServer attaches itself as self.server.dashboard.
    @property
    def dashboard(self) -> 'DashboardServer':
        return self.server.dashboard  # type: ignore[attr-defined]

    def do_GET(self):  # noqa: N802 (stdlib name)
        path = self.path.split('?', 1)[0]
        try:
            if path == '/' or path == '/index.html':
                self._serve_static('index.html', 'text/html; charset=utf-8')
            elif path.startswith('/static/'):
                fname = path[len('/static/') :]
                self._serve_static(fname, _guess_mime(fname))
            elif path == '/state':
                self._serve_state()
            elif path == '/events':
                self._serve_events()
            elif path.startswith('/map/'):
                self._serve_map(path[len('/map/') :])
            else:
                self.send_error(404, 'Not Found')
        except (BrokenPipeError, ConnectionResetError):
            # Browser closed the SSE tab. Normal, don't spam logs.
            pass
        except Exception:
            log.exception('handler error on %s', path)
            try:
                self.send_error(500, 'Internal Server Error')
            except Exception:
                pass

    def do_POST(self):  # noqa: N802 (stdlib name)
        path = self.path.split('?', 1)[0]
        # Localhost only. Reject anything else outright; the underlying
        # bind is already 127.0.0.1, but belt-and-braces.
        client_ip = (self.client_address or ('',))[0]
        if client_ip not in ('127.0.0.1', '::1'):
            self._send_json(403, {'error': 'forbidden (non-local)'})
            return
        try:
            if path == '/op/say':
                self._op_say()
            elif path == '/op/walk':
                self._op_walk()
            elif path == '/op/attack':
                self._op_attack()
            else:
                self.send_error(404, 'Not Found')
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            log.exception('POST handler error on %s', path)
            try:
                self._send_json(500, {'error': 'internal error'})
            except Exception:
                pass

    # -- operator helpers ----------------------------------------------

    def _read_json_body(self) -> dict | None:
        length = int(self.headers.get('Content-Length') or 0)
        if length <= 0 or length > 8192:
            return None
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode('utf-8'))
        except Exception:
            return None

    def _send_json(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(payload)

    def _op_say(self) -> None:
        body = self._read_json_body() or {}
        text = body.get('text', '')
        if not isinstance(text, str):
            self._send_json(400, {'error': 'text must be a string'})
            return
        text = text.strip()
        if not text:
            self._send_json(400, {'error': 'text is empty'})
            return
        if len(text) > _MAX_OP_TEXT:
            self._send_json(400,
                            {'error': f'text too long (>{_MAX_OP_TEXT} chars)'})
            return
        hooks = self.dashboard.op_hooks
        # Echo into the chat tail no matter what so the operator sees
        # their own message land. Operator input is delivered to Claude
        # via the notify hook (an [Operator] channel notification) and
        # is intentionally NOT broadcast publicly in-game: it's a private
        # message from Bjorn to Claude, not chat the bot should speak.
        echo_line = f'[Operator] {text}'
        self.dashboard.record_op_line(echo_line, kind='operator')
        if hooks.notify is not None:
            try:
                hooks.notify(echo_line)
            except Exception:
                log.exception('operator notify hook failed')
        self._send_json(200, {'ok': True})

    def _op_walk(self) -> None:
        body = self._read_json_body() or {}
        try:
            x = int(body.get('x'))
            y = int(body.get('y'))
        except (TypeError, ValueError):
            self._send_json(400, {'error': 'x and y must be integers'})
            return
        # Clamp to a reasonable tile range. The dashboard doesn't know
        # the map dimensions here; the bot's walking primitive will
        # silently clamp again. We just sanity-bound it.
        if not (0 <= x < 4096 and 0 <= y < 4096):
            self._send_json(400, {'error': 'x/y out of range'})
            return
        hooks = self.dashboard.op_hooks
        echo_line = f'[Operator] walk to ({x},{y})'
        self.dashboard.record_op_line(echo_line, kind='operator')
        if hooks.notify is not None:
            try:
                hooks.notify(echo_line)
            except Exception:
                log.exception('operator notify hook failed')
        if hooks.walk is None:
            self._send_json(503, {'error': 'walk hook not wired'})
            return
        try:
            hooks.walk(x, y)
        except Exception as e:
            log.exception('operator walk hook failed')
            self._send_json(500, {'error': f'walk failed: {e}'})
            return
        self._send_json(200, {'ok': True, 'x': x, 'y': y})

    def _op_attack(self) -> None:
        body = self._read_json_body() or {}
        try:
            being_id = int(body.get('being_id'))
        except (TypeError, ValueError):
            self._send_json(400, {'error': 'being_id must be an integer'})
            return
        hooks = self.dashboard.op_hooks
        # Validate against the bot's known beings if the hook gives us one.
        if hooks.known_being_ids is not None:
            try:
                known = set(int(b) for b in hooks.known_being_ids())
            except Exception:
                known = set()
            if being_id not in known:
                self._send_json(404, {'error': f'unknown being_id {being_id}'})
                return
        echo_line = f'[Operator] attack #{being_id}'
        self.dashboard.record_op_line(echo_line, kind='operator')
        if hooks.notify is not None:
            try:
                hooks.notify(echo_line)
            except Exception:
                log.exception('operator notify hook failed')
        if hooks.attack is None:
            self._send_json(503, {'error': 'attack hook not wired'})
            return
        try:
            hooks.attack(being_id)
        except Exception as e:
            log.exception('operator attack hook failed')
            self._send_json(500, {'error': f'attack failed: {e}'})
            return
        self._send_json(200, {'ok': True, 'being_id': being_id})

    # -- static --------------------------------------------------------

    def _serve_static(self, name: str, mime: str):
        # Reject anything that escapes the static dir.
        safe_name = os.path.normpath(name).lstrip(os.sep)
        if safe_name.startswith('..') or os.path.isabs(safe_name):
            self.send_error(403, 'Forbidden')
            return
        full = os.path.join(STATIC_DIR, safe_name)
        if not os.path.isfile(full):
            self.send_error(404, 'Not Found')
            return
        with open(full, 'rb') as f:
            body = f.read()
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    # -- /state --------------------------------------------------------

    def _serve_state(self):
        snap = self.dashboard.broadcaster.latest() or {}
        body = json.dumps(snap, default=_json_default).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    # -- /events (SSE) -------------------------------------------------

    def _serve_events(self):
        broadcaster = self.dashboard.broadcaster
        client = broadcaster.subscribe()
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache, no-store')
        self.send_header('Connection', 'close')
        # SSE: 2 second retry hint to the browser.
        self.send_header('X-Accel-Buffering', 'no')
        self.end_headers()
        # Prelude: retry hint + initial snapshot.
        try:
            self.wfile.write(b'retry: 2000\n\n')
            latest = broadcaster.latest()
            if latest is not None:
                self._write_event(latest)
            keepalive_at = time.time() + 15.0
            while not self.dashboard._stopping:
                try:
                    state = client.q.get(timeout=1.0)
                except queue.Empty:
                    if time.time() >= keepalive_at:
                        self.wfile.write(b': keepalive\n\n')
                        self.wfile.flush()
                        keepalive_at = time.time() + 15.0
                    continue
                self._write_event(state)
                keepalive_at = time.time() + 15.0
        finally:
            broadcaster.unsubscribe(client)

    def _write_event(self, state: dict):
        payload = json.dumps(state, default=_json_default)
        msg = f'data: {payload}\n\n'.encode('utf-8')
        self.wfile.write(msg)
        self.wfile.flush()

    # -- /map/<name> ---------------------------------------------------

    def _serve_map(self, name: str):
        # Reuse the bot's existing TMX parser. No new dependency.
        from .maps import load_collision

        # Names like '008-1' or '008-1.tmx' both fine; strip extension.
        if name.endswith('.tmx'):
            name = name[:-4]
        # Reject anything with separators.
        if '/' in name or '\\' in name or '..' in name:
            self.send_error(400, 'Bad map name')
            return
        cmap = load_collision(name)
        if cmap is None:
            self.send_error(404, 'Map not found')
            return
        body = json.dumps(
            {
                'name': cmap.name,
                'width': cmap.width,
                'height': cmap.height,
                # Flatten to a single string of '0'/'1' chars to keep it
                # compact (one byte per tile). Browser-side: index = y*w + x.
                'walkable': ''.join(
                    '1' if cmap.data[y][x] else '0'
                    for y in range(cmap.height)
                    for x in range(cmap.width)
                ),
                'warps': list(cmap.warps),
            },
            default=_json_default,
        ).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        # Map data is immutable per session; cache aggressively.
        self.send_header('Cache-Control', 'public, max-age=3600')
        self.end_headers()
        self.wfile.write(body)


def _guess_mime(fname: str) -> str:
    if fname.endswith('.html'):
        return 'text/html; charset=utf-8'
    if fname.endswith('.js'):
        return 'application/javascript; charset=utf-8'
    if fname.endswith('.css'):
        return 'text/css; charset=utf-8'
    if fname.endswith('.json'):
        return 'application/json'
    if fname.endswith('.svg'):
        return 'image/svg+xml'
    return 'application/octet-stream'


def _json_default(obj):
    # Catch-all for stray dataclass-like objects.
    if hasattr(obj, '__dict__'):
        return {k: v for k, v in obj.__dict__.items() if not k.startswith('_')}
    return repr(obj)


# ---------------------------------------------------------------------------
# Server wrapper
# ---------------------------------------------------------------------------


class DashboardServer:
    """Threaded HTTP server with an SSE broadcaster and a state poller.

    Usage:
        server = DashboardServer.start(port=8765, state_provider=lambda: build_snapshot(client, item_name))
        # ... bot runs ...
        server.stop()
    """

    def __init__(self, host: str, port: int,
                 state_provider: Callable[[], dict] | None,
                 op_hooks: OperatorHooks | None = None) -> None:
        self.host = host
        self.port = port
        self.broadcaster = StateBroadcaster()
        self._state_provider = state_provider
        self.op_hooks = op_hooks or OperatorHooks()
        # Recent operator-echoed lines (kind=operator) so they show up
        # in the chat tail. Bounded; oldest entries get evicted.
        self._op_log: list[dict] = []
        self._op_log_lock = threading.Lock()
        self._http: ThreadingHTTPServer | None = None
        self._http_thread: threading.Thread | None = None
        self._poll_thread: threading.Thread | None = None
        self._stopping = False

    @classmethod
    def start(cls, port: int, state_provider: Callable[[], dict] | None,
              host: str = '127.0.0.1',
              op_hooks: OperatorHooks | None = None) -> 'DashboardServer':
        srv = cls(host=host, port=port, state_provider=state_provider,
                  op_hooks=op_hooks)
        srv._start()
        return srv

    def record_op_line(self, text: str, kind: str = 'operator') -> None:
        """Stash a one-line operator echo for the next snapshot's chat tail."""
        entry = {'ts': time.time(), 'kind': kind, 'text': text}
        with self._op_log_lock:
            self._op_log.append(entry)
            if len(self._op_log) > 50:
                self._op_log = self._op_log[-30:]

    def op_log_snapshot(self) -> list[dict]:
        with self._op_log_lock:
            return list(self._op_log)

    def _start(self) -> None:
        self._http = ThreadingHTTPServer((self.host, self.port), _Handler)
        # Tag the underlying socket as fast-close so a restart of the bot
        # doesn't trip on TIME_WAIT.
        self._http.daemon_threads = True
        self._http.dashboard = self  # type: ignore[attr-defined]
        actual_port = self._http.server_address[1]
        self.port = actual_port
        log.info('Dashboard listening on http://%s:%d/', self.host, actual_port)

        self._http_thread = threading.Thread(
            target=self._http.serve_forever,
            name='dashboard-http',
            daemon=True,
        )
        self._http_thread.start()

        if self._state_provider is not None:
            self._poll_thread = threading.Thread(
                target=self._poll_loop,
                name='dashboard-poll',
                daemon=True,
            )
            self._poll_thread.start()

    def _poll_loop(self) -> None:
        while not self._stopping:
            try:
                snap = self._state_provider() if self._state_provider else None
                if snap is not None:
                    self._inject_op_log(snap)
                    self.broadcaster.publish(snap)
            except Exception:
                log.exception('state poll failed')
            time.sleep(_POLL_INTERVAL)

    def _inject_op_log(self, snap: dict) -> None:
        """Merge operator echo entries into the snapshot's chat tail.

        The poller drives both branches (interactive + MCP) so we keep
        the bot side completely unaware of operator-originated chat.
        """
        op_log = self.op_log_snapshot()
        if not op_log:
            return
        chat = list(snap.get('chat_recent') or [])
        chat.extend(op_log)
        chat.sort(key=lambda e: e.get('ts', 0))
        snap['chat_recent'] = chat[-40:]

    def broadcast(self, state: dict) -> None:
        """Push an externally-built snapshot (cheap, non-blocking)."""
        self.broadcaster.publish(state)

    def stop(self, timeout: float = 1.0) -> None:
        self._stopping = True
        if self._http is not None:
            try:
                self._http.shutdown()
            except Exception:
                pass
            try:
                self._http.server_close()
            except Exception:
                pass
        if self._http_thread is not None:
            self._http_thread.join(timeout=timeout)
        if self._poll_thread is not None:
            self._poll_thread.join(timeout=timeout)
        log.info('Dashboard stopped')


__all__ = [
    'AGGRO_RANGES',
    'DashboardServer',
    'OperatorHooks',
    'StateBroadcaster',
    'build_snapshot',
]
