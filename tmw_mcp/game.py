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
import threading
import time
from dataclasses import dataclass, field

from .net import Connection
from .resources import default_manager
from .packets import (
    # Builders
    build_login_register,
    build_char_server_connect,
    build_char_select,
    build_char_create,
    build_map_server_connect,
    build_map_loaded,
    build_ping,
    build_walk,
    build_close_storage,
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
    ItemRemove,
    InventoryAdd,
    InventoryRemove,
    InventoryList,
    InventoryItem,
    EquipResult,
    StatUpdate1,
    StatUpdate5,
    NpcMessage,
    NpcNext,
    NpcClose,
    NpcChoice,
    NpcIntInputRequest,
    NpcStrInputRequest,
    TradeRequest,
    TradeResponse,
    TradeItemAdd,
    TradeOk,
    TradeCancel,
    TradeComplete,
    BeingChangeLook,
    SkillDamage,
    BeingStatusChange,
    PlayerStatusChange,
    BeingEffect,
    AttackRange,
    ArrowEquip,
    PartyInvited,
    build_party_message,
    build_party_leave,
    build_party_reply,
    PartyInfo,
    PartyMessage,
    NpcBuySellChoice,
    NpcBuyList,
    NpcSellList,
    NpcBuyResponse,
    NpcSellResponse,
    build_npc_buy_sell,
    build_npc_buy,
    build_npc_sell,
    ItemUseResult,
    OnlineList,
    build_online_list_request,
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


def _chat_kind(message: str) -> str:
    """Classify a public-chat line for the dashboard.

    The server folds many message types into the same 0x008e/0x008d
    packets: plain public chat, "Server : ..." admin announcements,
    and (rarely) text the player typed themselves. We only get the
    final flattened string, so this is a prefix sniff.
    """
    # Server announcements use a "Server : ..." prefix server-side.
    if message.startswith('Server :') or message.startswith('Server:'):
        return 'server'
    # Channel-tagged messages (e.g. "#general : foo").
    if message.startswith('#'):
        return 'channel'
    return 'say'


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
    level: int = 0


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
        self.npc_dialog_open: bool = False  # True while server-side NPC lock may be active
        self.npc_dialog: list[str] = []
        self.npc_choices: list[str] = []
        self.npc_waiting_next: bool = False
        self.npc_waiting_close: bool = False
        self.npc_waiting_choice: bool = False
        self.npc_waiting_input: str = ''  # '', 'int', or 'str'

        # Chat log
        self.chat_log: list[str] = []
        self.whisper_log: list[tuple[str, str]] = []
        # Structured chat history for the dashboard. Each entry is a dict
        # with keys 'ts' (float wallclock), 'kind' (str), and 'text' (str).
        # Populated in parallel with chat_log; trimmed in lockstep.
        self.chat_entries: list[dict] = []
        self.online_list: list = []
        # Party members: account_id -> name
        self.party_members: dict[int, str] = {}

        # Timing
        self.last_ping = 0.0
        self.tick = 0

        # Path-walking state
        self._path_queue: list[tuple[int, int]] = []
        self._path_goal: tuple[int, int] | None = None
        self._path_callback = None  # callable(success: bool, x: int, y: int)
        self._walk_arrival: float = 0.0
        self._walk_sent_at: float = 0.0
        self._walk_dest: tuple[int, int] | None = None
        self._walk_response_received: bool = True

        # Pickup queue — processed one at a time
        self._pickup_queue: list[tuple] = []
        self._pickup_active: bool = False

        # Behavior state (used by run_auto_behaviors)
        self._auto_attack_target: int = 0
        self._hunt_type: str = ''
        self._hunt_home: tuple[int, int] | None = None
        # Follow mode is keyed by target name so it survives map transitions
        # (the block_id may change when a player warps to a new map server).
        # ``_follow_target_id`` is a cache of the currently-resolved being.
        # ``_follow_state`` is one of: idle / following / waiting / warping.
        # ``_follow_last_seen_*`` track the most recent visible position so
        # the bot can walk onto the warp tile the target stepped on.
        self._follow_target_name: str = ''
        self._follow_target_id: int = 0
        self._follow_state: str = 'idle'
        self._follow_last_seen_at: float = 0.0
        self._follow_last_seen_pos: tuple[int, int] | None = None
        self._follow_last_seen_map: str = ''
        # Seconds we wait for the target to reappear before giving up.
        self._follow_timeout: float = 10.0
        # Legacy alias: some external code still reads _follow_target as an
        # int. We keep it in sync with _follow_target_id so the dashboard's
        # ``auto.follow_target`` field stays meaningful.
        self._follow_target: int = 0
        self._board_target: int = 0
        self._attack_range: int = 1

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

    def _kick_resource_download(self, host_url: str) -> None:
        """Fire off a background thread to populate the resource overlay.

        We don't block login on this: the overlay just lights up
        asynchronously, and data lookups (item names, maps, monster
        names) seamlessly start returning real values once it's ready.
        Failures are logged and otherwise non-fatal; the bot can still
        play, it just sees ``item#NNN`` and synthetic species names.
        """
        if not host_url:
            return
        rm = default_manager()
        if rm.override_dir:
            return  # Dev mode: TMW_CLIENT_DATA points at a checkout.

        def _run() -> None:
            try:
                rm.update_from(host_url)
            except Exception as e:
                log.warning('Resource update from %s failed: %s', host_url, e)

        t = threading.Thread(target=_run, name='tmw-resource-update',
                             daemon=True)
        t.start()

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
                self._kick_resource_download(self.update_host)
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
                   char_name: str = '', world: str = '') -> bool:
        """Perform full login: login server -> char server -> map server.

        Picks the character matching ``char_name``. When ``char_name``
        is empty, falls back to the account's first character.
        """
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

        # Resolve the requested character (by name, or first if unspecified).
        chosen = None
        if char_name:
            for c in self.characters:
                if c.char_name == char_name:
                    chosen = c
                    break
            if chosen is None:
                have = ', '.join(c.char_name for c in self.characters)
                log.error('No character named %r on this account (have: %s)',
                          char_name, have)
                return False
        else:
            chosen = self.characters[0]

        self.player.char_name = chosen.char_name
        self.player.base_level = chosen.base_level
        self.player.job_level = chosen.job_level
        self.player.base_exp = chosen.base_exp
        self.player.job_exp = chosen.job_exp
        self.player.zeny = chosen.zeny
        self.player.hp = chosen.hp
        self.player.max_hp = chosen.max_hp
        self.player.sp = chosen.sp
        self.player.max_sp = chosen.max_sp

        # Step 3: Select character
        map_info = self.select_character(chosen.char_num)
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
            if pkt.level:
                b.level = pkt.level
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
            if pkt.level:
                b.level = pkt.level
            self.beings[pkt.block_id] = b
            return ('being_move', pkt)

        elif isinstance(pkt, BeingSpawn):
            # Leave name empty; resolve the species fallback live at display
            # time via being_display_name() so it self-heals once the
            # monsters.xml overlay finishes downloading. b.name is only set
            # from an authoritative server name response (0x0095).
            b = Being(block_id=pkt.block_id, species=pkt.species,
                     x=pkt.x, y=pkt.y, speed=pkt.speed)
            self.beings[pkt.block_id] = b
            return ('being_spawn', pkt)

        elif isinstance(pkt, BeingRemove):
            self.beings.pop(pkt.block_id, None)
            return ('being_remove', pkt)

        elif isinstance(pkt, WalkResponse):
            # Don't update position to destination immediately!
            # The server walks tile-by-tile; we'd desync if we jump to dest.
            # Instead, update to the walk SOURCE (where server confirms we are NOW)
            # and store destination for path advancement.
            old_x, old_y = self.player.x, self.player.y
            self.player.x = pkt.x0
            self.player.y = pkt.y0
            self._walk_dest = (pkt.x1, pkt.y1)
            self._walk_response_received = True
            if abs(pkt.x0 - old_x) > 1 or abs(pkt.y0 - old_y) > 1:
                # Expected when a new walk is issued mid-stride — the server
                # has moved us tile-by-tile since our last position update.
                # The x0 from WalkResponse is authoritative; we already
                # snapped to it above.
                log.debug('Walk source correction: server at (%d,%d), we thought (%d,%d) -> dest (%d,%d)',
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
            self._append_chat_entry(pkt.message, _chat_kind(pkt.message))
            if len(self.chat_log) > 200:
                self.chat_log = self.chat_log[-100:]
            return ('chat', pkt)

        elif isinstance(pkt, PartyInfo):
            self.party_members.clear()
            for aid, name, map_name, leader, online in (pkt.members or []):
                self.party_members[aid] = name
            return ('party_info', pkt)

        elif isinstance(pkt, PartyMessage):
            self.chat_log.append(f'[party] {pkt.message}')
            self._append_chat_entry(f'[party] {pkt.message}', 'party')
            if len(self.chat_log) > 200:
                self.chat_log = self.chat_log[-100:]
            return ('party_chat', pkt)

        elif isinstance(pkt, WhisperMessage):
            self.whisper_log.append((pkt.sender, pkt.message))
            self._append_chat_entry(
                f'[whisper from {pkt.sender}] {pkt.message}', 'whisper'
            )
            if len(self.whisper_log) > 200:
                self.whisper_log = self.whisper_log[-100:]
            return ('whisper', pkt)

        elif isinstance(pkt, WhisperResponse):
            return ('whisper_response', pkt)

        elif isinstance(pkt, GmChat):
            self.chat_log.append('[GM] ' + pkt.message)
            self._append_chat_entry('[GM] ' + pkt.message, 'gm')
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
            # A warp does not end the NPC session (TMWA keeps npc_id).
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
            self._cancel_path()
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

        elif isinstance(pkt, ItemRemove):
            self.floor_items.pop(pkt.block_id, None)
            return ('item_remove', pkt)

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

        elif isinstance(pkt, EquipResult):
            if pkt.success and pkt.index in self.inventory:
                item = self.inventory[pkt.index]
                if item.equipped:
                    # Was equipped, now unequipped.
                    item.equipped = 0
                    # If a weapon came off, fall back to melee range. The
                    # server normally re-sends 0x013a; this is a safety
                    # net for when the packet is missed or arrives late.
                    if pkt.equip_point & 0x0002:  # EPOS::WEAPON
                        self._attack_range = 1
                else:
                    # Was unequipped, now equipped.
                    item.equipped = pkt.equip_point
                    # If a weapon went on, seed _attack_range from the
                    # item DB so a stale value from a previous weapon
                    # can't cause melee-style auto-walk. The server's
                    # 0x013a (AttackRange) will override this with the
                    # authoritative value, including arrow bonuses.
                    if pkt.equip_point & 0x0002:
                        from .items import item_attack_range
                        ar = item_attack_range(item.name_id)
                        if ar:
                            self._attack_range = ar
            return ('equip_result', pkt)

        elif isinstance(pkt, ItemUseResult):
            if pkt.index in self.inventory:
                if pkt.amount <= 0:
                    del self.inventory[pkt.index]
                else:
                    self.inventory[pkt.index].amount = pkt.amount
            return ('item_use_result', pkt)

        elif isinstance(pkt, OnlineList):
            self.online_list = pkt.players
            return ('online_list', pkt)

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

        elif isinstance(pkt, NpcIntInputRequest):
            self.npc_id = pkt.npc_id
            self.npc_waiting_input = 'int'
            return ('npc_int_input_request', pkt)

        elif isinstance(pkt, NpcStrInputRequest):
            self.npc_id = pkt.npc_id
            self.npc_waiting_input = 'str'
            return ('npc_str_input_request', pkt)

        elif isinstance(pkt, TradeRequest):
            return ('trade_request', pkt)

        elif isinstance(pkt, TradeResponse):
            return ('trade_response', pkt)

        elif isinstance(pkt, TradeItemAdd):
            return ('trade_item_add', pkt)

        elif isinstance(pkt, TradeOk):
            return ('trade_ok', pkt)

        elif isinstance(pkt, TradeCancel):
            return ('trade_cancel', pkt)

        elif isinstance(pkt, TradeComplete):
            return ('trade_complete', pkt)

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

        elif isinstance(pkt, ArrowEquip):
            # tmwAthena does not send 0x00aa EquipResult for ammo. The
            # ARROW slot bit (EPOS::ARROW = 0x8000) is what the equipped
            # field for armor / weapons would use to encode "currently in
            # the ammo slot". Track it that way so consumers can ask
            # "is this item equipped?" uniformly.
            if pkt.index in self.inventory:
                # Clear any previously equipped ammo (only one ammo at a time).
                for item in self.inventory.values():
                    if item.equipped & 0x8000:
                        item.equipped &= ~0x8000
                self.inventory[pkt.index].equipped |= 0x8000
            return ('arrow_equip', pkt)

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

    def _append_chat_entry(self, text: str, kind: str) -> None:
        """Append a structured chat entry alongside chat_log.

        Stamps wallclock time on arrival so the dashboard can render
        per-message timestamps. Keeps the same trimming policy as
        ``chat_log`` (cap 200, trim to last 100).
        """
        self.chat_entries.append({'ts': time.time(), 'kind': kind, 'text': text})
        if len(self.chat_entries) > 200:
            self.chat_entries = self.chat_entries[-100:]

    def say(self, message: str):
        """Send a chat message.

        Modern tmwAthena servers (>= 0x100408) expect just the message
        without the player name prefix.
        """
        self.map_conn.send_packet(build_chat(message))

    def whisper(self, target: str, message: str):
        """Send a private message.

        The server does not echo outgoing whispers back to the sender
        (only the recipient receives a 0x0097 packet; the sender just
        gets a 0x0098 delivery-status). To keep the dashboard chat tail
        symmetric with incoming whispers, append a synthetic entry to
        ``chat_entries`` (kind ``whisper_out``) and mirror it in
        ``chat_log`` so anything that pages through chat history sees it.
        """
        self.map_conn.send_packet(build_whisper(target, message))
        line = f'[whisper to {target}] {message}'
        self.chat_log.append(line)
        if len(self.chat_log) > 200:
            self.chat_log = self.chat_log[-100:]
        self._append_chat_entry(line, 'whisper_out')

    def request_online_list(self):
        """Request the list of online players from the server."""
        self.map_conn.send_packet(build_online_list_request())

    def _snap_walk_position(self):
        """If a previous walk should have completed, snap player position to its destination."""
        if self._walk_dest and time.time() >= self._walk_arrival:
            self.player.x, self.player.y = self._walk_dest
            self._walk_dest = None

    def walk_to(self, x: int, y: int):
        """Walk to a position, clamping to max ~10 tiles to avoid server rejection.
        Cancels any in-progress path walk."""
        self._cancel_path()
        self._snap_walk_position()
        self._walk_step(x, y)

    def _walk_step(self, x: int, y: int):
        """Send a single walk packet. Clamps to stay under the server's walkpath limit (MAX_WALKPATH=48)."""
        px, py = self.player.x, self.player.y
        dx, dy = x - px, y - py
        adx, ady = abs(dx), abs(dy)
        max_dist = 30  # headroom under server MAX_WALKPATH=48
        if adx + ady > max_dist and adx + ady > 0:
            scale = max_dist / max(adx, ady) if max(adx, ady) > 0 else 1
            x = px + int(dx * scale)
            y = py + int(dy * scale)
            adx, ady = abs(x - px), abs(y - py)
        # Server walks diagonally first, then cardinal for the remainder.
        # Diagonal tiles take speed * 1.4, cardinal tiles take speed.
        # (see calc_next_walk_step in tmwa/src/map/pc.cpp)
        speed_s = self.player.speed / 1000.0  # ms -> seconds
        diagonal = min(adx, ady)
        cardinal = adx + ady - 2 * diagonal
        now = time.time()
        self._walk_sent_at = now
        self._walk_arrival = now + diagonal * speed_s * 1.4 + cardinal * speed_s
        self._walk_response_received = False
        self.map_conn.send_packet(build_walk(x, y))

    def walk_path(self, x: int, y: int, callback=None):
        """Walk to (x,y) using A* pathfinding. Calls callback(success, x, y) on completion."""
        from .maps import load_collision
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

        # Waypoints at direction changes: each segment is a straight run
        # (cardinal or diagonal), which the server walks reliably without
        # having to re-plan around obstacles. Long runs are split to keep
        # each walk packet modest.
        MAX_RUN = 20
        waypoints = []
        seg_dir = None
        run_len = 0
        for i in range(1, len(path)):
            dx = path[i][0] - path[i - 1][0]
            dy = path[i][1] - path[i - 1][1]
            d = (dx, dy)
            if d != seg_dir or run_len >= MAX_RUN:
                if seg_dir is not None:
                    waypoints.append(path[i - 1])
                seg_dir = d
                run_len = 1
            else:
                run_len += 1
        waypoints.append(path[-1])

        self._path_queue = waypoints
        # Send first step
        first = self._path_queue.pop(0)
        self._walk_step(first[0], first[1])

    # How long we wait for a WalkResponse ack past the sent time before
    # concluding the server dropped the walk packet.
    _WALK_ACK_TIMEOUT = 1.5

    def _advance_path(self):
        """Called from game loop to send next path segment when current walk completes."""
        if self._path_goal is None:
            return
        if not self._walk_response_received:
            # Watchdog: if the server never acknowledged, the walk was dropped
            # (e.g. destination currently blocked by a being). Abort with
            # callback(False) instead of stalling the caller.
            if time.time() > self._walk_sent_at + self._WALK_ACK_TIMEOUT:
                log.warning('walk: no WalkResponse within %.1fs, aborting path to %s',
                            self._WALK_ACK_TIMEOUT, self._path_goal)
                goal = self._path_goal
                cb = self._path_callback
                self._path_queue.clear()
                self._path_goal = None
                self._path_callback = None
                self._walk_response_received = True  # unblock future walks
                self._pickup_active = False
                if cb:
                    cb(False, goal[0], goal[1])
            return
        if time.time() < self._walk_arrival:
            return  # still walking

        self._snap_walk_position()

        if self._path_queue:
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
        """Cancel any in-progress path walk and clear walk state."""
        self._walk_dest = None
        self._walk_arrival = 0.0
        if self._path_goal is not None:
            self._path_queue.clear()
            self._path_goal = None
            self._path_callback = None
            # Don't clear pickup queue — let it retry on next process cycle
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
        # Avoid duplicates in queue
        if any(t[0] == item_id for t in self._pickup_queue):
            return
        self._pickup_queue.append((item_id, item.x, item.y, callback))
        if not self._pickup_active:
            self._process_next_pickup()

    def _process_next_pickup(self):
        """Walk to and pick up the nearest item in the queue."""
        if not self._pickup_queue:
            self._pickup_active = False
            return
        self._pickup_active = True
        # Pick nearest item from queue
        px, py = self.player.x, self.player.y
        self._pickup_queue.sort(key=lambda t: abs(t[1] - px) + abs(t[2] - py))
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

    def _reset_npc_dialog(self):
        """Clear all NPC dialog state back to the defaults from __init__.

        Called when we acknowledge closing a dialog, on a map warp (the
        being is gone), or when starting a fresh click on an NPC.
        """
        self.npc_id = 0
        self.npc_dialog_open = False
        self.npc_dialog.clear()
        self.npc_choices.clear()
        self.npc_waiting_next = False
        self.npc_waiting_close = False
        self.npc_waiting_choice = False
        self.npc_waiting_input = ''

    def click_npc(self, npc_id: int):
        """Click on an NPC."""
        self._reset_npc_dialog()
        self.npc_id = npc_id
        self.npc_dialog_open = True
        self.map_conn.send_packet(build_npc_click(npc_id))

    def close_storage(self):
        """Close the server-side storage (Kafra) window. Must be called after clicking a
        storage NPC, otherwise the server silently blocks walks and item use."""
        self.map_conn.send_packet(build_close_storage())

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
            # The dialog is done once we acknowledge the close, so drop any
            # lingering state (otherwise the dashboard/state keep showing it).
            self._reset_npc_dialog()

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

    def party_message(self, message: str):
        """Send a message to party members."""
        self.map_conn.send_packet(build_party_message(message))

    def party_leave(self):
        """Leave the current party."""
        self.map_conn.send_packet(build_party_leave())

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

    def quit_cleanly(self, drain_seconds: float = 0.3):
        """Send CMSG_QUIT (0x018a) to the map server, briefly drain the socket
        so the server has a chance to reply with SMSG_MAP_QUIT_RESPONSE
        (0x018b), then close all connections.

        Sending CMSG_QUIT clears the server-side account-online entry right
        away, so a fresh login can happen immediately. Without it the server
        keeps the session cached for several seconds and the next login is
        rejected as "already logged in".
        """
        from .packets import build_client_quit

        if self.map_conn:
            try:
                self.map_conn.send_packet(build_client_quit())
            except Exception:
                # The socket may already be half-closed. Best effort only.
                pass
            # Drain briefly so the server can flush the quit response and
            # release the session entry. We don't care what comes back, we
            # only want the TCP FIN-ack round-trip to happen.
            deadline = time.time() + max(0.0, drain_seconds)
            while time.time() < deadline:
                try:
                    pkt = self.map_conn.recv_packet_nonblock(timeout=0.05)
                    if pkt is None:
                        break
                except Exception:
                    break
        self.disconnect()
