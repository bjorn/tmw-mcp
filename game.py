"""
TMW Game Client - manages the full connection lifecycle and game state.

Flow:
  1. Connect to login server (port 6901)
  2. Authenticate -> get char server list
  3. Connect to char server -> get character list
  4. Select character -> get map server address
  5. Connect to map server -> play!
"""

import logging
import struct
import time
from dataclasses import dataclass, field

from net import Connection
from packets import (
    # Builders
    build_login_register,
    build_char_server_connect,
    build_char_select,
    build_char_create,
    build_map_server_connect,
    build_map_loaded,
    build_ping,
    build_walk,
    build_player_action,
    build_chat,
    build_npc_click,
    build_name_request,
    build_whisper,
    build_change_dir,
    build_item_pickup,
    build_respawn_or_switch,
    build_npc_menu_choice,
    build_npc_next,
    build_npc_close,
    build_npc_int_input,
    build_npc_str_input,
    # Parsed types
    UpdateHost,
    LoginSuccess,
    LoginError,
    CharLoginSuccess,
    CharInfo,
    CharMapInfo,
    MapLoginSuccess,
    BeingVisible,
    BeingMove,
    BeingSpawn,
    BeingRemove,
    ConnectionProblem,
    WalkResponse,
    PlayerStop,
    BeingAction,
    ChatMessage,
    ChangeMap,
    ChangeMapServer,
    BeingNameResponse,
    WhisperMessage,
    WhisperResponse,
    GmChat,
    ItemVisible,
    ItemDropped,
    InventoryAdd,
    InventoryRemove,
    InventoryList,
    InventoryItem,
    StatUpdate1,
    StatUpdate5,
    NpcMessage,
    NpcNext,
    NpcClose,
    NpcChoice,
    BeingChangeLook,
    SkillDamage,
    BeingStatusChange,
    PlayerStatusChange,
    BeingEffect,
    AttackRange,
    PartyInvited,
    build_party_reply,
    NpcBuySellChoice,
    NpcBuyList,
    NpcSellList,
    NpcBuyResponse,
    NpcSellResponse,
    build_npc_buy_sell,
    build_npc_buy,
    build_npc_sell,
    ItemUseResult,
)

log = logging.getLogger(__name__)


# SP type constants (from clif.t.hpp)
SP_SPEED = 0x0000
SP_BASEEXP = 0x0001
SP_JOBEXP = 0x0002
SP_HP = 0x0005
SP_MAXHP = 0x0006
SP_SP = 0x0007
SP_MAXSP = 0x0008
SP_STATUSPOINT = 0x0009
SP_BASELEVEL = 0x000b
SP_SKILLPOINT = 0x000c
SP_STR = 0x000d
SP_AGI = 0x000e
SP_VIT = 0x000f
SP_INT = 0x0010
SP_DEX = 0x0011
SP_LUK = 0x0012
SP_ZENY = 0x0014
SP_NEXTBASEEXP = 0x0016
SP_NEXTJOBEXP = 0x0017
SP_WEIGHT = 0x0018
SP_MAXWEIGHT = 0x0019
SP_ATK1 = 0x0029
SP_ATK2 = 0x002a
SP_DEF1 = 0x002b
SP_DEF2 = 0x002c
SP_MDEF1 = 0x002d
SP_MDEF2 = 0x002e
SP_HIT = 0x002f
SP_FLEE1 = 0x0030
SP_FLEE2 = 0x0031
SP_CRITICAL = 0x0032
SP_JOBLEVEL = 0x0037
SP_GM = 0x003a

SP_NAMES = {
    SP_SPEED: 'speed', SP_BASEEXP: 'base_exp', SP_JOBEXP: 'job_exp',
    SP_HP: 'hp', SP_MAXHP: 'max_hp', SP_SP: 'sp', SP_MAXSP: 'max_sp',
    SP_STATUSPOINT: 'status_point', SP_BASELEVEL: 'base_level',
    SP_SKILLPOINT: 'skill_point',
    SP_STR: 'str', SP_AGI: 'agi', SP_VIT: 'vit',
    SP_INT: 'int', SP_DEX: 'dex', SP_LUK: 'luk',
    SP_ZENY: 'zeny', SP_NEXTBASEEXP: 'next_base_exp',
    SP_NEXTJOBEXP: 'next_job_exp', SP_WEIGHT: 'weight',
    SP_MAXWEIGHT: 'max_weight',
    SP_ATK1: 'atk1', SP_ATK2: 'atk2', SP_DEF1: 'def1', SP_DEF2: 'def2',
    SP_MDEF1: 'mdef1', SP_MDEF2: 'mdef2', SP_HIT: 'hit',
    SP_FLEE1: 'flee1', SP_FLEE2: 'flee2', SP_CRITICAL: 'critical',
    SP_JOBLEVEL: 'job_level', SP_GM: 'gm_level',
}


@dataclass
class Being:
    block_id: int = 0
    name: str = ''
    species: int = 0
    x: int = 0
    y: int = 0
    speed: int = 150
    hp: int = 0
    max_hp: int = 0
    direction: int = 0


@dataclass
class FloorItem:
    block_id: int = 0
    name_id: int = 0
    amount: int = 0
    x: int = 0
    y: int = 0


@dataclass
class PlayerState:
    """Current player's state."""
    account_id: int = 0
    char_id: int = 0
    char_name: str = ''
    map_name: str = ''
    x: int = 0
    y: int = 0
    direction: int = 0
    hp: int = 0
    max_hp: int = 0
    sp: int = 0
    max_sp: int = 0
    base_level: int = 0
    job_level: int = 0
    base_exp: int = 0
    job_exp: int = 0
    next_base_exp: int = 0
    next_job_exp: int = 0
    zeny: int = 0
    status_point: int = 0
    skill_point: int = 0
    str_: int = 0
    agi: int = 0
    vit: int = 0
    int_: int = 0
    dex: int = 0
    luk: int = 0
    weight: int = 0
    max_weight: int = 0
    speed: int = 150
    atk1: int = 0
    atk2: int = 0
    def1: int = 0
    def2: int = 0
    mdef1: int = 0
    mdef2: int = 0
    hit: int = 0
    flee1: int = 0
    flee2: int = 0
    critical: int = 0
    gm_level: int = 0


class GameClient:
    """Full TMW game client."""

    def __init__(self, server: str = 'server.themanaworld.org',
                 login_port: int = 6901):
        self.server = server
        self.login_port = login_port

        # Connection state
        self.login_conn: Connection | None = None
        self.char_conn: Connection | None = None
        self.map_conn: Connection | None = None

        # Auth state
        self.login_id1: int = 0
        self.login_id2: int = 0
        self.account_id: int = 0
        self.sex: int = 0

        # Game state
        self.player = PlayerState()
        self.beings: dict[int, Being] = {}
        self.floor_items: dict[int, FloorItem] = {}
        self.inventory: dict[int, InventoryItem] = {}  # index -> item
        self.update_host: str = ''
        self.characters: list[CharInfo] = []

        # NPC dialog state
        self.npc_id: int = 0
        self.npc_dialog: list[str] = []
        self.npc_choices: list[str] = []
        self.npc_waiting_next: bool = False
        self.npc_waiting_close: bool = False
        self.npc_waiting_choice: bool = False

        # Chat log
        self.chat_log: list[str] = []
        self.whisper_log: list[tuple[str, str]] = []

        # Timing
        self.last_ping = 0.0
        self.tick = 0

        # Path-walking state
        self._path_queue: list[tuple[int, int]] = []
        self._path_goal: tuple[int, int] | None = None
        self._path_callback = None  # callable(success: bool, x: int, y: int)
        self._walk_arrival: float = 0.0

        # Pickup queue — processed one at a time
        self._pickup_queue: list[tuple] = []
        self._pickup_active: bool = False

    # ------------------------------------------------------------------
    # Login flow
    # ------------------------------------------------------------------

    def register(self, username: str, password: str,
                 gender: str = 'M') -> LoginSuccess | LoginError:
        """Register a new account and log in.

        The tmwAthena server creates an account when logging in with
        a username that ends in _M or _F (if registration is enabled).
        """
        suffix = '_' + gender.upper()
        return self.login(username + suffix, password)

    def login(self, username: str, password: str) -> LoginSuccess | LoginError:
        """Connect to login server and authenticate."""
        self.login_conn = Connection(self.server, self.login_port)
        self.login_conn.send_packet(build_login_register(username, password))

        # The server may send update host (0x0063) before the login result
        while True:
            result = self.login_conn.recv_packet()
            if isinstance(result, UpdateHost):
                self.update_host = result.url
                log.info('Update host: %s', self.update_host)
                continue
            elif isinstance(result, LoginSuccess):
                self.login_id1 = result.login_id1
                self.login_id2 = result.login_id2
                self.account_id = result.account_id
                self.sex = result.sex
                self.player.account_id = result.account_id
                log.info('Login success! Account ID: %d, %d server(s)',
                         result.account_id, len(result.servers))
                return result
            elif isinstance(result, LoginError):
                log.error('Login failed: code=%d msg=%s',
                          result.error_code, result.error_message)
                return result
            elif isinstance(result, ConnectionProblem):
                log.error('Connection problem: %d', result.error_code)
                return LoginError(error_code=result.error_code)
            else:
                log.debug('Unexpected packet during login: %r', result)

    def _resolve_ip(self, ip: str) -> str:
        """Use login server hostname when the reported IP is a private/loopback address."""
        if ip.startswith('127.') or ip.startswith('10.') or ip.startswith('192.168.'):
            log.info('Server reported private IP %s, using %s instead', ip, self.server)
            return self.server
        if ip.startswith('172.'):
            second = int(ip.split('.')[1])
            if 16 <= second <= 31:
                log.info('Server reported private IP %s, using %s instead', ip, self.server)
                return self.server
        return ip

    def connect_char_server(self, server_ip: str, server_port: int) -> CharLoginSuccess | None:
        """Connect to the character server."""
        server_ip = self._resolve_ip(server_ip)
        self.char_conn = Connection(server_ip, server_port)
        self.char_conn.send_packet(
            build_char_server_connect(
                self.account_id, self.login_id1, self.login_id2, self.sex
            )
        )

        for _ in range(10):
            result = self.char_conn.recv_packet()
            if isinstance(result, CharLoginSuccess):
                self.characters = result.characters
                log.info('Char server: %d character(s)', len(result.characters))
                return result
            elif isinstance(result, ConnectionProblem):
                log.error('Char server connection problem: %d', result.error_code)
                return None
            else:
                # Skip transaction wrappers and other packets
                log.debug('Skipping char server packet: %r', result)
                continue
        log.error('Char server: did not receive character list')
        return None

    def select_character(self, slot: int) -> CharMapInfo | None:
        """Select a character and get map server info."""
        self.char_conn.send_packet(build_char_select(slot))
        result = self.char_conn.recv_packet()
        if isinstance(result, CharMapInfo):
            self.player.char_id = result.char_id
            self.player.map_name = result.map_name
            log.info('Selected char %d -> map %s at %s:%d',
                     result.char_id, result.map_name, result.ip, result.port)
            return result
        elif isinstance(result, ConnectionProblem):
            log.error('Character selection failed: %d', result.error_code)
            return None
        else:
            log.warning('Unexpected packet on char select: %r', result)
            return None

    def connect_map_server(self, ip: str, port: int) -> MapLoginSuccess | None:
        """Connect to the map server."""
        ip = self._resolve_ip(ip)
        self.map_conn = Connection(ip, port)
        self.map_conn.send_packet(
            build_map_server_connect(
                self.account_id, self.player.char_id,
                self.login_id1, self.sex
            )
        )

        for _ in range(10):
            result = self.map_conn.recv_packet()
            if isinstance(result, MapLoginSuccess):
                self.player.x = result.x
                self.player.y = result.y
                self.player.direction = result.direction
                self.tick = result.tick
                log.info('Map server connected! Position: (%d, %d)',
                         result.x, result.y)
                # Tell server we loaded the map
                self.map_conn.send_packet(build_map_loaded())
                return result
            elif isinstance(result, ConnectionProblem):
                log.error('Map server connection failed: %d', result.error_code)
                return None
            else:
                log.debug('Skipping map server packet: %r', result)
                continue
        log.error('Map server: did not receive login success')
        return None

    # ------------------------------------------------------------------
    # Full login sequence
    # ------------------------------------------------------------------

    def full_login(self, username: str, password: str,
                   char_slot: int = 0, world: str = '') -> bool:
        """Perform full login: login server -> char server -> map server."""
        # Step 1: Login
        login_result = self.login(username, password)
        if isinstance(login_result, LoginError):
            return False

        if not login_result.servers:
            log.error('No char servers available')
            return False

        # Step 2: Choose world/server
        for s in login_result.servers:
            log.info('Available world: %s (%s:%d, %d users)',
                     s.name, s.ip, s.port, s.users)
        srv = login_result.servers[0]
        if world:
            for s in login_result.servers:
                if world.lower() in s.name.lower():
                    srv = s
                    break
            else:
                log.warning('World %r not found, using %s', world, srv.name)
        log.info('Selected world: %s', srv.name)
        char_result = self.connect_char_server(srv.ip, srv.port)
        if char_result is None:
            return False

        if not self.characters:
            log.warning('No characters on this account')
            return False

        # Find character in requested slot and load initial stats
        for c in self.characters:
            self.player.char_name = c.char_name
            self.player.base_level = c.base_level
            self.player.job_level = c.job_level
            self.player.base_exp = c.base_exp
            self.player.job_exp = c.job_exp
            self.player.zeny = c.zeny
            self.player.hp = c.hp
            self.player.max_hp = c.max_hp
            self.player.sp = c.sp
            self.player.max_sp = c.max_sp
            if c.char_num == char_slot:
                break

        # Step 3: Select character
        map_info = self.select_character(char_slot)
        if map_info is None:
            return False

        # Step 4: Map server
        map_result = self.connect_map_server(map_info.ip, map_info.port)
        if map_result is None:
            return False

        self.last_ping = time.time()
        return True

    # ------------------------------------------------------------------
    # Game loop helpers
    # ------------------------------------------------------------------

    def send_ping(self):
        """Send a keepalive ping if needed."""
        now = time.time()
        if now - self.last_ping >= 15.0:
            self.tick += int((now - self.last_ping) * 1000)
            self.map_conn.send_packet(build_ping(self.tick & 0xFFFFFFFF))
            self.last_ping = now

    def process_packets(self, timeout: float = 0.1) -> list:
        """Read and process all available packets. Returns list of events."""
        events = []
        pkt = self.map_conn.recv_packet_nonblock(timeout)
        while pkt is not None:
            event = self._handle_packet(pkt)
            if event is not None:
                events.append(event)
            # Check for more packets already buffered
            if self.map_conn.has_data():
                pkt = self.map_conn.recv_packet_nonblock(0.0)
            else:
                pkt = self.map_conn.recv_packet_nonblock(0.01)
                if pkt is None:
                    break
        return events

    def _handle_packet(self, pkt):
        """Handle a parsed packet and update game state. Returns event tuple or None."""
        if isinstance(pkt, BeingVisible):
            b = self.beings.get(pkt.block_id, Being())
            b.block_id = pkt.block_id
            b.species = pkt.species
            b.x = pkt.x
            b.y = pkt.y
            b.speed = pkt.speed
            b.hp = pkt.hp
            b.max_hp = pkt.max_hp
            b.direction = pkt.direction
            self.beings[pkt.block_id] = b
            if pkt.block_id == self.account_id:
                self.player.x = pkt.x
                self.player.y = pkt.y
                self.player.hp = pkt.hp
                self.player.max_hp = pkt.max_hp
            return ('being_visible', pkt)

        elif isinstance(pkt, BeingMove):
            b = self.beings.get(pkt.block_id, Being())
            b.block_id = pkt.block_id
            b.species = pkt.species
            b.x = pkt.x1
            b.y = pkt.y1
            b.speed = pkt.speed
            b.hp = pkt.hp
            b.max_hp = pkt.max_hp
            self.beings[pkt.block_id] = b
            return ('being_move', pkt)

        elif isinstance(pkt, BeingSpawn):
            b = Being(block_id=pkt.block_id, species=pkt.species,
                     x=pkt.x, y=pkt.y, speed=pkt.speed)
            self.beings[pkt.block_id] = b
            return ('being_spawn', pkt)

        elif isinstance(pkt, BeingRemove):
            self.beings.pop(pkt.block_id, None)
            return ('being_remove', pkt)

        elif isinstance(pkt, WalkResponse):
            old_x, old_y = self.player.x, self.player.y
            self.player.x = pkt.x1
            self.player.y = pkt.y1
            # Log if the walk source doesn't match our tracked position
            if abs(pkt.x0 - old_x) > 1 or abs(pkt.y0 - old_y) > 1:
                log.warning('Walk source mismatch! Server says from (%d,%d) but we thought (%d,%d) -> dest (%d,%d)',
                            pkt.x0, pkt.y0, old_x, old_y, pkt.x1, pkt.y1)
            return ('walk', pkt)

        elif isinstance(pkt, PlayerStop):
            b = self.beings.get(pkt.block_id)
            if b:
                b.x = pkt.x
                b.y = pkt.y
            if pkt.block_id == self.account_id:
                self.player.x = pkt.x
                self.player.y = pkt.y
            return ('stop', pkt)

        elif isinstance(pkt, BeingAction):
            return ('action', pkt)

        elif isinstance(pkt, ChatMessage):
            self.chat_log.append(pkt.message)
            if len(self.chat_log) > 200:
                self.chat_log = self.chat_log[-100:]
            return ('chat', pkt)

        elif isinstance(pkt, WhisperMessage):
            self.whisper_log.append((pkt.sender, pkt.message))
            if len(self.whisper_log) > 200:
                self.whisper_log = self.whisper_log[-100:]
            return ('whisper', pkt)

        elif isinstance(pkt, WhisperResponse):
            return ('whisper_response', pkt)

        elif isinstance(pkt, GmChat):
            self.chat_log.append('[GM] ' + pkt.message)
            return ('gm_chat', pkt)

        elif isinstance(pkt, BeingNameResponse):
            b = self.beings.get(pkt.block_id)
            if b:
                b.name = pkt.name
            return ('name', pkt)

        elif isinstance(pkt, ChangeMap):
            self.player.map_name = pkt.map_name
            self.player.x = pkt.x
            self.player.y = pkt.y
            self.beings.clear()
            self.floor_items.clear()
            self._cancel_path()
            self.map_conn.send_packet(build_map_loaded())
            return ('map_change', pkt)

        elif isinstance(pkt, ChangeMapServer):
            # Need to reconnect to a different map server
            self.player.map_name = pkt.map_name
            self.player.x = pkt.x
            self.player.y = pkt.y
            self.beings.clear()
            self.floor_items.clear()
            self.map_conn.close()
            self.connect_map_server(pkt.ip, pkt.port)
            return ('map_server_change', pkt)

        elif isinstance(pkt, StatUpdate1):
            self._apply_stat(pkt.sp_type, pkt.value)
            return ('stat', pkt)

        elif isinstance(pkt, StatUpdate5):
            self.player.status_point = pkt.status_point
            self.player.str_ = pkt.str_attr
            self.player.agi = pkt.agi_attr
            self.player.vit = pkt.vit_attr
            self.player.int_ = pkt.int_attr
            self.player.dex = pkt.dex_attr
            self.player.luk = pkt.luk_attr
            self.player.atk1 = pkt.atk_sum
            self.player.mdef1 = pkt.mdef
            self.player.def1 = pkt.def_
            return ('stats', pkt)

        elif isinstance(pkt, ItemVisible):
            self.floor_items[pkt.block_id] = FloorItem(
                block_id=pkt.block_id, name_id=pkt.name_id,
                amount=pkt.amount, x=pkt.x, y=pkt.y)
            return ('item_visible', pkt)

        elif isinstance(pkt, ItemDropped):
            self.floor_items[pkt.block_id] = FloorItem(
                block_id=pkt.block_id, name_id=pkt.name_id,
                amount=pkt.amount, x=pkt.x, y=pkt.y)
            return ('item_dropped', pkt)

        elif isinstance(pkt, InventoryList):
            for item in pkt.items:
                self.inventory[item.index] = item
            log.info('Inventory: %d items', len(pkt.items))
            return ('inventory_list', pkt)

        elif isinstance(pkt, InventoryAdd):
            if pkt.pickup_fail == 0:
                if pkt.index in self.inventory:
                    # Server sends delta for stacking items
                    self.inventory[pkt.index].amount += pkt.amount
                else:
                    self.inventory[pkt.index] = InventoryItem(
                        index=pkt.index, name_id=pkt.name_id,
                        item_type=pkt.item_type, amount=pkt.amount)
            return ('inventory_add', pkt)

        elif isinstance(pkt, InventoryRemove):
            if pkt.index in self.inventory:
                self.inventory[pkt.index].amount -= pkt.amount
                if self.inventory[pkt.index].amount <= 0:
                    del self.inventory[pkt.index]
            return ('inventory_remove', pkt)

        elif isinstance(pkt, ItemUseResult):
            if pkt.index in self.inventory:
                if pkt.amount <= 0:
                    del self.inventory[pkt.index]
                else:
                    self.inventory[pkt.index].amount = pkt.amount
            return ('item_use_result', pkt)

        elif isinstance(pkt, NpcMessage):
            self.npc_id = pkt.npc_id or self.npc_id
            self.npc_dialog.append(pkt.message)
            return ('npc_message', pkt)

        elif isinstance(pkt, NpcNext):
            self.npc_id = pkt.npc_id
            self.npc_waiting_next = True
            return ('npc_next', pkt)

        elif isinstance(pkt, NpcClose):
            self.npc_id = pkt.npc_id
            self.npc_waiting_close = True
            return ('npc_close', pkt)

        elif isinstance(pkt, NpcChoice):
            self.npc_id = pkt.npc_id
            self.npc_choices = pkt.choices
            self.npc_waiting_choice = True
            return ('npc_choice', pkt)

        elif isinstance(pkt, NpcBuySellChoice):
            self.npc_id = pkt.npc_id
            self.shop_buy_list = []
            self.shop_sell_list = []
            return ('shop_choice', pkt)

        elif isinstance(pkt, NpcBuyList):
            self.shop_buy_list = pkt.items
            return ('shop_buy_list', pkt)

        elif isinstance(pkt, NpcSellList):
            self.shop_sell_list = pkt.items
            return ('shop_sell_list', pkt)

        elif isinstance(pkt, NpcBuyResponse):
            return ('shop_buy_result', pkt)

        elif isinstance(pkt, NpcSellResponse):
            return ('shop_sell_result', pkt)

        elif isinstance(pkt, SkillDamage):
            return ('skill_damage', pkt)

        elif isinstance(pkt, BeingChangeLook):
            return ('being_change_look', pkt)

        elif isinstance(pkt, BeingStatusChange):
            return ('being_status_change', pkt)

        elif isinstance(pkt, PlayerStatusChange):
            return ('player_status_change', pkt)

        elif isinstance(pkt, BeingEffect):
            return ('being_effect', pkt)

        elif isinstance(pkt, AttackRange):
            self._attack_range = pkt.attack_range
            return ('attack_range', pkt)

        elif isinstance(pkt, PartyInvited):
            return ('party_invited', pkt)

        elif isinstance(pkt, tuple):
            if pkt[0] == 'pong':
                return None  # Silently handle pong
            return ('raw', pkt)

        return None

    def _apply_stat(self, sp_type: int, value: int):
        """Apply a stat update to player state."""
        mapping = {
            SP_HP: 'hp', SP_MAXHP: 'max_hp', SP_SP: 'sp', SP_MAXSP: 'max_sp',
            SP_STATUSPOINT: 'status_point', SP_BASELEVEL: 'base_level',
            SP_SKILLPOINT: 'skill_point', SP_BASEEXP: 'base_exp',
            SP_JOBEXP: 'job_exp', SP_NEXTBASEEXP: 'next_base_exp',
            SP_NEXTJOBEXP: 'next_job_exp', SP_ZENY: 'zeny',
            SP_WEIGHT: 'weight', SP_MAXWEIGHT: 'max_weight',
            SP_STR: 'str_', SP_AGI: 'agi', SP_VIT: 'vit',
            SP_INT: 'int_', SP_DEX: 'dex', SP_LUK: 'luk',
            SP_ATK1: 'atk1', SP_ATK2: 'atk2',
            SP_DEF1: 'def1', SP_DEF2: 'def2',
            SP_MDEF1: 'mdef1', SP_MDEF2: 'mdef2',
            SP_HIT: 'hit', SP_FLEE1: 'flee1', SP_FLEE2: 'flee2',
            SP_CRITICAL: 'critical', SP_SPEED: 'speed',
            SP_JOBLEVEL: 'job_level', SP_GM: 'gm_level',
        }
        attr = mapping.get(sp_type)
        if attr:
            setattr(self.player, attr, value)

    # ------------------------------------------------------------------
    # Player actions
    # ------------------------------------------------------------------

    def say(self, message: str):
        """Send a chat message.

        Modern tmwAthena servers (>= 0x100408) expect just the message
        without the player name prefix.
        """
        self.map_conn.send_packet(build_chat(message))

    def whisper(self, target: str, message: str):
        """Send a private message."""
        self.map_conn.send_packet(build_whisper(target, message))

    def walk_to(self, x: int, y: int):
        """Walk to a position, clamping to max ~10 tiles to avoid server rejection.
        Cancels any in-progress path walk."""
        self._cancel_path()
        self._walk_step(x, y)

    def _walk_step(self, x: int, y: int):
        """Send a single walk packet, clamping to max ~10 tiles."""
        import math
        px, py = self.player.x, self.player.y
        dx, dy = x - px, y - py
        dist = abs(dx) + abs(dy)
        max_dist = 10
        if dist > max_dist and dist > 0:
            scale = max_dist / max(abs(dx), abs(dy)) if max(abs(dx), abs(dy)) > 0 else 1
            x = px + int(dx * scale)
            y = py + int(dy * scale)
            dist = abs(x - px) + abs(y - py)
        self._walk_arrival = time.time() + dist * 0.15
        self.map_conn.send_packet(build_walk(x, y))

    def walk_path(self, x: int, y: int, callback=None):
        """Walk to (x,y) using A* pathfinding. Calls callback(success, x, y) on completion."""
        from maps import load_collision
        self._cancel_path()
        self._path_goal = (x, y)
        self._path_callback = callback

        cmap = load_collision(self.player.map_name)
        if cmap is None:
            # No collision data — fall back to direct walk
            self._walk_step(x, y)
            self._path_queue = []
            return

        path = cmap.find_path(self.player.x, self.player.y, x, y)
        if path is None:
            self._path_goal = None
            if callback:
                callback(False, x, y)
            return

        if len(path) <= 1:
            # Already there
            if callback:
                callback(True, x, y)
            return

        # Break path into waypoints spaced ~10 tiles apart
        waypoints = []
        i = 10
        while i < len(path):
            waypoints.append(path[i])
            i += 10
        # Always include final destination
        if not waypoints or waypoints[-1] != path[-1]:
            waypoints.append(path[-1])

        self._path_queue = waypoints
        # Send first step
        first = self._path_queue.pop(0)
        self._walk_step(first[0], first[1])

    def _advance_path(self):
        """Called from game loop to send next path segment when current walk completes."""
        if self._path_goal is None:
            return
        if time.time() < self._walk_arrival:
            return  # still walking

        if self._path_queue:
            # Check we're roughly on track (within 3 tiles of expected position)
            wp = self._path_queue[0]
            px, py = self.player.x, self.player.y
            # Send next segment
            nxt = self._path_queue.pop(0)
            self._walk_step(nxt[0], nxt[1])
        else:
            # Path complete
            goal = self._path_goal
            cb = self._path_callback
            self._path_goal = None
            self._path_callback = None
            if cb:
                cb(True, goal[0], goal[1])

    def _cancel_path(self):
        """Cancel any in-progress path walk."""
        if self._path_goal is not None:
            self._path_queue.clear()
            self._path_goal = None
            self._path_callback = None
            # Reset pickup queue if a pickup walk was cancelled
            self._pickup_queue.clear()
            self._pickup_active = False

    def attack(self, target_id: int, continuous: bool = False):
        """Attack a target."""
        action = 7 if continuous else 0
        self.map_conn.send_packet(build_player_action(target_id, action))

    def sit(self):
        """Sit down."""
        self.map_conn.send_packet(build_player_action(0, 2))

    def stand(self):
        """Stand up."""
        self.map_conn.send_packet(build_player_action(0, 3))

    def pickup(self, item_id: int):
        """Pick up an item."""
        self.map_conn.send_packet(build_item_pickup(item_id))

    def queue_pickup(self, item_id: int, callback=None):
        """Queue an item for pickup — walks to it and picks it up.
        Multiple calls are processed sequentially. Calls callback(item_id) when done."""
        item = None
        for it in self.floor_items.values():
            if it.block_id == item_id:
                item = it
                break
        if item is None:
            if callback:
                callback(item_id)
            return
        self._pickup_queue.append((item_id, item.x, item.y, callback))
        if not self._pickup_active:
            self._process_next_pickup()

    def _process_next_pickup(self):
        """Walk to and pick up the next item in the queue."""
        if not self._pickup_queue:
            self._pickup_active = False
            return
        self._pickup_active = True
        item_id, ix, iy, cb = self._pickup_queue.pop(0)
        px, py = self.player.x, self.player.y
        dx, dy = abs(ix - px), abs(iy - py)
        log = logging.getLogger('pickup')
        log.info('Processing pickup #%d at (%d,%d), player at (%d,%d), dist=(%d,%d)',
                 item_id, ix, iy, px, py, dx, dy)
        if dx > 1 or dy > 1:
            def on_arrive(success, x, y, iid=item_id, icb=cb):
                log.info('Walk to item #%d: success=%s, now at (%d,%d)', iid, success, x, y)
                if success:
                    self.pickup(iid)
                if icb:
                    icb(iid)
                self._process_next_pickup()
            self.walk_path(ix, iy, callback=on_arrive)
        else:
            log.info('Adjacent pickup #%d', item_id)
            self.pickup(item_id)
            if cb:
                cb(item_id)
            self._process_next_pickup()

    def shop_buy(self, npc_id: int):
        """Request buy list from shop NPC."""
        self.map_conn.send_packet(build_npc_buy_sell(npc_id, buy=True))

    def shop_sell(self, npc_id: int):
        """Request sell list from shop NPC."""
        self.map_conn.send_packet(build_npc_buy_sell(npc_id, buy=False))

    def buy_items(self, items: list[tuple[int, int]]):
        """Buy items: [(count, name_id), ...]"""
        self.map_conn.send_packet(build_npc_buy(items))

    def sell_items(self, items: list[tuple[int, int]]):
        """Sell items: [(index, count), ...]"""
        self.map_conn.send_packet(build_npc_sell(items))

    def click_npc(self, npc_id: int):
        """Click on an NPC."""
        self.npc_dialog.clear()
        self.npc_choices.clear()
        self.npc_waiting_next = False
        self.npc_waiting_close = False
        self.npc_waiting_choice = False
        self.map_conn.send_packet(build_npc_click(npc_id))

    def npc_next_response(self):
        """Continue NPC dialog."""
        if self.npc_waiting_next:
            self.npc_waiting_next = False
            self.map_conn.send_packet(build_npc_next(self.npc_id))

    def npc_close_response(self):
        """Close NPC dialog."""
        if self.npc_waiting_close:
            self.npc_waiting_close = False
            self.map_conn.send_packet(build_npc_close(self.npc_id))

    def npc_choose(self, choice: int):
        """Choose from NPC menu (1-based index)."""
        if self.npc_waiting_choice:
            self.npc_waiting_choice = False
            self.map_conn.send_packet(build_npc_menu_choice(self.npc_id, choice))

    def npc_input_int(self, value: int):
        """Submit integer input to NPC."""
        self.map_conn.send_packet(build_npc_int_input(self.npc_id, value))

    def npc_input_str(self, text: str):
        """Submit string input to NPC."""
        self.map_conn.send_packet(build_npc_str_input(self.npc_id, text))

    def request_name(self, block_id: int):
        """Request a being's name."""
        self.map_conn.send_packet(build_name_request(block_id))

    def party_reply(self, account_id: int, accept: bool):
        """Accept or reject a party invitation."""
        self.map_conn.send_packet(build_party_reply(account_id, accept))

    def respawn(self):
        """Respawn after death."""
        self.map_conn.send_packet(build_respawn_or_switch(0))

    def switch_character(self):
        """Switch to character select."""
        self.map_conn.send_packet(build_respawn_or_switch(1))

    def face(self, direction: int):
        """Change facing direction (0-7)."""
        self.map_conn.send_packet(build_change_dir(direction))

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def nearby_beings(self, radius: int = 20) -> list[Being]:
        """Get beings near the player."""
        px, py = self.player.x, self.player.y
        result = []
        for b in self.beings.values():
            if b.block_id == self.account_id:
                continue
            dx = abs(b.x - px)
            dy = abs(b.y - py)
            if dx <= radius and dy <= radius:
                result.append(b)
        result.sort(key=lambda b: abs(b.x - px) + abs(b.y - py))
        return result

    def nearby_items(self, radius: int = 20) -> list[FloorItem]:
        """Get floor items near the player."""
        px, py = self.player.x, self.player.y
        result = []
        for item in self.floor_items.values():
            dx = abs(item.x - px)
            dy = abs(item.y - py)
            if dx <= radius and dy <= radius:
                result.append(item)
        result.sort(key=lambda i: abs(i.x - px) + abs(i.y - py))
        return result

    def status_summary(self) -> str:
        """Return a brief status string."""
        p = self.player
        lines = [
            f'{p.char_name} Lv.{p.base_level}/{p.job_level} '
            f'HP:{p.hp}/{p.max_hp} SP:{p.sp}/{p.max_sp}',
            f'Map: {p.map_name} ({p.x},{p.y})',
            f'EXP: {p.base_exp}/{p.next_base_exp} '
            f'Job: {p.job_exp}/{p.next_job_exp} Zeny: {p.zeny}',
            f'STR:{p.str_} AGI:{p.agi} VIT:{p.vit} '
            f'INT:{p.int_} DEX:{p.dex} LUK:{p.luk}',
        ]
        return '\n'.join(lines)

    def disconnect(self):
        """Close all connections."""
        for conn in (self.map_conn, self.char_conn, self.login_conn):
            if conn:
                conn.close()
