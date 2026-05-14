#!/usr/bin/env python3
"""
TMW Bot - Non-interactive client that logs in, explores, and writes state to files.

Reads commands from a command file and writes output to a log file,
allowing Claude to control it via file I/O.

Usage:
    python bot.py [--credentials FILE] [--verbose]

Communication:
    - Reads commands from: cmd.txt (consumed after reading)
    - Writes output to: bot_log.txt (append)
    - Writes state to: bot_state.txt (overwritten each tick)
"""

import json
import logging
import os
import sys
import time

from .game import GameClient

_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(_DIR, 'bot_log.txt')
STATE_FILE = os.path.join(_DIR, 'bot_state.txt')
CMD_FILE = os.path.join(_DIR, 'cmd.txt')
CHAT_LOG = os.path.join(_DIR, 'chat_history.log')
NPC_LOG = os.path.join(_DIR, 'npc_history.log')

log = logging.getLogger('bot')


def write_log(msg: str, to_stderr: bool = False):
    """Append a message to the log file, optionally print to stderr."""
    timestamp = time.strftime('%H:%M:%S')
    line = f'[{timestamp}] {msg}'
    if to_stderr:
        print(line, file=sys.stderr)
    with open(LOG_FILE, 'a') as f:
        f.write(line + '\n')


def write_chat_log(msg: str):
    """Append to persistent chat history (survives restarts)."""
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
    with open(CHAT_LOG, 'a') as f:
        f.write(f'[{timestamp}] {msg}\n')


def write_npc_log(npc_name: str, msg: str):
    """Append to persistent NPC interaction history."""
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
    with open(NPC_LOG, 'a') as f:
        f.write(f'[{timestamp}] [{npc_name}] {msg}\n')


def write_state(client: GameClient):
    """Write current game state to file."""
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
    from .monsters import monster_name
    for b in client.nearby_beings(radius=30):
        name = b.name or monster_name(b.species)
        hp_str = f' HP:{b.hp}/{b.max_hp}' if b.max_hp > 0 else ''
        lines.append(f'  [{b.block_id}] {name} at ({b.x},{b.y}){hp_str}')
    lines.append('')
    lines.append('inventory:')
    for idx in sorted(client.inventory.keys()):
        item = client.inventory[idx]
        from .items import item_name
        lines.append(f'  [{idx}] {item_name(item.name_id)} x{item.amount}')
    lines.append('')
    lines.append('floor_items:')
    for item in client.nearby_items(radius=15):
        from .items import item_name
        lines.append(f'  [{item.block_id}] {item_name(item.name_id)} x{item.amount} at ({item.x},{item.y})')
    lines.append('')
    lines.append('npc_dialog:')
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
    lines.append('')
    lines.append('recent_chat:')
    for msg in client.chat_log[-10:]:
        lines.append(f'  {msg}')
    for sender, msg in client.whisper_log[-5:]:
        lines.append(f'  [whisper from {sender}] {msg}')
    # Minimap
    from .maps import load_collision
    cmap = load_collision(p.map_name)
    if cmap:
        lines.append('')
        lines.append('minimap (radius 8, @=you #=wall .=path M=monster N=NPC $=item):')
        view = cmap.render_around(p.x, p.y, 14,
                                  beings=client.beings, items=client.floor_items)
        for row in view.split('\n'):
            lines.append(f'  {row}')

    with open(STATE_FILE, 'w') as f:
        f.write('\n'.join(lines) + '\n')


def read_commands() -> list[str]:
    """Read and consume commands from cmd file."""
    if not os.path.exists(CMD_FILE):
        return []
    try:
        with open(CMD_FILE) as f:
            lines = f.read().strip().split('\n')
        os.remove(CMD_FILE)
        return [l.strip() for l in lines if l.strip()]
    except (OSError, FileNotFoundError):
        return []


def is_safe_message(msg: str) -> bool:
    """Check that a message doesn't accidentally leak the bot's password."""
    from .credentials import password_for_safety_check
    password = password_for_safety_check()
    if password and password in msg:
        return False
    return True


def execute_command(client: GameClient, cmd: str):
    """Execute a single command."""
    parts = cmd.split(maxsplit=1)
    action = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ''

    if action == 'say':
        if not is_safe_message(args):
            write_log('BLOCKED: message contained credentials!')
            return
        client.say(args)
        write_log(f'Said: {args}')

    elif action == 'whisper':
        wparts = args.split(maxsplit=1)
        if len(wparts) == 2:
            if not is_safe_message(wparts[1]):
                write_log('BLOCKED: whisper contained credentials!')
                return
            client.whisper(wparts[0], wparts[1])
            write_log(f'Whispered to {wparts[0]}: {wparts[1]}')

    elif action == 'walk':
        xy = args.split()
        if len(xy) == 2:
            x, y = int(xy[0]), int(xy[1])
            client.walk_to(x, y)
            write_log(f'Walking to ({x},{y})')

    elif action == 'sit':
        client.sit()
        write_log('Sitting down')

    elif action == 'stand':
        client.stand()
        write_log('Standing up')

    elif action == 'attack':
        target_id = int(args)
        client.attack(target_id, continuous=True)
        client._auto_attack_target = target_id
        write_log(f'Attacking #{target_id} (continuous)')

    elif action == 'stopattack':
        client._auto_attack_target = 0
        client._hunt_type = ''
        write_log('Stopped auto-attack and hunting')

    elif action == 'hunt':
        # Continuously hunt a monster type by name
        if args:
            client._hunt_type = args.strip()
            write_log(f'Hunting mode: {client._hunt_type}')
        else:
            client._hunt_type = ''
            write_log('Stopped hunting')

    elif action == 'pickup':
        item_id = int(args)
        client.pickup(item_id)
        write_log(f'Picking up #{item_id}')

    elif action == 'equip':
        from .packets import build_equip_item
        index = int(args)
        client.map_conn.send_packet(build_equip_item(index))
        write_log(f'Equipping item at index {index}')

    elif action == 'npc':
        npc_id = int(args)
        client.click_npc(npc_id)
        write_log(f'Talking to NPC #{npc_id}')

    elif action == 'next':
        client.npc_next_response()
        write_log('NPC: next')

    elif action == 'close':
        # Force send close packet regardless of client state
        from .packets import build_npc_close
        npc_id = int(args) if args else client.npc_id
        client.map_conn.send_packet(build_npc_close(npc_id))
        client.npc_waiting_close = False
        client.npc_waiting_next = False
        client.npc_waiting_choice = False
        client.npc_dialog.clear()
        write_log(f'NPC: close #{npc_id} (forced)')

    elif action == 'choose':
        client.npc_choose(int(args))
        write_log(f'NPC: chose {args}')

    elif action == 'face':
        client.face(int(args))
        write_log(f'Facing direction {args}')

    elif action == 'name':
        block_id = int(args)
        client.request_name(block_id)

    elif action == 'stat':
        # Increase a stat: stat STR, stat AGI, stat VIT, stat INT, stat DEX, stat LUK
        from .packets import build_stat_increase
        stat_map = {
            'str': 0x000d, 'agi': 0x000e, 'vit': 0x000f,
            'int': 0x0010, 'dex': 0x0011, 'luk': 0x0012,
        }
        stat_name = args.lower().strip()
        if stat_name in stat_map:
            client.map_conn.send_packet(build_stat_increase(stat_map[stat_name]))
            write_log(f'Increasing {stat_name.upper()}')
        else:
            write_log(f'Unknown stat: {args}. Use: str, agi, vit, int, dex, luk')

    elif action == 'map':
        from .maps import load_collision
        radius = int(args) if args else 10
        cmap = load_collision(client.player.map_name)
        if cmap:
            view = cmap.render_around(
                client.player.x, client.player.y, radius,
                beings=client.beings, items=client.floor_items)
            write_log(f'Map around ({client.player.x},{client.player.y}):\n{view}')
        else:
            write_log(f'No collision data for {client.player.map_name}')

    elif action == 'respawn':
        client.respawn()
        write_log('Respawning')

    elif action == 'follow':
        if args:
            result = start_follow(client, args)
            write_log(result)
        else:
            stop_follow(client)
            write_log('Stopped following')

    elif action == 'quit':
        write_log('Quit requested')
        client.disconnect()
        sys.exit(0)

    elif action == 'restart':
        write_log('Restarting bot...')
        client.disconnect()
        os.execv(sys.executable, [sys.executable] + sys.argv)

    elif action == 'emote':
        # Send emote: 0x00bf with emote type u8
        import struct as st
        emote_id = int(args) if args else 0
        client.map_conn.send_packet(st.pack('<HB', 0x00bf, emote_id))
        write_log(f'Emote {emote_id}')

    elif action == 'use':
        # Use an item by inventory index
        import struct
        index = int(args)
        pkt = struct.pack('<HHI', 0x00a7, index, 0)
        client.map_conn.send_packet(pkt)
        write_log(f'Using item at index {index}')

    elif action == 'board':
        # Keep trying to board a ferry/portal until it works
        npc_id = int(args) if args else 0
        client._board_target = npc_id
        write_log(f'Trying to board #{npc_id} (will retry until map changes)')

    elif action == 'watch':
        # Watch a specific being's movement for debugging
        if args:
            client._watch_id = int(args)
            write_log(f'Watching being #{args} for movement')
        else:
            client._watch_id = 0
            write_log('Stopped watching')

    else:
        write_log(f'Unknown command: {cmd}')


def format_event(client: GameClient, etype: str, data) -> str | None:
    """Format a game event as a human-readable string. Returns None for silent events."""
    if etype == 'chat':
        return f'[Chat] {data.message}'
    elif etype == 'party_chat':
        sender = client.party_members.get(data.account_id)
        if sender:
            return f'[Party] {sender} : {data.message}'
        return f'[Party] {data.message}'
    elif etype == 'whisper':
        return f'[Whisper from {data.sender}] {data.message}'
    elif etype == 'gm_chat':
        return f'[GM] {data.message}'
    elif etype == 'npc_message':
        return f'[NPC] {data.message}'
    elif etype == 'npc_next':
        return '[NPC waits - send "next"]'
    elif etype == 'npc_close':
        return '[NPC done - send "close"]'
    elif etype == 'npc_choice':
        return f'[NPC choices: {", ".join(f"{i+1}={c}" for i, c in enumerate(data.choices))}]'
    elif etype == 'npc_str_input_request':
        return '[NPC requests text input - use tmw_npc_input_str]'
    elif etype == 'npc_int_input_request':
        return '[NPC requests number input - use tmw_npc_input_int]'
    elif etype == 'trade_request':
        return f'[Trade] {data.char_name} wants to trade! Use tmw_trade_accept or tmw_trade_reject.'
    elif etype == 'trade_response':
        msgs = {0: 'too far', 1: 'character does not exist', 2: 'busy', 3: 'accepted', 4: 'rejected', 5: 'blocked by GM'}
        return f'[Trade] Response: {msgs.get(data.type, "unknown")}'
    elif etype == 'trade_item_add':
        if data.name_id == 0:
            return f'[Trade] Other party added {data.amount} GP'
        from .items import item_name
        return f'[Trade] Other party added {data.amount}x {item_name(data.name_id)}'
    elif etype == 'trade_ok':
        who = 'you' if data.who == 0 else 'other party'
        return f'[Trade] {who} locked. Use tmw_trade_commit after both sides lock.'
    elif etype == 'trade_cancel':
        return '[Trade] Trade cancelled.'
    elif etype == 'trade_complete':
        return f'[Trade] {"SUCCESS" if data.fail == 0 else "FAILED"}'
    elif etype == 'shop_choice':
        return f'[Shop NPC #{data.npc_id} - use tmw_shop_buy or tmw_shop_sell]'
    elif etype == 'shop_buy_list':
        from .items import item_name
        lines = ['[Shop Buy List]']
        for item in data.items:
            lines.append(f'  {item_name(item.name_id)} (#{item.name_id}) - {item.price} GP')
        return '\n'.join(lines)
    elif etype == 'shop_sell_list':
        lines = ['[Shop Sell List]']
        for item in data.items:
            lines.append(f'  slot {item["index"]} - {item["price"]} GP')
        return '\n'.join(lines)
    elif etype == 'shop_buy_result':
        return f'[Shop] Buy {"succeeded" if data.fail == 0 else "FAILED"}'
    elif etype == 'shop_sell_result':
        return f'[Shop] Sell {"succeeded" if data.fail == 0 else "FAILED"}'
    elif etype == 'map_change':
        return f'[Warped to {data.map_name} ({data.x},{data.y})]'
    elif etype == 'action' and data.damage > 0:
        if data.dst_id == client.account_id:
            return f'[Hit] took {data.damage} damage'
    elif etype == 'being_remove' and data.reason == 1:
        return f'[Died] #{data.block_id}'
    elif etype == 'being_effect' and data.effect_type == 402:
        return '[Ferry] Ship bell! Ferry has docked - exit now if this is your stop!'
    elif etype == 'party_invited':
        return f'[Party] Invited to party "{data.party_name}" by account #{data.account_id}. Use tmw_party_reply to accept or reject.'
    return None


# Events that should push a channel notification to wake Claude
WAKEUP_EVENTS = {
    'chat', 'whisper', 'gm_chat', 'party_chat',
    'npc_message', 'npc_next', 'npc_close', 'npc_choice',
    'npc_str_input_request', 'npc_int_input_request',
    'trade_request', 'trade_response', 'trade_item_add',
    'trade_ok', 'trade_cancel', 'trade_complete',
    'shop_choice', 'shop_buy_list', 'shop_sell_list',
    'shop_buy_result', 'shop_sell_result',
    'map_change', 'map_server_change',
}


def is_wakeup_event(client: GameClient, etype: str, data) -> bool:
    """Return True if this event should wake Claude via channel notification."""
    if etype in WAKEUP_EVENTS:
        return True
    if etype == 'action' and data.damage > 0 and data.dst_id == client.account_id:
        return True
    if etype == 'being_remove' and data.reason == 1 and data.block_id == client.account_id:
        return True
    if etype == 'being_effect' and data.effect_type == 402:
        return True
    if etype == 'party_invited':
        return True
    return False


# ---------------------------------------------------------------------------
# Follow mode (lives here so the legacy file-based bot.py and the MCP server
# share the exact same implementation).
# ---------------------------------------------------------------------------


def _resolve_follow_target(client: GameClient, name: str) -> int:
    """Return the closest visible block_id for the given player name, or 0.

    Identity continuity: if the previously-locked id is still visible AND
    its current name matches, prefer it over any other same-named player.
    Otherwise pick the spatially closest match.
    """
    locked = client._follow_target_id
    if locked and locked in client.beings:
        cur = client.beings[locked]
        if (cur.name or '').lower() == name.lower():
            return locked
    name_lc = name.lower()
    px, py = client.player.x, client.player.y
    best = 0
    best_dist = 1 << 30
    for bid, b in client.beings.items():
        if bid == client.account_id:
            continue
        if (b.name or '').lower() != name_lc:
            continue
        d = abs(b.x - px) + abs(b.y - py)
        if d < best_dist:
            best_dist = d
            best = bid
    return best


def start_follow(client: GameClient, name_or_id: str) -> str:
    """Begin following ``name_or_id``. Returns a one-line status string."""
    # Try numeric id first, then resolve a being's name from that id so the
    # follow state is name-keyed even if the operator gave us a block_id.
    resolved_name = ''
    try:
        bid = int(name_or_id)
        b = client.beings.get(bid)
        if b and b.name:
            resolved_name = b.name
    except ValueError:
        resolved_name = name_or_id.strip()
    if not resolved_name:
        return f'Cannot find player: {name_or_id}'

    client._follow_target_name = resolved_name
    client._follow_target_id = _resolve_follow_target(client, resolved_name)
    client._follow_last_seen_map = client.player.map_name
    client._follow_last_seen_at = time.time()
    # Stop hunting so the two auto-behaviours don't fight for the path.
    client._hunt_type = ''
    client._hunt_home = None
    if client._follow_target_id:
        b = client.beings[client._follow_target_id]
        client._follow_state = 'following'
        client._follow_last_seen_pos = (b.x, b.y)
        client._follow_target = client._follow_target_id
        return f'Following {b.name or resolved_name}'
    # Target not yet visible: stay armed and wait for them to show up.
    client._follow_state = 'waiting'
    client._follow_last_seen_pos = None
    client._follow_target = 0
    return f'Following {resolved_name} (waiting for them to appear)'


def stop_follow(client: GameClient) -> None:
    """Clear all follow state."""
    client._follow_target_name = ''
    client._follow_target_id = 0
    client._follow_state = 'idle'
    client._follow_last_seen_pos = None
    client._follow_last_seen_map = ''
    client._follow_last_seen_at = 0.0
    client._follow_target = 0


def _tick_follow(client: GameClient, tick_count: int) -> None:
    """One tick of follow logic. Called from run_auto_behaviors.

    States:
      * following: target visible on our map; chase to within 3 tiles.
      * warping: target stepped onto a warp tile, we've sent ourselves
        after them and are waiting for our own map change.
      * waiting: target out of sight (just out of view, or on another
        map). Re-resolve on each tick; time out after
        ``_follow_timeout`` seconds.

    Map changes are detected by comparing ``client.player.map_name`` with
    ``_follow_last_seen_map``: when they diverge, we reset spatial state
    and drop back to waiting on the new map until the target reappears.
    """
    name = client._follow_target_name
    state = client._follow_state
    now = time.time()

    # Did our own map change since the last tick? If so, drop any per-map
    # caches and start re-searching by name on the new map.
    cur_map = client.player.map_name
    if cur_map != client._follow_last_seen_map:
        client._follow_last_seen_map = cur_map
        client._follow_target_id = 0
        client._follow_last_seen_pos = None
        client._follow_target = 0
        # Treat the bot's own warp as fresh contact: refresh the seen-at
        # clock so the timeout doesn't fire mid-transition.
        client._follow_last_seen_at = now
        client._follow_state = 'waiting'
        state = 'waiting'

    # Try to resolve the target by name on every tick; this is the cheap
    # part of robustness across portal flickers.
    bid = _resolve_follow_target(client, name)
    if bid:
        target = client.beings[bid]
        client._follow_target_id = bid
        client._follow_target = bid
        client._follow_last_seen_pos = (target.x, target.y)
        client._follow_last_seen_at = now
        client._follow_state = 'following'
        # Only push a fresh walk every ~1.6s (8 ticks); the walk packet
        # is heavy and the server walks tile-by-tile anyway.
        if tick_count % 8 == 0:
            px, py = client.player.x, client.player.y
            dx = abs(target.x - px)
            dy = abs(target.y - py)
            if dx > 3 or dy > 3:
                tx = target.x + (1 if px > target.x else -1 if px < target.x else 0)
                ty = target.y + (1 if py > target.y else -1 if py < target.y else 0)
                client.walk_to(tx, ty)
        return

    # Target is not currently visible.
    client._follow_target_id = 0
    client._follow_target = 0
    last_pos = client._follow_last_seen_pos
    # If they vanished right next to a warp tile and we know where it
    # was, walk onto that tile so the server warps us behind them.
    if state == 'following' and last_pos is not None:
        # Use the map collision data we already cache in maps.py.
        try:
            from .maps import load_collision
            cmap = load_collision(cur_map)
        except Exception:
            cmap = None
        on_warp = bool(cmap and cmap.is_warp(last_pos[0], last_pos[1]))
        if on_warp:
            client._follow_state = 'warping'
            # Walk onto the warp tile. Server will move us to the
            # destination map; the map-change handler resets state.
            client.walk_to(last_pos[0], last_pos[1])
            return
        # Otherwise they just moved out of view; sit tight and poll.
        client._follow_state = 'waiting'
        return

    # Timeout handling: if the target stays missing for too long, give up.
    if now - client._follow_last_seen_at > client._follow_timeout:
        write_log(
            f'[Follow] target {name} not seen in '
            f'{int(client._follow_timeout)} seconds, giving up'
        )
        stop_follow(client)
        return
    # Stay in the current waiting/warping state.


def run_auto_behaviors(client: GameClient, tick_count: int):
    """Run automated behaviors: auto-attack, hunt, follow, board."""
    # Yield to pickup queue — don't walk/attack while picking up items
    if client._pickup_active or client._pickup_queue:
        return
    # Snap position so distance checks below use up-to-date coordinates
    client._snap_walk_position()
    # Auto-attack: chase target into melee range and keep attacking
    import time
    auto_target = client._auto_attack_target
    walk_arrival = client._walk_arrival
    attack_range = client._attack_range
    if auto_target and tick_count % 4 == 0:
        if auto_target in client.beings:
            target = client.beings[auto_target]
            px, py = client.player.x, client.player.y
            dx = abs(target.x - px)
            dy = abs(target.y - py)
            now = time.time()
            if dx > attack_range or dy > attack_range:
                # Walk toward target but stop at attack range
                sx = max(-1, min(1, target.x - px))
                sy = max(-1, min(1, target.y - py))
                dest_x = target.x - sx * (attack_range - 1)
                dest_y = target.y - sy * (attack_range - 1)
                client.walk_to(dest_x, dest_y)
            elif now >= walk_arrival:
                # In range — attack!
                client.attack(auto_target, continuous=True)
        else:
            client._auto_attack_target = 0
            write_log(f'[Auto-attack target #{auto_target} gone]')
            # Auto-pickup nearby items after kill (queue all in radius;
            # the pickup queue dedupes and processes sequentially).
            if client._hunt_type:
                for item in client.nearby_items(radius=5):
                    client.queue_pickup(item.block_id)

    # Hunt mode: find nearest monster of target type(s) and attack it
    hunt_type = client._hunt_type
    if hunt_type and not auto_target and tick_count % 4 == 0:
        # Set hunt home position on first tick
        if not client._hunt_home:
            client._hunt_home = (client.player.x, client.player.y)
        hx, hy = client._hunt_home
        hunt_names = [n.strip().lower() for n in hunt_type.split(',')]
        px, py = client.player.x, client.player.y
        best = None
        best_dist = 999
        hunt_leash = 20  # max distance from home to chase monsters
        for b in client.beings.values():
            if b.name.lower() in hunt_names and b.max_hp > 0:
                # Only chase monsters within leash range of home
                home_dist = abs(b.x - hx) + abs(b.y - hy)
                if home_dist > hunt_leash:
                    continue
                dist = abs(b.x - px) + abs(b.y - py)
                if dist < best_dist:
                    best = b
                    best_dist = dist
        if best:
            client._auto_attack_target = best.block_id
            # Walk toward, but stop at attack range. Otherwise a ranged
            # hunter would charge straight onto the target tile before the
            # auto-attack logic refines the path on the next tick.
            sx = max(-1, min(1, best.x - px))
            sy = max(-1, min(1, best.y - py))
            dest_x = best.x - sx * (attack_range - 1)
            dest_y = best.y - sy * (attack_range - 1)
            client.walk_to(dest_x, dest_y)
        else:
            # Pick up nearby items first (queue all in radius)
            picked = False
            if client.floor_items:
                for item in client.nearby_items(radius=5):
                    client.queue_pickup(item.block_id)
                    picked = True
            # Roam to find more monsters (every ~3 seconds = tick_count % 12)
            # Roam relative to HOME position, not current position
            # Avoid warp tiles to prevent accidentally leaving the map
            if not picked and tick_count % 12 == 0:
                import random
                from .maps import load_collision
                roam_radius = 8
                cmap = load_collision(client.player.map_name)
                for _ in range(10):  # retry up to 10 times to find a safe tile
                    rx = hx + random.randint(-roam_radius, roam_radius)
                    ry = hy + random.randint(-roam_radius, roam_radius)
                    if cmap and cmap.is_warp(rx, ry):
                        continue
                    client.walk_to(rx, ry)
                    break

    # Board: keep trying to click a dock NPC until we warp
    board_target = client._board_target
    if board_target and tick_count % 15 == 0:
        from .packets import build_npc_close, build_npc_click
        client.map_conn.send_packet(build_npc_close(board_target))
        client.map_conn.send_packet(build_npc_click(board_target))

    # Follow: stay within 3 tiles of target player, handling map transitions.
    # Runs every tick (~0.2s) so we can react to the target disappearing
    # near a warp tile within a single map step.
    if client._follow_target_name:
        _tick_follow(client, tick_count)


def main():
    import argparse
    parser = argparse.ArgumentParser(description='TMW Bot')
    parser.add_argument('--credentials', default='credentials.json')
    parser.add_argument('--verbose', '-v', action='store_true')
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level,
                        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
                        datefmt='%H:%M:%S')
    if not args.verbose:
        logging.getLogger('net').setLevel(logging.WARNING)

    # Clear log
    with open(LOG_FILE, 'w') as f:
        f.write('')

    # Load credentials (env vars override the file).
    from .credentials import load_credentials
    creds = load_credentials(args.credentials)
    if not creds.get('username') or not creds.get('password'):
        write_log('No credentials: set TMW_USERNAME/TMW_PASSWORD or '
                  'provide a credentials.json.', to_stderr=True)
        sys.exit(1)

    write_log(f'Logging in as {creds["username"]}...', to_stderr=True)

    client = GameClient(creds['server'], creds['port'])
    if not client.full_login(creds['username'], creds['password'],
                             creds.get('char_slot', 0),
                             world=creds.get('world', '')):
        write_log('Login failed!', to_stderr=True)
        sys.exit(1)

    write_log(f'Logged in as {client.player.char_name}!', to_stderr=True)
    write_log(f'Map: {client.player.map_name} ({client.player.x},{client.player.y})', to_stderr=True)

    # Request names for all visible beings
    for b_id in list(client.beings.keys()):
        client.request_name(b_id)

    # Main loop
    write_log('Bot running. Write commands to cmd.txt.', to_stderr=True)
    tick_count = 0
    try:
        while True:
            # Process incoming packets
            events = client.process_packets(timeout=0.2)
            for event in events:
                etype, data = event[0], event[1]
                msg = format_event(client, etype, data)
                if msg:
                    write_log(msg, to_stderr=True)

                # Persistent logging for chat/NPC
                if etype == 'chat':
                    write_chat_log(data.message)
                elif etype == 'whisper':
                    write_chat_log(f'[whisper from {data.sender}] {data.message}')
                elif etype == 'gm_chat':
                    write_chat_log(f'[GM] {data.message}')
                elif etype == 'npc_message':
                    npc_name = client.beings.get(data.npc_id, None)
                    npc_name = npc_name.name if npc_name else f'NPC#{data.npc_id}'
                    write_npc_log(npc_name, data.message)
                elif etype == 'map_change':
                    if getattr(client, '_board_target', 0):
                        client._board_target = 0
                        write_log('[Boarded! Stopped retry.]', to_stderr=True)

                # Watch mode
                watch_id = getattr(client, '_watch_id', 0)
                if watch_id and hasattr(data, 'block_id') and data.block_id == watch_id:
                    if etype == 'being_visible':
                        write_log(f'[WATCH] VISIBLE at ({data.x},{data.y})', to_stderr=True)
                    elif etype == 'being_move':
                        write_log(f'[WATCH] MOVE ({data.x0},{data.y0})->({data.x1},{data.y1})', to_stderr=True)
                    elif etype == 'stop':
                        write_log(f'[WATCH] STOP at ({data.x},{data.y})', to_stderr=True)
                    elif etype == 'being_remove':
                        write_log(f'[WATCH] REMOVE reason={data.reason}', to_stderr=True)

            # Automated behaviors
            run_auto_behaviors(client, tick_count)

            # Send keepalive
            client.send_ping()

            # Read commands (manual commands override AI)
            for cmd in read_commands():
                write_log(f'> {cmd}', to_stderr=True)
                try:
                    execute_command(client, cmd)
                except Exception as e:
                    write_log(f'Command error: {e}', to_stderr=True)

            # Write state every ~1 second
            tick_count += 1
            if tick_count % 5 == 0:
                write_state(client)

            # Request names for unnamed beings periodically
            if tick_count % 25 == 0:
                for b in client.nearby_beings():
                    if not b.name:
                        client.request_name(b.block_id)

    except KeyboardInterrupt:
        write_log('Interrupted')
    finally:
        write_log('Disconnecting...')
        client.disconnect()


if __name__ == '__main__':
    main()
