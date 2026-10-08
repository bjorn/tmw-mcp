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

log = logging.getLogger(__name__)


def _default_notify(content: str) -> None:
    log.info('notify: %s', content)


def execute_tool_command(client: GameClient, cmd: str, kw: dict,
                         notify=_default_notify) -> str:
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
