#!/usr/bin/env python3
"""
Register a new TMW account and optionally create a character.

Usage:
    python register.py [--server HOST] [--port PORT]
                       [--user USER] [--password PASS] [--gender M/F]
                       [--char-name NAME] [--char-slot SLOT]
"""

import argparse
import json
import logging
import os
import secrets
import string
import sys

from .game import GameClient
from .packets import LoginError, LoginSuccess, CharInfo, CharMapInfo


def generate_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits + '!@#%^&*'
    return ''.join(secrets.choice(alphabet) for _ in range(length))


def save_credentials(path: str, data: dict):
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)
    os.chmod(path, 0o600)
    print(f'Credentials saved to {path}')


def main():
    parser = argparse.ArgumentParser(description='Register a TMW account')
    parser.add_argument('--server', default='server.themanaworld.org')
    parser.add_argument('--port', type=int, default=6901)
    parser.add_argument('--user', '-u', default='Thorbot',
                        help='Account username (4+ chars)')
    parser.add_argument('--password', '-p',
                        help='Password (4+ chars, auto-generated if omitted)')
    parser.add_argument('--gender', '-g', default='M', choices=['M', 'F'])
    parser.add_argument('--char-name', default='Thorbot',
                        help='Character name to create')
    parser.add_argument('--char-slot', type=int, default=0,
                        help='Character slot (0-2)')
    parser.add_argument('--credentials-file', default='credentials.json',
                        help='Where to save credentials')
    parser.add_argument('--verbose', '-v', action='store_true')
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level,
                        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
                        datefmt='%H:%M:%S')
    if not args.verbose:
        logging.getLogger('net').setLevel(logging.WARNING)

    password = args.password or generate_password()

    print(f'Registering account "{args.user}" on {args.server}:{args.port}...')

    client = GameClient(args.server, args.port)
    result = client.register(args.user, password, args.gender)

    if isinstance(result, LoginError):
        if result.error_code == 9:
            # Account already exists - just log in normally
            print('Account already exists, logging in...')
            client.disconnect()
            import time
            time.sleep(3)
            client = GameClient(args.server, args.port)
            result = client.login(args.user, password)
            if isinstance(result, LoginError):
                print(f'Login failed: error code {result.error_code}')
                client.disconnect()
                return 1
        else:
            error_messages = {
                0: 'Unregistered ID (registration might be disabled)',
                1: 'Incorrect password',
                4: 'Permanently blocked',
                5: 'Client version too old',
                7: 'Server full',
            }
            msg = error_messages.get(result.error_code,
                                     f'Unknown error code {result.error_code}')
            print(f'Registration failed: {msg}')
            client.disconnect()
            return 1

    print(f'Logged in! Account ID: {result.account_id}')
    print(f'  Username: {args.user}')
    print(f'  Password: {password}')

    if not result.servers:
        print('No character servers available.')
        client.disconnect()
        return 1

    # Connect to char server
    srv = result.servers[0]
    print(f'Connecting to char server {srv.name} ({srv.ip}:{srv.port})...')
    char_result = client.connect_char_server(srv.ip, srv.port)

    if char_result is None:
        print('Failed to connect to character server.')
        client.disconnect()
        return 1

    # Create character if none exist
    if not client.characters:
        print(f'Creating character "{args.char_name}"...')
        from .packets import build_char_create
        # Balanced starting stats: must sum to 30 (server default)
        stats = (5, 5, 5, 5, 5, 5)  # str, agi, vit, int, dex, luk
        client.char_conn.send_packet(
            build_char_create(args.char_name, stats, args.char_slot,
                              hair_color=5, hair_style=3)
        )
        for _ in range(5):
            create_result = client.char_conn.recv_packet()
            if isinstance(create_result, CharInfo):
                print(f'Character created: {create_result.char_name} '
                      f'(ID: {create_result.char_id})')
                break
            elif isinstance(create_result, tuple) and len(create_result) == 2:
                pkt_id = create_result[0]
                if pkt_id == 0x006e or (isinstance(pkt_id, int) and pkt_id == 0x006e):
                    print(f'Character creation failed (name taken or invalid)')
                    client.disconnect()
                    return 1
                # Skip hold_notify and other wrappers
                continue
            else:
                print(f'Character creation unexpected: {create_result}')
                client.disconnect()
                return 1
    else:
        print(f'Account already has {len(client.characters)} character(s)')

    # Save credentials
    creds = {
        'server': args.server,
        'port': args.port,
        'username': args.user,
        'password': password,
        'gender': args.gender,
        'char_name': args.char_name,
        'char_slot': args.char_slot,
        'update_host': client.update_host,
    }
    save_credentials(args.credentials_file, creds)

    client.disconnect()
    print('Done!')
    return 0


if __name__ == '__main__':
    sys.exit(main())
