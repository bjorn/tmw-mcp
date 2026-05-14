#!/usr/bin/env python3
"""
TMW CLI Client - Interactive text-based client for The Mana World.

Usage:
    python main.py [--server HOST] [--port PORT] [--user USER] [--password PASS] [--char SLOT]
"""

import argparse
import logging
import os
import sys
import time

from .bot import run_auto_behaviors
from .game import GameClient, Being, FloorItem
from .packets import (
    BeingVisible, BeingMove, BeingSpawn, BeingRemove,
    BeingAction, ChatMessage, WhisperMessage, GmChat,
    NpcMessage, NpcNext, NpcClose, NpcChoice,
    ChangeMap, ChangeMapServer, WalkResponse,
    SkillDamage, InventoryAdd, StatUpdate1,
)


def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
        datefmt='%H:%M:%S',
    )
    # Quiet down the packet-level debug unless really verbose
    if not verbose:
        logging.getLogger('net').setLevel(logging.WARNING)


def print_event(event, client: GameClient):
    """Print a game event to the console."""
    if event is None:
        return
    etype = event[0]
    data = event[1]

    if etype == 'chat':
        print(f'  [Chat] {data.message}')

    elif etype == 'whisper':
        print(f'  [Whisper from {data.sender}] {data.message}')

    elif etype == 'gm_chat':
        print(f'  [GM] {data.message}')

    elif etype == 'npc_message':
        print(f'  [NPC] {data.message}')

    elif etype == 'npc_next':
        print('  [NPC waits - type "next" to continue]')

    elif etype == 'npc_close':
        print('  [NPC done - type "close" to close dialog]')

    elif etype == 'npc_choice':
        print('  [NPC offers choices:]')
        for i, choice in enumerate(data.choices, 1):
            print(f'    {i}. {choice}')
        print('  [Type "choose N" to pick]')

    elif etype == 'map_change':
        print(f'  [Warped to {data.map_name} ({data.x},{data.y})]')

    elif etype == 'action':
        if data.damage > 0:
            src = client.beings.get(data.src_id)
            dst = client.beings.get(data.dst_id)
            src_name = src.name if src else f'#{data.src_id}'
            dst_name = dst.name if dst else f'#{data.dst_id}'
            if data.dst_id == client.account_id:
                dst_name = client.player.char_name
            if data.src_id == client.account_id:
                src_name = client.player.char_name
            print(f'  [Combat] {src_name} -> {dst_name}: {data.damage} dmg')

    elif etype == 'name':
        pass  # Name responses are silently stored

    elif etype == 'being_remove':
        if data.reason == 1:
            b = client.beings.get(data.block_id)
            name = b.name if b else f'#{data.block_id}'
            print(f'  [Died] {name}')

    elif etype == 'skill_damage':
        if data.damage > 0 and data.dst_id == client.account_id:
            print(f'  [Skill hit] took {data.damage} damage')


def print_help():
    print("""
Commands:
  status / s         - Show player status
  look / l           - Show nearby beings
  items              - Show nearby items on ground
  walk X Y / w X Y   - Walk to position
  say MESSAGE        - Chat to nearby players
  whisper NAME MSG   - Send private message
  sit / stand        - Sit down / stand up
  attack ID          - Attack a being (continuous)
  stopattack         - Stop auto-attack and hunting
  hunt NAME          - Hunt monsters by name (comma-separated for multiple)
  hunt               - Stop hunting
  follow NAME/ID     - Follow a player
  follow             - Stop following
  pickup ID          - Pick up an item
  npc ID             - Click on an NPC
  next               - Continue NPC dialog
  close              - Close NPC dialog
  choose N           - Choose NPC menu option
  face N             - Face direction (0=south, 2=left, 4=north, 6=right)
  chat               - Show recent chat log
  respawn            - Respawn after death
  quit / q           - Disconnect and exit
  help / h / ?       - Show this help
""")


def run_interactive(client: GameClient):
    """Main interactive loop."""
    print(f'\nLogged in as {client.player.char_name}!')
    print(f'Map: {client.player.map_name} ({client.player.x},{client.player.y})')
    print('Type "help" for commands.\n')

    # Request names for beings we can see
    for b_id in list(client.beings.keys()):
        client.request_name(b_id)

    tick_count = 0
    running = True
    while running:
        # Process incoming packets
        events = client.process_packets(timeout=0.1)
        for event in events:
            print_event(event, client)

        # Automated behaviors (hunt, follow, auto-attack)
        run_auto_behaviors(client, tick_count)
        tick_count += 1

        # Send keepalive
        client.send_ping()

        # Check for user input (non-blocking via short timeout)
        # We'll use a simple approach: try to read with a prompt
        try:
            import select
            readable, _, _ = select.select([sys.stdin], [], [], 0.1)
            if not readable:
                continue
            line = sys.stdin.readline()
            if not line:
                break
            line = line.strip()
        except (EOFError, KeyboardInterrupt):
            break

        if not line:
            continue

        parts = line.split(maxsplit=1)
        cmd = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ''

        try:
            if cmd in ('quit', 'q', 'exit'):
                running = False

            elif cmd in ('help', 'h', '?'):
                print_help()

            elif cmd in ('status', 's'):
                print(client.status_summary())

            elif cmd in ('look', 'l'):
                beings = client.nearby_beings()
                if not beings:
                    print('  No beings nearby.')
                for b in beings[:20]:
                    name = b.name or f'species:{b.species}'
                    hp_str = f' HP:{b.hp}/{b.max_hp}' if b.max_hp > 0 else ''
                    print(f'  [{b.block_id}] {name} at ({b.x},{b.y}){hp_str}')
                    if not b.name:
                        client.request_name(b.block_id)

            elif cmd == 'items':
                items = client.nearby_items()
                if not items:
                    print('  No items nearby.')
                for item in items[:20]:
                    print(f'  [{item.block_id}] item#{item.name_id} x{item.amount} '
                          f'at ({item.x},{item.y})')

            elif cmd in ('walk', 'w'):
                xy = args.split()
                if len(xy) == 2:
                    x, y = int(xy[0]), int(xy[1])
                    client.walk_to(x, y)
                    print(f'  Walking to ({x},{y})...')
                else:
                    print('  Usage: walk X Y')

            elif cmd == 'say':
                if args:
                    client.say(args)
                else:
                    print('  Usage: say MESSAGE')

            elif cmd == 'whisper':
                wparts = args.split(maxsplit=1)
                if len(wparts) == 2:
                    client.whisper(wparts[0], wparts[1])
                else:
                    print('  Usage: whisper NAME MESSAGE')

            elif cmd == 'sit':
                client.sit()
                print('  Sitting down.')

            elif cmd == 'stand':
                client.stand()
                print('  Standing up.')

            elif cmd == 'attack':
                if args:
                    target_id = int(args)
                    client.attack(target_id, continuous=True)
                    client._auto_attack_target = target_id
                    print(f'  Attacking #{target_id} (continuous)')
                else:
                    print('  Usage: attack ID')

            elif cmd == 'stopattack':
                client._auto_attack_target = 0
                client._hunt_type = ''
                client._hunt_home = None
                print('  Stopped auto-attack and hunting.')

            elif cmd == 'hunt':
                if args:
                    client._hunt_type = args.strip()
                    client._hunt_home = (client.player.x, client.player.y)
                    print(f'  Hunting: {client._hunt_type} (home: {client.player.x},{client.player.y})')
                else:
                    client._hunt_type = ''
                    client._hunt_home = None
                    print('  Stopped hunting.')

            elif cmd == 'follow':
                from .bot import start_follow, stop_follow
                if args:
                    print('  ' + start_follow(client, args))
                else:
                    stop_follow(client)
                    print('  Stopped following.')

            elif cmd == 'pickup':
                if args:
                    item_id = int(args)
                    client.pickup(item_id)
                else:
                    print('  Usage: pickup ID')

            elif cmd == 'npc':
                if args:
                    npc_id = int(args)
                    client.click_npc(npc_id)
                    print(f'  Talking to NPC #{npc_id}...')
                else:
                    print('  Usage: npc ID')

            elif cmd == 'next':
                client.npc_next_response()

            elif cmd == 'close':
                client.npc_close_response()

            elif cmd == 'choose':
                if args:
                    client.npc_choose(int(args))
                else:
                    print('  Usage: choose N')

            elif cmd == 'face':
                if args:
                    client.face(int(args))
                else:
                    print('  Usage: face N (0=south, 2=left, 4=north, 6=right)')

            elif cmd == 'chat':
                if not client.chat_log:
                    print('  No chat messages yet.')
                for msg in client.chat_log[-20:]:
                    print(f'  {msg}')

            elif cmd == 'respawn':
                client.respawn()
                print('  Respawning...')

            else:
                print(f'  Unknown command: {cmd}. Type "help" for commands.')

        except Exception as e:
            print(f'  Error: {e}')

    print('Disconnecting...')
    client.disconnect()


def main():
    parser = argparse.ArgumentParser(description='TMW CLI Client')
    parser.add_argument('--server', default='server.themanaworld.org',
                        help='Login server address')
    parser.add_argument('--port', type=int, default=6901,
                        help='Login server port')
    parser.add_argument('--user', '-u', help='Account username')
    parser.add_argument('--password', '-p', help='Account password')
    parser.add_argument('--char', '-c', type=int, default=0,
                        help='Character slot (default: 0)')
    parser.add_argument('--credentials', default='credentials.json',
                        help='Path to credentials JSON file')
    parser.add_argument('--verbose', '-v', action='store_true',
                        help='Verbose logging')
    parser.add_argument('--dashboard-port', type=int, default=0,
                        help='Start the browser dashboard on 127.0.0.1:PORT '
                             '(default: 0 = off)')
    args = parser.parse_args()

    setup_logging(args.verbose)

    # Load credentials from file if available
    if not args.user and os.path.exists(args.credentials):
        import json
        with open(args.credentials) as f:
            creds = json.load(f)
        args.user = creds.get('username', args.user)
        args.password = creds.get('password', args.password)
        args.server = creds.get('server', args.server)
        args.port = creds.get('port', args.port)
        if 'char_slot' in creds:
            args.char = creds['char_slot']
        args.world = creds.get('world', '')
        print(f'Loaded credentials for {args.user} from {args.credentials}')

    if not args.user:
        args.user = input('Username: ')
    if not args.password:
        import getpass
        args.password = getpass.getpass('Password: ')

    client = GameClient(args.server, args.port)

    print(f'Connecting to {args.server}:{args.port}...')
    if not client.full_login(args.user, args.password, args.char,
                             world=getattr(args, 'world', '')):
        print('Failed to connect. Check credentials and try again.')
        sys.exit(1)

    # Process initial flood of packets (stat updates, inventory, etc.)
    print('Loading game state...')
    for _ in range(20):
        client.process_packets(timeout=0.2)
        client.send_ping()

    # Optional dashboard.
    dashboard = None
    if args.dashboard_port:
        from .dashboard import DashboardServer, OperatorHooks, build_snapshot
        from .items import item_name

        # In interactive mode there's no MCP session, so operator
        # notifications just print. Walks and attacks go straight to
        # the game client; the interactive loop is single-threaded so
        # we accept the small race window.
        def _op_walk(x: int, y: int) -> None:
            client.walk_path(x, y)

        def _op_attack(being_id: int) -> None:
            client.attack(being_id, continuous=True)
            client._auto_attack_target = being_id

        def _op_say(text: str) -> None:
            client.say(text)

        def _op_notify(text: str) -> None:
            print(text)

        def _op_known_ids():
            return list((client.beings or {}).keys())

        hooks = OperatorHooks(
            walk=_op_walk,
            attack=_op_attack,
            say=_op_say,
            notify=_op_notify,
            known_being_ids=_op_known_ids,
        )
        dashboard = DashboardServer.start(
            port=args.dashboard_port,
            state_provider=lambda: build_snapshot(client, item_name),
            op_hooks=hooks,
        )
        print(f'Dashboard: http://127.0.0.1:{dashboard.port}/')

    try:
        run_interactive(client)
    finally:
        if dashboard is not None:
            dashboard.stop()


if __name__ == '__main__':
    main()
