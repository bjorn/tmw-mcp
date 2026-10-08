"""Transport-independent tool command dispatch.

``execute_tool_command`` runs one named command against a GameClient on
the caller's thread and returns a human-readable result string. It is
shared by the MCP server (which feeds it through ``send_command`` /
``drain_command_queue``) and by in-process consumers like tmw-npc that
own their own game loop.

``notify`` is an optional callable for progress events produced while a
command runs (for example walk-arrival callbacks). The MCP server passes
``push_notification``; standalone consumers can pass their own sink or
leave the default, which just logs.
"""

import logging

from .bot import is_safe_message
from .game import GameClient
from .items import item_name
from .maps import load_collision
from .monsters import being_display_name

log = logging.getLogger(__name__)


def _default_notify(content: str) -> None:
    log.info('notify: %s', content)


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
        lines.append('  [WARNING: NPC dialog lock active but no dialog received, send close to unlock]')
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


def format_inventory(client: GameClient) -> str:
    """Format the inventory as a string."""
    lines = []
    for idx in sorted(client.inventory.keys()):
        item = client.inventory[idx]
        equipped = ' [EQUIPPED]' if item.equipped else ''
        lines.append(f'  [{idx}] {item_name(item.name_id)} x{item.amount}{equipped}')
    return '\n'.join(lines) if lines else '(empty)'


def execute_tool_command(client: GameClient, cmd: str, kw: dict,
                         notify=_default_notify) -> str:
    """Execute a single tool command on the game thread. Returns result string."""
    if cmd == 'state':
        return format_game_state(client)

    elif cmd == 'inventory':
        return format_inventory(client)

    elif cmd == 'say':
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
                notify(f'[Walk] Arrived at ({x},{y})')
            else:
                notify(f'[Walk] Failed to reach ({x},{y})')
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
        client.emote(kw['emote_id'])
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
        # Poll for response: must process packets ourselves since we're on the game thread
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
# Tool catalog
# ---------------------------------------------------------------------------
# Transport-independent description of every command accepted by
# execute_tool_command. ``name`` is what a model sees, ``command`` is the
# dispatch key, ``parameters`` is a JSON Schema for the keyword arguments.
# The MCP server exposes this same surface through FastMCP; in-process
# consumers (e.g. tmw-npc) can translate the specs to OpenAI tool format
# directly.

def _spec(name: str, description: str,
          properties: dict | None = None,
          required: list | None = None,
          command: str | None = None) -> dict:
    params: dict = {'type': 'object', 'properties': properties or {}}
    if required:
        params['required'] = required
    return {'name': name, 'command': command or name,
            'description': description, 'parameters': params}


_str = lambda d: {'type': 'string', 'description': d}
_int = lambda d: {'type': 'integer', 'description': d}
_bool = lambda d: {'type': 'boolean', 'description': d}

TOOL_SPECS: list[dict] = [
    _spec('state', 'Get current game state: character info, position, '
          'HP/SP, nearby beings, floor items.'),
    _spec('inventory', 'List inventory items.'),
    _spec('online_list', 'Get list of all players currently online.'),
    _spec('say', 'Send a public chat message in-game.',
          {'message': _str('The line to speak')}, ['message']),
    _spec('whisper', 'Send a private message to a player.',
          {'target': _str('Player name'), 'message': _str('The line to speak')},
          ['target', 'message']),
    _spec('party_message', 'Send a message to party members.',
          {'message': _str('The line to speak')}, ['message']),
    _spec('party_leave', 'Leave the current party.'),
    _spec('walk', 'Walk to coordinates (x, y) using pathfinding.',
          {'x': _int('X coordinate'), 'y': _int('Y coordinate')},
          ['x', 'y']),
    _spec('face', 'Change facing direction (0=south, 2=west, 4=north, 6=east).',
          {'direction': _int('Direction')}, ['direction']),
    _spec('sit', 'Sit down.'),
    _spec('stand', 'Stand up.'),
    _spec('attack', 'Attack a being by ID (starts continuous attack).',
          {'target_id': _int('Being ID')}, ['target_id']),
    _spec('stopattack', 'Stop auto-attack and hunting.'),
    _spec('hunt', 'Continuously hunt monster(s) by name. Pass empty string '
          'to stop.', {'monster_name': _str('Monster name')}),
    _spec('respawn', 'Respawn after death.'),
    _spec('party_reply', 'Accept or reject a party invitation.',
          {'account_id': _int('Account ID'), 'accept': _bool('Accept?')},
          ['account_id']),
    _spec('attack_range', 'Override weapon attack range (1=melee).',
          {'range': _int('Attack range')}, ['range']),
    _spec('ferry_exit', 'Auto-exit the ferry after N bell rings.',
          {'bells': _int('Bell count')}, ['bells']),
    _spec('pickup', 'Pick up a floor item by ID.',
          {'item_id': _int('Item ID')}, ['item_id']),
    _spec('shop_buy', "Open a shop NPC's buy list.",
          {'npc_id': _int('NPC ID')}, ['npc_id']),
    _spec('shop_sell', "Open a shop NPC's sell list.",
          {'npc_id': _int('NPC ID')}, ['npc_id']),
    _spec('buy', 'Buy items from shop. Must open buy list first.',
          {'name_id': _int('Item name ID'), 'count': _int('Amount')},
          ['name_id', 'count']),
    _spec('sell', 'Sell items to shop. Must open sell list first.',
          {'index': _int('Inventory index'), 'count': _int('Amount')},
          ['index', 'count']),
    _spec('equip', 'Equip or unequip an item by inventory index.',
          {'index': _int('Inventory index')}, ['index']),
    _spec('use', 'Use an item by inventory index.',
          {'index': _int('Inventory index')}, ['index']),
    _spec('drop', 'Drop an item on the ground. Amount 0 = entire stack.',
          {'index': _int('Inventory index'), 'amount': _int('Amount')},
          ['index']),
    _spec('npc', 'Click on an NPC to start dialog.',
          {'npc_id': _int('NPC ID')}, ['npc_id']),
    _spec('next', 'Continue NPC dialog (click Next).', command='next'),
    _spec('close', 'Close NPC dialog.',
          {'npc_id': _int('NPC ID, 0 = current')}, command='close'),
    _spec('close_storage', 'Close the storage dialog.'),
    _spec('choose', 'Choose an NPC menu option (1-based index).',
          {'choice': _int('Option index')}, ['choice']),
    _spec('npc_input_str', 'Submit text input to an NPC dialog.',
          {'text': _str('Text')}, ['text']),
    _spec('npc_input_int', 'Submit integer input to an NPC dialog.',
          {'value': _int('Value')}, ['value']),
    _spec('trade_request', 'Request a player-to-player trade.',
          {'target': _str('Player name')}, ['target']),
    _spec('trade_accept', 'Accept an incoming trade request.'),
    _spec('trade_reject', 'Reject an incoming trade request.'),
    _spec('trade_add_item', 'Add an inventory item to the trade offer.',
          {'index': _int('Inventory index'), 'amount': _int('Amount, 0 = all')},
          ['index']),
    _spec('trade_add_zeny', 'Add zeny (GP) to the trade offer.',
          {'amount': _int('Amount')}, ['amount']),
    _spec('trade_lock', 'Lock your side of the trade.'),
    _spec('trade_commit', 'Commit the trade after both sides have locked.'),
    _spec('trade_cancel', 'Cancel the current trade.'),
    _spec('follow', 'Follow a player by name or ID. Empty string stops.',
          {'target': _str('Player name or ID')}),
    _spec('emote', 'Show an emote sprite above the character.',
          {'emote_id': _int('Emote ID from client-data emotes.xml')},
          ['emote_id']),
    _spec('stat', 'Increase a stat: str, agi, vit, int, dex, or luk.',
          {'stat_name': _str('Stat name')}, ['stat_name']),
    _spec('map', 'Show ASCII minimap around current position.',
          {'radius': _int('View radius in tiles')}),
]
