#!/usr/bin/env python3
"""
TMW Bot MCP Server - exposes game commands as MCP tools and pushes
game events as channel notifications to wake Claude from idle.

Usage (via .mcp.json):
    { "mcpServers": { "tmw": { "command": "tmw-mcp" } } }
"""

import asyncio
import json
import logging
import os
import queue
import sys
import threading
import time
from concurrent.futures import Future
from contextlib import asynccontextmanager

# Ensure all logging goes to stderr (stdout is reserved for MCP JSON-RPC)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    datefmt='%H:%M:%S',
    stream=sys.stderr,
)
logging.getLogger('net').setLevel(logging.WARNING)

# Package directory. Used only for resolving package-local resources;
# logs and state go to tmw_mcp.paths.state_dir(), not here. We do NOT
# chdir into it: doing so would override whatever cwd the MCP host
# launched us from (e.g. the user's project root), which is where
# `credentials.json` is normally found.
CLIENT_DIR = os.path.dirname(os.path.abspath(__file__))

from mcp.server.fastmcp import FastMCP, Context
from mcp.types import JSONRPCNotification, JSONRPCMessage
from mcp.shared.message import SessionMessage

from .game import GameClient
from .bot import (
    write_log, write_chat_log, write_npc_log, write_state,
    format_event, is_wakeup_event, run_auto_behaviors,
    execute_command, is_safe_message,
)
from .items import item_name
from .maps import load_collision
from .monsters import being_display_name
from .paths import bot_log_path, mcp_startup_log_path

log = logging.getLogger('mcp_server')


# Optional browser dashboard. Off by default; enable by passing
# --dashboard-port PORT or setting TMW_DASHBOARD_PORT in the environment.
DASHBOARD_PORT = 0
_dashboard_server = None  # set in _connect_game() if DASHBOARD_PORT > 0


# ---------------------------------------------------------------------------
# Shared state between MCP async context and game thread
# ---------------------------------------------------------------------------

class SharedState:
    def __init__(self):
        self.client: GameClient | None = None
        self.game_thread: threading.Thread | None = None
        self.running: bool = False
        self.command_queue: queue.Queue = queue.Queue()
        self.event_loop: asyncio.AbstractEventLoop | None = None
        self.notification_queue: asyncio.Queue | None = None
        self.session_ref = None  # ServerSession, captured on first tool call

state = SharedState()


# ---------------------------------------------------------------------------
# Channel notification bridge (game thread -> async MCP)
# ---------------------------------------------------------------------------

# Set to True by run_daemon_mode() to redirect notifications to stdout JSON-RPC
# instead of going through the in-process MCP session.
SHIM_MODE = False
_shim_stdout_lock = threading.Lock()


def push_notification(content: str):
    """Push a channel notification from the game thread.

    In normal (FastMCP) mode this queues onto the asyncio loop, which forwards
    it as a ``notifications/claude/channel`` MCP message. In ``--shim`` daemon
    mode it writes a line-delimited JSON-RPC notification to stdout so the
    parent shim process can forward it to the real MCP client.
    """
    if SHIM_MODE:
        try:
            msg = json.dumps({
                'jsonrpc': '2.0',
                'method': 'channel',
                'params': {'text': content},
            })
        except Exception as e:
            log.warning('Cannot encode notification: %s', e)
            return
        with _shim_stdout_lock:
            try:
                sys.stdout.write(msg + '\n')
                sys.stdout.flush()
                log.info('SHIM notify out: %s', content[:80])
            except Exception as e:
                log.warning('Failed to write shim notification: %s', e)
        return

    if state.event_loop and state.notification_queue:
        state.event_loop.call_soon_threadsafe(
            state.notification_queue.put_nowait, content
        )
        log.info('Queued notification: %s', content[:80])
    else:
        log.warning('Cannot push notification (no event loop/queue): %s', content[:80])


async def notification_forwarder():
    """Async task: drain notification queue and send MCP channel messages."""
    while True:
        content = await state.notification_queue.get()
        session = state.session_ref
        if session is None:
            continue  # No session yet; drop (or we could buffer, but startup events aren't critical)
        try:
            notif = JSONRPCNotification(
                method='notifications/claude/channel',
                params={'content': content},
                jsonrpc='2.0',
            )
            msg = SessionMessage(
                message=JSONRPCMessage.model_validate(notif.model_dump())
            )
            await session.send_message(msg)
            log.info('Sent notification OK: %s', content[:80])
        except Exception as e:
            log.warning('Failed to send channel notification: %s: %s', type(e).__name__, e)


# ---------------------------------------------------------------------------
# Command queue (MCP tools -> game thread)
# ---------------------------------------------------------------------------

def send_command(cmd_name: str, **kwargs) -> str:
    """Send a command to the game thread and wait for the result."""
    future = Future()
    state.command_queue.put((future, cmd_name, kwargs))
    try:
        return future.result(timeout=5.0)
    except Exception as e:
        return f'Error: {e}'


def drain_command_queue(client: GameClient):
    """Process all pending MCP tool commands on the game thread.

    ``future`` may be ``None`` when an operator action (from the
    dashboard) enqueues a fire-and-forget command; in that case we
    log instead of propagating result/exception to any waiter.
    """
    while not state.command_queue.empty():
        try:
            future, cmd_name, kwargs = state.command_queue.get_nowait()
        except queue.Empty:
            break
        try:
            result = _execute_tool_command(client, cmd_name, kwargs)
            if future is not None:
                future.set_result(result)
            else:
                log.info('Operator %s: %s', cmd_name, result)
        except Exception as e:
            if future is not None:
                future.set_exception(e)
            else:
                log.exception('Operator %s failed', cmd_name)


def _execute_tool_command(client: GameClient, cmd: str, kw: dict) -> str:
    """Execute a single tool command on the game thread. Returns result string."""
    if cmd == 'say':
        msg = kw['message']
        if not is_safe_message(msg):
            return 'BLOCKED: message contained credentials!'
        client.say(msg)
        return f'Said: {msg}'

    elif cmd == 'whisper':
        if not is_safe_message(kw['message']):
            return 'BLOCKED: message contained credentials!'
        client.whisper(kw['target'], kw['message'])
        return f'Whispered to {kw["target"]}: {kw["message"]}'

    elif cmd == 'party_message':
        msg = kw['message']
        if not is_safe_message(msg):
            return 'BLOCKED: message contained credentials!'
        client.party_message(msg)
        return f'Party: {msg}'

    elif cmd == 'party_leave':
        client.party_leave()
        return 'Left party'

    elif cmd == 'walk':
        def on_walk_done(success, x, y):
            if success:
                push_notification(f'[Walk] Arrived at ({x},{y})')
            else:
                push_notification(f'[Walk] Failed to reach ({x},{y})')
        client.walk_path(kw['x'], kw['y'], callback=on_walk_done)
        return f'Pathfinding walk to ({kw["x"]},{kw["y"]})'

    elif cmd == 'attack':
        target_id = kw['target_id']
        client.attack(target_id, continuous=True)
        client._auto_attack_target = target_id
        return f'Attacking #{target_id} (continuous)'

    elif cmd == 'stopattack':
        client._auto_attack_target = 0
        client._hunt_type = ''
        client._hunt_home = None
        return 'Stopped auto-attack and hunting'

    elif cmd == 'hunt':
        name = kw.get('monster_name', '')
        client._hunt_type = name
        if name:
            client._hunt_home = (client.player.x, client.player.y)
            return f'Hunting: {name} (home: {client.player.x},{client.player.y})'
        else:
            client._hunt_home = None
            return 'Stopped hunting'

    elif cmd == 'pickup':
        item_id = kw['item_id']
        client.queue_pickup(item_id)
        return f'Picking up #{item_id}'

    elif cmd == 'npc':
        client.click_npc(kw['npc_id'])
        return f'Talking to NPC #{kw["npc_id"]}'

    elif cmd == 'next':
        client.npc_next_response()
        return 'NPC: next'

    elif cmd == 'close':
        from .packets import build_npc_close
        npc_id = kw.get('npc_id') or client.npc_id
        client.map_conn.send_packet(build_npc_close(npc_id))
        client.npc_dialog_open = False
        client.npc_waiting_close = False
        client.npc_waiting_next = False
        client.npc_waiting_choice = False
        client.npc_waiting_input = ''
        client.npc_dialog.clear()
        return f'NPC: close #{npc_id}'

    elif cmd == 'close_storage':
        client.close_storage()
        return 'Sent CMSG_CLOSE_STORAGE'

    elif cmd == 'choose':
        client.npc_choose(kw['choice'])
        return f'NPC: chose {kw["choice"]}'

    elif cmd == 'npc_input_str':
        client.npc_input_str(kw['text'])
        client.npc_waiting_input = ''
        return f'NPC: input "{kw["text"]}"'

    elif cmd == 'npc_input_int':
        client.npc_input_int(kw['value'])
        client.npc_waiting_input = ''
        return f'NPC: input {kw["value"]}'

    elif cmd == 'trade_request':
        from .packets import build_trade_request
        target = kw['target']
        being = None
        for b in client.beings.values():
            if b.name and b.name.lower() == target.lower():
                being = b
                break
        if not being:
            return f'Cannot find player: {target}'
        client.map_conn.send_packet(build_trade_request(being.block_id))
        return f'Trade requested with {being.name}'

    elif cmd == 'trade_accept':
        from .packets import build_trade_response
        client.map_conn.send_packet(build_trade_response(True))
        return 'Trade accepted'

    elif cmd == 'trade_reject':
        from .packets import build_trade_response
        client.map_conn.send_packet(build_trade_response(False))
        return 'Trade rejected'

    elif cmd == 'trade_add_item':
        from .packets import build_trade_add
        idx = kw['index']
        item = client.inventory.get(idx)
        if not item:
            return f'No item at index {idx}'
        amount = kw.get('amount', 0) or item.amount
        # TMWA ioff2: inventory index + 2 for items
        client.map_conn.send_packet(build_trade_add(idx + 2, amount))
        return f'Added {amount}x {item_name(item.name_id)} to trade'

    elif cmd == 'trade_add_zeny':
        from .packets import build_trade_add
        amount = kw['amount']
        client.map_conn.send_packet(build_trade_add(0, amount))
        return f'Added {amount} GP to trade'

    elif cmd == 'trade_lock':
        from .packets import build_trade_lock
        client.map_conn.send_packet(build_trade_lock())
        return 'Trade locked (ready)'

    elif cmd == 'trade_commit':
        from .packets import build_trade_commit
        client.map_conn.send_packet(build_trade_commit())
        return 'Trade committed'

    elif cmd == 'trade_cancel':
        from .packets import build_trade_cancel
        client.map_conn.send_packet(build_trade_cancel())
        return 'Trade cancelled'

    elif cmd == 'sit':
        client.sit()
        return 'Sitting down'

    elif cmd == 'stand':
        client.stand()
        return 'Standing up'

    elif cmd == 'follow':
        from .bot import start_follow, stop_follow
        target = kw.get('target', '')
        if not target:
            stop_follow(client)
            return 'Stopped following'
        return start_follow(client, target)

    elif cmd == 'shop_buy':
        client.shop_buy(kw['npc_id'])
        return f'Requesting buy list from shop #{kw["npc_id"]}'

    elif cmd == 'shop_sell':
        client.shop_sell(kw['npc_id'])
        return f'Requesting sell list from shop #{kw["npc_id"]}'

    elif cmd == 'buy':
        items = [(kw['count'], kw['name_id'])]
        client.buy_items(items)
        return f'Buying {kw["count"]}x item#{kw["name_id"]}'

    elif cmd == 'sell':
        items = [(kw['index'], kw['count'])]
        client.sell_items(items)
        return f'Selling {kw["count"]}x from slot {kw["index"]}'

    elif cmd == 'equip':
        from .packets import build_equip_item, build_unequip_item
        index = kw['index']
        item = client.inventory.get(index)
        if item and item.equipped:
            client.map_conn.send_packet(build_unequip_item(index))
            return f'Unequipping item at index {index}'
        else:
            client.map_conn.send_packet(build_equip_item(index))
            return f'Equipping item at index {index}'

    elif cmd == 'use':
        import struct
        pkt = struct.pack('<HHI', 0x00a7, kw['index'], 0)
        client.map_conn.send_packet(pkt)
        return f'Using item at index {kw["index"]}'

    elif cmd == 'emote':
        import struct
        pkt = struct.pack('<HB', 0x00bf, kw['emote_id'])
        client.map_conn.send_packet(pkt)
        return f'Emote {kw["emote_id"]}'

    elif cmd == 'stat':
        from .packets import build_stat_increase
        stat_map = {
            'str': 0x000d, 'agi': 0x000e, 'vit': 0x000f,
            'int': 0x0010, 'dex': 0x0011, 'luk': 0x0012,
        }
        sn = kw['stat_name'].lower().strip()
        if sn in stat_map:
            client.map_conn.send_packet(build_stat_increase(stat_map[sn]))
            return f'Increasing {sn.upper()}'
        return f'Unknown stat: {sn}. Use: str, agi, vit, int, dex, luk'

    elif cmd == 'face':
        client.face(kw['direction'])
        return f'Facing direction {kw["direction"]}'

    elif cmd == 'respawn':
        client.respawn()
        return 'Respawning'

    elif cmd == 'party_reply':
        client.party_reply(kw['account_id'], kw['accept'])
        action = 'Accepted' if kw['accept'] else 'Rejected'
        return f'{action} party invite from #{kw["account_id"]}'

    elif cmd == 'ferry_exit':
        client._ferry_exit_at_bell = kw['bells']
        return f'Will auto-exit ferry after {kw["bells"]} bell(s)'

    elif cmd == 'drop':
        from .packets import build_drop_item
        index = kw['index']
        amount = kw.get('amount', 0)
        item = client.inventory.get(index)
        if not item:
            return f'No item at index {index}'
        drop_amount = amount if amount > 0 else item.amount
        client.map_conn.send_packet(build_drop_item(index, drop_amount))
        name = item_name(item.name_id)
        return f'Dropping {drop_amount}x {name} from slot [{index}]'

    elif cmd == 'online_list':
        client.online_list = []
        client.request_online_list()
        # Poll for response — must process packets ourselves since we're on the game thread
        import time
        for _ in range(50):
            client.process_packets(timeout=0.1)
            if client.online_list:
                break
        if not client.online_list:
            return 'No response from server (timeout)'
        lines = []
        for p in client.online_list:
            gm = ' [GM]' if p.gm_level else ''
            lines.append(f'  {p.name} (lv{p.level}){gm}')
        return f'{len(client.online_list)} players online:\n' + '\n'.join(lines)

    elif cmd == 'attack_range':
        client._attack_range = kw['range']
        return f'Attack range set to {kw["range"]}'

    elif cmd == 'map':
        cmap = load_collision(client.player.map_name)
        if cmap:
            view = cmap.render_around(
                client.player.x, client.player.y, kw.get('radius', 10),
                beings=client.beings, items=client.floor_items)
            return f'Map around ({client.player.x},{client.player.y}):\n{view}'
        return f'No collision data for {client.player.map_name}'

    return f'Unknown command: {cmd}'


# ---------------------------------------------------------------------------
# Game loop (runs in background thread)
# ---------------------------------------------------------------------------

def game_loop():
    """Synchronous game loop running in a dedicated thread."""
    client = state.client
    tick_count = 0
    death_notified = False

    # Request names for visible beings
    for b_id in list(client.beings.keys()):
        client.request_name(b_id)

    push_notification(
        f'Game connected! {client.player.char_name} at '
        f'{client.player.map_name} ({client.player.x},{client.player.y})'
    )

    last_exp = [0]  # mutable for closure; tracks EXP for kill notifications
    last_beings = {}  # block_id -> being data, for kill name lookups
    nearby_kill_buffer: dict[str, int] = {}  # name -> count, flushed once/min
    nearby_kill_flush_at = time.time() + 60.0

    while state.running:
        try:
            events = client.process_packets(timeout=0.2)
        except Exception as e:
            # When _quit_rpc clears state.running and closes the socket,
            # any in-flight process_packets() call on the game thread races
            # the close and surfaces here as EBADF (or similar). That's
            # planned shutdown, not a real disconnect, so don't alarm the
            # user with a "Game connection lost" notification.
            if not state.running:
                log.info('Game socket closed during planned shutdown: %s', e)
                break
            log.error('Game connection error: %s', e)
            push_notification(f'[ERROR] Game connection lost: {e}')
            state.running = False
            break

        for event in events:
          try:
            etype, data = event[0], event[1]

            # Log to file
            msg = format_event(client, etype, data)
            if msg:
                write_log(msg)

            # Persistent chat/NPC logs
            if etype == 'chat':
                write_chat_log(data.message)
            elif etype == 'party_chat':
                write_chat_log(msg or f'[party] {data.message}')
            elif etype == 'whisper':
                write_chat_log(f'[whisper from {data.sender}] {data.message}')
            elif etype == 'gm_chat':
                write_chat_log(f'[GM] {data.message}')
            elif etype == 'npc_message':
                npc_name = client.beings.get(data.npc_id, None)
                npc_name = npc_name.name if npc_name else f'NPC#{data.npc_id}'
                write_npc_log(npc_name, data.message)
            elif etype == 'map_change':
                death_notified = False
                # Warp notification is sent by is_wakeup_event below
                if getattr(client, '_board_target', 0):
                    client._board_target = 0
                    write_log('[Boarded! Stopped retry.]')

            # Auto-exit ferry on bell
            if etype == 'being_effect' and data.effect_type == 402:
                ferry_exit = getattr(client, '_ferry_exit_at_bell', 0)
                if ferry_exit > 0:
                    client._ferry_exit_at_bell = ferry_exit - 1
                    if ferry_exit == 1:
                        client.walk_to(39, 29)  # Ferry exit portal
                        msg = '[Ferry] Ship bell! AUTO-EXITING — this is our stop!'
                    else:
                        msg = f'[Ferry] Ship bell! Staying on — {ferry_exit - 1} more stop(s) to go.'

            # Notify on item pickup
            if etype == 'inventory_add' and data.pickup_fail == 0:
                name = item_name(data.name_id)
                push_notification(f'[Pickup] Got {name} x{data.amount}')

            # Snapshot all currently visible beings for kill tracking
            # Must happen before being_remove check since game.py already popped the being
            for bid, b in client.beings.items():
                if b.max_hp > 0:
                    last_beings[bid] = b

            # Track kills: when a monster is removed due to death
            if etype == 'being_remove' and data.block_id != client.account_id:
                killed = last_beings.pop(data.block_id, None)
                if data.reason == 1 and killed is not None:
                    xp_now = client.player.base_exp
                    xp_gained = max(0, xp_now - last_exp[0]) if last_exp[0] > 0 else 0
                    last_exp[0] = xp_now
                    xp_str = f' (+{xp_gained} EXP)' if xp_gained > 0 else ''
                    kill_name = being_display_name(killed) or f'#{data.block_id}'
                    if xp_gained > 0:
                        push_notification(f'[Kill] {kill_name}{xp_str}')
                    else:
                        nearby_kill_buffer[kill_name] = nearby_kill_buffer.get(kill_name, 0) + 1

            # Push channel notification for interesting events.
            # Skip our own outgoing chat: the server echoes it back as 0x008d
            # with our account_id, so it would otherwise round-trip into our
            # own context. (Some echos arrive as 0x008e with block_id 0; check
            # for both.) The dashboard chat tail still shows the message
            # because that's fed off game.py's chat_log; only the upstream
            # channel notification is suppressed.
            is_own_chat = (
                etype == 'chat'
                and getattr(data, 'block_id', None) in (0, client.account_id)
            )
            if not is_own_chat and is_wakeup_event(client, etype, data):
                notif_msg = msg
                # Enrich combat notifications with HP info
                if etype == 'action' and data.damage > 0:
                    src = client.beings.get(data.src_id) or last_beings.get(data.src_id)
                    src_name = (being_display_name(src) if src else '') or f'#{data.src_id}'
                    crit = ' CRIT' if data.damage_type == 0x0a else ''
                    notif_msg = f'[Combat] {src_name} hit you for {data.damage}{crit} (HP: {client.player.hp}/{client.player.max_hp})'
                elif etype == 'being_remove' and data.reason == 1 and data.block_id == client.account_id:
                    if not death_notified:
                        notif_msg = '[Death] You died!'
                        death_notified = True
                        # Drop follow state: we can't chase anyone while dead.
                        if client._follow_target_name:
                            from .bot import stop_follow
                            stop_follow(client)
                    else:
                        notif_msg = None
                if notif_msg:
                    push_notification(notif_msg)
          except Exception as e:
            log.error('Event handler error: %s', e, exc_info=True)
            write_log(f'[ERROR] Event handler: {e}')

        # Flush buffered nearby-kill summary once per minute
        now = time.time()
        if now >= nearby_kill_flush_at:
            if nearby_kill_buffer:
                parts = ', '.join(
                    f'{n} x{c}' for n, c in sorted(
                        nearby_kill_buffer.items(), key=lambda kv: -kv[1]
                    )
                )
                total = sum(nearby_kill_buffer.values())
                push_notification(
                    f'[Nearby Kills] {total} in last minute: {parts}'
                )
                nearby_kill_buffer.clear()
            nearby_kill_flush_at = now + 60.0

        # Automated behaviors
        run_auto_behaviors(client, tick_count)

        # Advance pathfinding walk
        client._advance_path()

        # Process MCP tool commands
        drain_command_queue(client)

        # Keepalive
        client.send_ping()

        # Write state file periodically (still useful for debugging)
        tick_count += 1
        if tick_count % 5 == 0:
            write_state(client)

        # Request names for unnamed beings
        if tick_count % 25 == 0:
            for b in client.nearby_beings():
                if not b.name:
                    client.request_name(b.block_id)

    log.info('Game loop ended')


# ---------------------------------------------------------------------------
# State formatting (for the `state` tool)
# ---------------------------------------------------------------------------

def format_game_state(client: GameClient) -> str:
    """Format full game state as a string."""
    p = client.player
    lines = [
        f'character: {p.char_name}',
        f'map: {p.map_name}',
        f'position: {p.x},{p.y}',
        f'hp: {p.hp}/{p.max_hp}',
        f'sp: {p.sp}/{p.max_sp}',
        f'level: {p.base_level}/{p.job_level}',
        f'exp: {p.base_exp}/{p.next_base_exp}',
        f'job_exp: {p.job_exp}/{p.next_job_exp}',
        f'zeny: {p.zeny}',
        f'stats: STR:{p.str_} AGI:{p.agi} VIT:{p.vit} INT:{p.int_} DEX:{p.dex} LUK:{p.luk}',
        f'status_point: {p.status_point}',
        f'weight: {p.weight}/{p.max_weight}',
        '',
        'nearby_beings:',
    ]
    for b in client.nearby_beings(radius=30):
        name = being_display_name(b)
        hp_str = f' HP:{b.hp}/{b.max_hp}' if b.max_hp > 0 else ''
        lv_str = f' lv{b.level}' if b.level > 0 else ''
        lines.append(f'  [{b.block_id}] {name} at ({b.x},{b.y}){lv_str}{hp_str}')
    lines.append('')
    lines.append('floor_items:')
    for item in client.nearby_items(radius=15):
        lines.append(f'  [{item.block_id}] {item_name(item.name_id)} x{item.amount} at ({item.x},{item.y})')
    lines.append('')
    lines.append('npc_dialog:')
    if client.npc_dialog_open and not client.npc_dialog and not client.npc_waiting_next and not client.npc_waiting_close and not client.npc_waiting_choice:
        lines.append('  [WARNING: NPC dialog lock active but no dialog received — send close to unlock]')
    if client.npc_dialog:
        for msg in client.npc_dialog:
            lines.append(f'  {msg}')
    if client.npc_waiting_next:
        lines.append('  [waiting: next]')
    if client.npc_waiting_close:
        lines.append('  [waiting: close]')
    if client.npc_waiting_choice:
        for i, c in enumerate(client.npc_choices, 1):
            lines.append(f'  [{i}] {c}')
        lines.append('  [waiting: choose N]')
    if client.npc_waiting_input == 'str':
        lines.append('  [waiting: text input]')
    elif client.npc_waiting_input == 'int':
        lines.append('  [waiting: number input]')
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# MCP Server + Tools
# ---------------------------------------------------------------------------

def ensure_session(ctx):
    """Capture the ServerSession (if any) and lazily connect to the game.

    ``ctx`` is the FastMCP Context when called from a tool; in ``--shim``
    daemon mode it is ``None``, because notifications go out on stdout JSON-RPC
    rather than through a live MCP session.
    """
    if ctx is not None and state.session_ref is None:
        state.session_ref = ctx.session
    if state.client is None:
        _connect_game()


def _connect_game():
    """Connect to the game server (called lazily on first tool use)."""
    if state.client is not None:
        return  # Already connected

    from .credentials import load_credentials, DEFAULT_FILE

    # Look for credentials.json next to the working dir; load_credentials
    # itself promotes TMW_CREDENTIALS_FILE over whatever path we pass.
    cwd_file = os.path.abspath(DEFAULT_FILE)
    path = cwd_file if os.path.exists(cwd_file) else None
    creds = load_credentials(path)
    if not creds.get('username') or not creds.get('password'):
        raise RuntimeError(
            'No credentials. Set TMW_USERNAME and TMW_PASSWORD (and any '
            'other TMW_* env vars), set TMW_CREDENTIALS_FILE to a JSON '
            'path, or drop a credentials.json in the working directory. '
            'Run ``tmw-mcp-register`` to create an account.'
        )

    log.info('Logging in as %s...', creds['username'])

    with open(bot_log_path(), 'w') as f:
        f.write('')

    client = GameClient(creds['server'], creds['port'])
    if not client.full_login(creds['username'], creds['password'],
                             creds.get('char_name', ''),
                             world=creds.get('world', '')):
        raise RuntimeError('Game login failed!')

    state.client = client
    state.running = True

    log.info('Logged in as %s on %s', client.player.char_name, client.player.map_name)
    write_log(f'MCP server started. Logged in as {client.player.char_name}.')

    # Start game loop thread
    state.game_thread = threading.Thread(target=game_loop, name='game-loop', daemon=True)
    state.game_thread.start()

    # Start notification forwarder (FastMCP mode only). In ``--shim`` daemon
    # mode notifications are written to stdout as JSON-RPC, no asyncio task
    # needed.
    if not SHIM_MODE:
        state._forward_task = asyncio.create_task(notification_forwarder())

    # Optional browser dashboard.
    global _dashboard_server
    if DASHBOARD_PORT and _dashboard_server is None:
        try:
            from .dashboard import DashboardServer, OperatorHooks, build_snapshot

            # Operator hooks run on the dashboard HTTP thread. ``walk``
            # and ``attack`` enqueue a command on ``state.command_queue``
            # so the game thread processes them on its next tick: the
            # same path the `walk` and `attack` tools take. ``notify`` uses
            # the existing push_notification bridge (which already does
            # asyncio.call_soon_threadsafe back to the MCP event loop)
            # so [Operator] lines land in Claude's conversation just
            # like any other channel notification.

            def _op_walk(x: int, y: int) -> None:
                state.command_queue.put((None, 'walk', {'x': x, 'y': y}))

            def _op_attack(being_id: int) -> None:
                state.command_queue.put(
                    (None, 'attack', {'target_id': being_id})
                )

            def _op_say(text: str) -> None:
                state.command_queue.put((None, 'say', {'message': text}))

            def _op_known_ids():
                return list((client.beings or {}).keys())

            def _op_notify(text: str) -> None:
                # Also persist to chat_history.log so it survives daemon
                # restarts and is visible to any tail-based observer.
                # This is the primary delivery channel for [Operator]
                # input now that the dashboard no longer broadcasts it
                # publicly in-game.
                try:
                    write_chat_log(text)
                except Exception:
                    log.exception('write_chat_log for operator failed')
                push_notification(text)

            hooks = OperatorHooks(
                walk=_op_walk,
                attack=_op_attack,
                say=_op_say,
                notify=_op_notify,
                known_being_ids=_op_known_ids,
            )
            _dashboard_server = DashboardServer.start(
                port=DASHBOARD_PORT,
                state_provider=lambda: build_snapshot(client, item_name),
                op_hooks=hooks,
            )
            log.info('Dashboard available at http://127.0.0.1:%d/',
                     _dashboard_server.port)
        except Exception as e:
            log.error('Failed to start dashboard on port %d: %s',
                      DASHBOARD_PORT, e)


@asynccontextmanager
async def lifespan(server: FastMCP):
    """Lightweight lifespan — game login happens lazily on first tool call."""
    state.event_loop = asyncio.get_running_loop()
    state.notification_queue = asyncio.Queue()
    log.info('MCP server ready (game connects on first tool call)')

    try:
        yield state
    finally:
        state.running = False
        if hasattr(state, '_forward_task'):
            state._forward_task.cancel()
        if state.game_thread:
            state.game_thread.join(timeout=3.0)
        global _dashboard_server
        if _dashboard_server is not None:
            _dashboard_server.stop()
            _dashboard_server = None
        if state.client:
            state.client.disconnect()
        log.info('MCP server shut down')


mcp = FastMCP(
    "tmw",
    instructions=(
        "TMW game bot for The Mana World MMORPG. "
        "Use the `state` tool to see the game world, then use other tools to act. "
        "Channel notifications will alert you to chat messages, NPC dialogs, "
        "combat, and map changes."
    ),
    lifespan=lifespan,
)


# --- Game state tool ---

@mcp.tool(name="state")
def tmw_state(ctx: Context) -> str:
    """Get current game state: character info, position, HP/SP, nearby beings, floor items."""
    ensure_session(ctx)
    client = state.client
    if not client:
        return 'Game not connected'
    return format_game_state(client)


@mcp.tool(name="inventory")
def tmw_inventory(ctx: Context) -> str:
    """List inventory items."""
    ensure_session(ctx)
    client = state.client
    if not client:
        return 'Game not connected'
    lines = []
    for idx in sorted(client.inventory.keys()):
        item = client.inventory[idx]
        equipped = ' [EQUIPPED]' if item.equipped else ''
        lines.append(f'  [{idx}] {item_name(item.name_id)} x{item.amount}{equipped}')
    return '\n'.join(lines) if lines else '(empty)'


@mcp.tool(name="online")
def tmw_online(ctx: Context) -> str:
    """Get list of all players currently online."""
    ensure_session(ctx)
    return send_command('online_list')


# --- Chat tools ---

@mcp.tool(name="say")
def tmw_say(ctx: Context, message: str) -> str:
    """Send a public chat message in-game."""
    ensure_session(ctx)
    return send_command('say', message=message)


@mcp.tool(name="whisper")
def tmw_whisper(ctx: Context, target: str, message: str) -> str:
    """Send a private message to a player."""
    ensure_session(ctx)
    return send_command('whisper', target=target, message=message)


@mcp.tool(name="party_chat")
def tmw_party_chat(ctx: Context, message: str) -> str:
    """Send a message to party members."""
    ensure_session(ctx)
    return send_command('party_message', message=message)


@mcp.tool(name="party_leave")
def tmw_party_leave(ctx: Context) -> str:
    """Leave the current party."""
    ensure_session(ctx)
    return send_command('party_leave')


# --- Movement tools ---

@mcp.tool(name="walk")
def tmw_walk(ctx: Context, x: int, y: int) -> str:
    """Walk to coordinates (x, y) using A* pathfinding. Sends a channel notification on arrival or failure."""
    ensure_session(ctx)
    return send_command('walk', x=x, y=y)


@mcp.tool(name="face")
def tmw_face(ctx: Context, direction: int) -> str:
    """Change facing direction (0=south, 2=west, 4=north, 6=east)."""
    ensure_session(ctx)
    return send_command('face', direction=direction)


@mcp.tool(name="sit")
def tmw_sit(ctx: Context) -> str:
    """Sit down."""
    ensure_session(ctx)
    return send_command('sit')


@mcp.tool(name="stand")
def tmw_stand(ctx: Context) -> str:
    """Stand up."""
    ensure_session(ctx)
    return send_command('stand')


# --- Combat tools ---

@mcp.tool(name="attack")
def tmw_attack(ctx: Context, target_id: int) -> str:
    """Attack a being by ID (starts continuous attack)."""
    ensure_session(ctx)
    return send_command('attack', target_id=target_id)


@mcp.tool(name="stop_attack")
def tmw_stop_attack(ctx: Context) -> str:
    """Stop auto-attack and hunting."""
    ensure_session(ctx)
    return send_command('stopattack')


@mcp.tool(name="hunt")
def tmw_hunt(ctx: Context, monster_name: str) -> str:
    """Continuously hunt monster(s) by name. Comma-separated for multiple types. Pass empty string to stop."""
    ensure_session(ctx)
    return send_command('hunt', monster_name=monster_name)


@mcp.tool(name="respawn")
def tmw_respawn(ctx: Context) -> str:
    """Respawn after death."""
    ensure_session(ctx)
    return send_command('respawn')


@mcp.tool(name="party_reply")
def tmw_party_reply(ctx: Context, account_id: int, accept: bool = True) -> str:
    """Accept or reject a party invitation."""
    ensure_session(ctx)
    return send_command('party_reply', account_id=account_id, accept=accept)


@mcp.tool(name="attack_range")
def tmw_attack_range(ctx: Context, range: int = 1) -> str:
    """Override weapon attack range (normally auto-detected from server). 1=melee, 2=scythe/polearm."""
    ensure_session(ctx)
    return send_command('attack_range', range=range)


@mcp.tool(name="ferry_exit")
def tmw_ferry_exit(ctx: Context, bells: int = 1) -> str:
    """Auto-exit the ferry after N bell rings. E.g. bells=1 exits at next stop, bells=2 skips one stop then exits."""
    ensure_session(ctx)
    return send_command('ferry_exit', bells=bells)


# --- Item tools ---

@mcp.tool(name="pickup")
def tmw_pickup(ctx: Context, item_id: int) -> str:
    """Pick up a floor item by ID."""
    ensure_session(ctx)
    return send_command('pickup', item_id=item_id)


@mcp.tool(name="shop_buy")
def tmw_shop_buy(ctx: Context, npc_id: int) -> str:
    """Open a shop NPC's buy list. Use after clicking a shop NPC (0x00c4 event). The buy list will appear in game state."""
    ensure_session(ctx)
    return send_command('shop_buy', npc_id=npc_id)


@mcp.tool(name="shop_sell")
def tmw_shop_sell(ctx: Context, npc_id: int) -> str:
    """Open a shop NPC's sell list. Use after clicking a shop NPC (0x00c4 event)."""
    ensure_session(ctx)
    return send_command('shop_sell', npc_id=npc_id)


@mcp.tool(name="buy")
def tmw_buy(ctx: Context, name_id: int, count: int = 1) -> str:
    """Buy items from shop. Must open buy list first with tmw_shop_buy."""
    ensure_session(ctx)
    return send_command('buy', name_id=name_id, count=count)


@mcp.tool(name="sell")
def tmw_sell(ctx: Context, index: int, count: int = 1) -> str:
    """Sell items to shop. Must open sell list first with tmw_shop_sell."""
    ensure_session(ctx)
    return send_command('sell', index=index, count=count)


@mcp.tool(name="equip")
def tmw_equip(ctx: Context, index: int) -> str:
    """Equip an item by inventory index."""
    ensure_session(ctx)
    return send_command('equip', index=index)


@mcp.tool(name="use")
def tmw_use(ctx: Context, index: int) -> str:
    """Use an item by inventory index."""
    ensure_session(ctx)
    return send_command('use', index=index)


@mcp.tool(name="drop")
def tmw_drop(ctx: Context, index: int, amount: int = 0) -> str:
    """Drop an item on the ground. Amount 0 = drop entire stack."""
    ensure_session(ctx)
    return send_command('drop', index=index, amount=amount)


# --- NPC tools ---

@mcp.tool(name="npc")
def tmw_npc(ctx: Context, npc_id: int) -> str:
    """Click on an NPC to start dialog."""
    ensure_session(ctx)
    return send_command('npc', npc_id=npc_id)


@mcp.tool(name="npc_next")
def tmw_npc_next(ctx: Context) -> str:
    """Continue NPC dialog (click Next)."""
    ensure_session(ctx)
    return send_command('next')


@mcp.tool(name="npc_close")
def tmw_npc_close(ctx: Context, npc_id: int = 0) -> str:
    """Close NPC dialog. If npc_id is 0 (default), closes the NPC tracked by the client;
    pass an explicit id to close a specific NPC when the client state is stale (e.g., after
    clicking a storage NPC that never sent a dialog packet)."""
    ensure_session(ctx)
    return send_command('close', npc_id=npc_id)


@mcp.tool(name="close_storage")
def tmw_close_storage(ctx: Context) -> str:
    """Send CMSG_CLOSE_STORAGE (0x00f7) to the server. Required after clicking a storage
    NPC — without it, the server leaves sd->state.storage_open set and silently drops
    walks and item-use packets."""
    ensure_session(ctx)
    return send_command('close_storage')


@mcp.tool(name="npc_choose")
def tmw_npc_choose(ctx: Context, choice: int) -> str:
    """Choose an NPC menu option (1-based index)."""
    ensure_session(ctx)
    return send_command('choose', choice=choice)


@mcp.tool(name="npc_input_str")
def tmw_npc_input_str(ctx: Context, text: str) -> str:
    """Submit text input to an NPC dialog."""
    ensure_session(ctx)
    return send_command('npc_input_str', text=text)


@mcp.tool(name="npc_input_int")
def tmw_npc_input_int(ctx: Context, value: int) -> str:
    """Submit integer input to an NPC dialog."""
    ensure_session(ctx)
    return send_command('npc_input_int', value=value)


# --- Trade tools ---

@mcp.tool(name="trade_request")
def tmw_trade_request(ctx: Context, target: str) -> str:
    """Request a player-to-player trade with the given player name."""
    ensure_session(ctx)
    return send_command('trade_request', target=target)


@mcp.tool(name="trade_accept")
def tmw_trade_accept(ctx: Context) -> str:
    """Accept an incoming trade request."""
    ensure_session(ctx)
    return send_command('trade_accept')


@mcp.tool(name="trade_reject")
def tmw_trade_reject(ctx: Context) -> str:
    """Reject an incoming trade request."""
    ensure_session(ctx)
    return send_command('trade_reject')


@mcp.tool(name="trade_add_item")
def tmw_trade_add_item(ctx: Context, index: int, amount: int = 0) -> str:
    """Add an inventory item to the trade offer (amount=0 means entire stack)."""
    ensure_session(ctx)
    return send_command('trade_add_item', index=index, amount=amount)


@mcp.tool(name="trade_add_zeny")
def tmw_trade_add_zeny(ctx: Context, amount: int) -> str:
    """Add zeny (GP) to the trade offer."""
    ensure_session(ctx)
    return send_command('trade_add_zeny', amount=amount)


@mcp.tool(name="trade_lock")
def tmw_trade_lock(ctx: Context) -> str:
    """Lock your side of the trade (indicate readiness to commit)."""
    ensure_session(ctx)
    return send_command('trade_lock')


@mcp.tool(name="trade_commit")
def tmw_trade_commit(ctx: Context) -> str:
    """Commit the trade after both sides have locked."""
    ensure_session(ctx)
    return send_command('trade_commit')


@mcp.tool(name="trade_cancel")
def tmw_trade_cancel(ctx: Context) -> str:
    """Cancel the current trade."""
    ensure_session(ctx)
    return send_command('trade_cancel')


# --- Social tools ---

@mcp.tool(name="follow")
def tmw_follow(ctx: Context, target: str) -> str:
    """Follow a player by name or ID. Pass empty string to stop."""
    ensure_session(ctx)
    return send_command('follow', target=target)


@mcp.tool(name="emote")
def tmw_emote(ctx: Context, emote_id: int) -> str:
    """Send an emote."""
    ensure_session(ctx)
    return send_command('emote', emote_id=emote_id)


# --- Character tools ---

@mcp.tool(name="stat")
def tmw_stat(ctx: Context, stat_name: str) -> str:
    """Increase a stat: str, agi, vit, int, dex, or luk."""
    ensure_session(ctx)
    return send_command('stat', stat_name=stat_name)


@mcp.tool(name="map")
def tmw_map(ctx: Context, radius: int = 10) -> str:
    """Show ASCII minimap around current position."""
    ensure_session(ctx)
    return send_command('map', radius=radius)


# ---------------------------------------------------------------------------
# Shim (daemon) mode: line-delimited JSON-RPC over stdin/stdout
# ---------------------------------------------------------------------------
#
# When launched with ``--shim`` the process does not speak MCP directly.
# Instead a tiny JSON-RPC dialect lets a parent ``mcp_shim.py`` process
# forward tool calls and channel notifications to the real MCP client.
#
# Requests (parent -> daemon), one JSON object per line:
#   {"jsonrpc":"2.0","id":N,"method":"list_tools","params":{}}
#   {"jsonrpc":"2.0","id":N,"method":"call_tool",
#    "params":{"name":"say","arguments":{"message":"hi"}}}
#   {"jsonrpc":"2.0","id":N,"method":"quit","params":{}}
#
# Responses (daemon -> parent), one JSON object per line, same id:
#   {"jsonrpc":"2.0","id":N,"result":...}
#   {"jsonrpc":"2.0","id":N,"error":{"code":...,"message":"..."}}
#
# Notifications (daemon -> parent), no id field:
#   {"jsonrpc":"2.0","method":"channel","params":{"text":"[Chat] hi"}}


def _tool_to_dict(tool) -> dict:
    """Serialize a FastMCP Tool to the JSON shape the shim re-emits to MCP."""
    out = {
        'name': tool.name,
        'description': tool.description or '',
        'inputSchema': tool.parameters,
    }
    if tool.title:
        out['title'] = tool.title
    return out


def _list_tools_rpc() -> list[dict]:
    return [_tool_to_dict(t) for t in mcp._tool_manager.list_tools()]


async def _call_tool_rpc(name: str, arguments: dict) -> str:
    """Dispatch a tool call by name; return the tool's string result.

    Tools that take a ``ctx`` parameter receive ``None``; ``ensure_session``
    handles that case (it skips capturing a session and just lazy-connects).
    """
    tool = mcp._tool_manager.get_tool(name)
    if tool is None:
        raise ValueError(f'Unknown tool: {name}')
    result = await tool.run(arguments or {}, context=None, convert_result=False)
    # Tool functions return plain strings; force-stringify defensively.
    return result if isinstance(result, str) else str(result)


def _quit_rpc() -> str:
    """Send CMSG_QUIT to the map server, drain briefly, shut the game thread.

    The caller will exit the process after the response is written.
    """
    client = state.client
    state.running = False  # ask the game loop to stop
    if client is not None:
        try:
            client.quit_cleanly(drain_seconds=0.3)
        except Exception as e:
            log.warning('quit_cleanly failed: %s', e)
    if state.game_thread is not None:
        state.game_thread.join(timeout=2.0)
    global _dashboard_server
    if _dashboard_server is not None:
        try:
            _dashboard_server.stop()
        except Exception:
            pass
        _dashboard_server = None
    return 'ok'


def _write_rpc(obj: dict) -> None:
    line = json.dumps(obj, separators=(',', ':'))
    with _shim_stdout_lock:
        sys.stdout.write(line + '\n')
        sys.stdout.flush()


async def _dispatch_request(req: dict) -> dict | None:
    """Process one JSON-RPC request. Returns the response dict, or None for
    notifications (requests without an id)."""
    req_id = req.get('id')
    method = req.get('method')
    params = req.get('params') or {}

    if method == 'list_tools':
        result = _list_tools_rpc()
    elif method == 'call_tool':
        name = params.get('name')
        arguments = params.get('arguments') or {}
        try:
            result = await _call_tool_rpc(name, arguments)
        except Exception as e:
            log.exception('call_tool %s failed', name)
            if req_id is None:
                return None
            return {
                'jsonrpc': '2.0',
                'id': req_id,
                'error': {'code': -32000, 'message': str(e)},
            }
    elif method == 'quit':
        result = _quit_rpc()
    elif method == 'ping':
        result = 'pong'
    else:
        if req_id is None:
            return None
        return {
            'jsonrpc': '2.0',
            'id': req_id,
            'error': {
                'code': -32601,
                'message': f'Method not found: {method}',
            },
        }

    if req_id is None:
        return None
    return {'jsonrpc': '2.0', 'id': req_id, 'result': result}


async def run_daemon_mode() -> int:
    """Run the JSON-RPC line protocol until stdin closes or 'quit' is handled.

    Returns the desired process exit code.
    """
    global SHIM_MODE
    SHIM_MODE = True

    log.info('Daemon mode: starting JSON-RPC loop on stdin/stdout')

    # Connect to the game eagerly so list_tools/call_tool work right away.
    # If login fails we still want to surface the error: write an immediate
    # notification, then keep accepting requests so the parent can decide.
    try:
        _connect_game()
    except Exception as e:
        log.exception('Game connect failed at daemon startup')
        push_notification(f'[Bot] login failed: {e}')

    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(loop=loop)
    proto = asyncio.StreamReaderProtocol(reader, loop=loop)
    await loop.connect_read_pipe(lambda: proto, sys.stdin)

    quit_requested = False
    while True:
        line = await reader.readline()
        if not line:
            log.info('Daemon stdin closed; exiting')
            break
        try:
            req = json.loads(line.decode('utf-8'))
        except Exception as e:
            log.warning('Bad JSON-RPC line: %s', e)
            continue

        method = req.get('method')
        resp = await _dispatch_request(req)
        if resp is not None:
            _write_rpc(resp)
        if method == 'quit':
            quit_requested = True
            break

    return 0 if quit_requested else 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Console script entry point.

    Parses CLI flags (--shim, --dashboard-port), then either runs the
    daemon-mode JSON-RPC dialect (for use behind mcp_shim) or speaks MCP
    over stdio directly. Honours TMW_DASHBOARD_PORT as a fallback.
    """
    global DASHBOARD_PORT

    import anyio
    import argparse
    from mcp.server.stdio import stdio_server

    # Also log to a file so we can diagnose startup failures
    # (stderr may not be visible when launched by Claude Code)
    _fh = logging.FileHandler(mcp_startup_log_path(), mode='w')
    _fh.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s',
                                       datefmt='%H:%M:%S'))
    logging.getLogger().addHandler(_fh)

    # Optional dashboard: --dashboard-port PORT or TMW_DASHBOARD_PORT env var.
    _ap = argparse.ArgumentParser(description='TMW MCP server')
    _ap.add_argument('--dashboard-port', type=int, default=0,
                     help='Start the browser dashboard on 127.0.0.1:PORT '
                          '(default: 0 = off; also honoured via '
                          'TMW_DASHBOARD_PORT env)')
    _ap.add_argument('--shim', action='store_true',
                     help='Run in daemon mode: speak line-delimited JSON-RPC '
                          'on stdin/stdout instead of MCP. Used by '
                          'mcp_shim.py for the self-restart architecture.')
    _cli_args, _ = _ap.parse_known_args()
    DASHBOARD_PORT = _cli_args.dashboard_port or int(
        os.environ.get('TMW_DASHBOARD_PORT', '0') or '0'
    )
    if DASHBOARD_PORT:
        log.info('Dashboard will start on port %d once the game connects',
                 DASHBOARD_PORT)

    log.info('MCP server process starting (pid=%d, cwd=%s, shim=%s)',
             os.getpid(), os.getcwd(), _cli_args.shim)

    if _cli_args.shim:
        try:
            rc = anyio.run(run_daemon_mode)
            sys.exit(rc or 0)
        except Exception:
            log.exception('Daemon mode crashed')
            raise
    else:

        async def run_stdio_with_channel():
            async with stdio_server() as (read_stream, write_stream):
                init_options = mcp._mcp_server.create_initialization_options(
                    experimental_capabilities={'claude/channel': {}},
                )
                await mcp._mcp_server.run(read_stream, write_stream, init_options)

        try:
            anyio.run(run_stdio_with_channel)
        except Exception:
            log.exception('MCP server crashed')
            raise


if __name__ == '__main__':
    main()
