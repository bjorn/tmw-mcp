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

from game import GameClient

LOG_FILE = 'bot_log.txt'
STATE_FILE = 'bot_state.txt'
CMD_FILE = 'cmd.txt'

log = logging.getLogger('bot')


def write_log(msg: str):
    """Append a message to the log file and print it."""
    timestamp = time.strftime('%H:%M:%S')
    line = f'[{timestamp}] {msg}'
    print(line)
    with open(LOG_FILE, 'a') as f:
        f.write(line + '\n')


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
    for b in client.nearby_beings(radius=30):
        name = b.name or f'species:{b.species}'
        hp_str = f' HP:{b.hp}/{b.max_hp}' if b.max_hp > 0 else ''
        lines.append(f'  [{b.block_id}] {name} at ({b.x},{b.y}){hp_str}')
    lines.append('')
    lines.append('floor_items:')
    for item in client.nearby_items(radius=15):
        lines.append(f'  [{item.block_id}] item#{item.name_id} x{item.amount} at ({item.x},{item.y})')
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


def execute_command(client: GameClient, cmd: str):
    """Execute a single command."""
    parts = cmd.split(maxsplit=1)
    action = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ''

    if action == 'say':
        client.say(args)
        write_log(f'Said: {args}')

    elif action == 'whisper':
        wparts = args.split(maxsplit=1)
        if len(wparts) == 2:
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
        write_log('Stopped auto-attack')

    elif action == 'pickup':
        item_id = int(args)
        client.pickup(item_id)
        write_log(f'Picking up #{item_id}')

    elif action == 'equip':
        from packets import build_equip_item
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
        from packets import build_npc_close
        client.map_conn.send_packet(build_npc_close(client.npc_id))
        client.npc_waiting_close = False
        client.npc_waiting_next = False
        client.npc_waiting_choice = False
        client.npc_dialog.clear()
        write_log('NPC: close (forced)')

    elif action == 'choose':
        client.npc_choose(int(args))
        write_log(f'NPC: chose {args}')

    elif action == 'face':
        client.face(int(args))
        write_log(f'Facing direction {args}')

    elif action == 'name':
        block_id = int(args)
        client.request_name(block_id)

    elif action == 'respawn':
        client.respawn()
        write_log('Respawning')

    elif action == 'quit':
        write_log('Quit requested')
        client.disconnect()
        sys.exit(0)

    else:
        write_log(f'Unknown command: {cmd}')


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

    # Load credentials
    with open(args.credentials) as f:
        creds = json.load(f)

    write_log(f'Logging in as {creds["username"]}...')

    client = GameClient(creds['server'], creds['port'])
    if not client.full_login(creds['username'], creds['password'],
                             creds.get('char_slot', 0)):
        write_log('Login failed!')
        sys.exit(1)

    write_log(f'Logged in as {client.player.char_name}!')
    write_log(f'Map: {client.player.map_name} ({client.player.x},{client.player.y})')

    # Request names for all visible beings
    for b_id in list(client.beings.keys()):
        client.request_name(b_id)

    # Main loop
    write_log('Bot running. Write commands to cmd.txt.')
    tick_count = 0
    try:
        while True:
            # Process incoming packets
            events = client.process_packets(timeout=0.2)
            for event in events:
                etype = event[0]
                data = event[1]
                if etype == 'chat':
                    write_log(f'[Chat] {data.message}')
                elif etype == 'whisper':
                    write_log(f'[Whisper from {data.sender}] {data.message}')
                elif etype == 'gm_chat':
                    write_log(f'[GM] {data.message}')
                elif etype == 'npc_message':
                    write_log(f'[NPC] {data.message}')
                elif etype == 'npc_next':
                    write_log('[NPC waits - send "next"]')
                elif etype == 'npc_close':
                    write_log('[NPC done - send "close"]')
                elif etype == 'npc_choice':
                    write_log(f'[NPC choices: {", ".join(f"{i+1}={c}" for i, c in enumerate(data.choices))}]')
                elif etype == 'map_change':
                    write_log(f'[Warped to {data.map_name} ({data.x},{data.y})]')
                elif etype == 'action' and data.damage > 0:
                    if data.dst_id == client.account_id:
                        write_log(f'[Hit] took {data.damage} damage')
                elif etype == 'being_remove' and data.reason == 1:
                    write_log(f'[Died] #{data.block_id}')
                elif etype == 'name':
                    pass  # Silently stored

            # Re-send auto-attack if target still alive
            auto_target = getattr(client, '_auto_attack_target', 0)
            if auto_target and tick_count % 10 == 0:
                if auto_target in client.beings:
                    client.attack(auto_target, continuous=True)
                else:
                    client._auto_attack_target = 0
                    write_log(f'[Auto-attack target #{auto_target} gone]')

            # Send keepalive
            client.send_ping()

            # Read commands
            for cmd in read_commands():
                write_log(f'> {cmd}')
                try:
                    execute_command(client, cmd)
                except Exception as e:
                    write_log(f'Command error: {e}')

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
