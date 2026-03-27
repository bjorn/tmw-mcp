#!/usr/bin/env python3
"""
Extract all user-facing packet sizes from tmwa/tools/protocol.py.

This script imports the protocol definitions and extracts packet IDs and sizes
for all client<->server ("user") channel packets.
"""

import sys
import os

# Add tmwa tools dir to path so we can import protocol
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tmwa', 'tools'))

# We need to intercept the Context.dump() call and just extract packet info
# Instead, let's parse the protocol.py output more directly

import importlib.util
spec = importlib.util.spec_from_file_location(
    "protocol",
    os.path.join(os.path.dirname(__file__), '..', 'tmwa', 'tools', 'protocol.py')
)
protocol = importlib.util.module_from_spec(spec)

# Monkey-patch OpenWrite to not write files
class FakeFile:
    def write(self, *a): pass
    def __enter__(self): return self
    def __exit__(self, *a): pass

original_open_write = protocol.__dict__.get('OpenWrite')

class FakeOpenWrite:
    def __init__(self, filename): pass
    def __enter__(self): return FakeFile()
    def __exit__(self, *a): pass

# We can't easily import protocol.py because it tries to write files.
# Instead, let's parse it with regex.

import re

proto_path = os.path.join(os.path.dirname(__file__), '..', 'tmwa', 'tools', 'protocol.py')
with open(proto_path) as f:
    content = f.read()

# Find all user channel definitions
# Pattern: chan_user.s/r(0xNNNN, 'name', ... fixed_size=N or head_size=N ...)
# We need to find packets on *_user channels

# First find channel variable names that end with _user
user_channels = set()
for m in re.finditer(r'(\w+_user)\s*=\s*ctx\.chan\(', content):
    user_channels.add(m.group(1))

# Also any_user
user_channels.add('any_user')

print(f'# User channels: {sorted(user_channels)}')
print()

# Now find all packet definitions on user channels
# They look like: channel.r/s/x(0xNNNN, 'name', ...
# followed by fixed_size=N or head_size=N (variable)

packets = {}

# Split into packet blocks
# Each packet starts with a channel.r/s/x(0x... call
pattern = re.compile(
    r'(\w+_user)\.[rsx]\((0x[0-9a-fA-F]+),\s*\'([^\']+)\'',
)

for m in pattern.finditer(content):
    channel = m.group(1)
    pkt_id = int(m.group(2), 16)
    name = m.group(3)

    # Find the size info - look ahead from this position
    # for fixed_size= or head_size= (which means variable)
    start = m.start()
    # Find the end of this packet definition (next channel.x( or end of function)
    next_pkt = pattern.search(content, m.end())
    end = next_pkt.start() if next_pkt else len(content)
    block = content[start:end]

    if 'fixed_size=' in block:
        size_m = re.search(r'fixed_size=(\d+)', block)
        if size_m:
            packets[pkt_id] = int(size_m.group(1))
    elif 'payload_size=' in block:
        size_m = re.search(r'payload_size=(\d+)', block)
        if size_m:
            # payload packets are special (like 0x8000)
            packets[pkt_id] = None  # treat as variable
    elif 'head_size=' in block:
        packets[pkt_id] = None  # variable-size
    else:
        print(f'# WARNING: no size info for 0x{pkt_id:04x} ({name})')

# Output as Python dict
print('# Auto-generated from tmwa/tools/protocol.py')
print('# Maps packet_id -> fixed size (int) or None (variable-length)')
print('PACKET_SIZES: dict[int, int | None] = {')
for pkt_id in sorted(packets.keys()):
    size = packets[pkt_id]
    if size is None:
        print(f'    0x{pkt_id:04x}: None,')
    else:
        print(f'    0x{pkt_id:04x}: {size},')
print('}')
print(f'\n# Total: {len(packets)} packets')


if __name__ == '__main__':
    pass
