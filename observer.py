#!/usr/bin/env python3
"""
Observer - logs in with a second account near Claudius and logs all
being movement packets to help debug the teleportation issue.
"""

import json
import logging
import time

from game import GameClient, Being
from packets import BeingVisible, BeingMove, BeingSpawn, BeingRemove, PlayerStop

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s: %(message)s',
                    datefmt='%H:%M:%S')
logging.getLogger('net').setLevel(logging.WARNING)

with open('observer_creds.json') as f:
    creds = json.load(f)

client = GameClient(creds['server'], creds['port'])
if not client.full_login(creds['username'], creds['password'],
                         creds.get('char_slot', 0),
                         world=creds.get('world', '')):
    print('Observer login failed!')
    exit(1)

print(f'Observer logged in at ({client.player.x},{client.player.y})')
print(f'Watching for Claudius (account 2293340)...')
print()

CLAUDIUS_ACCOUNT = 2293340
claudius_pos = None

# Process initial packets
for _ in range(20):
    client.process_packets(timeout=0.2)
    client.send_ping()

try:
    while True:
        events = client.process_packets(timeout=0.2)
        client.send_ping()

        for event in events:
            etype, data = event[0], event[1]

            if etype == 'being_visible' and isinstance(data, BeingVisible):
                if data.block_id == CLAUDIUS_ACCOUNT:
                    old = claudius_pos
                    claudius_pos = (data.x, data.y)
                    jump = ''
                    if old and (abs(old[0]-data.x) > 1 or abs(old[1]-data.y) > 1):
                        dist = abs(old[0]-data.x) + abs(old[1]-data.y)
                        jump = f' *** JUMP dist={dist} from {old} ***'
                    print(f'[VISIBLE] Claudius at ({data.x},{data.y}) speed={data.speed}{jump}')

            elif etype == 'being_move' and isinstance(data, BeingMove):
                if data.block_id == CLAUDIUS_ACCOUNT:
                    old = claudius_pos
                    claudius_pos = (data.x1, data.y1)
                    print(f'[MOVE] Claudius ({data.x0},{data.y0})->({data.x1},{data.y1}) speed={data.speed}')
                    if old and (abs(old[0]-data.x0) > 2 or abs(old[1]-data.y0) > 2):
                        print(f'  *** SOURCE MISMATCH: last known {old}, move says from ({data.x0},{data.y0}) ***')

            elif etype == 'stop' and isinstance(data, PlayerStop):
                if data.block_id == CLAUDIUS_ACCOUNT:
                    claudius_pos = (data.x, data.y)
                    print(f'[STOP] Claudius at ({data.x},{data.y})')

            elif etype == 'being_remove' and isinstance(data, BeingRemove):
                if data.block_id == CLAUDIUS_ACCOUNT:
                    print(f'[REMOVE] Claudius (reason={data.reason})')
                    claudius_pos = None

            elif etype == 'name' and hasattr(data, 'block_id'):
                if data.block_id == CLAUDIUS_ACCOUNT:
                    print(f'[NAME] Claudius confirmed as #{data.block_id}')

except KeyboardInterrupt:
    pass
finally:
    client.disconnect()
