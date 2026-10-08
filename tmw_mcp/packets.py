"""
TMW protocol packet definitions.

All packets are little-endian. Each starts with a u16 packet ID.
Fixed-size packets have a known size. Variable-size packets have a u16
length field at offset 2 (counting from the packet ID).

This file defines packet structures for the login, char, and map servers
as used by the client ("user" channel).
"""

import struct
from dataclasses import dataclass, field
from typing import ClassVar


# ---------------------------------------------------------------------------
# Position encoding helpers
# ---------------------------------------------------------------------------

def encode_pos1(x: int, y: int, direction: int = 0) -> bytes:
    """Encode Position1: 10-bit x, 10-bit y, 4-bit direction in 3 bytes."""
    b0 = (x >> 2) & 0xFF
    b1 = ((x & 3) << 6) | ((y >> 4) & 0x3F)
    b2 = ((y & 0xF) << 4) | (direction & 0xF)
    return bytes([b0, b1, b2])


def decode_pos1(data: bytes) -> tuple[int, int, int]:
    """Decode Position1 -> (x, y, direction)."""
    p = data[:3]
    x = (p[0] & 0xFF) << 2 | (p[1] >> 6)
    y = (p[1] & 0x3F) << 4 | (p[2] >> 4)
    d = p[2] & 0x0F
    return x, y, d


def decode_pos2(data: bytes) -> tuple[int, int, int, int]:
    """Decode Position2 -> (x0, y0, x1, y1)."""
    p = data[:5]
    x0 = (p[0] & 0xFF) << 2 | (p[1] >> 6)
    y0 = (p[1] & 0x3F) << 4 | (p[2] >> 4)
    x1 = (p[2] & 0x0F) << 6 | (p[3] >> 2)
    y1 = (p[3] & 0x03) << 8 | p[4]
    return x0, y0, x1, y1


# ---------------------------------------------------------------------------
# String helpers
# ---------------------------------------------------------------------------

def encode_str(s: str, length: int) -> bytes:
    """Encode a fixed-length null-terminated string."""
    b = s.encode('latin-1', errors='replace')[:length - 1]
    return b + b'\x00' * (length - len(b))


def decode_str(data: bytes) -> str:
    """Decode a null-terminated string."""
    end = data.find(b'\x00')
    if end >= 0:
        data = data[:end]
    return data.decode('latin-1', errors='replace')


# ---------------------------------------------------------------------------
# Packet size table: packet_id -> fixed size, or None for variable
# ---------------------------------------------------------------------------

# Auto-generated from tmwa/tools/protocol.py via extract_packets.py
PACKET_SIZES: dict[int, int | None] = {
    0x0061: 50, 0x0062: 3, 0x0063: None, 0x0064: 55, 0x0065: 17,
    0x0066: 3, 0x0067: 37, 0x0068: 46, 0x0069: None, 0x006a: 23,
    0x006b: None, 0x006c: 3, 0x006d: 108, 0x006e: 3, 0x006f: 2,
    0x0070: 3, 0x0071: 28, 0x0072: 19, 0x0073: 11, 0x0078: 54,
    0x007b: 60, 0x007c: 41, 0x007d: 2, 0x007e: 6, 0x007f: 6,
    0x0080: 7, 0x0081: 3, 0x0085: 5, 0x0087: 12, 0x0088: 10,
    0x0089: 7, 0x008a: 29, 0x008c: None, 0x008d: None, 0x008e: None,
    0x0090: 7, 0x0091: 22, 0x0092: 28, 0x0094: 6, 0x0095: 30,
    0x0096: None, 0x0097: None, 0x0098: 3, 0x009a: None, 0x009b: 5,
    0x009c: 9, 0x009d: 17, 0x009e: 17, 0x009f: 6, 0x00a0: 23,
    0x00a1: 6, 0x00a2: 6, 0x00a4: None, 0x00a6: None, 0x00a7: 8,
    0x00a8: 7, 0x00a9: 6, 0x00aa: 7, 0x00ab: 4, 0x00ac: 7,
    0x00af: 6, 0x00b0: 8, 0x00b1: 8, 0x00b2: 3, 0x00b3: 3,
    0x00b4: None, 0x00b5: 6, 0x00b6: 6, 0x00b7: None, 0x00b8: 7,
    0x00b9: 6, 0x00bb: 5, 0x00bc: 6, 0x00bd: 44, 0x00be: 5,
    0x00bf: 3, 0x00c0: 7, 0x00c4: 6, 0x00c5: 7, 0x00c6: None,
    0x00c7: None, 0x00c8: None, 0x00c9: None, 0x00ca: 3, 0x00cb: 3,
    0x00cd: 6, 0x00e4: 6, 0x00e5: 26, 0x00e6: 3, 0x00e7: 3,
    0x00e8: 8, 0x00e9: 19, 0x00eb: 2, 0x00ec: 3, 0x00ed: 2,
    0x00ee: 2, 0x00ef: 2, 0x00f0: 3, 0x00f2: 6, 0x00f3: 8,
    0x00f4: 21, 0x00f5: 8, 0x00f6: 8, 0x00f7: 2, 0x00f8: 2,
    0x00f9: 26, 0x00fa: 3, 0x00fb: None, 0x00fc: 6, 0x00fd: 27,
    0x00fe: 30, 0x00ff: 10, 0x0100: 2, 0x0101: 6, 0x0102: 6,
    0x0103: 30, 0x0105: 31, 0x0106: 10, 0x0107: 10, 0x0108: None,
    0x0109: None, 0x010e: 11, 0x010f: None, 0x0110: 10, 0x0112: 4,
    0x0118: 2, 0x0119: 13, 0x0139: 16, 0x013a: 4, 0x013b: 4,
    0x013c: 4, 0x0141: 14, 0x0142: 6, 0x0143: 10, 0x0146: 6,
    0x0148: 8, 0x018a: 4, 0x018b: 4, 0x0195: 102, 0x0196: 9,
    0x0199: 4, 0x019a: 14, 0x019b: 10, 0x01b1: 7, 0x01c8: 13,
    0x01d4: 6, 0x01d5: None, 0x01d7: 11, 0x01d8: 54, 0x01d9: 53,
    0x01da: 60, 0x01de: 33, 0x01ee: None, 0x01f0: None, 0x020c: 10,
    0x0210: 2, 0x0211: None, 0x0212: 16, 0x0214: 8, 0x0215: None,
    0x0225: None, 0x0226: 10, 0x0227: None, 0x0228: None, 0x0229: None,
    0x0230: None, 0x0231: 34, 0x0232: 10, 0x0233: 14,
    0x7530: 2, 0x7531: 10, 0x7532: 2, 0x8000: None,
}

# For server->client packets we need to know sizes to parse incoming data
# For variable packets, we read the first 4 bytes (id + length)


# ---------------------------------------------------------------------------
# Packet builders (client -> server)
# ---------------------------------------------------------------------------

def build_login_register(username: str, password: str, client_version: int = 8) -> bytes:
    """0x0064: Login to the login server."""
    pkt = struct.pack('<HI', 0x0064, client_version)
    pkt += encode_str(username, 24)
    pkt += encode_str(password, 24)
    pkt += struct.pack('<B', 0x03)  # flags: version 2 features
    assert len(pkt) == 55
    return pkt


def build_char_server_connect(account_id: int, login_id1: int, login_id2: int, sex: int) -> bytes:
    """0x0065: Connect to character server."""
    pkt = struct.pack('<HIIIHI',
                      0x0065, account_id, login_id1, login_id2,
                      0,  # unused client protocol version
                      0)  # placeholder
    # Repack properly: id(2) + account_id(4) + login_id1(4) + login_id2(4) + unused(2) + sex(1) = 17
    pkt = struct.pack('<H', 0x0065)
    pkt += struct.pack('<I', account_id)
    pkt += struct.pack('<I', login_id1)
    pkt += struct.pack('<I', login_id2)
    pkt += struct.pack('<H', 0)  # unused client protocol version
    pkt += struct.pack('<B', sex)
    assert len(pkt) == 17
    return pkt


def build_char_select(slot: int) -> bytes:
    """0x0066: Select a character by slot number."""
    return struct.pack('<HB', 0x0066, slot)


def build_char_create(name: str, stats: tuple[int, ...], slot: int,
                      hair_color: int = 0, hair_style: int = 0) -> bytes:
    """0x0067: Create a new character."""
    pkt = struct.pack('<H', 0x0067)
    pkt += encode_str(name, 24)
    pkt += struct.pack('<6B', *stats)  # str, agi, vit, int, dex, luk
    pkt += struct.pack('<BHH', slot, hair_color, hair_style)
    assert len(pkt) == 37
    return pkt


def build_map_server_connect(account_id: int, char_id: int,
                             login_id1: int, sex: int) -> bytes:
    """0x0072: Connect to map server."""
    pkt = struct.pack('<H', 0x0072)
    pkt += struct.pack('<I', account_id)
    pkt += struct.pack('<I', char_id)
    pkt += struct.pack('<I', login_id1)
    pkt += struct.pack('<I', 0)  # client tick
    pkt += struct.pack('<B', sex)
    assert len(pkt) == 19
    return pkt


def build_map_loaded() -> bytes:
    """0x007d: Tell map server we finished loading the map."""
    return struct.pack('<H', 0x007d)


def build_ping(client_tick: int = 0) -> bytes:
    """0x007e: Ping the map server."""
    return struct.pack('<HI', 0x007e, client_tick)


def build_walk(x: int, y: int, direction: int = 0) -> bytes:
    """0x0085: Walk to position."""
    pkt = struct.pack('<H', 0x0085)
    pkt += encode_pos1(x, y, direction)
    assert len(pkt) == 5
    return pkt


def build_close_storage() -> bytes:
    """0x00f7: Close the storage (Kafra) window. Clears sd->state.storage_open
    server-side; without this, walks/item-use are silently dropped while it's set."""
    return struct.pack('<H', 0x00f7)


def build_client_quit() -> bytes:
    """0x018a: CMSG_CLIENT_QUIT. Tells the map server we're closing the session
    cleanly so it can drop the account-online entry immediately, letting us
    re-login without the usual cache delay. Payload is a 2-byte 'unused' word.
    """
    return struct.pack('<HH', 0x018a, 0)


def build_player_action(target_id: int, action: int) -> bytes:
    """0x0089: Perform action (attack, sit, stand).
    action: 0=attack, 7=continuous attack, 2=sit, 3=stand
    """
    return struct.pack('<HIB', 0x0089, target_id, action)


def build_emote(emote_id: int) -> bytes:
    """0x00bf: Show an emote sprite above the character.

    Emote ids come from the client-data emotes.xml.
    """
    return struct.pack('<HB', 0x00bf, emote_id)


def build_chat(message: str) -> bytes:
    """0x008c: Send a chat message to nearby players."""
    msg_bytes = message.encode('utf-8') + b'\x00'
    length = 4 + len(msg_bytes)
    pkt = struct.pack('<HH', 0x008c, length)
    pkt += msg_bytes
    return pkt


def build_npc_click(npc_id: int) -> bytes:
    """0x0090: Click on an NPC."""
    return struct.pack('<HIB', 0x0090, npc_id, 0)


def build_name_request(block_id: int) -> bytes:
    """0x0094: Request a being's name."""
    return struct.pack('<HI', 0x0094, block_id)


def build_whisper(target_name: str, message: str) -> bytes:
    """0x0096: Send a private message."""
    msg_bytes = message.encode('utf-8') + b'\x00'
    length = 28 + len(msg_bytes)
    pkt = struct.pack('<HH', 0x0096, length)
    pkt += encode_str(target_name, 24)
    pkt += msg_bytes
    return pkt


def build_change_dir(direction: int) -> bytes:
    """0x009b: Change facing direction."""
    return struct.pack('<HHB', 0x009b, 0, direction)


def build_stat_increase(sp_type: int) -> bytes:
    """0x00bb: Spend a status point to increase a stat.
    sp_type: 0x000d=STR, 0x000e=AGI, 0x000f=VIT, 0x0010=INT, 0x0011=DEX, 0x0012=LUK
    """
    return struct.pack('<HHB', 0x00bb, sp_type, 0)


def build_item_pickup(object_id: int) -> bytes:
    """0x009f: Pick up an item from the ground."""
    return struct.pack('<HI', 0x009f, object_id)


def build_equip_item(index: int, epos: int = 0) -> bytes:
    """0x00a9: Equip an item from inventory. index is ioff2 (inventory offset)."""
    return struct.pack('<HHH', 0x00a9, index, epos)

def build_unequip_item(index: int) -> bytes:
    """0x00ab: Unequip an item. index is ioff2 (inventory offset)."""
    return struct.pack('<HH', 0x00ab, index)


def build_drop_item(index: int, amount: int) -> bytes:
    """0x00a2: Drop an item."""
    return struct.pack('<HHH', 0x00a2, index, amount)


def build_respawn_or_switch(flag: int) -> bytes:
    """0x00b2: 0=respawn, 1=switch character."""
    return struct.pack('<HB', 0x00b2, flag)


def build_npc_menu_choice(npc_id: int, choice: int) -> bytes:
    """0x00b8: Choose from NPC menu (1-based), 0xff to cancel."""
    return struct.pack('<HIB', 0x00b8, npc_id, choice)


def build_npc_next(npc_id: int) -> bytes:
    """0x00b9: Continue NPC dialog."""
    return struct.pack('<HI', 0x00b9, npc_id)


def build_npc_close(npc_id: int) -> bytes:
    """0x0146: Close NPC dialog."""
    return struct.pack('<HI', 0x0146, npc_id)


def build_npc_buy_sell(npc_id: int, buy: bool = True) -> bytes:
    """0x00c5: Choose to buy (type=0) or sell (type=1) from shop NPC."""
    return struct.pack('<HIB', 0x00c5, npc_id, 0 if buy else 1)


def build_npc_buy(items: list[tuple[int, int]]) -> bytes:
    """0x00c8: Buy items. items = [(count, name_id), ...]"""
    length = 4 + 4 * len(items)
    pkt = struct.pack('<HH', 0x00c8, length)
    for count, name_id in items:
        pkt += struct.pack('<HH', count, name_id)
    return pkt


def build_npc_sell(items: list[tuple[int, int]]) -> bytes:
    """0x00c9: Sell items. items = [(index, count), ...]"""
    length = 4 + 4 * len(items)
    pkt = struct.pack('<HH', 0x00c9, length)
    for index, count in items:
        pkt += struct.pack('<HH', index, count)
    return pkt


def build_trade_request(block_id: int) -> bytes:
    """0x00e4: Ask another player to trade."""
    return struct.pack('<HI', 0x00e4, block_id)


def build_trade_response(accept: bool) -> bytes:
    """0x00e6: Respond to a trade request (type 3=accept, 4=reject)."""
    return struct.pack('<HB', 0x00e6, 3 if accept else 4)


def build_trade_add(index: int, amount: int) -> bytes:
    """0x00e8: Add an item or zeny to the trade.

    index=0 means zeny; otherwise inventory index (ioff2, which is index+2 per TMWA convention).
    """
    return struct.pack('<HHI', 0x00e8, index, amount)


def build_trade_lock() -> bytes:
    """0x00eb: Indicate readiness to end trade (half-lock)."""
    return struct.pack('<H', 0x00eb)


def build_trade_cancel() -> bytes:
    """0x00ed: Cancel an ongoing trade."""
    return struct.pack('<H', 0x00ed)


def build_trade_commit() -> bytes:
    """0x00ef: Actually perform the trade (after both sides locked)."""
    return struct.pack('<H', 0x00ef)


def build_npc_int_input(npc_id: int, value: int) -> bytes:
    """0x0143: Submit integer input to NPC."""
    return struct.pack('<HIi', 0x0143, npc_id, value)


def build_npc_str_input(npc_id: int, text: str) -> bytes:
    """0x01d5: Submit string input to NPC."""
    msg_bytes = text.encode('utf-8') + b'\x00'
    length = 8 + len(msg_bytes)
    pkt = struct.pack('<HHI', 0x01d5, length, npc_id)
    pkt += msg_bytes
    return pkt


def build_party_message(message: str) -> bytes:
    """0x0108: Send a message to party members."""
    msg_bytes = message.encode('utf-8') + b'\x00'
    length = 4 + len(msg_bytes)
    pkt = struct.pack('<HH', 0x0108, length)
    pkt += msg_bytes
    return pkt


def build_party_reply(account_id: int, accept: bool) -> bytes:
    """0x00ff: Reply to party invitation. accept=True to join, False to reject."""
    return struct.pack('<HIi', 0x00ff, account_id, 1 if accept else 0)


def build_party_leave() -> bytes:
    """0x0100: Leave the current party."""
    return struct.pack('<H', 0x0100)


def build_online_list_request() -> bytes:
    """0x0210: Request the online player list."""
    return struct.pack('<H', 0x0210)


# ---------------------------------------------------------------------------
# Packet parsers (server -> client)
# ---------------------------------------------------------------------------

@dataclass
class UpdateHost:
    """0x0063"""
    url: str = ''

@dataclass
class LoginSuccess:
    """0x0069"""
    login_id1: int = 0
    account_id: int = 0
    login_id2: int = 0
    sex: int = 0
    servers: list = field(default_factory=list)

@dataclass
class ServerInfo:
    ip: str = ''
    port: int = 0
    name: str = ''
    users: int = 0

@dataclass
class LoginError:
    """0x006a"""
    error_code: int = 0
    error_message: str = ''

@dataclass
class CharLoginSuccess:
    """0x006b"""
    characters: list = field(default_factory=list)

@dataclass
class CharInfo:
    char_id: int = 0
    base_exp: int = 0
    zeny: int = 0
    job_exp: int = 0
    job_level: int = 0
    hp: int = 0
    max_hp: int = 0
    sp: int = 0
    max_sp: int = 0
    speed: int = 0
    species: int = 0
    hair_style: int = 0
    weapon: int = 0
    base_level: int = 0
    skill_point: int = 0
    hair_color: int = 0
    char_name: str = ''
    str_: int = 0
    agi: int = 0
    vit: int = 0
    int_: int = 0
    dex: int = 0
    luk: int = 0
    char_num: int = 0
    sex: int = 0

@dataclass
class CharMapInfo:
    """0x0071: char server tells us where to go"""
    char_id: int = 0
    map_name: str = ''
    ip: str = ''
    port: int = 0

@dataclass
class MapLoginSuccess:
    """0x0073"""
    tick: int = 0
    x: int = 0
    y: int = 0
    direction: int = 0

@dataclass
class BeingVisible:
    """0x0078 / 0x01d8"""
    block_id: int = 0
    speed: int = 0
    species: int = 0
    hair_style: int = 0
    weapon: int = 0
    head_bottom: int = 0
    head_top: int = 0
    head_mid: int = 0
    hair_color: int = 0
    sex: int = 0
    x: int = 0
    y: int = 0
    direction: int = 0
    hp: int = 0
    max_hp: int = 0
    level: int = 0

@dataclass
class BeingMove:
    """0x007b / 0x01da"""
    block_id: int = 0
    speed: int = 0
    species: int = 0
    x0: int = 0
    y0: int = 0
    x1: int = 0
    y1: int = 0
    tick: int = 0
    hp: int = 0
    max_hp: int = 0
    level: int = 0

@dataclass
class BeingSpawn:
    """0x007c"""
    block_id: int = 0
    speed: int = 0
    species: int = 0
    x: int = 0
    y: int = 0

@dataclass
class BeingRemove:
    """0x0080"""
    block_id: int = 0
    reason: int = 0  # 0=out of sight, 1=dead, 2=logout, 3=teleport

@dataclass
class ConnectionProblem:
    """0x0081"""
    error_code: int = 0

@dataclass
class WalkResponse:
    """0x0087"""
    tick: int = 0
    x0: int = 0
    y0: int = 0
    x1: int = 0
    y1: int = 0

@dataclass
class PlayerStop:
    """0x0088"""
    block_id: int = 0
    x: int = 0
    y: int = 0

@dataclass
class BeingAction:
    """0x008a"""
    src_id: int = 0
    dst_id: int = 0
    tick: int = 0
    damage: int = 0
    damage_type: int = 0

@dataclass
class ChatMessage:
    """0x008d / 0x008e"""
    block_id: int = 0  # 0 if from self (0x008e)
    message: str = ''

@dataclass
class PartyInfo:
    """0x00fb: Party member list."""
    party_name: str = ''
    members: list = None  # list of (account_id, name, map_name, is_leader, is_online)

@dataclass
class PartyMessage:
    """0x0109: Incoming party chat message."""
    account_id: int = 0
    message: str = ''

@dataclass
class ChangeMap:
    """0x0091"""
    map_name: str = ''
    x: int = 0
    y: int = 0

@dataclass
class ChangeMapServer:
    """0x0092"""
    map_name: str = ''
    x: int = 0
    y: int = 0
    ip: str = ''
    port: int = 0

@dataclass
class BeingNameResponse:
    """0x0095"""
    block_id: int = 0
    name: str = ''

@dataclass
class WhisperMessage:
    """0x0097"""
    sender: str = ''
    message: str = ''

@dataclass
class WhisperResponse:
    """0x0098"""
    flag: int = 0  # 0=success, 1=target not found, 2=ignored

@dataclass
class GmChat:
    """0x009a"""
    message: str = ''

@dataclass
class ItemVisible:
    """0x009d"""
    block_id: int = 0
    name_id: int = 0
    amount: int = 0
    x: int = 0
    y: int = 0

@dataclass
class ItemDropped:
    """0x009e"""
    block_id: int = 0
    name_id: int = 0
    amount: int = 0
    x: int = 0
    y: int = 0

@dataclass
class ItemRemove:
    """0x00a1"""
    block_id: int = 0

@dataclass
class InventoryAdd:
    """0x00a0"""
    index: int = 0
    amount: int = 0
    name_id: int = 0
    item_type: int = 0
    pickup_fail: int = 0

@dataclass
class EquipResult:
    """0x00aa (equip) / 0x00ac (unequip) response."""
    index: int = 0
    equip_point: int = 0
    success: int = 0

@dataclass
class InventoryRemove:
    """0x00af"""
    index: int = 0
    amount: int = 0

@dataclass
class ItemUseResult:
    """0x01c8: Item use result with remaining amount."""
    index: int = 0
    name_id: int = 0
    block_id: int = 0
    amount: int = 0
    ok: int = 0

@dataclass
class InventoryItem:
    """Single inventory item."""
    index: int = 0
    name_id: int = 0
    item_type: int = 0
    amount: int = 0
    equip_point: int = 0   # where it CAN be equipped (bitmask)
    equipped: int = 0      # where it IS equipped (0 = not equipped)

@dataclass
class InventoryList:
    """0x01ee"""
    items: list = field(default_factory=list)

@dataclass
class ShopItem:
    """Single shop item for buy list."""
    price: int = 0
    name_id: int = 0
    item_type: int = 0

@dataclass
class NpcBuySellChoice:
    """0x00c4: NPC is a shop, choose buy or sell."""
    npc_id: int = 0

@dataclass
class NpcBuyList:
    """0x00c6: List of items available to buy."""
    items: list = field(default_factory=list)

@dataclass
class NpcSellList:
    """0x00c7: List of items that can be sold."""
    items: list = field(default_factory=list)

@dataclass
class NpcBuyResponse:
    """0x00ca: Buy result."""
    fail: int = 0

@dataclass
class NpcSellResponse:
    """0x00cb: Sell result."""
    fail: int = 0

@dataclass
class StatUpdate1:
    """0x00b0"""
    sp_type: int = 0
    value: int = 0

@dataclass
class NpcMessage:
    """0x00b4"""
    npc_id: int = 0
    message: str = ''

@dataclass
class NpcNext:
    """0x00b5"""
    npc_id: int = 0

@dataclass
class NpcClose:
    """0x00b6"""
    npc_id: int = 0

@dataclass
class NpcChoice:
    """0x00b7"""
    npc_id: int = 0
    choices: list[str] = field(default_factory=list)

@dataclass
class TradeRequest:
    """0x00e5: Someone wants to trade with you."""
    char_name: str = ''

@dataclass
class TradeResponse:
    """0x00e7: Result of our trade request (0=too far, 1=character doesn't exist, 2=busy, 3=accepted, 4=rejected, 5=GM block)."""
    type: int = 0

@dataclass
class TradeItemAdd:
    """0x00e9: Other party added an item (or zeny if name_id=0)."""
    amount: int = 0
    name_id: int = 0

@dataclass
class TradeOk:
    """0x00ec: Someone locked their side (who: 0=self, 1=other)."""
    who: int = 0

@dataclass
class TradeCancel:
    """0x00ee: Trade cancelled."""
    pass

@dataclass
class TradeComplete:
    """0x00f0: Trade result (fail: 0=success, 1=fail)."""
    fail: int = 0

@dataclass
class NpcIntInputRequest:
    """0x0142: Server requests integer input from player."""
    npc_id: int = 0

@dataclass
class NpcStrInputRequest:
    """0x01d4: Server requests string input from player."""
    npc_id: int = 0

@dataclass
class StatUpdate5:
    """0x00bd: Big stat update with all attributes."""
    status_point: int = 0
    str_attr: int = 0
    str_upd: int = 0
    agi_attr: int = 0
    agi_upd: int = 0
    vit_attr: int = 0
    vit_upd: int = 0
    int_attr: int = 0
    int_upd: int = 0
    dex_attr: int = 0
    dex_upd: int = 0
    luk_attr: int = 0
    luk_upd: int = 0
    atk_sum: int = 0
    matk1: int = 0
    matk2: int = 0
    def_: int = 0
    mdef: int = 0

@dataclass
class BeingChangeLook:
    """0x01d7"""
    block_id: int = 0
    look_type: int = 0
    look_id: int = 0
    look_id2: int = 0

@dataclass
class PlayerWarp:
    """0x0091 alias"""
    map_name: str = ''
    x: int = 0
    y: int = 0

@dataclass
class SkillDamage:
    """0x01de"""
    skill_id: int = 0
    src_id: int = 0
    dst_id: int = 0
    tick: int = 0
    damage: int = 0

@dataclass
class BeingStatusChange:
    """0x0196"""
    status: int = 0
    block_id: int = 0
    flag: int = 0

@dataclass
class PlayerStatusChange:
    """0x0119"""
    block_id: int = 0
    opt1: int = 0
    opt2: int = 0
    option: int = 0
    unused: int = 0

@dataclass
class BeingEffect:
    """0x019b"""
    block_id: int = 0
    effect_type: int = 0


@dataclass
class AttackRange:
    """0x013a: Server notifies player's current attack range."""
    attack_range: int = 1


@dataclass
class ArrowEquip:
    """0x013c: Server notifies that an ammo item is now equipped.

    Unlike weapons / armor, ammo equip does NOT produce a 0x00aa
    EquipResult. The client must track ammo equip state from this
    packet instead. *index* is ioff2 (inventory offset, matches the
    key used in GameClient.inventory).
    """
    index: int = 0


@dataclass
class PartyInvited:
    """0x00fe: You're invited to join a party."""
    account_id: int = 0
    party_name: str = ''


@dataclass
class OnlineListEntry:
    """One entry from the 0x0211 online list."""
    account_id: int = 0
    name: str = ''
    level: int = 0
    gm_level: int = 0
    gender: int = 0


@dataclass
class OnlineList:
    """0x0211: Full online player list."""
    players: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Packet parser dispatch
# ---------------------------------------------------------------------------

def _ip_str(data: bytes) -> str:
    """Convert 4 raw bytes to dotted-quad IP string."""
    return '%d.%d.%d.%d' % (data[0], data[1], data[2], data[3])


def parse_char_info(data: bytes) -> CharInfo:
    """Parse a 106-byte CharSelect struct."""
    c = CharInfo()
    c.char_id = struct.unpack_from('<I', data, 0)[0]
    c.base_exp = struct.unpack_from('<I', data, 4)[0]
    c.zeny = struct.unpack_from('<I', data, 8)[0]
    c.job_exp = struct.unpack_from('<I', data, 12)[0]
    c.job_level = struct.unpack_from('<I', data, 16)[0]
    c.hp = struct.unpack_from('<H', data, 42)[0]
    c.max_hp = struct.unpack_from('<H', data, 44)[0]
    c.sp = struct.unpack_from('<H', data, 46)[0]
    c.max_sp = struct.unpack_from('<H', data, 48)[0]
    c.speed = struct.unpack_from('<H', data, 50)[0]
    c.species = struct.unpack_from('<H', data, 52)[0]
    c.hair_style = struct.unpack_from('<H', data, 54)[0]
    c.weapon = struct.unpack_from('<H', data, 56)[0]
    c.base_level = struct.unpack_from('<H', data, 58)[0]
    c.skill_point = struct.unpack_from('<H', data, 60)[0]
    c.hair_color = struct.unpack_from('<H', data, 70)[0]
    c.char_name = decode_str(data[74:98])
    c.str_ = data[98]
    c.agi = data[99]
    c.vit = data[100]
    c.int_ = data[101]
    c.dex = data[102]
    c.luk = data[103]
    c.char_num = data[104]
    c.sex = data[105]
    return c


def parse_packet(packet_id: int, data: bytes):
    """Parse a complete packet (including the 2-byte ID prefix).

    Returns a dataclass instance, or (packet_id, raw_data) for unhandled packets.
    """
    # data includes the packet ID at the start
    if packet_id == 0x0063:
        # Update host notify (variable)
        length = struct.unpack_from('<H', data, 2)[0]
        url = data[4:length].rstrip(b'\x00').decode('latin-1', errors='replace')
        return UpdateHost(url=url)

    elif packet_id == 0x0069:
        # Login success
        result = LoginSuccess()
        result.login_id1 = struct.unpack_from('<I', data, 4)[0]
        result.account_id = struct.unpack_from('<I', data, 8)[0]
        result.login_id2 = struct.unpack_from('<I', data, 12)[0]
        result.sex = data[46]
        length = struct.unpack_from('<H', data, 2)[0]
        n_servers = (length - 47) // 32
        for i in range(n_servers):
            off = 47 + i * 32
            srv = ServerInfo()
            srv.ip = _ip_str(data[off:off+4])
            srv.port = struct.unpack_from('<H', data, off + 4)[0]
            srv.name = decode_str(data[off+6:off+26])
            srv.users = struct.unpack_from('<H', data, off + 26)[0]
            result.servers.append(srv)
        return result

    elif packet_id == 0x006a:
        result = LoginError()
        result.error_code = data[2]
        result.error_message = decode_str(data[3:23])
        return result

    elif packet_id == 0x006b:
        # Char login success
        result = CharLoginSuccess()
        length = struct.unpack_from('<H', data, 2)[0]
        n_chars = (length - 24) // 106
        for i in range(n_chars):
            off = 24 + i * 106
            result.characters.append(parse_char_info(data[off:off+106]))
        return result

    elif packet_id == 0x006c:
        return ConnectionProblem(error_code=data[2])

    elif packet_id == 0x006d:
        return parse_char_info(data[2:108])

    elif packet_id == 0x0071:
        result = CharMapInfo()
        result.char_id = struct.unpack_from('<I', data, 2)[0]
        result.map_name = decode_str(data[6:22])
        result.ip = _ip_str(data[22:26])
        result.port = struct.unpack_from('<H', data, 26)[0]
        return result

    elif packet_id == 0x0073:
        result = MapLoginSuccess()
        result.tick = struct.unpack_from('<I', data, 2)[0]
        result.x, result.y, result.direction = decode_pos1(data[6:9])
        return result

    elif packet_id in (0x0078, 0x01d8):
        result = BeingVisible()
        result.block_id = struct.unpack_from('<I', data, 2)[0]
        result.speed = struct.unpack_from('<H', data, 6)[0]
        result.species = struct.unpack_from('<H', data, 14)[0]
        result.hair_style = data[16]
        result.weapon = struct.unpack_from('<H', data, 18)[0]
        result.head_bottom = struct.unpack_from('<H', data, 20)[0]
        result.head_top = struct.unpack_from('<H', data, 24)[0]
        result.head_mid = struct.unpack_from('<H', data, 26)[0]
        result.hair_color = data[28]
        result.hp = struct.unpack_from('<I', data, 32)[0]
        result.max_hp = struct.unpack_from('<I', data, 36)[0]
        result.sex = data[45]
        result.x, result.y, result.direction = decode_pos1(data[46:49])
        if packet_id == 0x01d8:
            result.level = data[52]
        return result

    elif packet_id in (0x007b, 0x01da):
        result = BeingMove()
        result.block_id = struct.unpack_from('<I', data, 2)[0]
        result.speed = struct.unpack_from('<H', data, 6)[0]
        result.species = struct.unpack_from('<H', data, 14)[0]
        result.tick = struct.unpack_from('<I', data, 22)[0]
        result.hp = struct.unpack_from('<I', data, 36)[0]
        result.max_hp = struct.unpack_from('<I', data, 40)[0]
        result.x0, result.y0, result.x1, result.y1 = decode_pos2(data[50:55])
        if packet_id == 0x01da:
            result.level = data[58]
        return result

    elif packet_id == 0x007c:
        result = BeingSpawn()
        result.block_id = struct.unpack_from('<I', data, 2)[0]
        result.speed = struct.unpack_from('<H', data, 6)[0]
        result.species = struct.unpack_from('<H', data, 20)[0]
        result.x, result.y, _ = decode_pos1(data[36:39])
        return result

    elif packet_id == 0x007f:
        # Pong
        return ('pong', struct.unpack_from('<I', data, 2)[0])

    elif packet_id == 0x0080:
        return BeingRemove(
            block_id=struct.unpack_from('<I', data, 2)[0],
            reason=data[6],
        )

    elif packet_id == 0x0081:
        return ConnectionProblem(error_code=data[2])

    elif packet_id == 0x0087:
        result = WalkResponse()
        result.tick = struct.unpack_from('<I', data, 2)[0]
        result.x0, result.y0, result.x1, result.y1 = decode_pos2(data[6:11])
        return result

    elif packet_id == 0x0088:
        return PlayerStop(
            block_id=struct.unpack_from('<I', data, 2)[0],
            x=struct.unpack_from('<H', data, 6)[0],
            y=struct.unpack_from('<H', data, 8)[0],
        )

    elif packet_id == 0x008a:
        result = BeingAction()
        result.src_id = struct.unpack_from('<I', data, 2)[0]
        result.dst_id = struct.unpack_from('<I', data, 6)[0]
        result.tick = struct.unpack_from('<I', data, 10)[0]
        result.damage = struct.unpack_from('<H', data, 22)[0]
        result.damage_type = data[26]
        return result

    elif packet_id == 0x008d:
        # Being chat
        length = struct.unpack_from('<H', data, 2)[0]
        block_id = struct.unpack_from('<I', data, 4)[0]
        message = data[8:length].rstrip(b'\x00').decode('utf-8', errors='replace')
        return ChatMessage(block_id=block_id, message=message)

    elif packet_id == 0x008e:
        # Player chat (own)
        length = struct.unpack_from('<H', data, 2)[0]
        message = data[4:length].rstrip(b'\x00').decode('utf-8', errors='replace')
        return ChatMessage(block_id=0, message=message)

    elif packet_id == 0x0091:
        return ChangeMap(
            map_name=decode_str(data[2:18]),
            x=struct.unpack_from('<H', data, 18)[0],
            y=struct.unpack_from('<H', data, 20)[0],
        )

    elif packet_id == 0x0092:
        return ChangeMapServer(
            map_name=decode_str(data[2:18]),
            x=struct.unpack_from('<H', data, 18)[0],
            y=struct.unpack_from('<H', data, 20)[0],
            ip=_ip_str(data[22:26]),
            port=struct.unpack_from('<H', data, 26)[0],
        )

    elif packet_id == 0x0095:
        return BeingNameResponse(
            block_id=struct.unpack_from('<I', data, 2)[0],
            name=decode_str(data[6:30]),
        )

    elif packet_id == 0x0097:
        length = struct.unpack_from('<H', data, 2)[0]
        sender = decode_str(data[4:28])
        message = data[28:length].rstrip(b'\x00').decode('utf-8', errors='replace')
        return WhisperMessage(sender=sender, message=message)

    elif packet_id == 0x0098:
        return WhisperResponse(flag=data[2])

    elif packet_id == 0x009a:
        length = struct.unpack_from('<H', data, 2)[0]
        message = data[4:length].rstrip(b'\x00').decode('utf-8', errors='replace')
        return GmChat(message=message)

    elif packet_id == 0x009d:
        return ItemVisible(
            block_id=struct.unpack_from('<I', data, 2)[0],
            name_id=struct.unpack_from('<H', data, 6)[0],
            amount=struct.unpack_from('<H', data, 13)[0],
            x=struct.unpack_from('<H', data, 9)[0],
            y=struct.unpack_from('<H', data, 11)[0],
        )

    elif packet_id == 0x009e:
        return ItemDropped(
            block_id=struct.unpack_from('<I', data, 2)[0],
            name_id=struct.unpack_from('<H', data, 6)[0],
            amount=struct.unpack_from('<H', data, 15)[0],
            x=struct.unpack_from('<H', data, 9)[0],
            y=struct.unpack_from('<H', data, 11)[0],
        )

    elif packet_id == 0x00a0:
        return InventoryAdd(
            index=struct.unpack_from('<H', data, 2)[0],
            amount=struct.unpack_from('<H', data, 4)[0],
            name_id=struct.unpack_from('<H', data, 6)[0],
            item_type=data[21],
            pickup_fail=data[22],
        )

    elif packet_id == 0x00a1:
        return ItemRemove(
            block_id=struct.unpack_from('<I', data, 2)[0],
        )

    elif packet_id == 0x00aa:
        # Equip result: index(2) + equip_point(2) + success(1)
        return EquipResult(
            index=struct.unpack_from('<H', data, 2)[0],
            equip_point=struct.unpack_from('<H', data, 4)[0],
            success=data[6],
        )

    elif packet_id == 0x00ac:
        # Unequip result: index(2) + equip_point(2) + success(1)
        return EquipResult(
            index=struct.unpack_from('<H', data, 2)[0],
            equip_point=struct.unpack_from('<H', data, 4)[0],
            success=data[6],
        )

    elif packet_id == 0x00af:
        return InventoryRemove(
            index=struct.unpack_from('<H', data, 2)[0],
            amount=struct.unpack_from('<H', data, 4)[0],
        )

    elif packet_id in (0x00b0, 0x00b1):
        return StatUpdate1(
            sp_type=struct.unpack_from('<H', data, 2)[0],
            value=struct.unpack_from('<I', data, 4)[0],
        )

    elif packet_id == 0x00b3:
        return ('char_switch_ok', data[2])

    elif packet_id == 0x00b4:
        length = struct.unpack_from('<H', data, 2)[0]
        npc_id = struct.unpack_from('<I', data, 4)[0]
        message = data[8:length].rstrip(b'\x00').decode('utf-8', errors='replace')
        return NpcMessage(npc_id=npc_id, message=message)

    elif packet_id == 0x00b5:
        return NpcNext(npc_id=struct.unpack_from('<I', data, 2)[0])

    elif packet_id == 0x00b6:
        return NpcClose(npc_id=struct.unpack_from('<I', data, 2)[0])

    elif packet_id == 0x00b7:
        length = struct.unpack_from('<H', data, 2)[0]
        npc_id = struct.unpack_from('<I', data, 4)[0]
        raw = data[8:length].rstrip(b'\x00').decode('utf-8', errors='replace')
        choices = [c for c in raw.split(':') if c]
        return NpcChoice(npc_id=npc_id, choices=choices)

    elif packet_id == 0x00e5:
        name = data[2:26].rstrip(b'\x00').decode('utf-8', errors='replace')
        return TradeRequest(char_name=name)

    elif packet_id == 0x00e7:
        return TradeResponse(type=data[2])

    elif packet_id == 0x00e9:
        amount = struct.unpack_from('<I', data, 2)[0]
        name_id = struct.unpack_from('<H', data, 6)[0]
        return TradeItemAdd(amount=amount, name_id=name_id)

    elif packet_id == 0x00ec:
        return TradeOk(who=data[2])

    elif packet_id == 0x00ee:
        return TradeCancel()

    elif packet_id == 0x00f0:
        return TradeComplete(fail=data[2])

    elif packet_id == 0x0142:
        return NpcIntInputRequest(npc_id=struct.unpack_from('<I', data, 2)[0])

    elif packet_id == 0x01d4:
        return NpcStrInputRequest(npc_id=struct.unpack_from('<I', data, 2)[0])

    elif packet_id == 0x00bd:
        r = StatUpdate5()
        r.status_point = struct.unpack_from('<H', data, 2)[0]
        r.str_attr = data[4]
        r.str_upd = data[5]
        r.agi_attr = data[6]
        r.agi_upd = data[7]
        r.vit_attr = data[8]
        r.vit_upd = data[9]
        r.int_attr = data[10]
        r.int_upd = data[11]
        r.dex_attr = data[12]
        r.dex_upd = data[13]
        r.luk_attr = data[14]
        r.luk_upd = data[15]
        r.atk_sum = struct.unpack_from('<H', data, 16)[0]
        r.matk1 = struct.unpack_from('<H', data, 20)[0]
        r.matk2 = struct.unpack_from('<H', data, 22)[0]
        r.def_ = struct.unpack_from('<H', data, 24)[0]
        r.mdef = struct.unpack_from('<H', data, 26)[0]
        return r

    elif packet_id == 0x00be:
        return ('stat_price', struct.unpack_from('<H', data, 2)[0], data[4])

    elif packet_id == 0x00c0:
        return ('being_emotion', struct.unpack_from('<I', data, 2)[0], data[6])

    elif packet_id == 0x00c4:
        return NpcBuySellChoice(npc_id=struct.unpack_from('<I', data, 2)[0])

    elif packet_id == 0x00c6:
        length = struct.unpack_from('<H', data, 2)[0]
        items = []
        for i in range((length - 4) // 11):
            off = 4 + i * 11
            items.append(ShopItem(
                price=struct.unpack_from('<I', data, off + 4)[0],
                item_type=data[off + 8],
                name_id=struct.unpack_from('<H', data, off + 9)[0],
            ))
        return NpcBuyList(items=items)

    elif packet_id == 0x00c7:
        length = struct.unpack_from('<H', data, 2)[0]
        items = []
        for i in range((length - 4) // 10):
            off = 4 + i * 10
            items.append({
                'index': struct.unpack_from('<H', data, off)[0],
                'price': struct.unpack_from('<I', data, off + 2)[0],
            })
        return NpcSellList(items=items)

    elif packet_id == 0x00ca:
        return NpcBuyResponse(fail=data[2])

    elif packet_id == 0x00cb:
        return NpcSellResponse(fail=data[2])

    elif packet_id == 0x0109:
        length = struct.unpack_from('<H', data, 2)[0]
        account_id = struct.unpack_from('<I', data, 4)[0]
        message = data[8:length].rstrip(b'\x00').decode('utf-8', errors='replace')
        return PartyMessage(account_id=account_id, message=message)

    elif packet_id == 0x0119:
        return PlayerStatusChange(
            block_id=struct.unpack_from('<I', data, 2)[0],
            opt1=struct.unpack_from('<H', data, 6)[0],
            opt2=struct.unpack_from('<H', data, 8)[0],
            option=struct.unpack_from('<H', data, 10)[0],
            unused=data[12],
        )

    elif packet_id == 0x0141:
        # stat update 3: sp_type(u16) + zero(u16) + base(u32) + bonus(u32)
        return StatUpdate1(
            sp_type=struct.unpack_from('<H', data, 2)[0],
            value=struct.unpack_from('<I', data, 6)[0],  # base stat at offset 6
        )

    elif packet_id == 0x0196:
        return BeingStatusChange(
            status=struct.unpack_from('<H', data, 2)[0],
            block_id=struct.unpack_from('<I', data, 4)[0],
            flag=data[8],
        )

    elif packet_id == 0x019b:
        return BeingEffect(
            block_id=struct.unpack_from('<I', data, 2)[0],
            effect_type=struct.unpack_from('<I', data, 6)[0],
        )

    elif packet_id == 0x013a:
        return AttackRange(
            attack_range=struct.unpack_from('<H', data, 2)[0],
        )

    elif packet_id == 0x013c:
        # Arrow / ammo equip notify. Payload is ioff2 only.
        return ArrowEquip(
            index=struct.unpack_from('<H', data, 2)[0],
        )

    elif packet_id == 0x00fe:
        return PartyInvited(
            account_id=struct.unpack_from('<I', data, 2)[0],
            party_name=data[6:30].rstrip(b'\x00').decode('latin-1'),
        )

    elif packet_id == 0x00fb:
        length = struct.unpack_from('<H', data, 2)[0]
        party_name = data[4:28].rstrip(b'\x00').decode('utf-8', errors='replace')
        members = []
        offset = 28
        while offset + 46 <= length:
            aid = struct.unpack_from('<I', data, offset)[0]
            name = data[offset+4:offset+28].rstrip(b'\x00').decode('utf-8', errors='replace')
            map_name = data[offset+28:offset+44].rstrip(b'\x00').decode('utf-8', errors='replace')
            leader = data[offset+44] == 0
            online = data[offset+45] == 0
            members.append((aid, name, map_name, leader, online))
            offset += 46
        return PartyInfo(party_name=party_name, members=members)

    elif packet_id == 0x01c8:
        return ItemUseResult(
            index=struct.unpack_from('<H', data, 2)[0],
            name_id=struct.unpack_from('<H', data, 4)[0],
            block_id=struct.unpack_from('<I', data, 6)[0],
            amount=struct.unpack_from('<H', data, 10)[0],
            ok=data[12],
        )

    elif packet_id == 0x01d7:
        return BeingChangeLook(
            block_id=struct.unpack_from('<I', data, 2)[0],
            look_type=data[6],
            look_id=struct.unpack_from('<H', data, 7)[0],
            look_id2=struct.unpack_from('<H', data, 9)[0],
        )

    elif packet_id == 0x01de:
        return SkillDamage(
            skill_id=struct.unpack_from('<H', data, 2)[0],
            src_id=struct.unpack_from('<I', data, 4)[0],
            dst_id=struct.unpack_from('<I', data, 8)[0],
            tick=struct.unpack_from('<I', data, 12)[0],
            damage=struct.unpack_from('<I', data, 24)[0],
        )

    elif packet_id == 0x00a4:
        # Equipment list: head=4, repeat_size=20
        # Per item: index(2) name_id(2) type(1) identified(1) equip_point(2) equipped(2) broken(1) refine(1) card0-3(8)
        length = struct.unpack_from('<H', data, 2)[0]
        result = InventoryList()
        n_items = (length - 4) // 20
        for i in range(n_items):
            off = 4 + i * 20
            item = InventoryItem(
                index=struct.unpack_from('<H', data, off)[0],
                name_id=struct.unpack_from('<H', data, off + 2)[0],
                item_type=data[off + 4],
                amount=1,
                equip_point=struct.unpack_from('<H', data, off + 6)[0],
                equipped=struct.unpack_from('<H', data, off + 8)[0],
            )
            result.items.append(item)
        return result

    elif packet_id == 0x01ee:
        # Inventory list: head=4, repeat_size=18
        # repeat: ioff2(u16) + name_id(u16) + item_type(u8) + identify(u8)
        #         + amount(u16) + epos(u16) + card0-3(u16x4)
        length = struct.unpack_from('<H', data, 2)[0]
        result = InventoryList()
        n_items = (length - 4) // 18
        for i in range(n_items):
            off = 4 + i * 18
            item = InventoryItem(
                index=struct.unpack_from('<H', data, off)[0],
                name_id=struct.unpack_from('<H', data, off + 2)[0],
                item_type=data[off + 4],
                amount=struct.unpack_from('<H', data, off + 6)[0],
            )
            result.items.append(item)
        return result

    elif packet_id == 0x0229:
        length = struct.unpack_from('<H', data, 2)[0]
        message = data[5:length].rstrip(b'\x00').decode('utf-8', errors='replace')
        return NpcMessage(npc_id=0, message=message)

    elif packet_id == 0x0211:
        # Online list: head=4, repeat_size=31
        # repeat: account_id(u32) + char_name(24) + level(u8) + gm_level(u8) + gender(u8)
        length = struct.unpack_from('<H', data, 2)[0]
        result = OnlineList()
        n_entries = (length - 4) // 31
        for i in range(n_entries):
            off = 4 + i * 31
            entry = OnlineListEntry(
                account_id=struct.unpack_from('<I', data, off)[0],
                name=decode_str(data[off + 4:off + 28]),
                level=data[off + 28],
                gm_level=data[off + 29],
                gender=data[off + 30],
            )
            result.players.append(entry)
        return result

    elif packet_id == 0x8000:
        # Special hold/transaction packet - just acknowledge
        return ('hold_notify', struct.unpack_from('<H', data, 2)[0] if len(data) >= 4 else 0)

    # Unhandled packet - return raw
    return (packet_id, data)
